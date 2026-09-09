from fastapi.testclient import TestClient

from relay.app import create_app
from relay.settings import RelaySettings


def test_membership_removal_preflight_for_configured_browser_only(tmp_path):
    origin = "http://127.0.0.1:8177"
    application = create_app(
        RelaySettings(
            relay_token=b"atlas-cors-test-token-at-least-32-bytes",
            log_dir=tmp_path,
            console_origins=(origin,),
        )
    )
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "DELETE",
        "Access-Control-Request-Headers": "authorization",
    }
    with TestClient(application) as client:
        route = "/api/atlas/account/spaces/example/membership"
        allowed = client.options(route, headers=headers)
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == origin
        assert "DELETE" in allowed.headers["access-control-allow-methods"]
        unavailable = client.delete(route, headers={"Origin": origin})
        assert unavailable.status_code == 503
        assert (
            unavailable.json()["detail"] == "Account sign-in is not configured for this workspace."
        )
        refused = client.options(route, headers={**headers, "Origin": "https://untrusted.example"})
        assert refused.status_code == 400
        assert "access-control-allow-origin" not in refused.headers
