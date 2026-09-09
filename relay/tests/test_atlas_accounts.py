"""Cryptographically verified, account-bound collaboration without operator authority."""

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.atlas import (
    AtlasError,
    AtlasStore,
    CaptureMetadata,
    CaptureRequest,
    Contributor,
    NewSpace,
)
from relay.atlas_identity import AtlasIdentitySettings, AtlasIdentityVerifier, VerifiedIdentity
from relay.settings import RelaySettings

ISSUER = "https://sweep-test.clerk.accounts.dev"
ORIGIN = "http://127.0.0.1:8177"
TOKEN = "atlas-account-tests-operator-credential"
OWNER = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/api/sessions/austin-test/atlas/spaces"
SPACE = {"title": "Austin garden test", "latitude": 30.27, "longitude": -97.74}
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
)


@pytest.fixture(scope="module")
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, AtlasIdentitySettings(ISSUER, pem, (ORIGIN,))


def signed(keys, subject="user_alex", **changes):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": subject,
        "sid": "sess_test",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "azp": ORIGIN,
        **changes,
    }
    return {"Authorization": "Bearer " + jwt.encode(claims, keys[0], algorithm="RS256")}


def test_valid_identity_and_no_network_requirement(keys):
    verified = AtlasIdentityVerifier(keys[1]).verify(signed(keys)["Authorization"])
    assert verified == VerifiedIdentity(ISSUER, "user_alex")


def test_account_creates_private_owned_space_and_manages_members(api, keys):
    auth, guest, stranger = signed(keys), signed(keys, "guest"), signed(keys, "stranger")
    payload = {"draft_id": str(uuid.uuid4()), "space": SPACE}
    endpoint = "/api/atlas/account/spaces"
    assert api.post(endpoint, json=payload).status_code == 401
    response = api.post(endpoint, headers=auth, json=payload)
    assert response.status_code == 201, response.text
    grant = response.json()
    assert grant["role"] == "owner"
    assert "token" not in response.text
    url = f"/api/sessions/{grant['session']}/atlas/spaces/{grant['space_id']}"
    assert api.get(url, headers=auth).status_code == 200
    assert api.get(url, headers=stranger).status_code == 403
    assert api.get(url.replace(grant["session"], "other"), headers=auth).status_code == 403
    assert api.post(endpoint, headers=auth, json=payload).json() == grant
    assert len(api.get(endpoint, headers=auth).json()["spaces"]) == 1
    different = {**payload, "space": {**SPACE, "title": "Different place"}}
    assert api.post(endpoint, headers=auth, json=different).status_code == 409
    other = api.post(endpoint, headers=stranger, json=payload).json()
    assert other["space_id"] != grant["space_id"]
    assert other["session"] != grant["session"]
    assert upload(api, url, auth).status_code == 201
    assert api.post(url + "/status", headers=auth, json={"status": "resolved"}).status_code == 200
    assert upload(api, url, auth).status_code == 409
    assert api.post(url + "/status", headers=auth, json={"status": "active"}).status_code == 200
    invitation = api.post(url + "/account-invitations", headers=auth, json={"role": "contributor"})
    assert invitation.status_code == 200
    assert (
        api.post(
            "/api/atlas/account/invitations/accept",
            headers=guest,
            json={"token": invitation.json()["token"]},
        ).status_code
        == 200
    )
    members = api.get(url + "/members", headers=auth).json()["members"]
    owner_id = next(m["account_id"] for m in members if m["role"] == "owner")
    guest_id = next(m["account_id"] for m in members if m["role"] == "contributor")
    for other_auth in (guest, stranger):
        assert api.get(url + "/members", headers=other_auth).status_code == 403
        assert (
            api.post(url + "/account-invitations", headers=other_auth, json={}).status_code == 403
        )
        assert api.delete(url + "/members/" + owner_id, headers=other_auth).status_code == 403
        assert (
            api.post(url + "/status", headers=other_auth, json={"status": "resolved"}).status_code
            == 403
        )
    assert (
        api.post(url + "/account-invitations", headers=auth, json={"role": "owner"}).status_code
        == 422
    )
    assert (
        api.delete(endpoint + f"/{grant['space_id']}/membership", headers=auth).status_code == 409
    )
    assert api.delete(url + "/members/" + owner_id, headers=auth).status_code == 409
    assert api.delete(url + "/members/" + owner_id, headers=OWNER).status_code == 409
    assert api.delete(url + "/members/" + guest_id, headers=auth).status_code == 200
    assert api.get(url, headers=guest).status_code == 403
    for suffix in ("/invitation", "/reconstruction"):
        assert api.post(url + suffix, headers=auth).status_code == 401
    assert api.get("/metrics", headers=auth).status_code == 401
    assert api.get(BASE, headers=auth).status_code == 401
    assert api.post(BASE, headers=auth, json=SPACE).status_code == 401


