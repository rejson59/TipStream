import asyncio

from fastapi.testclient import TestClient

from app import bridge, main
from app.models import GameAction, TikTokEvent


def test_browser_event_ingest_is_loopback_only_and_normalizes_source(monkeypatch):
    seen = []

    async def capture(event, *, trigger_actions=True):
        seen.append((event, trigger_actions))

    monkeypatch.setattr(main, "process_event", capture)
    client = TestClient(main.app, client=("127.0.0.1", 12345))
    response = client.post("/api/browser/event", json={
        "event": {"type": "follow", "user": "viewer", "nickname": "Viewer", "source": "forged"}
    })
    assert response.status_code == 200
    assert response.json()["received"] == "follow"
    assert len(seen) == 1
    assert seen[0][0].source == "browser"
    assert seen[0][1] is False

    triggered = client.post("/api/browser/event", json={
        "event": {"type": "follow", "user": "viewer"}, "trigger_actions": True
    })
    assert triggered.status_code == 200
    assert seen[1][1] is True

    remote = TestClient(main.app, client=("198.51.100.9", 12345))
    assert remote.post("/api/browser/event", json={"event": {"type": "follow", "user": "viewer"}}).status_code == 403
    assert remote.get("/api/browser/status").status_code == 403


def test_browser_rejects_system_events_and_oversized_text():
    client = TestClient(main.app, client=("127.0.0.1", 12345))
    assert client.post("/api/browser/event", json={"event": {"type": "system"}}).status_code == 422
    response = client.post("/api/browser/event", json={
        "event": {"type": "comment", "user": "viewer", "comment": "x" * 1001}
    })
    assert response.status_code == 422


def test_browser_source_has_local_receipt_counters():
    hub = bridge.Hub()
    asyncio.run(hub.publish_event(TikTokEvent(type="like", user="viewer", like_count=3, source="browser")))
    assert hub.snapshot()["browser_bridge"]["events_received"] == 1
    assert hub.snapshot()["total_likes"] == 3


def test_action_history_records_execution_success_skip_and_failure(monkeypatch, tmp_path):
    hub = bridge.Hub()
    monkeypatch.setattr(main, "hub", hub)
    monkeypatch.setattr(bridge, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(bridge, "_udp_send", lambda _data: None)

    async def successful(_action):
        return "Akcja potwierdzona przez RCON."

    async def skipped(_action):
        return None

    async def failed(_action):
        raise RuntimeError("RCON odmówił wykonania")

    async def run():
        monkeypatch.setattr(main, "_run_parkour_action", successful)
        await main.publish_game_action(GameAction(action="parkour_jump", rule_name="Skok"))
        monkeypatch.setattr(main, "_run_parkour_action", skipped)
        await main.publish_game_action(GameAction(action="parkour_speed", rule_name="Szybkość"))
        monkeypatch.setattr(main, "_run_parkour_action", failed)
        await main.publish_game_action(GameAction(action="parkour_shield", rule_name="Tarcza"))

    asyncio.run(run())
    assert [item["execution_status"] for item in hub.recent_actions] == [
        "succeeded", "sent", "failed"
    ]
    assert hub.recent_actions[0]["execution_message"] == "Akcja potwierdzona przez RCON."
    assert "wyłączone" in hub.recent_actions[1]["execution_message"]
    assert "RCON odmówił" in hub.recent_actions[2]["execution_message"]
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert sum('"record_type": "action_status"' in line for line in lines) == 3
