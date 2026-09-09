"""Account-bound membership alongside, never inferred from, legacy invitation access."""

import hashlib
import json
import secrets
import uuid

from relay.atlas import AtlasError
from relay.atlas_identity import VerifiedIdentity


class AtlasAccounts:
    def __init__(self, atlas):
        self.atlas = atlas
        with atlas.lock:
            atlas.db.executescript("""
                CREATE TABLE IF NOT EXISTS atlas_accounts (
                  id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
                  created_at INTEGER NOT NULL, UNIQUE(issuer, subject));
                CREATE TABLE IF NOT EXISTS atlas_members (
                  space TEXT NOT NULL, account TEXT NOT NULL, role TEXT NOT NULL,
                  joined_at INTEGER NOT NULL, PRIMARY KEY(space, account));
                CREATE INDEX IF NOT EXISTS atlas_member_account ON atlas_members(account);
                CREATE TABLE IF NOT EXISTS atlas_account_invites (
                  id TEXT PRIMARY KEY, space TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                  role TEXT NOT NULL, expires_at INTEGER NOT NULL,
                  accepted_by TEXT, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS atlas_account_invite_space
                  ON atlas_account_invites(space);
            """)

    def account(self, identity: VerifiedIdentity) -> dict:
        with self.atlas.lock, self.atlas.db:
            lookup = "SELECT id,created_at FROM atlas_accounts WHERE issuer=? AND subject=?"
            key = (identity.issuer, identity.subject)
            row = self.atlas.db.execute(lookup, key).fetchone()
            if row:
                return {"id": row[0], "created_at": row[1]}
            self.atlas.db.execute(
                "INSERT OR IGNORE INTO atlas_accounts VALUES (?,?,?,?)",
                ("acct_" + uuid.uuid4().hex, identity.issuer, identity.subject, self.atlas.clock()),
            )
            row = self.atlas.db.execute(lookup, key).fetchone()
            return {"id": row[0], "created_at": row[1]}

    def role(self, space: str, account: str) -> str:
        with self.atlas.lock:
            row = self.atlas.db.execute(
                "SELECT role FROM atlas_members WHERE space=? AND account=?", (space, account)
            ).fetchone()
            if not row:
                raise AtlasError("This account does not have access to this space.", 403)
            return row[0]

    def require_contributor(self, space: str, account: str):
        if self.role(space, account) not in ("contributor", "owner"):
            raise AtlasError("This account cannot contribute to this space.", 403)

    def require_owner(self, space: str, account: str):
        if self.role(space, account) != "owner":
            raise AtlasError("Only this space's owner can manage account access.", 403)

    def spaces(self, account: str) -> list[dict]:
        with self.atlas.lock:
            return [
                {"space": self.atlas.summary(json.loads(data)), "session": session, "role": role}
                for session, data, role in self.atlas.db.execute(
                    "SELECT s.session,s.data,m.role FROM spaces s "
                    "JOIN atlas_members m ON s.id=m.space WHERE m.account=? ORDER BY s.id",
                    (account,),
                )
            ]

    def members(self, space: str) -> list[dict]:
        with self.atlas.lock:
            return [
                {"account_id": account, "role": role, "joined_at": joined}
                for account, role, joined in self.atlas.db.execute(
                    "SELECT account,role,joined_at FROM atlas_members "
                    "WHERE space=? ORDER BY joined_at,account",
                    (space,),
                )
            ]

    def invitations(self, space: str) -> list[dict]:
        with self.atlas.lock:
            return [
                {"id": identifier, "role": role, "expires_at": expires}
                for identifier, role, expires in self.atlas.db.execute(
                    "SELECT id,role,expires_at FROM atlas_account_invites WHERE space=? "
                    "AND accepted_by IS NULL AND revoked=0 AND expires_at>? ORDER BY expires_at,id",
                    (space, self.atlas.clock()),
                )
            ]

    def preview(self, token: str, account: str) -> dict:
        with self.atlas.lock:
            row = self.atlas.db.execute(
                "SELECT s.data,i.role,i.expires_at,i.accepted_by FROM atlas_account_invites i "
                "JOIN spaces s ON s.id=i.space WHERE i.token_hash=? AND i.revoked=0 "
                "AND i.expires_at>?",
                (self.digest(token), self.atlas.clock()),
            ).fetchone()
            if not row or row[3] not in (None, account):
                raise AtlasError("This invitation is unavailable. Ask for a new one.", 403)
            space = json.loads(row[0])
            return {
                "title": space["title"],
                "place": space["place"],
                "role": row[1],
                "expires_at": row[2],
            }

    def invite(
        self, space: str, role: str, lifetime_hours: int, *, actor_id: str | None = None
    ) -> dict:
        if role not in ("viewer", "contributor") or not 1 <= lifetime_hours <= 168:
            raise AtlasError("Choose a viewer or contributor invitation lasting 1–168 hours.")
        identifier, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        expires = self.atlas.clock() + lifetime_hours * 3_600_000
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            if actor_id is not None:
                self.require_owner(space, actor_id)
            if not self.atlas.db.execute("SELECT 1 FROM spaces WHERE id=?", (space,)).fetchone():
                raise AtlasError("Space not found.", 404)
            count = self.atlas.db.execute(
                "SELECT COUNT(*) FROM atlas_account_invites WHERE space=? "
                "AND revoked=0 AND accepted_by IS NULL AND expires_at>?",
                (space, self.atlas.clock()),
            ).fetchone()[0]
            if count >= 50:
                raise AtlasError("This space already has 50 pending account invitations.", 429)
            self.atlas.db.execute(
                "INSERT INTO atlas_account_invites "
                "(id,space,token_hash,role,expires_at) VALUES (?,?,?,?,?)",
                (identifier, space, self.digest(token), role, expires),
            )
        return {"id": identifier, "token": token, "role": role, "expires_at": expires}

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def accept(self, token: str, account: str) -> dict:
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            row = self.atlas.db.execute(
                "SELECT i.id,i.space,i.role,i.expires_at,i.accepted_by,i.revoked,s.session "
                "FROM atlas_account_invites i JOIN spaces s ON s.id=i.space "
                "WHERE token_hash=?",
                (self.digest(token),),
            ).fetchone()
            if not row or row[5] or row[3] <= self.atlas.clock():
                raise AtlasError("This invitation is unavailable. Ask for a new one.", 403)
            identifier, space, role, _expires, accepted_by, _revoked, session = row
            if accepted_by is not None:
                if accepted_by != account:
                    raise AtlasError("This invitation has already been accepted.", 409)
                # Retry is idempotent, but cannot restore removed membership.
                return {"space_id": space, "session": session, "role": self.role(space, account)}
            if self.atlas.db.execute(
                "SELECT 1 FROM atlas_members WHERE space=? AND account=?", (space, account)
            ).fetchone():
                raise AtlasError("You already belong to this space.", 409)
            self.atlas.db.execute(
                "INSERT INTO atlas_members VALUES (?,?,?,?)",
                (space, account, role, self.atlas.clock()),
            )
            self.atlas.db.execute(
                "UPDATE atlas_account_invites SET accepted_by=? WHERE id=?", (account, identifier)
            )
            return {"space_id": space, "session": session, "role": role}

    def revoke_invite(self, space: str, identifier: str, *, actor_id: str | None = None):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            if actor_id is not None:
                self.require_owner(space, actor_id)
            # Revoking an invitation stops redemption, not an already accepted membership.
            self.atlas.db.execute(
                "UPDATE atlas_account_invites SET revoked=1 WHERE space=? AND id=?",
                (space, identifier),
            )

    def remove(self, space: str, account: str, *, actor_id: str | None = None):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            if actor_id is not None:
                self.require_owner(space, actor_id)
            existing = self.atlas.db.execute(
                "SELECT role FROM atlas_members WHERE space=? AND account=?", (space, account)
            ).fetchone()
            if existing and existing[0] == "owner":
                raise AtlasError(
                    "The owner cannot leave or be removed. Ownership must be retained.", 409
                )
            self.atlas.db.execute(
                "DELETE FROM atlas_members WHERE space=? AND account=?", (space, account)
            )
            self.atlas.db.execute(
                "UPDATE atlas_account_invites SET revoked=1 WHERE space=? AND accepted_by=?",
                (space, account),
            )
            self.atlas.db.execute(
                "DELETE FROM presence WHERE space=? AND contributor=?", (space, account)
            )
