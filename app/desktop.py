"""Native GiftStream window with an embedded web UI and local FastAPI backend."""
from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
import webview

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import update_state
from app.main import app
from app.paths import data_dir, installed_release_tag
from app.shortcuts import create_start_menu_shortcut
from app.updater import current_install, prepare_latest
from app.version import RELEASE_TAG

GITHUB_REPOSITORY = os.environ.get("GIFTSTREAM_GITHUB_REPOSITORY", "rejson59/GiftStream")
UPDATE_CHECK_SECONDS = 30 * 60
FIRST_UPDATE_CHECK_SECONDS = 15
IS_WINDOWS = os.name == "nt"
_INSTANCE_MUTEX_HANDLE = None
_INSTANCE_LOCK_FILE = None


def _acquire_single_instance() -> bool:
    """Hold a process-wide lock so repeated EXE clicks cannot open more windows."""
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_LOCK_FILE
    if IS_WINDOWS:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, "Local\\GiftStream.SingleInstance")
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX_HANDLE = handle
        return True

    # Use a per-user advisory lock in the system temp directory on Unix; keep
    # runtime lock files out of the checkout and release automatically on exit.
    import fcntl
    import tempfile

    lock_path = Path(tempfile.gettempdir()) / f"giftstream-{os.getuid()}-instance.lock"
    lock_file = lock_path.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return False
    _INSTANCE_LOCK_FILE = lock_file
    return True


def _release_single_instance() -> None:
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_LOCK_FILE
    if _INSTANCE_MUTEX_HANDLE is not None:
        import ctypes
        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_bool
        close_handle(_INSTANCE_MUTEX_HANDLE)
        _INSTANCE_MUTEX_HANDLE = None
    if _INSTANCE_LOCK_FILE is not None:
        import fcntl
        fcntl.flock(_INSTANCE_LOCK_FILE.fileno(), fcntl.LOCK_UN)
        _INSTANCE_LOCK_FILE.close()
        _INSTANCE_LOCK_FILE = None


def _focus_existing_window() -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        find_window = user32.FindWindowW
        find_window.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
        find_window.restype = ctypes.c_void_p
        window = find_window(None, "GiftStream")
        if window:
            user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
            user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
            user32.ShowWindow(window, 9)  # SW_RESTORE
            user32.SetForegroundWindow(window)
    except Exception:
        pass


def _publish_downloaded_update(tag: str, executable: Path) -> None:
    """Publish a verified update and automatically schedule its Windows restart."""
    update_state.publish_update(tag, executable)
    if IS_WINDOWS:
        update_state.request_apply()