def test_owned_creation_is_atomic_bounded_and_does_not_claim_legacy(tmp_path):
    atlas = AtlasStore(tmp_path)
    account = atlas.accounts.account(VerifiedIdentity(ISSUER, "creator"))["id"]
    legacy = atlas.create("legacy", NewSpace(**SPACE))["space"]["id"]
    assert atlas.accounts.spaces(account) == []
    atlas.db.execute(
        "CREATE TRIGGER reject_owner BEFORE INSERT ON atlas_members "
        "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        atlas.create("ignored", NewSpace(**SPACE), str(uuid.uuid4()), account_id=account)
    assert atlas.db.execute("SELECT count(*) FROM spaces").fetchone()[0] == 1
    assert atlas.db.execute("SELECT count(*) FROM published_drafts").fetchone()[0] == 0
    atlas.db.execute("DROP TRIGGER reject_owner")
    first_draft = str(uuid.uuid4())
    first = atlas.create("ignored", NewSpace(**SPACE), first_draft, account_id=account)
    assert first["contributor_token"] is None
    for _ in range(19):
        atlas.create("ignored", NewSpace(**SPACE), str(uuid.uuid4()), account_id=account)
    with pytest.raises(AtlasError) as error:
        atlas.create("ignored", NewSpace(**SPACE), str(uuid.uuid4()), account_id=account)
    assert error.value.status == 409
    assert (
        atlas.create("ignored", NewSpace(**SPACE), first_draft, account_id=account)["space"]["id"]
        == first["space"]["id"]
    )
    assert all(item["space"]["id"] != legacy for item in atlas.accounts.spaces(account))
    atlas.close()
    reopened = AtlasStore(tmp_path)
    assert len(reopened.accounts.spaces(account)) == 20
    reopened.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.clerk.accounts.dev"},
        {"azp": "https://other.example"},
        {"azp": None},
        {"exp": 1},
        {"nbf": 9999999999},
        {"iat": 9999999999},
        {"sub": ""},
        {"sub": " user_alex"},
        {"sub": ["user_alex"]},
        {"sid": ""},
        {"sid": None},
        {"sts": "pending"},
        {"sts": "ended"},
        {"aud": "an-unconfigured-audience"},
        {"exp": "9999999999"},
        {"exp": 9999999999},
    ],
)
def test_invalid_signed_claims_refused(keys, changes):
    with pytest.raises(HTTPException) as error:
        AtlasIdentityVerifier(keys[1]).verify(signed(keys, **changes)["Authorization"])
    assert error.value.status_code == 401
    assert error.value.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("claim", ["iss", "sub", "sid", "exp", "nbf", "iat", "azp"])
def test_required_claims(keys, claim):
    token = signed(keys)["Authorization"][7:]
    claims = jwt.decode(token, options={"verify_signature": False})
    del claims[claim]
    token = jwt.encode(claims, keys[0], algorithm="RS256")
    with pytest.raises(HTTPException):
        AtlasIdentityVerifier(keys[1]).verify("Bearer " + token)


