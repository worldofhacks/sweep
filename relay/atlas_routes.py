"""Atlas HTTP surface, using relay authentication and narrowly scoped space invitations."""

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, ValidationError

from relay.atlas import (
    MAX_MEDIA_BYTES,
    AtlasError,
    AtlasModel,
    AtlasStore,
    CaptureMetadata,
    CaptureRequest,
    Contributor,
    NewSpace,
    SurfaceRequest,
)
from relay.atlas_account_routes import account_for, install_atlas_account_routes, manage_space


class SpaceStatus(AtlasModel):
    status: Literal["active", "resolved"]


class LeaveSpace(AtlasModel):
    contributor_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")


async def read_json(request: Request, model):
    data = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 8192:
                    raise HTTPException(413, "Request is too large.")
        return model.model_validate_json(data)
    except (ValidationError, ValueError):
        raise HTTPException(422, "Check the fields and location in your request.") from None
    except TimeoutError:
        raise HTTPException(408, "The request timed out.") from None


def media_type(path: Path, claimed: str) -> str:
    """Admit a small set of inert image/video containers, never browser documents."""
    with path.open("rb") as handle:
        prefix = handle.read(32)
    valid = (
        claimed == "image/jpeg"
        and prefix.startswith(b"\xff\xd8\xff")
        or claimed == "image/png"
        and prefix.startswith(b"\x89PNG\r\n\x1a\n")
        or claimed == "image/webp"
        and prefix[:4] == b"RIFF"
        and prefix[8:12] == b"WEBP"
        or claimed == "video/mp4"
        and prefix[4:8] == b"ftyp"
        or claimed == "video/webm"
        and prefix.startswith(b"\x1a\x45\xdf\xa3")
    )
    if not valid:
        raise AtlasError("Use a JPEG, PNG, WebP, MP4 or WebM capture.", 415)
    return claimed