class GiftStreamDesktop:
    def __init__(self) -> None:
        self.port = self._choose_port(8000)
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
            use_colors=False,
        ))
        self.server_thread = threading.Thread(target=self._serve, name="GiftStream API", daemon=True)
        self.update_thread = threading.Thread(target=self._update_loop, name="GiftStream updater", daemon=True)
        self.stop_event = threading.Event()
        self.window = None
        self.current_tag = installed_release_tag()

    @staticmethod
    def _choose_port(preferred: int) -> int:
        for port in [preferred, *range(preferred + 1, preferred + 21)]:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _serve(self) -> None:
        try:
            asyncio.run(self.server.serve())
        except Exception:
            self.server.should_exit = True

    def _wait_for_server(self) -> bool:
        self.server_thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if self.server.started:
                return True
            if not self.server_thread.is_alive():
                return False
            self.stop_event.wait(0.1)
        return bool(self.server.started)

    def _update_loop(self) -> None:
        next_check = time.monotonic() + FIRST_UPDATE_CHECK_SECONDS
        while not self.stop_event.is_set():
            manual_check = update_state.take_check_request()
            if manual_check or time.monotonic() >= next_check:
                update_state.begin_check()
                message = "Nie udało się sprawdzić aktualizacji. Spróbuj ponownie później."
                try:
                    tag, executable, updated, message = prepare_latest(
                        GITHUB_REPOSITORY, data_dir(), on_progress=update_state.report_progress
                    )
                    if updated and tag != self.current_tag:
                        create_start_menu_shortcut(executable)
                        _publish_downloaded_update(tag, executable)
                        # Updates are verified before installation; automatically hand over to the new build.
                        if self._apply_update(executable):
                            return
                except Exception as exc:
                    # Network trouble must never interrupt the running stream or UI.
                    message = f"Nie udało się sprawdzić aktualizacji: {exc}"
                finally:
                    update_state.finish_check(message)
                next_check = time.monotonic() + UPDATE_CHECK_SECONDS

            pending = update_state.take_apply_request()
            if pending is not None and self._apply_update(pending):
                return
            remaining = max(0.05, next_check - time.monotonic())
            self.stop_event.wait(min(0.25, remaining))

    def _apply_update(self, executable: Path) -> bool:
        if os.name != "nt":
            return False
        released_instance_lock = False
        try:
            create_start_menu_shortcut(executable)
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            # The replacement process must acquire the mutex before it can show UI.
            _release_single_instance()
            released_instance_lock = True
            subprocess.Popen([str(executable)], cwd=str(data_dir()), creationflags=flags, close_fds=True)
            self.stop()
            return True
        except OSError:
            # Keep the downloaded update available so it can be retried at next launch.
            if released_instance_lock:
                _acquire_single_instance()
            return False

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.server.should_exit = True
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    def run(self) -> None:
        data_dir()
        if not self._wait_for_server():
            raise RuntimeError("Nie udało się uruchomić lokalnego serwera GiftStream.")

        self.window = webview.create_window(
            "GiftStream",
            f"{self.base_url}/?desktop=1",
            width=1440,
            height=920,
            min_size=(1024, 700),
            background_color="#0b0b0d",
            text_select=True,
        )
        self.window.events.closed += self.stop
        # Shortcut creation invokes PowerShell/COM on Windows; never make the UI wait on it.
        threading.Thread(
            target=create_start_menu_shortcut,
            args=(Path(sys.executable).resolve(),),
            name="GiftStream shortcut",
            daemon=True,
        ).start()
        self.update_thread.start()
        try:
            # Windows uses the native Edge WebView2 control; no external browser tab is opened.
            webview.start(gui="edgechromium", debug=False, http_server=False)
        except Exception as exc:
            if os.name == "nt":
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        0,
                        f"Nie udało się uruchomić okna GiftStream. Sprawdź, czy Microsoft Edge WebView2 jest dostępny w systemie.\n\n{exc}",
                        "GiftStream",
                        0x10,
                    )
                except Exception:
                    pass
            raise
        finally:
            self.stop()
            self.server_thread.join(timeout=5)
            update_state.clear()


def _start_installed_version_if_needed() -> bool:
    """Do only local installation work before opening the UI; network checks run later."""
    if not getattr(sys, "frozen", False) or not IS_WINDOWS:
        return False

    running_executable = Path(sys.executable).resolve()
    root = data_dir()
    cached = current_install(root)
    cached_version = tuple(int(part) for part in re.findall(r"\d+", cached[0])[:3]) if cached else ()
    running_version = tuple(int(part) for part in re.findall(r"\d+", RELEASE_TAG)[:3])
    if cached and cached_version > running_version:
        # A previously downloaded update is already verified; prefer it without waiting on internet.
        latest_executable = cached[1].resolve()
        if latest_executable != running_executable:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                subprocess.Popen([str(latest_executable)], cwd=str(root), creationflags=flags, close_fds=True)
                return True
            except OSError:
                return False
        return False

    # Keep launch fast: no synchronous PowerShell, network check, or executable copy here.
    # The native window is shown first; shortcut creation runs asynchronously from run().
    return False


def main() -> None:
    if _start_installed_version_if_needed():
        return
    if not _acquire_single_instance():
        _focus_existing_window()
        return
    try:
        GiftStreamDesktop().run()
    finally:
        _release_single_instance()


if __name__ == "__main__":
    main()