def test_signature_algorithm_audience_and_bounds(keys):
    verifier = AtlasIdentityVerifier(keys[1])
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(signed(keys)["Authorization"][7:], options={"verify_signature": False})
    invalid = [
        None,
        "",
        "Bearer ",
        "Bearer " + "a" * 8193,
        "Basic password",
        "Bearer " + jwt.encode(claims, private, algorithm="RS256"),
        "Bearer " + jwt.encode(claims, "test-only-shared-key" * 3, algorithm="HS256"),
        "Bearer " + jwt.encode(claims, None, algorithm="none"),
    ]
    for value in invalid:
        with pytest.raises(HTTPException):
            verifier.verify(value)
    with_audience = AtlasIdentityVerifier(
        AtlasIdentitySettings(ISSUER, keys[1].public_key, (ORIGIN,), "sweep-atlas")
    )
    assert (
        with_audience.verify(signed(keys, aud="sweep-atlas")["Authorization"]).subject
        == "user_alex"
    )
    with pytest.raises(HTTPException):
        with_audience.verify(signed(keys)["Authorization"])


def test_identity_config_is_explicit_and_incomplete_config_refuses(keys):
    assert AtlasIdentitySettings.from_env({}) is None
    for environment in ({"SWEEP_ATLAS_JWT_ISSUER": ISSUER}, {"SWEEP_ATLAS_JWT_AUDIENCE": "x"}):
        with pytest.raises(ValueError):
            AtlasIdentitySettings.from_env(environment)
    for issuer in ("http://example.com", "https://example.com/path", "https://u:p@example.com"):
        with pytest.raises(ValueError):
            AtlasIdentitySettings(issuer, keys[1].public_key, (ORIGIN,))
    for parties in ((), ("*",), ("http://example.com",), ("https://example.com/path",)):
        with pytest.raises(ValueError):
            AtlasIdentitySettings(ISSUER, keys[1].public_key, parties)
    with pytest.raises(ValueError):
        AtlasIdentityVerifier(AtlasIdentitySettings(ISSUER, "not a key", (ORIGIN,)))
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": TOKEN,
            "SWEEP_ATLAS_JWT_ISSUER": ISSUER,
            "SWEEP_ATLAS_JWT_PUBLIC_KEY": keys[1].public_key,
            "SWEEP_ATLAS_AUTHORIZED_PARTIES": ORIGIN,
        }
    )
    assert settings.atlas_identity == keys[1]


@pytest.fixture
def api(tmp_path, keys):
    app = create_app(
        RelaySettings(relay_token=TOKEN.encode(), log_dir=tmp_path, atlas_identity=keys[1])
    )
    with TestClient(app) as client:
        yield client


def create_space(client):
    response = client.post(BASE, headers=OWNER, json=SPACE)
    assert response.status_code == 201, response.text
    data = response.json()
    return f"{BASE}/{data['space']['id']}", data


def join(client, url, auth, role="contributor"):
    invitation = client.post(url + "/account-invitations", headers=OWNER, json={"role": role})
    assert invitation.status_code == 200, invitation.text
    value = invitation.json()
    response = client.post(
        "/api/atlas/account/invitations/accept", headers=auth, json={"token": value["token"]}
    )
    assert response.status_code == 200, response.text
    return value


def upload(client, url, auth, **changes):
    metadata = {
        "contributor_id": "client-can-choose-this",
        "name": "Alex",
        "kind": "photo",
        "source": "import",
        **changes,
    }
    return client.post(
        url + "/captures",
        content=PNG,
        headers={
            **auth,
            "Content-Type": "image/png",
            "X-Sweep-Capture": json.dumps(metadata),
        },
    )


def test_complete_account_invitation_capture_and_revocation(api, keys):
    url, created = create_space(api)
    alex, sam = signed(keys), signed(keys, "user_sam")
    account = api.post("/api/atlas/account", headers=alex).json()["account"]
    assert api.post("/api/atlas/account", headers=alex).json()["account"] == account
    assert api.get(url, headers=alex).status_code == 403
    invitation = join(api, url, alex)
    assert api.get(url, headers=alex).status_code == 200
    assert api.get(url.replace("austin-test", "other"), headers=alex).status_code == 403
    assert api.get(url, headers=sam).status_code == 403
    assert api.get("/api/atlas/account/spaces", headers=alex).json()["spaces"][0]["role"] == (
        "contributor"
    )
    assert api.get("/api/atlas/account/spaces", headers=sam).json()["spaces"] == []
    duplicate_accept = api.post(
        "/api/atlas/account/invitations/accept", headers=alex, json={"token": invitation["token"]}
    )
    assert duplicate_accept.status_code == 200
    assert (
        api.post(
            "/api/atlas/account/invitations/accept",
            headers=sam,
            json={"token": invitation["token"]},
        ).status_code
        == 409
    )

    result = upload(api, url, alex)
    assert result.status_code == 201, result.text
    capture = result.json()
    assert capture["contributor_id"] == capture["account_id"] == account["id"]
    assert capture["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert capture["captured_at"] is None
    media_url = url + f"/captures/{capture['id']}/media"
    assert api.get(media_url, headers=alex).content == PNG
    assert api.get(media_url, headers=alex).headers["Cache-Control"] == "no-store"
    memory = api.get(url + f"/captures/{capture['id']}/memory", headers=alex).json()
    assert memory["can_edit"] is True
    assert memory["can_analyze"] is False
    assert api.post(url + "/reconstruction", headers=alex).status_code == 401
    assert api.post(url + "/status", headers=alex, json={"status": "resolved"}).status_code == 403
    assert api.get("/metrics", headers=alex).status_code == 401
    assert api.get(BASE, headers=alex).status_code == 401
    assert api.get(url + "/members", headers=alex).status_code == 403

    assert api.delete(url + "/members/" + account["id"], headers=OWNER).status_code == 200
    assert api.get(media_url, headers=alex).status_code == 403
    assert api.get(url + f"/captures/{capture['id']}/memory", headers=alex).status_code == 403
    assert api.get(url, headers=alex).status_code == 403
    assert upload(api, url, alex).status_code == 403
    assert api.get("/api/atlas/account/spaces", headers=alex).json()["spaces"] == []
    assert (
        api.post(
            "/api/atlas/account/invitations/accept",
            headers=alex,
            json={"token": invitation["token"]},
        ).status_code
        == 403
    )
    # Independent legacy grants keep their original behavior and need separate revocation.
    legacy = {"Authorization": "Bearer " + created["contributor_token"]}
    assert api.get(url, headers=legacy).status_code == 200
    assert api.get(url, headers=OWNER).status_code == 200
    api.post(url + "/invitation", headers=OWNER)
    assert api.get(url, headers=legacy).status_code == 403


def test_viewer_cannot_mutate_and_legacy_bytes_do_not_gain_account_authorship(api, keys):
    url, created = create_space(api)
    viewer = signed(keys, "viewer")
    join(api, url, viewer, "viewer")
    assert api.get(url, headers=viewer).status_code == 200
    for route in ("/captures", "/presence", "/leave", "/requests"):
        assert api.post(url + route, headers=viewer, json={}).status_code == 403
    assert api.post(url + "/account-invitations", headers=viewer, json={}).status_code == 403
    legacy = {"Authorization": "Bearer " + created["contributor_token"]}
    original = upload(api, url, legacy).json()
    assert "account_id" not in original
    auth = signed(keys)
    join(api, url, auth)
    # Deduplicating an existing original must not claim its authorship for a new account.
    assert upload(api, url, auth).json() == original
    assert upload(api, url, auth, account_id="spoofed-account").status_code == 422


def test_short_lived_token_can_finish_admitted_bounded_upload(api, keys, monkeypatch):
    url, _ = create_space(api)
    auth = signed(keys)
    join(api, url, auth)
    from relay import atlas_routes

    original_media_type = atlas_routes.media_type
    real_datetime = jwt.api_jwt.datetime

    class AfterTokenExpiry:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=120)

    def elapsed_transfer(path, claimed):
        monkeypatch.setattr(jwt.api_jwt, "datetime", AfterTokenExpiry)
        return original_media_type(path, claimed)

    monkeypatch.setattr(atlas_routes, "media_type", elapsed_transfer)
    result = upload(api, url, auth)
    assert result.status_code == 201, result.text
    # Expiration is still enforced on every new request, not cached by account membership.
    assert api.get(url, headers=auth).status_code == 401


