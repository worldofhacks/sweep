"""Memory context belongs to a capture; space invitations never grant paid analysis authority."""

import asyncio
import tempfile
import threading
from pathlib import Path

from fastapi import BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from relay.atlas import AtlasError
from relay.memory_context import analyze, capabilities
from relay.memory_media import admitted_asset, inspect_media
from relay.memory_store import (
    ACTIVE_ANALYSIS,
    AnalyzeMemory,
    CancelMemory,
    MemoryAsset,
    ReviewMemory,
    SaveMemory,
)


def install_memory_routes(app, authorize, access, atlas, resolve):
    from relay.atlas_routes import read_json

    base = "/api/sessions/{session}/atlas/spaces/{identifier}/captures/{capture_id}/memory"
    jobs = threading.BoundedSemaphore(2)
    uploads = asyncio.Semaphore(2)

    def owner(session, identifier, authorization):
        authorize(authorization)
        atlas().check(identifier, session=session)

    def editor(session, identifier, capture_id, authorization):
        _, account_id = resolve(session, identifier, authorization)
        if account_id is None:
            owner(session, identifier, authorization)
        value = atlas().memories.capture(identifier, capture_id)
        if not atlas().memories.can_edit(identifier, value, account_id, account_id is None):
            raise AtlasError("Only the Space owner or this capture's contributor can edit it.", 403)
        return account_id

    def response(session, identifier, capture_id, authorization):
        _, account_id = resolve(session, identifier, authorization)
        try:
            owner(session, identifier, authorization)
            can_analyze = True
        except HTTPException:
            can_analyze = False
        value = atlas().memories.get(identifier, capture_id)
        can_edit = atlas().memories.can_edit(identifier, value["capture"], account_id, can_analyze)
        return JSONResponse(
            {
                **value,
                "can_edit": can_edit,
                "can_remove": can_edit,
                "can_cancel": can_edit
                and (value.get("analysis") or {}).get("status") in ACTIVE_ANALYSIS,
                "can_analyze": can_analyze,
                "analysis_idempotency": True,
                **(
                    {"analysis_allowance": atlas().memories.allowance.status(identifier)}
                    if can_analyze
                    else {}
                ),
                "capabilities": capabilities(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get(base)
    def get_memory(
        session: str,
        identifier: str,
        capture_id: str,
        authorization: str | None = Header(default=None),
    ):
        return response(session, identifier, capture_id, authorization)

    @app.post(base)
    async def save_memory(
        session: str,
        identifier: str,
        capture_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        account_id = editor(session, identifier, capture_id, authorization)
        atlas().memories.save(
            identifier, capture_id, await read_json(request, SaveMemory), account_id=account_id
        )
        return response(session, identifier, capture_id, authorization)

    @app.get(base + "/history")
    def memory_history(
        session: str,
        identifier: str,
        capture_id: str,
        authorization: str | None = Header(default=None),
    ):
        access(session, identifier, authorization)
        return JSONResponse(
            {"history": atlas().memories.history(identifier, capture_id)},
            headers={"Cache-Control": "no-store"},
        )

    @app.get(base + "/history/{before}")
    def earlier_history(
        session: str,
        identifier: str,
        capture_id: str,
        before: int,
        authorization: str | None = Header(default=None),
    ):
        access(session, identifier, authorization)
        if not 1 <= before <= 201:
            raise AtlasError("Choose an existing memory history page.", 422)
        return JSONResponse(
            {"history": atlas().memories.history(identifier, capture_id, before)},
            headers={"Cache-Control": "no-store"},
        )

    @app.post(base + "/inspect")
    def inspect_memory(
        session: str,
        identifier: str,
        capture_id: str,
        authorization: str | None = Header(default=None),
    ):
        account_id = editor(session, identifier, capture_id, authorization)
        capture = atlas().memories.capture(identifier, capture_id)
        if atlas().memories.get(identifier, capture_id).get("inspection"):
            return response(session, identifier, capture_id, authorization)
        if not jobs.acquire(blocking=False):
            raise AtlasError("Two context operations are running. Please try again shortly.", 429)
        operation = None
        try:
            operation = atlas().removal.begin_operation(identifier, capture_id, "inspection")
            result = inspect_media(atlas().media / capture_id, capture["mime"])
            atlas().memories.inspected(identifier, capture_id, result, account_id=account_id)
        finally:
            jobs.release()
            if operation:
                atlas().removal.finish_operation(operation)
        return response(session, identifier, capture_id, authorization)

    @app.post(base + "/review")
    async def review_memory(
        session: str,
        identifier: str,
        capture_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        account_id = editor(session, identifier, capture_id, authorization)
        atlas().memories.review(
            identifier, capture_id, await read_json(request, ReviewMemory), account_id=account_id
        )
        return response(session, identifier, capture_id, authorization)

    @app.post(base + "/assets", status_code=201)
    async def upload_asset(
        session: str,
        identifier: str,
        capture_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        account_id = editor(session, identifier, capture_id, authorization)
        atlas().memories.capture(identifier, capture_id)
        raw = request.headers.get("x-sweep-memory-asset", "")
        if len(raw) > 2048:
            raise AtlasError("Track metadata is too large.", 413)
        try:
            metadata = MemoryAsset.model_validate_json(raw)
        except ValidationError:
            raise AtlasError(
                "Name the recording, choose its role, and confirm you can share it.", 422
            ) from None
        operation = atlas().removal.begin_operation(identifier, capture_id, "memory_upload")
        inspection_task = None
        path = None
        try:
            async with asyncio.timeout(90), uploads:
                with tempfile.NamedTemporaryFile(
                    dir=atlas().root, prefix=f"memory-upload-{operation}-", delete=False
                ) as handle:
                    path = Path(handle.name)
                    size = 0
                    async for part in request.stream():
                        size += len(part)
                        if size > 64 * 1024 * 1024:
                            raise AtlasError("Memory tracks must be smaller than 64 MB.", 413)
                        handle.write(part)
                inspection_task = asyncio.create_task(
                    asyncio.to_thread(admitted_asset, path, request.headers.get("content-type", ""))
                )
                mime = await asyncio.shield(inspection_task)
                return atlas().memories.add_asset(
                    identifier, capture_id, path, mime, metadata, account_id=account_id
                )
        except TimeoutError:
            raise AtlasError(
                "The recording upload timed out. Keep the original and retry.", 408
            ) from None
        finally:
            if inspection_task is not None:
                try:
                    await inspection_task
                except Exception:
                    pass  # Preserve the original error, after the reader has actually stopped.
            if path is not None:
                path.unlink(missing_ok=True)
            atlas().removal.finish_operation(operation)

    @app.get(base + "/assets/{asset_id}/media")
    def asset_media(
        session: str,
        identifier: str,
        capture_id: str,
        asset_id: str,
        authorization: str | None = Header(default=None),
    ):
        access(session, identifier, authorization)
        value = atlas().memories.get(identifier, capture_id)
        asset = next((a for a in value["assets"] if a["id"] == asset_id), None)
        if not asset:
            raise AtlasError("Recording not found in this memory.", 404)
        return FileResponse(
            atlas().memories.media / asset["id"],
            media_type=asset["mime"],
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.post(base + "/cancel")
    async def cancel_memory(
        session: str,
        identifier: str,
        capture_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        account_id = editor(session, identifier, capture_id, authorization)
        options = await read_json(request, CancelMemory)
        atlas().memories.cancel(identifier, capture_id, options, account_id=account_id)
        return response(session, identifier, capture_id, authorization)

    @app.post(base + "/analyze", status_code=202)
    async def analyze_memory(
        session: str,
        identifier: str,
        capture_id: str,
        request: Request,
        background: BackgroundTasks,
        authorization: str | None = Header(default=None),
    ):
        owner(session, identifier, authorization)
        options = await read_json(request, AnalyzeMemory)
        memory = atlas().memories
        acquired = False

        def reserve():
            nonlocal acquired
            acquired = jobs.acquire(blocking=False)
            return acquired

        try:
            admission = memory.admit(identifier, capture_id, options, reserve=reserve)
        except BaseException:
            if acquired:
                jobs.release()
            raise

        value = admission.context

        def process():
            try:
                result = analyze(memory, value, options)
                memory.finish(identifier, capture_id, value["analysis"], result)
            except Exception:
                memory.finish(
                    identifier,
                    capture_id,
                    value["analysis"],
                    {
                        "status": "failed",
                        "warnings": ["Analysis could not finish. Your originals are unchanged."],
                    },
                )
            finally:
                jobs.release()

        if admission.created:
            background.add_task(process)
        return JSONResponse(
            {
                **value,
                "can_edit": True,
                "can_remove": True,
                "can_cancel": (value.get("analysis") or {}).get("status") in ACTIVE_ANALYSIS,
                "can_analyze": True,
                "analysis_idempotency": True,
                "analysis_allowance": memory.allowance.status(identifier),
                "analysis_request": {
                    "id": options.request_id,
                    "analysis_id": admission.analysis_id,
                    "rejection": admission.rejection,
                    "replayed": admission.replayed,
                    "current": bool(admission.analysis_id)
                    and (value.get("analysis") or {}).get("id") == admission.analysis_id,
                },
                "capabilities": capabilities(),
            },
            status_code=202 if admission.created else 200,
            headers={"Cache-Control": "no-store"},
        )
