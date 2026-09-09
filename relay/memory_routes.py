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
from relay.memory_store import AnalyzeMemory, MemoryAsset, SaveMemory


def install_memory_routes(app, authorize, access, atlas):
    from relay.atlas_routes import read_json

    base = "/api/sessions/{session}/atlas/spaces/{identifier}/captures/{capture_id}/memory"
    jobs = threading.BoundedSemaphore(2)
    uploads = asyncio.Semaphore(2)

    def owner(session, identifier, authorization):
        authorize(authorization)
        atlas().check(identifier, session=session)

    def response(session, identifier, capture_id, authorization):
        access(session, identifier, authorization)
        try:
            owner(session, identifier, authorization)
            can_edit = True
        except HTTPException:
            can_edit = False
        return JSONResponse(
            {
                **atlas().memories.get(identifier, capture_id),
                "can_edit": can_edit,
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
        owner(session, identifier, authorization)
        atlas().memories.save(identifier, capture_id, await read_json(request, SaveMemory))
        return response(session, identifier, capture_id, authorization)

    @app.post(base + "/inspect")
    def inspect_memory(
        session: str,
        identifier: str,
        capture_id: str,
        authorization: str | None = Header(default=None),
    ):
        owner(session, identifier, authorization)
        capture = atlas().memories.capture(identifier, capture_id)
        if not jobs.acquire(blocking=False):
            raise AtlasError("Two context operations are running. Please try again shortly.", 429)
        try:
            result = inspect_media(atlas().media / capture_id, capture["mime"])
            atlas().memories.inspected(identifier, capture_id, result)
        finally:
            jobs.release()
        return response(session, identifier, capture_id, authorization)

    @app.post(base + "/assets", status_code=201)
    async def upload_asset(
        session: str,
        identifier: str,
        capture_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        owner(session, identifier, authorization)
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
        path = None
        try:
            async with asyncio.timeout(90), uploads:
                with tempfile.NamedTemporaryFile(
                    dir=atlas().root, prefix="memory-upload-", delete=False
                ) as handle:
                    path = Path(handle.name)
                    size = 0
                    async for part in request.stream():
                        size += len(part)
                        if size > 64 * 1024 * 1024:
                            raise AtlasError("Memory tracks must be smaller than 64 MB.", 413)
                        handle.write(part)
                mime = await asyncio.to_thread(
                    admitted_asset, path, request.headers.get("content-type", "")
                )
                return atlas().memories.add_asset(identifier, capture_id, path, mime, metadata)
        except TimeoutError:
            raise AtlasError(
                "The recording upload timed out. Keep the original and retry.", 408
            ) from None
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

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
        if not jobs.acquire(blocking=False):
            raise AtlasError("Two context operations are running. Please try again shortly.", 429)
        memory = atlas().memories
        try:
            value = memory.begin(identifier, capture_id, options)
        except BaseException:
            jobs.release()
            raise

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

        background.add_task(process)
        return JSONResponse(
            {**value, "can_edit": True, "capabilities": capabilities()},
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )
