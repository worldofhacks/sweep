"""Explicit account enrollment and operator-managed, account-bound Space invitations."""

from typing import Literal
from uuid import UUID

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from relay.atlas import AtlasModel, NewSpace


class NewAccountInvitation(AtlasModel):
    role: Literal["viewer", "contributor"] = "contributor"
    lifetime_hours: int = Field(default=24, ge=1, le=168)


class AcceptAccountInvitation(AtlasModel):
    token: str = Field(pattern=r"^[a-zA-Z0-9_-]{43}$")


class CreateAccountSpace(AtlasModel):
    draft_id: UUID
    space: NewSpace


def account_for(app, authorization):
    verifier = getattr(app.state, "atlas_identity", None)
    if verifier is None:
        raise HTTPException(503, "Account sign-in is not configured for this workspace.")
    return app.state.atlas_store.accounts.account(verifier.verify(authorization))


def manage_space(app, authorize, store, session, identifier, authorization):
    """A Space owner is not a relay operator; return the account for commit checks."""
    try:
        authorize(authorization)
    except HTTPException:
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or authorization[7:].count(".") != 2
        ):
            raise
        account = account_for(app, authorization)
        store.accounts.require_owner(identifier, account["id"])
        store.check(identifier, session=session)
        return account["id"]
    store.check(identifier, session=session)
    return None


def install_atlas_account_routes(app, authorize, store):
    from relay.atlas_routes import read_json

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    def owner(session, identifier, authorization):
        return manage_space(app, authorize, store(), session, identifier, authorization)

    @app.post("/api/atlas/account")
    def enroll(authorization: str | None = Header(default=None)):
        return response({"account": account_for(app, authorization)})

    @app.get("/api/atlas/account/spaces")
    def spaces(authorization: str | None = Header(default=None)):
        account = account_for(app, authorization)
        return response({"spaces": store().accounts.spaces(account["id"])})

    @app.post("/api/atlas/account/spaces", status_code=201)
    async def create(request: Request, authorization: str | None = Header(default=None)):
        account = account_for(app, authorization)
        value = await read_json(request, CreateAccountSpace)
        result = store().create("", value.space, str(value.draft_id), account_id=account["id"])
        return JSONResponse(
            {
                "space_id": result["space"]["id"],
                "session": "account-" + account["id"],
                "role": "owner",
                "draft_id": str(value.draft_id),
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/atlas/account/invitations/accept")
    async def accept(request: Request, authorization: str | None = Header(default=None)):
        account = account_for(app, authorization)
        value = await read_json(request, AcceptAccountInvitation)
        return response(store().accounts.accept(value.token, account["id"]))

    @app.post("/api/atlas/account/invitations/preview")
    async def preview(request: Request, authorization: str | None = Header(default=None)):
        account = account_for(app, authorization)
        value = await read_json(request, AcceptAccountInvitation)
        return response(store().accounts.preview(value.token, account["id"]))

    @app.delete("/api/atlas/account/spaces/{identifier}/membership")
    def leave(identifier: str, authorization: str | None = Header(default=None)):
        account = account_for(app, authorization)
        store().accounts.remove(identifier, account["id"])
        return response({"removed": True})

    base = "/api/sessions/{session}/atlas/spaces/{identifier}"

    @app.get(base + "/account-invitations")
    def invitations(
        session: str, identifier: str, authorization: str | None = Header(default=None)
    ):
        owner(session, identifier, authorization)
        return response(
            {
                "enabled": getattr(app.state, "atlas_identity", None) is not None,
                "invitations": store().accounts.invitations(identifier),
            }
        )

    @app.post(base + "/account-invitations")
    async def invite(
        session: str,
        identifier: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        actor_id = owner(session, identifier, authorization)
        if getattr(app.state, "atlas_identity", None) is None:
            raise HTTPException(503, "Configure account sign-in before inviting an account.")
        value = await read_json(request, NewAccountInvitation)
        return response(
            store().accounts.invite(identifier, value.role, value.lifetime_hours, actor_id=actor_id)
        )

    @app.delete(base + "/account-invitations/{invitation_id}")
    def revoke(
        session: str,
        identifier: str,
        invitation_id: str,
        authorization: str | None = Header(default=None),
    ):
        actor_id = owner(session, identifier, authorization)
        store().accounts.revoke_invite(identifier, invitation_id, actor_id=actor_id)
        return response({"revoked": True})

    @app.get(base + "/members")
    def members(
        session: str,
        identifier: str,
        authorization: str | None = Header(default=None),
    ):
        owner(session, identifier, authorization)
        return response({"members": store().accounts.members(identifier)})

    @app.delete(base + "/members/{account_id}")
    def remove(
        session: str,
        identifier: str,
        account_id: str,
        authorization: str | None = Header(default=None),
    ):
        actor_id = owner(session, identifier, authorization)
        store().accounts.remove(identifier, account_id, actor_id=actor_id)
        return response({"removed": True})