def test_revoked_or_expired_invites_and_space_isolation(api, keys):
    url, _ = create_space(api)
    other, _ = create_space(api)
    invite = api.post(url + "/account-invitations", headers=OWNER, json={}).json()
    api.delete(other + "/account-invitations/" + invite["id"], headers=OWNER)
    api.delete(url + "/account-invitations/" + invite["id"], headers=OWNER)
    assert (
        api.post(
            "/api/atlas/account/invitations/accept",
            headers=signed(keys),
            json={"token": invite["token"]},
        ).status_code
        == 403
    )
    invite = api.post(url + "/account-invitations", headers=OWNER, json={}).json()
    atlas = api.app.state.atlas_store
    atlas.clock = lambda: invite["expires_at"]
    assert (
        api.post(
            "/api/atlas/account/invitations/accept",
            headers=signed(keys),
            json={"token": invite["token"]},
        ).status_code
        == 403
    )
    for role in ("owner", "operator", "admin"):
        assert (
            api.post(url + "/account-invitations", headers=OWNER, json={"role": role}).status_code
            == 422
        )


def test_invitation_preview_requires_identity_and_does_not_accept(api, keys):
    url, _ = create_space(api)
    invitation = api.post(
        url + "/account-invitations", headers=OWNER, json={"role": "viewer"}
    ).json()
    pending = api.get(url + "/account-invitations", headers=OWNER)
    assert pending.json()["enabled"] is True
    assert "token" not in pending.json()["invitations"][0]
    assert invitation["token"] not in pending.text
    endpoint = "/api/atlas/account/invitations/preview"
    assert api.post(endpoint, json={"token": invitation["token"]}).status_code == 401
    preview = api.post(endpoint, headers=signed(keys), json={"token": invitation["token"]})
    assert preview.status_code == 200
    assert preview.json()["title"] == SPACE["title"]
    assert preview.json()["role"] == "viewer"
    assert "latitude" not in preview.json()
    assert api.get(url, headers=signed(keys)).status_code == 403
    assert api.get(url + "/members", headers=OWNER).json()["members"] == []
    api.post(
        "/api/atlas/account/invitations/accept",
        headers=signed(keys),
        json={"token": invitation["token"]},
    )
    assert api.get(url + "/account-invitations", headers=OWNER).json()["invitations"] == []
    assert (
        api.post(
            endpoint, headers=signed(keys, "another-user"), json={"token": invitation["token"]}
        ).status_code
        == 403
    )


def test_disabled_identity_does_not_change_legacy_capture(tmp_path, keys):
    app = create_app(RelaySettings(relay_token=TOKEN.encode(), log_dir=tmp_path))
    with TestClient(app) as client:
        url, _ = create_space(client)
        assert client.post("/api/atlas/account", headers=signed(keys)).status_code == 503
        assert client.post(url + "/account-invitations", headers=OWNER, json={}).status_code == 503
        assert upload(client, url, OWNER).status_code == 201


def test_member_can_leave_without_removing_someone_else(api, keys):
    url, created = create_space(api)
    alex, sam = signed(keys), signed(keys, "user_sam")
    invite = join(api, url, alex)
    join(api, url, sam)
    endpoint = f"/api/atlas/account/spaces/{created['space']['id']}/membership"
    assert api.delete(endpoint, headers=alex).status_code == 200
    assert api.get(url, headers=alex).status_code == 403
    assert api.get(url, headers=sam).status_code == 200
    assert (
        api.post(
            "/api/atlas/account/invitations/accept", headers=alex, json={"token": invite["token"]}
        ).status_code
        == 403
    )
    assert api.delete(endpoint, headers=alex).status_code == 200


