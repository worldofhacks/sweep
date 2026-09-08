"""Run a local-only Atlas preview using the production app and an isolated data directory.

Build console first: cd console && pnpm build
Run: .venv/bin/python -m tools.atlas_preview --port 8177 --examples
The optional examples are explicitly marked demonstrations, never real incident reports.
"""

import argparse
import secrets
from pathlib import Path

import uvicorn
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from relay.app import create_app
from relay.atlas import AtlasStore, NewSpace
from relay.settings import RelaySettings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8177)
    parser.add_argument("--examples", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    dist = root / "console" / "dist"
    if not (dist / "index.html").is_file():
        parser.error("Build console first: cd console && pnpm build")
    token = secrets.token_urlsafe(32)
    session = "atlas-preview"
    log_dir = root / ".sweep" / "atlas-preview"
    if args.examples:
        store = AtlasStore(log_dir / "atlas")
        try:
            if not store.list(session):
                for title, category, lat, lon, place, description in [
                    (
                        "Demo · Creekside restoration",
                        "community",
                        37.4442,
                        -122.1594,
                        "San Francisquito Creek",
                        "A demonstration of neighbors documenting a restoration project. "
                        "Add a view of the creek, paths, or planting areas.",
                    ),
                    (
                        "Demo · Downtown accessibility",
                        "survey",
                        37.4448,
                        -122.1634,
                        "University Avenue",
                        "A sample space for a shared accessibility survey. "
                        "Capture crossings, paths, and entrances from multiple viewpoints.",
                    ),
                    (
                        "Demo · Storm damage survey",
                        "hazard",
                        37.4407,
                        -122.1516,
                        "Heritage Park",
                        "Demonstration scenario only — no live hazard is being reported. "
                        "Documenting an area together can help everyone understand what changed.",
                    ),
                ]:
                    store.create(
                        session,
                        NewSpace(
                            title=title,
                            category=category,
                            latitude=lat,
                            longitude=lon,
                            place=place,
                            radius=100,
                            description=description,
                        ),
                    )
        finally:
            store.close()
    application = create_app(
        RelaySettings(
            relay_token=token.encode(),
            log_dir=log_dir,
            console_origins=(f"http://127.0.0.1:{args.port}",),
        )
    )

    @application.get("/relay-bootstrap.json")
    def bootstrap():
        return JSONResponse(
            {
                "relay": {
                    "baseUrl": f"ws://127.0.0.1:{args.port}",
                    "sessionId": session,
                    "token": token,
                }
            },
            headers={"Cache-Control": "no-store"},
        )

    application.mount("/", StaticFiles(directory=dist, html=True), name="atlas-preview")
    uvicorn.run(application, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
