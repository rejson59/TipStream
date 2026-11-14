import hashlib
import json

import pytest

from app import updater
from app.updater import (
    DOWNLOAD_CHUNK_BYTES,
    UpdateError,
    _install_executable,
    download_executable,
    cache_running_executable,
    current_install,
    expected_sha256,
    prepare_latest,
    release_api_url,
    valid_release_tag,
)


def test_release_tag_and_repository_are_validated():
    assert valid_release_tag("v1.2.3")
    assert valid_release_tag("1.2.3-rc.1")
    assert not valid_release_tag("../../evil")
    with pytest.raises(UpdateError):
        release_api_url("bad/repo/path")


def test_sha256_manifest_requires_exact_executable_name():
    digest = hashlib.sha256(b"exe").hexdigest()
    manifest = f"{digest}  GiftStream.exe\n"
    assert expected_sha256(manifest) == digest
    with pytest.raises(UpdateError):
        expected_sha256(f"{digest}  another.exe")


def test_update_installs_only_the_expected_windows_executable(tmp_path):
    payload = b"MZ" + b"x" * 4096
    installed = _install_executable(payload, "v1.2.3", tmp_path)
    assert installed.name == "GiftStream.exe"
    assert installed.read_bytes() == payload
    assert current_install(tmp_path) == ("v1.2.3", installed)
    assert json.loads((tmp_path / "current.json").read_text())["tag"] == "v1.2.3"


def test_update_rejects_bad_payload_and_untrusted_tag(tmp_path):
    with pytest.raises(UpdateError):
        _install_executable(b"not a windows exe", "v1.2.3", tmp_path)
    with pytest.raises(UpdateError):
        _install_executable(b"MZ" + b"x" * 4096, "../../escape", tmp_path)


def test_running_executable_is_cached_as_offline_fallback(tmp_path):
    running = tmp_path / "download" / "GiftStream.exe"
    running.parent.mkdir()
    running.write_bytes(b"MZ" + b"x" * 4096)
    cached = cache_running_executable(running, "v1.0.0", tmp_path / "user-data")
    assert cached.read_bytes() == running.read_bytes()
    assert current_install(tmp_path / "user-data") == ("v1.0.0", cached)


def test_prepare_latest_downloads_only_after_checksum_verification(tmp_path, monkeypatch):
    payload = b"MZ" + b"x" * 4096
    manifest = f"{hashlib.sha256(payload).hexdigest()}  GiftStream.exe\n".encode()
    monkeypatch.setattr(updater, "fetch_latest_release", lambda _repo: {
        "tag": "v2.0.0", "executable_url": "https://github.com/example/GiftStream.exe",
        "checksum_url": "https://github.com/example/checksums",
    })
    monkeypatch.setattr(updater, "_open", lambda url, timeout=15: manifest)
    monkeypatch.setattr(updater, "download_executable", lambda url, on_progress=None: payload)
    tag, exe, updated, _message = prepare_latest("owner/repo", tmp_path)
    assert (tag, updated, exe.read_bytes()) == ("v2.0.0", True, payload)


def test_prepare_latest_falls_back_to_last_good_install_when_offline(tmp_path, monkeypatch):
    payload = b"MZ" + b"x" * 4096
    installed = _install_executable(payload, "v1.0.0", tmp_path)
    monkeypatch.setattr(updater, "fetch_latest_release", lambda _repo: (_ for _ in ()).throw(UpdateError("offline")))
    tag, exe, updated, _message = prepare_latest("owner/repo", tmp_path)
    assert (tag, exe, updated) == ("v1.0.0", installed, False)


def test_first_launch_uses_the_current_executable_if_offline(tmp_path, monkeypatch):
    running = tmp_path / "GiftStream.exe"
    running.write_bytes(b"MZ" + b"x" * 4096)
    monkeypatch.setattr(updater, "fetch_latest_release", lambda _repo: (_ for _ in ()).throw(UpdateError("offline")))
    tag, executable, updated, _message = prepare_latest(
        "owner/repo", tmp_path / "user-data", running_executable=running, running_tag="v1.0.0"
    )
    assert tag == "v1.0.0"
    assert executable.read_bytes() == running.read_bytes()
    assert updated is False


def test_starting_the_current_release_does_not_download_it_again(tmp_path, monkeypatch):
    payload = b"MZ" + b"x" * 4096
    installed = _install_executable(payload, "v2.0.0", tmp_path)
    monkeypatch.setattr(updater, "fetch_latest_release", lambda _repo: {"tag": "v2.0.0"})
    monkeypatch.setattr(updater, "_open", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected download")))
    tag, executable, updated, message = prepare_latest("owner/repo", tmp_path)
    assert (tag, executable, updated) == ("v2.0.0", installed, False)
    assert "najnowszą wersję" in message


def test_executable_download_uses_verified_parallel_http_ranges(monkeypatch):
    payload = b"MZ" + b"x" * (DOWNLOAD_CHUNK_BYTES + 123)
    requests = []

    class Response:
        def __init__(self, status, body, content_range=""):
            self.status = status
            self.headers = {"Content-Range": content_range}
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def getcode(self):
            return self.status

        def read(self, size=-1):
            return self._body if size < 0 else self._body[:size]

    def fake_urlopen(request, timeout=0):
        value = request.headers.get("Range") or request.get_header("Range")
        assert value
        requests.append(value)
        if value == "bytes=0-0":
            return Response(206, payload[:1], f"bytes 0-0/{len(payload)}")
        start, end = (int(part) for part in value.removeprefix("bytes=").split("-"))
        return Response(206, payload[start:end + 1], f"bytes {start}-{end}/{len(payload)}")

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
    progress = []
    result = download_executable("https://github.com/example/GiftStream.exe", lambda value, _msg: progress.append(value))
    assert result == payload
    assert requests[0] == "bytes=0-0"
    assert len(requests) == 3
    assert progress[-1] == 100


def test_executable_download_falls_back_when_server_ignores_range(monkeypatch):
    payload = b"MZ" + b"x" * 4096

    class Response:
        status = 200
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def getcode(self):
            return self.status

        def read(self, size=-1):
            return payload if size < 0 else payload[:size]

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert download_executable("https://github.com/example/GiftStream.exe") == payload
