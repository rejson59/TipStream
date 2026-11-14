"""GiftStream event bridge for the supported Minecraft Parkour mode and local tools.

Generic normalized events are also broadcast over WebSocket, UDP and JSONL for
local diagnostics/custom tools; they are not additional bundled game support.
Dashboard and overlays connect through /ws/feed.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
import uuid
from pathlib import Path

from fastapi import WebSocket

from .models import GameAction, TikTokEvent
from .earnings import resolve_diamonds
from .paths import data_dir
from . import diagnostics

UDP_HOST, UDP_PORT = "127.0.0.1", 47821
EVENTS_FILE = data_dir() / "events.jsonl"


class Hub:
    """Rozsyła eventy/akcje do wszystkich podłączonych klientów WS."""

    def __init__(self):
        self.game_clients: set[WebSocket] = set()
        self.feed_clients: set[WebSocket] = set()
        self.recent_events: list[dict] = []
        self.recent_actions: list[dict] = []
        self.donors: dict[str, dict] = {}   # user -> {diamonds, gifts, last}
        self.total_diamonds = 0
        self.total_gifts = 0
        self.viewer_count = 0
        self.total_likes = 0
        self.browser_event_count = 0
        self.browser_last_event_at = 0.0

    async def _broadcast(self, clients: set[WebSocket], payload: dict) -> None:
        dead = []
        for ws in list(clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    async def publish_event(self, ev: TikTokEvent) -> None:
        data = ev.model_dump()
        data["ts"] = time.time()
        if ev.source == "browser":
            self.browser_event_count += 1
            self.browser_last_event_at = data["ts"]
        if ev.type == "gift":
            diamonds, from_catalog = resolve_diamonds(ev.gift_id, ev.gift_name, ev.diamonds)
            data["earning_diamonds"] = diamonds
            data["diamonds_from_catalog"] = from_catalog or ev.diamonds_estimated
            if ev.repeat_end:
                count = max(ev.repeat_count, 1)
                self.total_gifts += count
                if diamonds > 0:
                    gift_diamonds = diamonds * count
                    self.total_diamonds += gift_diamonds
                    d = self.donors.setdefault(ev.user, {"user": ev.user, "diamonds": 0, "gifts": 0, "last": ""})
                    d["diamonds"] += gift_diamonds
                    d["gifts"] += count
                    d["last"] = ev.gift_name or ""
        self.recent_events.append(data)
        self.recent_events = self.recent_events[-200:]
        if ev.type == "like":
            self.total_likes += ev.like_count or 1
        await self._broadcast(self.feed_clients, {"kind": "event", "data": data})
        await self._broadcast(self.game_clients, {"kind": "event", "data": data})

    async def publish_action(self, ga: GameAction) -> str:
        data = ga.model_dump()
        action_id = uuid.uuid4().hex
        data.update({"action_id": action_id, "execution_status": "pending",
                     "execution_message": "Dopasowano regułę; oczekiwanie na wynik wykonania.",
                     "execution_updated_at": time.time()})
        self.recent_actions.append(data)
        self.recent_actions = self.recent_actions[-200:]
        await self._broadcast(self.feed_clients, {"kind": "action", "data": data})
        await self._broadcast(self.game_clients, {"kind": "action", "data": data})
        # Bridge: UDP + plik (nieblokujące, best-effort)
        asyncio.get_event_loop().run_in_executor(None, _udp_send, data)
        try:
            with EVENTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception as exc:
            diagnostics.warning("Nie udało się dopisać akcji do lokalnego dziennika JSONL: %s", exc)
        return action_id

    async def update_action(self, action_id: str, status: str, message: str) -> None:
        """Update the dashboard audit record without re-dispatching the game action."""
        update = {"action_id": action_id, "execution_status": status,
                  "execution_message": str(message)[:1000], "execution_updated_at": time.time()}
        action = next((item for item in reversed(self.recent_actions)
                       if item.get("action_id") == action_id), None)
        if action is None:
            return
        action.update(update)
        await self._broadcast(self.feed_clients, {"kind": "action_status", "data": update})
        try:
            with EVENTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"record_type": "action_status", **update}, ensure_ascii=False) + "\n")
        except Exception as exc:
            diagnostics.warning("Nie udało się zapisać wyniku akcji do dziennika JSONL: %s", exc)

    async def publish_status(self, status: dict) -> None:
        await self._broadcast(self.feed_clients, {"kind": "status", "data": status})
        await self._broadcast(self.game_clients, {"kind": "status", "data": status})

    def clear_activity(self) -> None:
        self.recent_events.clear()
        self.recent_actions.clear()
        try:
            EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            EVENTS_FILE.write_text("", encoding="utf-8")
        except OSError:
            pass

    def reset_stats(self) -> None:
        self.clear_activity()
        self.donors.clear()
        self.total_diamonds = 0
        self.total_gifts = 0
        self.viewer_count = 0
        self.total_likes = 0
        self.browser_event_count = 0
        self.browser_last_event_at = 0.0

    async def broadcast_snapshot(self) -> None:
        await self._broadcast(self.feed_clients, {"kind": "snapshot", "data": self.snapshot()})

    def snapshot(self) -> dict:
        top = sorted(self.donors.values(), key=lambda d: d["diamonds"], reverse=True)[:10]
        return {
            "recent_events": self.recent_events[-50:],
            "recent_actions": self.recent_actions[-50:],
            "top_donors": top,
            "total_diamonds": self.total_diamonds,
            "total_gifts": self.total_gifts,
            "viewer_count": self.viewer_count,
            "total_likes": self.total_likes,
            "browser_bridge": {"events_received": self.browser_event_count,
                               "last_event_at": self.browser_last_event_at or None},
        }


def _udp_send(payload: dict) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), (UDP_HOST, UDP_PORT))
    except Exception as exc:
        diagnostics.warning("Nie udało się wysłać lokalnej akcji przez UDP bridge: %s", exc)
