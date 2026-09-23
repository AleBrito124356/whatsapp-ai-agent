"""The HTTP layer end to end: verification, signatures, dedupe, dry-run outbox.

Uses FastAPI's TestClient against ``create_app(services)`` with a temp DB, so
nothing here depends on the developer's .env or touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.deps import build_services
from app.main import create_app
from app.security import compute_signature

from .conftest import OfflineLLM

SECRET = "".join(["local", "-", "test", "-", "secret"])
WA_ID = "50760001234"


def envelope(message: dict, name: str = "Ana") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "PNID"},
                            "contacts": [{"profile": {"name": name}, "wa_id": WA_ID}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def text_event(mid: str, body: str) -> dict:
    return envelope({"from": WA_ID, "id": mid, "timestamp": "1790000000", "type": "text", "text": {"body": body}})


def post_signed(client: TestClient, payload: dict, secret: str = SECRET):
    raw = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": compute_signature(secret, raw)},
    )


@pytest.fixture
def dry_services(tmp_path, clock):
    settings = Settings(app_secret=SECRET, verify_token="verify-me")
    assert settings.dry_run
    return build_services(settings, db_path=tmp_path / "http.sqlite", clock=clock, llm=OfflineLLM())


@pytest.fixture
def client(dry_services):
    with TestClient(create_app(dry_services)) as c:
        yield c


def test_get_verification_echoes_challenge(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_unsigned_and_badly_signed_posts_are_rejected(client):
    raw = json.dumps(text_event("wamid.1", "hola")).encode()
    assert client.post("/webhook", content=raw).status_code == 403
    assert post_signed(client, text_event("wamid.1", "hola"), secret="wrong").status_code == 403


def test_malformed_json_is_400(client):
    raw = b"{not json"
    resp = client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": compute_signature(SECRET, raw)})
    assert resp.status_code == 400


def test_signed_message_is_processed_into_the_outbox_and_replays_are_dropped(client, dry_services):
    cursor = client.get("/dev/outbox").json()["cursor"]
    resp = post_signed(client, text_event("wamid.hello", "hola"))
    assert resp.status_code == 200 and resp.json() == {"status": "received", "accepted": 1}

    out = client.get("/dev/outbox", params={"wa_id": WA_ID, "after": cursor, "for_message": "wamid.hello"}).json()
    assert out["handled"] is True
    assert [m["kind"] for m in out["messages"]] == ["buttons"]
    menu = out["messages"][0]
    assert "Barbería Studio Norte" in menu["body"]
    assert [o["id"] for o in menu["options"]] == ["menu:booking", "menu:info", "menu:human"]
    # The raw Graph payload is stored verbatim.
    assert menu["payload"]["messaging_product"] == "whatsapp" and menu["payload"]["to"] == WA_ID

    replay = post_signed(client, text_event("wamid.hello", "hola"))
    assert replay.json()["accepted"] == 0
    again = client.get("/dev/outbox", params={"after": out["cursor"]}).json()
    assert again["messages"] == []


def test_health_reports_dry_run(client):
    body = client.get("/health").json()
    assert body["dry_run"] is True and body["whatsapp"] == "dry_run"
    assert body["llm"] == "offline" and body["signature_verification"] is True


def _parse_env_example() -> dict:
    from .conftest import REPO_ROOT

    env = {}
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def test_env_example_placeholders_never_reach_graph_facebook(tmp_path, clock):
    """Copying .env.example verbatim must give an offline, dry-run server."""
    calls: list[str] = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        raise AssertionError(f"unexpected network call: {request.url}")

    env = _parse_env_example()
    env["DB_PATH"] = str(tmp_path / "example.sqlite")
    settings = Settings.from_env(env)
    assert not settings.whatsapp_configured and settings.dry_run and not settings.llm_configured

    services = build_services(settings, clock=clock, http_client=httpx.Client(transport=httpx.MockTransport(fail)))
    with TestClient(create_app(services)) as c:
        assert c.get("/health").json()["dry_run"] is True
        resp = post_signed(c, text_event("wamid.example", "quiero una cita"), secret=env["WHATSAPP_APP_SECRET"])
        assert resp.status_code == 200
        out = c.get("/dev/outbox", params={"for_message": "wamid.example"}).json()
    assert out["handled"] is True and out["messages"][0]["kind"] == "list"
    assert calls == []


def test_live_mode_posts_to_graph_and_hides_the_outbox(tmp_path, clock):
    """With real-looking credentials the Graph transport is used (mocked here)."""
    seen: list[httpx.Request] = []

    def graph(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    settings = Settings(whatsapp_token="EAAGrealtoken123", phone_number_id="123456789012345", app_secret=SECRET)
    assert settings.whatsapp_configured and not settings.dry_run
    services = build_services(
        settings,
        db_path=tmp_path / "live.sqlite",
        clock=clock,
        llm=OfflineLLM(),
        http_client=httpx.Client(transport=httpx.MockTransport(graph)),
    )
    with TestClient(create_app(services)) as c:
        assert c.get("/dev/outbox").status_code == 404
        assert post_signed(c, text_event("wamid.live", "hola")).status_code == 200
    urls = {str(r.url) for r in seen}
    assert urls == {"https://graph.facebook.com/v21.0/123456789012345/messages"}
    bodies = [json.loads(r.content) for r in seen]
    assert {"status": "read", "message_id": "wamid.live"}.items() <= bodies[0].items()
    assert bodies[-1]["type"] == "interactive"
    assert seen[-1].headers["Authorization"] == "Bearer EAAGrealtoken123"
