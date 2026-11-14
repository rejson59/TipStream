"""Thread-safe update status shared by the desktop host and the local web UI."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
_apply_requested = threading.Event()
_check_requested = threading.Event()
_pending_executable: Path | None = None
_state: dict[str, str | bool | int] = {
    "available": False,
    "tag": "",
    "message": "",
    "checking": False,
    "last_checked": "",
    "last_check_message": "",
    "check_id": 0,
    "progress": 0,
}


def publish_update(tag: str, executable: Path) -> None:
    global _pending_executable
    with _lock:
        _pending_executable = executable
        _state.update({
            "available": True,
            "tag": tag,
            "message": f"GiftStream {tag} pobrano. Aplikacja uruchomi się ponownie automatycznie.",
        })


def status() -> dict[str, str | bool | int]:
    with _lock:
        return dict(_state)


def request_check() -> bool:
    """Queue an immediate update check unless one is already running."""
    with _lock:
        if _state["checking"]:
            return False
        _state["checking"] = True
        _state["progress"] = 0
        _state["check_id"] = int(_state["check_id"]) + 1
        _state["last_check_message"] = "Sprawdzam dostępność aktualizacji…"
        _check_requested.set()
        return True


def take_check_request() -> bool:
    if not _check_requested.is_set():
        return False
    _check_requested.clear()
    return True


def begin_check() -> None:
    with _lock:
        if not _state["checking"]:
            _state["check_id"] = int(_state["check_id"]) + 1
        _state["checking"] = True
        _state["progress"] = 0
        _state["last_check_message"] = "Sprawdzam dostępność aktualizacji…"


def report_progress(progress: int, message: str) -> None:
    with _lock:
        _state["progress"] = max(0, min(100, int(progress)))
        _state["last_check_message"] = message[:180]


def finish_check(message: str) -> None:
    checked_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with _lock:
        _state.update({
            "checking": False,
            "last_checked": checked_at,
            "last_check_message": message,
        })


def request_apply() -> bool:
    with _lock:
        if not _state["available"] or _pending_executable is None:
            return False
        _apply_requested.set()
        return True


def take_apply_request() -> Path | None:
    if not _apply_requested.is_set():
        return None
    _apply_requested.clear()
    with _lock:
        return _pending_executable


def clear() -> None:
    global _pending_executable
    with _lock:
        _pending_executable = None
        _state.update({"available": False, "tag": "", "message": "", "checking": False,
                       "last_checked": "", "last_check_message": "", "check_id": 0, "progress": 0})
        _apply_requested.clear()
        _check_requested.clear()
