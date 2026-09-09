"""Opt-in, networkless verification of browser session identity; never fleet authority."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import HTTPException


@dataclass(frozen=True)
class AtlasIdentitySettings:
    issuer: str
    public_key: str = field(repr=False)
    authorized_parties: tuple[str, ...]
    audience: str | None = None

    def __post_init__(self):
        def origin(value, *, local=False):
            parsed = urlsplit(value)
            return (
                (
                    parsed.scheme == "https"
                    or (
                        local
                        and parsed.scheme == "http"
                        and parsed.hostname in ("localhost", "127.0.0.1")
                    )
                )
                and bool(parsed.netloc)
                and not (
                    parsed.path
                    or parsed.query
                    or parsed.fragment
                    or parsed.username
                    or parsed.password
                )
                and value == value.strip()
            )

        if not origin(self.issuer):
            raise ValueError("Atlas identity issuer must be an exact HTTPS origin.")
        if not self.authorized_parties or not all(
            origin(value, local=True) for value in self.authorized_parties
        ):
            raise ValueError("Atlas identity requires exact authorized browser origins.")
        if not 1 <= len(self.public_key) <= 8192:
            raise ValueError("Atlas identity requires a bounded PEM public key.")
        if self.audience is not None and not 1 <= len(self.audience) <= 256:
            raise ValueError("Atlas identity audience must be a bounded nonempty value.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]):
        names = (
            "SWEEP_ATLAS_JWT_ISSUER",
            "SWEEP_ATLAS_JWT_PUBLIC_KEY",
            "SWEEP_ATLAS_AUTHORIZED_PARTIES",
            "SWEEP_ATLAS_JWT_AUDIENCE",
        )
        if not any(name in environ for name in names):
            return None
        if not all(environ.get(name) for name in names[:3]):
            raise ValueError("Atlas identity configuration is incomplete.")
        return cls(
            issuer=environ[names[0]],
            public_key=environ[names[1]],
            authorized_parties=tuple(value.strip() for value in environ[names[2]].split(",")),
            audience=environ.get(names[3]),
        )


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str


class AtlasIdentityVerifier:
    def __init__(self, settings: AtlasIdentitySettings):
        self.settings = settings
        self.key = load_pem_public_key(settings.public_key.encode())
        if not isinstance(self.key, RSAPublicKey) or self.key.key_size < 2048:
            raise ValueError("Atlas identity requires an RSA public key of at least 2048 bits.")

    def verify(self, authorization: str | None) -> VerifiedIdentity:
        try:
            if not authorization or not authorization.startswith("Bearer "):
                raise ValueError
            token = authorization[7:]
            if not 1 <= len(token) <= 8192 or token.count(".") != 2:
                raise ValueError
            claims = jwt.decode(
                token,
                self.key,
                algorithms=["RS256"],
                issuer=self.settings.issuer,
                audience=self.settings.audience,
                leeway=5,
                options={"require": ["iss", "sub", "sid", "exp", "nbf", "iat", "azp"]},
            )
            # This browser-only adapter intentionally rejects absent azp and machine tokens.
            if claims["azp"] not in self.settings.authorized_parties:
                raise ValueError
            if claims.get("sts") not in (None, "active"):
                raise ValueError
            if any(type(claims[key]) is not int for key in ("exp", "iat", "nbf")):
                raise ValueError
            if not 0 < claims["exp"] - claims["iat"] <= 300:
                raise ValueError
            for key in ("sub", "sid"):
                value = claims[key]
                if not isinstance(value, str) or not 1 <= len(value) <= 256:
                    raise ValueError
                if not value.isprintable() or value != value.strip():
                    raise ValueError
            return VerifiedIdentity(claims["iss"], claims["sub"])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise HTTPException(
                401,
                "Sign in again to access your shared spaces.",
                headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
            ) from None