def install_atlas_routes(app: FastAPI, authorize):
    upload_slots = asyncio.Semaphore(2)

    def store() -> AtlasStore:
        return app.state.atlas_store

    def resolve(session: str, identifier: str, authorization: str | None, *, write=False):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Open a space invitation or connect your workspace.")
        try:
            authorize(authorization)
        except HTTPException:
            if authorization[7:].count(".") == 2:
                account = account_for(app, authorization)
                role = store().accounts.role(identifier, account["id"])
                if write and role not in ("contributor", "owner"):
                    raise HTTPException(
                        403, "This account can view, but cannot contribute here."
                    ) from None
                return store().check(identifier, session=session), account["id"]
            return store().check(identifier, token=authorization[7:]), None
        return store().check(identifier, session=session), None

    def access(session: str, identifier: str, authorization: str | None):
        return resolve(session, identifier, authorization)[0]

    install_atlas_account_routes(app, authorize, store)
    from relay.atlas_timeline import install_timeline_routes
    install_timeline_routes(app, authorize, resolve, store)
    from relay.atlas_removal import install_removal_routes
    install_removal_routes(app, authorize, resolve, store)

    from relay.memory_routes import install_memory_routes
    install_memory_routes(app, authorize, access, store, resolve)

    @app.exception_handler(AtlasError)
    async def atlas_error(_request: Request, error: AtlasError):
        return JSONResponse(
            {"detail": error.detail, **({"code": error.code} if error.code else {})},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    base = "/api/sessions/{session}/atlas/spaces"

    @app.get(base)
    def spaces(session: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        return {"spaces": store().list(session)}

    @app.post(base, status_code=201)
    async def create(
        session: str, request: Request, authorization: str | None = Header(default=None)
    ):
        authorize(authorization)
        if not 1 <= len(session) <= 128:
            raise HTTPException(400, "Workspace identifier is too long.")
        value = await read_json(request, NewSpace)
        return store().create(session, value)

    @app.get(base + "/{identifier}")
    def detail(session: str, identifier: str, authorization: str | None = Header(default=None)):
        access(session, identifier, authorization)
        return JSONResponse(store().detail(identifier), headers={"Cache-Control": "no-store"})

    @app.post(base + "/drafts/{draft_id}/publish", status_code=201)
    async def publish_draft(
        session: str,
        draft_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        if not 1 <= len(session) <= 128:
            raise HTTPException(400, "Workspace identifier is too long.")
        try:
            if str(uuid.UUID(draft_id)) != draft_id:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Use the original draft identifier.") from None
        value = await read_json(request, NewSpace)
        return JSONResponse(
            store().create(session, value, draft_id),
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.post(base + "/{identifier}/reconstruction", status_code=202)
    def reconstruction(
        session: str, identifier: str, authorization: str | None = Header(default=None)
    ):
        # A shared contribution invitation does not authorize expensive processing jobs.
        authorize(authorization)
        store().check(identifier, session=session)
        return JSONResponse(
            store().queue_reconstruction(identifier),
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.get(base + "/{identifier}/reconstruction/{job_id}/{artifact}")
    def reconstruction_artifact(
        session: str,
        identifier: str,
        job_id: str,
        artifact: str,
        authorization: str | None = Header(default=None),
    ):
        access(session, identifier, authorization)
        job = store().reconstruction_job(job_id)
        if job["space_id"] != identifier or job["status"] != "ready":
            raise HTTPException(404, "A completed reconstruction is not available.")
        if artifact not in ("cloud.glb", "manifest.json"):
            raise HTTPException(404, "Artifact not found.")
        path = store().root / "reconstructions" / job["id"] / artifact
        if not path.is_file():
            raise HTTPException(404, "The reconstruction artifact is missing. Start a new build.")
        return FileResponse(
            path,
            media_type="model/gltf-binary" if artifact.endswith("glb") else "application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post(base + "/{identifier}/status")
    async def status(
        session: str,
        identifier: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        account_id = manage_space(app, authorize, store(), session, identifier, authorization)
        value = await read_json(request, SpaceStatus)
        return store().update_status(identifier, value.status, account_id=account_id)

    @app.post(base + "/{identifier}/invitation")
    def invitation(session: str, identifier: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        store().check(identifier, session=session)
        return store().invitation(identifier)

    @app.post(base + "/{identifier}/captures", status_code=201)
    async def upload(
        session: str,
        identifier: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        space, account_id = resolve(session, identifier, authorization, write=True)
        if space["status"] != "active":
            raise AtlasError("This space is resolved. Reopen it before contributing.", 409)
        raw = request.headers.get("x-sweep-capture", "")
        if len(raw) > 8192:
            raise HTTPException(413, "Capture metadata is too large.")
        try:
            metadata = CaptureMetadata.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "The capture metadata is incomplete or invalid.") from None
        if account_id is not None:
            metadata = metadata.model_copy(update={"contributor_id": account_id})
        operation = store().removal.begin_operation(identifier, None, "original_upload")
        path = None
        try:
            async with asyncio.timeout(90), upload_slots:
                with tempfile.NamedTemporaryFile(
                    dir=store().root, prefix=f"upload-{operation}-", delete=False
                ) as handle:
                    path = Path(handle.name)
                    size = 0
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_MEDIA_BYTES:
                            raise HTTPException(413, "Each capture must be 64 MB or smaller.")
                        handle.write(chunk)
                mime = media_type(path, request.headers.get("content-type", ""))
                # Browser identity was verified at admission. A short-lived session token
                # may expire during this bounded transfer; membership is checked again
                # inside add_capture's transaction. Legacy invitation rotation still
                # invalidates an in-flight upload before it is committed.
                if account_id is None:
                    resolve(session, identifier, authorization, write=True)
                return store().add_capture(
                    identifier, metadata, path, mime, account_id=account_id
                )
        except TimeoutError:
            raise HTTPException(408, "The upload timed out. Please try again.") from None
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
            store().removal.finish_operation(operation)

    @app.get(base + "/{identifier}/captures/{capture_id}/media")
    def media(
        session: str,
        identifier: str,
        capture_id: str,
        authorization: str | None = Header(default=None),
    ):
        access(session, identifier, authorization)
        item = next(
            (c for c in store().detail(identifier)["captures"] if c["id"] == capture_id), None
        )
        if item is None:
            raise HTTPException(404, "Capture not found.")
        return FileResponse(
            store().media / item["id"],
            media_type=item["mime"],
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.post(base + "/{identifier}/presence")
    async def presence(
        session: str,
        identifier: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        resolve(session, identifier, authorization, write=True)
        value = await read_json(request, Contributor)
        _, account_id = resolve(session, identifier, authorization, write=True)
        if account_id is not None:
            value = value.model_copy(update={"contributor_id": account_id})
        return store().publish_presence(identifier, value, account_id=account_id)

    @app.post(base + "/{identifier}/leave")
    async def leave(
        session: str,
        identifier: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        resolve(session, identifier, authorization, write=True)
        value = await read_json(request, LeaveSpace)
        _, account_id = resolve(session, identifier, authorization, write=True)
        if account_id is not None:
            value = value.model_copy(update={"contributor_id": account_id})
        return store().leave(identifier, value.contributor_id, account_id=account_id)

    @app.post(base + "/{identifier}/requests", status_code=201)
    async def capture_request(
        session: str,
        identifier: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        resolve(session, identifier, authorization, write=True)
        value = await read_json(request, CaptureRequest)
        _, account_id = resolve(session, identifier, authorization, write=True)
        return store().request_capture(identifier, value, account_id=account_id)

    @app.post(base + "/{identifier}/surface-requests", status_code=201)
    async def surface_request(
        session: str,
        identifier: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        store().check(identifier, session=session)
        value = await read_json(request, SurfaceRequest)
        return store().request_surface(identifier, value)

    @app.post(base + "/{identifier}/surface-requests/{job_id}/{region_id}/dismiss")
    def dismiss_surface_request(
        session: str,
        identifier: str,
        job_id: str,
        region_id: str,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        store().check(identifier, session=session)
        return store().dismiss_surface_request(identifier, job_id, region_id)