def test_account_presence_cannot_impersonate_another_contributor(api, keys):
    url, _ = create_space(api)
    auth = signed(keys)
    join(api, url, auth)
    account = api.post("/api/atlas/account", headers=auth).json()["account"]["id"]
    value = {
        "contributor_id": "someone-else",
        "name": "User-chosen display name",
        "position": {
            "latitude": 30.27,
            "longitude": -97.74,
            "accuracy": 5,
            "timestamp": int(time.time() * 1000),
        },
    }
    posted = api.post(url + "/presence", headers=auth, json=value)
    assert posted.status_code == 200
    assert posted.json()["contributor_id"] == account
    assert (
        api.post(url + "/leave", headers=auth, json={"contributor_id": "someone-else"}).status_code
        == 200
    )
    assert api.get(url, headers=auth).json()["people"] == []


def test_pending_invitation_limit_and_role_changes_require_new_grant(tmp_path):
    store = AtlasStore(tmp_path)
    space = store.create("test", NewSpace(**SPACE))["space"]["id"]
    account = store.accounts.account(VerifiedIdentity(ISSUER, "user_one"))["id"]
    first = store.accounts.invite(space, "viewer", 1)
    store.accounts.accept(first["token"], account)
    upgrade = store.accounts.invite(space, "contributor", 1)
    with pytest.raises(AtlasError) as error:
        store.accounts.accept(upgrade["token"], account)
    assert error.value.status == 409
    assert store.accounts.role(space, account) == "viewer"
    for _ in range(49):
        store.accounts.invite(space, "viewer", 1)
    with pytest.raises(AtlasError) as error:
        store.accounts.invite(space, "viewer", 1)
    assert error.value.status == 429
    store.accounts.remove(space, account)
    store.accounts.accept(upgrade["token"], account)
    assert store.accounts.role(space, account) == "contributor"
    with pytest.raises(AtlasError):
        store.accounts.accept(first["token"], account)
    store.close()


def test_accounts_persist_and_removed_account_cannot_finalize_upload(tmp_path):
    store = AtlasStore(tmp_path)
    space = store.create("test", NewSpace(**SPACE))["space"]["id"]
    identity = VerifiedIdentity(ISSUER, "user_alex")
    account = store.accounts.account(identity)
    assert (
        store.accounts.account(VerifiedIdentity("https://other.example", identity.subject))
        != account
    )
    invitation = store.accounts.invite(space, "contributor", 1)
    store.accounts.accept(invitation["token"], account["id"])
    # Only the digest is persisted, not the usable invitation token.
    assert (
        store.db.execute("SELECT token_hash FROM atlas_account_invites").fetchone()[0]
        != (invitation["token"])
    )
    store.close()
    store = AtlasStore(tmp_path)
    assert store.accounts.account(identity) == account
    assert store.accounts.role(space, account["id"]) == "contributor"
    other = AtlasStore(tmp_path)
    other.accounts.remove(space, account["id"])
    staged = tmp_path / "test.png"
    staged.write_bytes(PNG)
    with pytest.raises(AtlasError) as error:
        store.add_capture(
            space,
            CaptureMetadata(
                contributor_id="untrusted-client", name="Alex", kind="photo", source="import"
            ),
            staged,
            "image/png",
            account_id=account["id"],
        )
    assert error.value.status == 403
    assert staged.read_bytes() == PNG
    assert store.captures(space) == []
    presence = Contributor(
        contributor_id="untrusted-client",
        name="Alex",
        position={
            "latitude": 30.27,
            "longitude": -97.74,
            "accuracy": 5,
            "timestamp": store.clock(),
        },
    )
    for operation in (
        lambda: store.publish_presence(space, presence, account_id=account["id"]),
        lambda: store.leave(space, "untrusted-client", account_id=account["id"]),
        lambda: store.request_capture(
            space, CaptureRequest(cell_id="5:5"), account_id=account["id"]
        ),
    ):
        with pytest.raises(AtlasError) as error:
            operation()
        assert error.value.status == 403
    other.close()
    store.close()
