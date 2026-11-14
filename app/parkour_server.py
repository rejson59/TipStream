"""Local 100 Levels Minecraft server bootstrap and loopback RCON control."""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time
import tempfile
import threading
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .paths import bundle_root, data_dir
from . import diagnostics

PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
MAP_FILENAME = "100-levels-5229-v1.7.4.zip"
MAP_VERSION = "26.2"  # Version the bundled world was prepared and tested against.
VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
SETTINGS_PATH = data_dir() / "minecraft_parkour.json"
MAX_SERVER_JAR_BYTES = 300 * 1024 * 1024
_VERSION_CACHE: tuple[float, dict[str, Any]] | None = None
_VERSION_CACHE_TTL = 600
_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_WINDOWS_PROCESS_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])
_START_LOCK = threading.Lock()
_SERVER_STARTUP_GRACE_SEC = 8.0


def _write_parkour_settings(state: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)


def _map_archive() -> Path:
    for path in (bundle_root() / "assets" / MAP_FILENAME, bundle_root() / MAP_FILENAME):
        if path.is_file():
            return path
    raise FileNotFoundError("Wbudowana mapa 100 Levels nie została znaleziona.")


def map_level_areas(archive: Path | None = None) -> dict[int, tuple[int, int, int, int, int]]:
    """Read level bounds from the bundled map datapack; values are x,z,dx,dz,dy."""
    with zipfile.ZipFile(archive or _map_archive()) as zf:
        text = zf.read("100 Levels/datapacks/main/data/game/function/level/setlevel.mcfunction").decode("utf-8")
    result = {}
    pattern = re.compile(r"@s\[x=(-?\d+),y=0,z=(-?\d+),dx=(\d+),dy=(\d+),dz=(\d+)\].*? level (\d+)$")
    for line in text.splitlines():
        match = pattern.search(line)
        if match:
            x, z, dx, dy, dz, level = map(int, match.groups())
            result[level] = (x, z, dx, dz, dy)
    if len(result) < 90 or not {1, 50, 100}.issubset(result):
        raise ValueError("Nie udało się odczytać zakresów poziomów z mapy parkour.")
    return result



def _windows_java_processes() -> list[dict[str, Any]]:
    """Inspect running Java processes without terminating or modifying them."""
    global _WINDOWS_PROCESS_CACHE
    if os.name != "nt":
        return []
    if _WINDOWS_PROCESS_CACHE[0] > time.monotonic():
        return [dict(item) for item in _WINDOWS_PROCESS_CACHE[1]]
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return []
    script = (
        "$items = @(Get-CimInstance Win32_Process -Filter \"Name='java.exe' OR Name='javaw.exe'\" "
        "| ForEach-Object { [pscustomobject]@{ name=$_.Name; path=$_.ExecutablePath; "
        "command=$_.CommandLine; pid=$_.ProcessId } }); "
        "ConvertTo-Json -InputObject $items -Compress"
    )
    try:
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=3, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        value = json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else []
        processes = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    except (OSError, subprocess.SubprocessError, ValueError):
        processes = []
    _WINDOWS_PROCESS_CACHE = (time.monotonic() + 15, processes)
    return [dict(item) for item in processes]


def _launcher_label(path: str) -> str:
    value = path.casefold()
    for key, label in (("horizon", "Horizon Launcher"), ("prism", "Prism Launcher"),
                       ("multimc", "MultiMC"), ("lunar", "Lunar Client"),
                       ("feather", "Feather Launcher"), ("minecraft", "Minecraft Launcher")):
        if key in value:
            return label
    return "Launcher Minecraft Java"


def _running_minecraft_clients() -> list[dict[str, Any]]:
    results = []
    markers = ("net.minecraft.client.main.main", "net.minecraft.launchwrapper.launch",
               "net.fabricmc.loader.impl.launch.knot.knotclient", "cpw.mods.modlauncher.launcher",
               "--gamedir", "--assetsdir")
    for proc in _windows_java_processes():
        command = str(proc.get("command") or "")
        path = str(proc.get("path") or "")
        if not any(marker in command.casefold() for marker in markers):
            continue
        version_match = re.search(r"(?:--version[ =]|-version[ =])([0-9][0-9A-Za-z.+_-]*)", command)
        results.append({"launcher": _launcher_label(path + " " + command),
                        "version": version_match.group(1) if version_match else None,
                        "pid": int(proc.get("pid") or 0)})
    return results


def _java_candidates():
    """Yield likely Java installations in priority order without crawling every root first."""
    seen: set[Path] = set()
    executable_name = "java.exe" if os.name == "nt" else "java"

    def normalize(value: str | Path | None) -> Path | None:
        if not value:
            return None
        path = Path(value).expanduser()
        if path.name.casefold() == "javaw.exe":
            path = path.with_name("java.exe")
        if path.is_dir():
            path = path / "bin" / executable_name
        try:
            path = path.resolve()
        except OSError:
            return None
        return path if path.is_file() and path not in seen else None

    def emit(value: str | Path | None):
        path = normalize(value)
        if path is not None:
            seen.add(path)
            return path
        return None

    quick = emit(shutil.which("java"))
    if quick:
        yield quick
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = emit(Path(java_home) / "bin" / executable_name)
        if candidate:
            yield candidate

    # A running launcher often exposes its exact, already-compatible Java path.
    if os.name == "nt":
        for proc in _windows_java_processes():
            candidate = emit(proc.get("path"))
            if candidate:
                yield candidate

        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        program_files = [Path(os.environ[name]) for name in ("ProgramFiles", "ProgramW6432") if os.environ.get(name)]
        roots = [appdata / ".minecraft" / "runtime", appdata / "Horizon", appdata / "HorizonLauncher",
                 appdata / "PrismLauncher", appdata / "MultiMC", local / "Horizon", local / "HorizonLauncher",
                 local / "PrismLauncher", local / "Programs" / "Horizon Launcher"]
        roots.extend(root / sub for root in program_files for sub in ("Eclipse Adoptium", "Java", "Microsoft"))
        checked = 0
        for root in roots:
            if not root.is_dir():
                continue
            try:
                # rglob is lazy; the first usable runtime returns immediately rather
                # than testing up to 100 executables with a fresh `java -version`.
                for path in root.rglob(executable_name):
                    candidate = emit(path)
                    if candidate:
                        yield candidate
                        checked += 1
                        if checked >= 100:
                            return
            except OSError:
                continue


def _java_major(executable: Path) -> int | None:
    try:
        result = subprocess.run([str(executable), "-version"], capture_output=True,
                                text=True, timeout=6, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = (result.stderr or "") + (result.stdout or "")
        match = re.search(r'version\s+"(?:1\.)?(\d+)', output)
        return int(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def find_java(required_major: int) -> tuple[Path, int] | None:
    for executable in _java_candidates():
        major = _java_major(executable)
        if major is not None and major >= required_major:
            return executable, major
    return None


def _safe_extract_java(payload: bytes, destination: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if len(infos) > 30000 or sum(item.file_size for item in infos) > 800 * 1024 * 1024:
            raise RuntimeError("Archiwum Java przekracza dozwolony rozmiar.")
        root = destination.resolve()
        for item in infos:
            member = PurePosixPath(item.filename.replace("\\", "/"))
            if member.is_absolute() or ".." in member.parts or not member.parts:
                raise RuntimeError("Archiwum Java zawiera niebezpieczną ścieżkę.")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Archiwum Java zawiera niedozwolony link symboliczny.")
            target = (destination / Path(*member.parts)).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError("Archiwum Java wychodzi poza katalog instalacji.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
        java_files = list(destination.rglob("java.exe"))
        if not java_files:
            raise RuntimeError("Pobrany pakiet Java nie zawiera java.exe.")
        return java_files[0]


def _install_java(required_major: int) -> Path:
    """Install an official Adoptium JRE per-user, with no admin rights required."""
    if os.name != "nt":
        raise RuntimeError(f"Wymagana jest Java {required_major}+. Zainstaluj ją i uruchom GiftStream ponownie.")
    architecture = platform.machine().casefold()
    arch = "aarch64" if architecture in {"arm64", "aarch64"} else "x64"
    repository = f"adoptium/temurin{required_major}-binaries"
    api_url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = urllib.request.Request(api_url, headers={"User-Agent": "GiftStream/1", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            release_data = response.read(12 * 1024 * 1024 + 1)
    except Exception as exc:
        raise RuntimeError(f"Nie udało się pobrać instalatora Java {required_major} z Adoptium: {exc}") from exc
    if len(release_data) > 12 * 1024 * 1024:
        raise RuntimeError("Odpowiedź katalogu Java przekracza dozwolony rozmiar.")
    release = json.loads(release_data.decode("utf-8"))
    assets = release.get("assets", [])
    wanted = re.compile(rf"^OpenJDK{required_major}U-(?:jre|jdk)_{arch}_windows_hotspot_.*\.zip$", re.IGNORECASE)
    asset = next((item for item in assets if wanted.fullmatch(str(item.get("name", "")))
                  and "jre_" in str(item.get("name", "")).casefold()), None)
    if not asset:
        asset = next((item for item in assets if wanted.fullmatch(str(item.get("name", "")))), None)
    if not asset:
        raise RuntimeError(f"Nie znaleziono oficjalnego pakietu Java {required_major} dla Windows/{arch}.")
    url = str(asset.get("browser_download_url", ""))
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path.startswith(f"/{repository}/releases/download/"):
        raise RuntimeError("Adoptium zwrócił nieprawidłowy adres instalatora Java.")
    request = urllib.request.Request(url, headers={"User-Agent": "GiftStream/1"})
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = response.read(400 * 1024 * 1024 + 1)
    if len(payload) > 400 * 1024 * 1024:
        raise RuntimeError("Instalator Java przekracza dozwolony rozmiar.")
    expected_digest = str(asset.get("digest", ""))
    if expected_digest.startswith("sha256:"):
        expected_digest = expected_digest.split(":", 1)[1]
    if not expected_digest:
        checksum_asset = next((item for item in assets if str(item.get("name", "")) in
                               {str(asset.get("name", "")) + ".sha256.txt", str(asset.get("name", "")) + ".sha256"}), None)
        if checksum_asset:
            checksum_url = str(checksum_asset.get("browser_download_url", ""))
            checksum_parsed = urllib.parse.urlparse(checksum_url)
            if checksum_parsed.scheme == "https" and checksum_parsed.hostname == "github.com" and checksum_parsed.path.startswith(f"/{repository}/releases/download/"):
                checksum_request = urllib.request.Request(checksum_url, headers={"User-Agent": "GiftStream/1"})
                with urllib.request.urlopen(checksum_request, timeout=20) as response:
                    checksum_text = response.read(4096).decode("ascii", errors="replace")
                match = re.search(r"\b([a-fA-F0-9]{64})\b", checksum_text)
                expected_digest = match.group(1) if match else ""
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_digest):
        raise RuntimeError("Brak poprawnej sumy SHA-256 dla instalatora Java.")
    if hashlib.sha256(payload).hexdigest().casefold() != expected_digest.casefold():
        raise RuntimeError("Suma SHA-256 pakietu Java jest nieprawidłowa.")

    install_root = data_dir() / "runtime" / f"temurin-{required_major}"
    install_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="java-install-", dir=install_root.parent) as temporary:
        staging = Path(temporary) / "extract"
        staging.mkdir()
        extracted_java = _safe_extract_java(payload, staging)
        java_home = extracted_java.parent.parent
        if install_root.exists():
            shutil.rmtree(install_root)
        shutil.copytree(java_home, install_root)
    executable = install_root / "bin" / "java.exe"
    if not executable.is_file() or (_java_major(executable) or 0) < required_major:
        shutil.rmtree(install_root, ignore_errors=True)
        raise RuntimeError("Automatycznie zainstalowana Java nie spełnia wymagań Minecraft.")
    return executable


def ensure_java(required_major: int) -> tuple[Path, int]:
    found = find_java(required_major)
    if found:
        diagnostics.info("Znaleziono kompatybilną Java %s: %s.", found[1], found[0])
        return found
    diagnostics.info("Nie znaleziono Java %s+; rozpoczynam instalację runtime Adoptium.", required_major)
    executable = _install_java(required_major)
    actual_major = _java_major(executable) or 0
    if actual_major < required_major:
        raise RuntimeError(f"Zainstalowana Java {actual_major} jest za stara; wymagane Java {required_major}+.")
    diagnostics.info("Zainstalowano Java %s: %s.", actual_major, executable)
    return executable, actual_major

def _version_manifest(force: bool = False) -> dict[str, Any]:
    """Fetch/cache Mojang's authoritative release manifest (stable releases only)."""
    global _VERSION_CACHE
    now = time.monotonic()
    if not force and _VERSION_CACHE and _VERSION_CACHE[0] > now:
        return _VERSION_CACHE[1]
    request = urllib.request.Request(VERSION_MANIFEST_URL, headers={"User-Agent": "GiftStream/1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeError("Manifest wersji Minecraft przekracza dozwolony rozmiar.")
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("versions"), list):
        raise RuntimeError("Mojang zwrócił nieprawidłowy manifest wersji Minecraft.")
    _VERSION_CACHE = (now + _VERSION_CACHE_TTL, manifest)
    return manifest


def minecraft_versions() -> dict[str, Any]:
    manifest = _version_manifest()
    releases = [
        {"id": str(entry.get("id", "")), "release_time": str(entry.get("releaseTime", ""))}
        for entry in manifest["versions"]
        if isinstance(entry, dict) and entry.get("type") == "release"
        and _VERSION_RE.fullmatch(str(entry.get("id", "")))
    ]
    latest = str(manifest.get("latest", {}).get("release", ""))
    if not any(item["id"] == latest for item in releases):
        raise RuntimeError("Nie udało się ustalić najnowszej stabilnej wersji Minecraft.")
    # A manageable selector: newest 40 releases, always including the bundled-map version.
    choices = releases[:40]
    if not any(item["id"] == MAP_VERSION for item in choices):
        bundled = next((item for item in releases if item["id"] == MAP_VERSION), None)
        if bundled:
            choices.append(bundled)
    return {"latest": latest, "versions": choices, "map_version": MAP_VERSION}


def _resolve_release(requested_version: str = "latest") -> tuple[str, dict[str, Any]]:
    # Revalidate the moving "latest" alias at setup time; a chosen numbered release
    # can use the short-lived manifest cache populated by the version selector.
    manifest = _version_manifest(force=requested_version == "latest")
    selected = str(manifest.get("latest", {}).get("release", "")) if requested_version == "latest" else requested_version
    if not _VERSION_RE.fullmatch(selected):
        raise ValueError("Wersja Minecraft musi być stabilnym wydaniem, np. 1.21.11.")
    version = next((entry for entry in manifest["versions"]
                    if isinstance(entry, dict) and entry.get("id") == selected and entry.get("type") == "release"), None)
    if not version:
        raise ValueError(f"Wersja Minecraft {selected} nie jest dostępna w oficjalnym katalogu wydań.")
    return selected, version


def _release_details(version: dict[str, Any]) -> dict[str, Any]:
    version_url = str(version.get("url", ""))
    version_host = urllib.parse.urlparse(version_url).hostname or ""
    if not version_url.startswith("https://") or not (version_host == "piston-meta.mojang.com" or version_host.endswith(".piston-meta.mojang.com")):
        raise RuntimeError("Manifest Minecraft zwrócił nieprawidłowy adres wydania.")
    with urllib.request.urlopen(version_url, timeout=15) as response:
        info = json.loads(response.read().decode("utf-8"))
    if not isinstance(info, dict):
        raise RuntimeError("Mojang zwrócił nieprawidłowe szczegóły wydania Minecraft.")
    return info


def _download_server_jar(server_dir: Path, requested_version: str = "latest") -> tuple[Path, int, str, Path]:
    """Download a selected official release server jar and verify Mojang's SHA-1."""
    selected, version = _resolve_release(requested_version)
    diagnostics.info("Manifest Mojang wybrał stabilne Minecraft %s; pobieram szczegóły wydania.", selected)
    version_info = _release_details(version)
    required_java = int(version_info.get("javaVersion", {}).get("majorVersion", 0))
    if required_java < 1:
        raise RuntimeError(f"Nie udało się odczytać wymagań Java dla Minecraft {selected}.")
    diagnostics.info("Minecraft %s wymaga Java %s+.", selected, required_java)
    java_executable, _installed_java = ensure_java(required_java)
    server = version_info.get("downloads", {}).get("server", {})
    url = str(server.get("url", ""))
    host = urllib.parse.urlparse(url).hostname or ""
    if not url.startswith("https://") or not (host == "piston-data.mojang.com" or host.endswith(".piston-data.mojang.com")):
        raise RuntimeError("Minecraft manifest zwrócił nieprawidłowy adres serwera.")
    request = urllib.request.Request(url, headers={"User-Agent": "GiftStream/1"})
    diagnostics.info("Pobieram plik serwera Minecraft %s z oficjalnego hosta Mojang.", selected)
    with urllib.request.urlopen(request, timeout=90) as response:
        jar = response.read(MAX_SERVER_JAR_BYTES + 1)
    if len(jar) < 1024 or len(jar) > MAX_SERVER_JAR_BYTES:
        raise RuntimeError("Pobrany plik serwera Minecraft ma nieprawidłowy rozmiar.")
    expected_sha1 = str(server.get("sha1", "")).lower()
    if not expected_sha1 or hashlib.sha1(jar).hexdigest() != expected_sha1:
        raise RuntimeError("Suma SHA-1 serwera Minecraft jest nieprawidłowa lub nie zgadza się.")
    destination = server_dir / "server.jar"
    temporary = destination.with_suffix(".jar.new")
    temporary.write_bytes(jar)
    os.replace(temporary, destination)
    diagnostics.info("Serwer Minecraft %s pobrano i zweryfikowano (SHA-1), rozmiar %s bajtów.", selected, len(jar))
    return destination, required_java, selected, java_executable


def _patch_map_datapack(world: Path) -> None:
    """Adapt known 100 Levels resources to current datapack JSON and add a compact HUD."""
    for name in ("disable", "enable"):
        modifier = world / "datapacks/main/data/common/item_modifier/training_mode/glint" / f"{name}.json"
        try:
            value = json.loads(modifier.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Nie udało się odczytać modyfikatora mapy {modifier.name}: {exc}") from exc
        # Minecraft 26.3 expects a typed object resource. The bundled map
        # stores a one-element loot-function array with the old `function` key;
        # simply unwrapping it is insufficient (26.3 reports "No key type").
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            value = value[0]
        if not isinstance(value, dict):
            raise ValueError(f"Nieznany format modyfikatora mapy {modifier.name}; nie można go bezpiecznie migrować.")
        modifier_type = value.pop("function", value.get("type"))
        if modifier_type != "minecraft:set_components":
            raise ValueError(f"Nieznany format modyfikatora mapy {modifier.name}; nie można go bezpiecznie migrować.")
        value["type"] = modifier_type
        modifier.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # The map already renders its timer in the actionbar. Add the player's
    # scoreboard level to those same actionbar messages (rather than replacing
    # the timer or taking over the map's optional sidebar scoreboard).
    timer_display = world / "datapacks/main/data/common/function/timer/display.mcfunction"
    try:
        lines = timer_display.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Brakuje funkcji wyświetlania timera mapy: {exc}") from exc
    updated = []
    hud = [
        {"text": "LEVEL ", "color": "yellow", "bold": True},
        {"score": {"objective": "level", "name": "@s"}, "color": "yellow", "bold": True},
        {"text": " / 100  ·  ", "color": "dark_gray"},
    ]
    for line in lines:
        if " actionbar " not in line:
            updated.append(line)
            continue
        command, payload = line.split(" actionbar ", 1)
        try:
            components = json.loads(payload)
        except ValueError as exc:
            raise ValueError("Nie udało się zaktualizować wyświetlania timera mapy.") from exc
        if not isinstance(components, list):
            raise ValueError("Nieznany format wyświetlania timera mapy.")
        if not any(isinstance(component, dict)
                   and component.get("score", {}).get("objective") == "level"
                   for component in components):
            components = hud + components
        updated.append(command + " actionbar " + json.dumps(components, ensure_ascii=False, separators=(",", ":")))
    if not any(" actionbar " in line for line in updated):
        raise ValueError("Nie znaleziono wyświetlania actionbar timera mapy.")
    timer_display.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _extract_map(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = [item for item in zf.infolist() if not item.is_dir()]
        total = sum(item.file_size for item in infos)
        if total > 100 * 1024 * 1024 or len(infos) > 5000:
            raise ValueError("Archiwum mapy przekracza dozwolony rozmiar.")
        required = {"100 Levels/level.dat", "100 Levels/datapacks/main/pack.mcmeta"}
        if not required.issubset({item.filename for item in infos}):
            raise ValueError("Archiwum nie zawiera poprawnej mapy Minecraft 100 Levels.")
        for item in infos:
            path = PurePosixPath(item.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "100 Levels":
                raise ValueError("Archiwum mapy zawiera niebezpieczną ścieżkę.")
            # Reject Unix symlinks so extraction cannot escape the selected server folder.
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archiwum mapy zawiera niedozwolony link symboliczny.")
            relative = Path(*path.parts[1:])
            target = (destination / relative).resolve()
            if destination.resolve() not in target.parents:
                raise ValueError("Ścieżka pliku mapy wychodzi poza katalog świata.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(item) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _write_server_properties(path: Path, password: str) -> None:
    values = {
        "motd": "GiftStream · Parkour 100 Levels",
        "level-name": "world",
        "server-ip": "127.0.0.1",
        "server-port": "25565",
        "enable-rcon": "true",
        "rcon.port": "25575",
        "rcon.password": password,
        # This server is loopback-only; avoid Mojang session-server throttling for local joins.
        "online-mode": "false",
        "white-list": "false",
        "enforce-whitelist": "false",
        "gamemode": "adventure",
        "difficulty": "normal",
        "max-players": "8",
        "view-distance": "8",
        "simulation-distance": "6",
        "spawn-protection": "0",
        "allow-flight": "true",
        "enable-command-block": "true",
    }
    path.write_text("# GiftStream local parkour server settings\n" + "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")


def _disable_server_whitelist(properties: Path) -> None:
    """Keep the private localhost server joinable without online authentication or a whitelist."""
    try:
        lines = properties.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    overrides = {"online-mode": "false", "white-list": "false", "enforce-whitelist": "false"}
    seen: set[str] = set()
    updated: list[str] = []
    for line in lines:
        key, sep, _value = line.partition("=")
        if sep and key.strip() in overrides:
            if key.strip() not in seen:
                updated.append(f"{key.strip()}=false")
                seen.add(key.strip())
        else:
            updated.append(line)
    for key, value in overrides.items():
        if key not in seen:
            updated.append(f"{key}={value}")
    if updated == lines:
        return
    temporary = properties.with_suffix(properties.suffix + ".tmp")
    temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.replace(temporary, properties)


def _recv_packet(sock: socket.socket) -> tuple[int, int, str]:
    header = _recv_exact(sock, 4)
    length = struct.unpack("<i", header)[0]
    if length < 10 or length > 1_000_000:
        raise ConnectionError("RCON zwrócił nieprawidłowy pakiet")
    payload = _recv_exact(sock, length)
    packet_id, packet_type = struct.unpack("<ii", payload[:8])
    text = payload[8:-2].decode("utf-8", errors="replace")
    return packet_id, packet_type, text


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        part = sock.recv(count - len(chunks))
        if not part:
            raise ConnectionError("Serwer Minecraft zamknął połączenie RCON")
        chunks.extend(part)
    return bytes(chunks)


def _send_packet(sock: socket.socket, packet_id: int, packet_type: int, text: str) -> None:
    body = struct.pack("<ii", packet_id, packet_type) + text.encode("utf-8") + b"\0\0"
    sock.sendall(struct.pack("<i", len(body)) + body)


class ParkourServer:
    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._process_started_at = 0.0
        self._next_rcon_probe_at = 0.0
        self._rcon_retry_delay = 1.0
        self._last_status_ready: bool | None = None
        self._last_logged_exit: int | None = None

    def settings(self) -> dict[str, Any]:
        try:
            value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _server_log_path(self) -> Path | None:
        server_dir = str(self.settings().get("server_dir", ""))
        return Path(server_dir) / "giftstream-server.log" if server_dir else None

    def diagnostic_lines(self, limit: int = 100) -> list[str]:
        """Return a safe, bounded tail of the JVM log for the copyable app diagnostics."""
        log_path = self._server_log_path()
        if log_path is None:
            return []
        try:
            # Server logs may grow for the entire stream; seek from the end instead
            # of loading the whole file whenever the dashboard refreshes diagnostics.
            with log_path.open("rb") as file:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(max(0, size - 256 * 1024), os.SEEK_SET)
                tail = file.read().decode("utf-8", errors="replace")
            lines = tail.splitlines()[-max(1, min(limit, 200)):]
        except OSError:
            return []
        return [f"[MINECRAFT] {diagnostics.redact(line)}" for line in lines]

    def clear_diagnostic_logs(self) -> None:
        log_path = self._server_log_path()
        if log_path is None or not log_path.exists():
            return
        try:
            log_path.write_text("", encoding="utf-8")
        except OSError as exc:
            diagnostics.warning("Nie udało się wyczyścić pliku logu Minecraft %s: %s", log_path, exc)

    def setup(self, server_dir: str, player: str, eula_accepted: bool, version: str = "latest") -> dict[str, Any]:
        if not eula_accepted:
            raise ValueError("Zaznacz akceptację EULA Minecraft przed utworzeniem serwera.")
        if not PLAYER_RE.fullmatch(player or ""):
            raise ValueError("Wpisz nick Minecraft: 3–16 liter, cyfr lub podkreśleń.")
        selected_version, _version_info = _resolve_release(version)
        if server_dir.strip():
            root = Path(server_dir).expanduser().resolve()
        else:
            root = (data_dir() / "MinecraftParkour" / "servers" / selected_version).resolve()
        if root == Path(root.anchor) or root == data_dir().resolve():
            raise ValueError("Wybierz osobny folder na lokalny serwer Minecraft.")
        state = self.settings()
        if self._process is not None and self._process.poll() is None:
            if state.get("server_dir") != str(root) or state.get("minecraft_version") != selected_version:
                raise RuntimeError("Zatrzymaj działający serwer przed przełączeniem wersji Minecraft.")
            return self.status()
        if state and (state.get("server_dir") != str(root) or state.get("minecraft_version") != selected_version):
            try:
                self._rcon("list", log_failure=False)
            except Exception:
                pass
            else:
                raise RuntimeError("Wykryto działający serwer w tle. Zatrzymaj go przed przełączeniem wersji, aby nie utracić kontroli RCON.")
        root.mkdir(parents=True, exist_ok=True)
        if (state.get("server_dir") == str(root) and state.get("minecraft_version") == selected_version
                and (root / "server.jar").is_file() and (root / "world" / "level.dat").is_file()):
            _patch_map_datapack(root / "world")
            self.start()
            return self.status()
        if any(root.iterdir()):
            raise FileExistsError("Folder wybranej wersji nie jest pusty. Dane świata są chronione — wybierz inny folder lub wersję.")

        password = secrets.token_urlsafe(24)
        map_stage = root / ".giftstream-world-stage"
        phase = "sprawdzanie dołączonej mapy"
        diagnostics.info("Rozpoczynam konfigurację Minecraft Parkour %s dla gracza %s w %s.",
                         selected_version, player, root)
        try:
            _map_archive()  # Fail before downloading server files if the bundled world is absent.
            phase = "pobieranie serwera i przygotowanie Java"
            diagnostics.info("Pobieram oficjalny serwer Minecraft %s i sprawdzam wymagania Java.", selected_version)
            jar, required_java, installed_version, java_executable = _download_server_jar(root, selected_version)
            diagnostics.info("Serwer pobrany i zweryfikowany; używam Java %s (%s).", required_java, java_executable)
            phase = "rozpakowywanie mapy"
            map_stage.mkdir()
            _extract_map(_map_archive(), map_stage)
            _patch_map_datapack(map_stage)
            diagnostics.info("Datapack mapy zaktualizowano pod Minecraft 26.3; HUD poziomu zachowuje istniejący timer.")
            staged_level = map_stage / "level.dat"
            if not staged_level.is_file():
                raise FileNotFoundError(f"Po rozpakowaniu mapy brakuje pliku {staged_level}.")
            # _extract_map deliberately strips the archive's top-level '100 Levels/' folder.
            # The staging directory itself is the complete world root.
            os.replace(map_stage, root / "world")
            diagnostics.info("Mapa 100 Levels rozpakowana i zainstalowana jako %s/world.", root)
            phase = "zapisywanie konfiguracji serwera"
            (root / "eula.txt").write_text("# Accepted in GiftStream setup\neula=true\n", encoding="ascii")
            _write_server_properties(root / "server.properties", password)
            state = {"server_dir": str(root), "player": player,
                     "rcon_host": "127.0.0.1", "rcon_port": 25575,
                     "server_address": "localhost:25565", "minecraft_version": installed_version,
                     "map_version": MAP_VERSION, "java_required": required_java,
                     "java_executable": str(java_executable), "server_jar": str(jar)}
            _write_parkour_settings({**state, "rcon_password": password})
            diagnostics.info("Zapisano konfigurację serwera i zgodę EULA w %s.", root)
        except Exception as exc:
            diagnostics.error("Konfiguracja Minecraft Parkour nie powiodła się podczas etapu '%s': %s",
                              phase, exc, exc_info=exc)
            shutil.rmtree(map_stage, ignore_errors=True)
            shutil.rmtree(root / "world", ignore_errors=True)
            for generated in (root / "server.jar", root / "server.jar.new", root / "eula.txt", root / "server.properties"):
                try:
                    generated.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    diagnostics.warning("Nie udało się posprzątać pliku %s: %s", generated, cleanup_error)
            raise
        shutil.rmtree(map_stage, ignore_errors=True)
        diagnostics.info("Konfiguracja zakończona; uruchamiam serwer Minecraft %s.", installed_version)
        self.start()
        return self.status()

    def start(self) -> dict[str, Any]:
        # Serialize startup requests so rapid clicks/API retries cannot spawn two JVMs.
        with _START_LOCK:
            return self._start_once()

    def _start_once(self) -> dict[str, Any]:
        state = self.settings()
        root = Path(str(state.get("server_dir", "")))
        diagnostics.info("Żądanie uruchomienia serwera Minecraft (wersja %s, katalog %s).",
                         state.get("minecraft_version", "nieznana"), root)
        jar = root / "server.jar"
        if not jar.is_file() or not (root / "eula.txt").is_file():
            raise RuntimeError("Najpierw utwórz lokalny serwer parkour.")
        _disable_server_whitelist(root / "server.properties")
        if self._process is not None and self._process.poll() is None:
            return self.status()
        # The server is deliberately detached from the dashboard process. After a
        # GiftStream restart, discover an already-running instance through loopback
        # RCON instead of launching a duplicate on the same port.
        try:
            self._rcon("list", log_failure=False)
            diagnostics.info("Wykryto już działający lokalny serwer Minecraft przez RCON; nie uruchamiam duplikatu.")
            return self.status()
        except Exception:
            pass
        try:
            _patch_map_datapack(root / "world")
        except Exception as exc:
            diagnostics.error("Nie udało się przygotować datapacku 100 Levels przed startem: %s", exc, exc_info=exc)
            raise RuntimeError(f"Nie udało się przygotować mapy 100 Levels: {exc}") from exc
        try:
            required_java = int(state.get("java_required") or 0)
            if required_java < 1:
                legacy_version = str(state.get("minecraft_version") or state.get("map_version") or MAP_VERSION)
                _version_id, version_entry = _resolve_release(legacy_version)
                version_details = _release_details(version_entry)
                required_java = int(version_details.get("javaVersion", {}).get("majorVersion", 0))
                if required_java < 1:
                    raise RuntimeError(f"Nie udało się ustalić wymagań Java dla Minecraft {legacy_version}.")
                state["java_required"] = required_java
            configured_java = Path(str(state.get("java_executable", "")))
            if configured_java.is_file() and (_java_major(configured_java) or 0) >= required_java:
                java = configured_java
            else:
                java, _major = ensure_java(required_java)
                state["java_executable"] = str(java)
                _write_parkour_settings(state)
        except Exception as exc:
            raise RuntimeError(f"Nie udało się przygotować Java dla serwera Minecraft: {exc}") from exc
        log = (root / "giftstream-server.log").open("ab")
        kwargs: dict[str, Any] = {"cwd": str(root), "stdin": subprocess.DEVNULL,
                                  "stdout": log, "stderr": subprocess.STDOUT, "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        else:
            kwargs["start_new_session"] = True
        try:
            self._process = subprocess.Popen([
                java, "-Xms512M", "-Xmx2G", "-XX:+UseG1GC",
                "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=150",
                "-jar", str(jar), "nogui",
            ], **kwargs)
            self._process_started_at = time.monotonic()
            self._next_rcon_probe_at = self._process_started_at + _SERVER_STARTUP_GRACE_SEC
            self._rcon_retry_delay = 1.0
            self._last_logged_exit = None
            self._last_status_ready = None
            diagnostics.info("Proces serwera Minecraft uruchomiono (PID %s); stdout/stderr zapisuję do %s.",
                             self._process.pid, root / "giftstream-server.log")
        except Exception as exc:
            diagnostics.error("Nie udało się uruchomić procesu serwera Minecraft: %s", exc, exc_info=exc)
            raise
        finally:
            log.close()
        return self.status()

    def _rcon(self, command: str, *, log_failure: bool = True) -> str:
        state = self.settings()
        host = str(state.get("rcon_host", "127.0.0.1"))
        port = int(state.get("rcon_port", 25575))
        password = str(state.get("rcon_password", ""))
        if host != "127.0.0.1" or not password or not (1 <= port <= 65535):
            raise RuntimeError("Brak bezpiecznej konfiguracji RCON na localhost.")
        command_kind = command.split(maxsplit=1)[0] if command else "<empty>"
        try:
            with socket.create_connection((host, port), timeout=3) as sock:
                sock.settimeout(3)
                _send_packet(sock, 1, 3, password)
                packet_id, _packet_type, _ = _recv_packet(sock)
                if packet_id == -1:
                    raise PermissionError("Serwer odrzucił hasło RCON.")
                if packet_id != 1:
                    _recv_packet(sock)
                _send_packet(sock, 2, 2, command)
                response_id, _response_type, text = _recv_packet(sock)
                if response_id != 2:
                    raise ConnectionError("Niepoprawna odpowiedź RCON.")
                return text.strip()
        except Exception as exc:
            # Log only the command verb: chat payloads, player data and secrets
            # must not leak into the copyable diagnostics.
            if log_failure:
                diagnostics.warning("Polecenie RCON '%s' nie powiodło się: %s", command_kind, exc)
            raise

    def status(self) -> dict[str, Any]:
        state = self.settings()
        clients = _running_minecraft_clients()
        client_info = {"minecraft_client_running": bool(clients), "minecraft_clients": clients}
        if not state:
            return {"configured": False, "running": False, "ready": False,
                    "server_address": "localhost:25565", "players_online": 0, "players": [],
                    **client_info,
                    "message": "Utwórz lokalny serwer i zaimportuj mapę 100 Levels."}
        _disable_server_whitelist(Path(str(state.get("server_dir", ""))) / "server.properties")
        response = {"configured": True, "running": False, "ready": False,
                    "server_address": state.get("server_address", "localhost:25565"),
                    "server_dir": state.get("server_dir", ""), "player": state.get("player", ""),
                    "minecraft_version": state.get("minecraft_version", MAP_VERSION),
                    "map_version": state.get("map_version", MAP_VERSION),
                    "java_required": state.get("java_required"),
                    "chat_relay_enabled": bool(state.get("chat_relay_enabled", False)),
                    "players_online": 0, "players": [], **client_info,
                    "message": "Serwer startuje — pierwsze uruchomienie może potrwać chwilę."}
        if self._process is not None and self._process.poll() is not None:
            code = int(self._process.returncode or 0)
            response["message"] = f"Serwer zakończył pracę (kod {code}). Zajrzyj do giftstream-server.log."
            if self._last_logged_exit != code:
                diagnostics.error("Proces serwera Minecraft zakończył pracę z kodem %s. Log JVM: %s",
                                  code, Path(str(state.get("server_dir", ""))) / "giftstream-server.log")
                self._last_logged_exit = code
            self._last_status_ready = False
            return response
        now = time.monotonic()
        process_running = self._process is not None and self._process.poll() is None
        if process_running:
            response["running"] = True
        # The vanilla server spends its first seconds loading registries/world data.
        # Do not block setup or hammer an unopened RCON socket during warm-up.
        if process_running and now - self._process_started_at < _SERVER_STARTUP_GRACE_SEC:
            response["message"] = "Minecraft uruchamia świat i rejestry…"
            return response
        if now < self._next_rcon_probe_at:
            response["message"] = "Minecraft uruchamia się — czekam na RCON."
            return response
        try:
            listing = self._rcon("list", log_failure=False)
            if self._last_status_ready is not True:
                # Existing servers can retain whitelist=true from older properties.
                # RCON runs as console, so disable it once they become reachable.
                try:
                    self._rcon("whitelist off", log_failure=False)
                    diagnostics.info("Wyłączono whitelistę serwera Parkour; serwer jest lokalny i nie wymaga listy dostępu.")
                except Exception as exc:
                    diagnostics.warning("Nie udało się wyłączyć whitelisty przez RCON: %s", exc)
            player_match = re.search(r"There are (\d+) of a max of \d+ players online:(.*)", listing, re.IGNORECASE)
            players = [name.strip() for name in (player_match.group(2) if player_match else "").split(",") if name.strip()]
            response.update({"running": True, "ready": True,
                             "players_online": int(player_match.group(1)) if player_match else len(players),
                             "players": players,
                             "message": "Minecraft gotowy · " + (listing or "RCON połączony")})
            self._rcon_retry_delay = 1.0
            self._next_rcon_probe_at = now + 2.0
            if self._last_status_ready is not True:
                diagnostics.info("Serwer Minecraft jest gotowy i odpowiada przez RCON (%s).", state.get("server_address", "localhost:25565"))
            self._last_status_ready = True
        except Exception as exc:
            response["running"] = process_running
            self._next_rcon_probe_at = now + self._rcon_retry_delay
            self._rcon_retry_delay = min(8.0, self._rcon_retry_delay * 1.7)
            if self._last_status_ready is not False:
                diagnostics.warning("Serwer Minecraft nie jest jeszcze gotowy przez RCON: %s", exc)
            self._last_status_ready = False
        return response

    def stop(self) -> dict[str, Any]:
        if not self.settings():
            diagnostics.warning("Poproszono o zatrzymanie Minecraft, ale serwer nie jest skonfigurowany.")
            return self.status()
        if self._process is not None and self._process.poll() is not None:
            code = int(self._process.returncode or 0)
            diagnostics.info("Serwer Minecraft był już zatrzymany (kod procesu %s); pomijam RCON stop.", code)
            self._last_status_ready = False
            return {**self.status(), "message": f"Serwer jest już zatrzymany (kod {code}); RCON nie jest potrzebny."}

        diagnostics.info("Wysyłam bezpieczne polecenie zatrzymania lokalnego serwera Minecraft.")
        try:
            self._rcon("stop")
        except Exception as exc:
            if self._process is not None and self._process.poll() is None:
                diagnostics.error("Nie udało się zatrzymać Minecraft przez RCON: %s", exc, exc_info=exc)
                raise RuntimeError(f"Nie udało się zatrzymać serwera przez RCON: {exc}") from exc
            # After a GiftStream restart there may be no tracked Popen handle. A
            # refused connection plus a non-ready status is an idempotent stop,
            # not an API error that encourages repeated stop/start attempts.
            current = self.status()
            if not current.get("running") and not current.get("ready"):
                diagnostics.info("RCON niedostępny, a serwer nie działa; traktuję zatrzymanie jako zakończone.")
                return {**current, "message": "Serwer jest już zatrzymany lub nie odpowiada przez RCON."}
            diagnostics.error("Nie udało się zatrzymać Minecraft przez RCON: %s", exc, exc_info=exc)
            raise RuntimeError(f"Nie udało się zatrzymać serwera przez RCON: {exc}") from exc
        self._last_status_ready = False
        return {**self.status(), "message": "Wysłano polecenie bezpiecznego zamknięcia serwera."}

    def command(self, command: str) -> str:
        return self._rcon(command)

    def relay_chat(self, message: str) -> str:
        """Relay plain TikTok chat text to Minecraft without interpreting it as a command."""
        if not isinstance(message, str) or not message:
            raise ValueError("Wiadomość czatu jest pusta.")
        if len(message) > 2000:
            raise ValueError("Wiadomość czatu przekracza bezpieczny limit 2000 znaków.")
        # Tellraw's JSON text component safely encodes quotes, slashes and command
        # characters as data. Preserve the message text (including punctuation and
        # Unicode) exactly; never prepend a username or pass it through RCON raw.
        component = json.dumps({"text": message}, ensure_ascii=False, separators=(",", ":"))
        return self._rcon(f"tellraw @a {component}")

    def get_player_level(self, player: str) -> int:
        if not PLAYER_RE.fullmatch(player or ""):
            raise ValueError("Niepoprawny nick gracza.")
        output = self._rcon(f"scoreboard players get {player} level")
        match = re.search(r"has\s+(-?\d+)\s+\[level\]", output) or re.search(r"(-?\d+)", output)
        if not match:
            raise RuntimeError(f"Nie udało się odczytać poziomu mapy: {output or 'gracz może być poza mapą'}")
        return int(match.group(1))

    def execute_action(self, action: str, player: str | None = None) -> str:
        state = self.settings()
        player = player or str(state.get("player", ""))
        diagnostics.info("Wykonuję akcję Minecraft '%s' dla gracza %s.", action, player or "nieustawiony")
        if not PLAYER_RE.fullmatch(player or ""):
            raise ValueError("Najpierw ustaw nick gracza Minecraft.")
        if action == "parkour_push":
            commands = [f"effect give {player} minecraft:slow_falling 3 0 true",
                        f"execute as {player} at {player} run tp @s ^ ^0.6 ^-3"]
            for command in commands:
                self._rcon(command)
            diagnostics.info("Akcja Parkour push zakończona powodzeniem dla gracza %s.", player)
            return "Delikatnie odrzucono gracza o 3 bloki do tyłu."
        if action in {"parkour_level_forward", "parkour_level_back"}:
            areas = map_level_areas()
            current = self.get_player_level(player)
            target = max(1, min(100, current + (1 if action.endswith("forward") else -1)))
            if target not in areas:
                raise RuntimeError(f"Mapa nie ma zakresu dla poziomu {target}.")
            x, z, dx, dz, dy = areas[target]
            # Drop into the region from above its declared vertical bound while
            # slow-falling, allowing the map's own course geometry to catch the player.
            y = dy + 3
            commands = [f"effect give {player} minecraft:slow_falling 30 0 true",
                        f"tp {player} {x + dx / 2:.1f} {y} {z + dz / 2:.1f}"]
            for command in commands:
                self._rcon(command)
            diagnostics.info("Przeniesiono gracza %s na obszar poziomu %s.", player, target)
            return f"Przeniesiono gracza na obszar poziomu {target}."
        effects = {
            "parkour_speed": ([f"effect give {player} minecraft:speed 8 1 true"], "Sprint przez 8 sekund!"),
            "parkour_slow": ([f"effect give {player} minecraft:slowness 7 1 true"], "Spowolnienie przez 7 sekund!"),
            "parkour_blind": ([f"effect give {player} minecraft:darkness 4 0 true"], "Ciemność przez 4 sekundy!"),
            "parkour_jump": ([f"effect give {player} minecraft:jump_boost 4 2 true",
                              f"effect give {player} minecraft:slow_falling 6 0 true"],
                             "Wysoki skok — bezpieczne lądowanie!"),
            "parkour_shield": ([f"effect give {player} minecraft:resistance 6 1 true"],
                               "Tarcza odporności na 6 sekund!"),
            "parkour_cleanse": ([f"effect clear {player} minecraft:slowness",
                                 f"effect clear {player} minecraft:darkness",
                                 f"effect clear {player} minecraft:blindness",
                                 f"effect clear {player} minecraft:nausea",
                                 f"effect clear {player} minecraft:weakness"],
                                "Usunięto negatywne efekty!"),
        }
        if action in effects:
            commands, message = effects[action]
            for command in commands:
                self._rcon(command)
            diagnostics.info("Zastosowano efekt Parkour '%s' dla gracza %s.", action, player)
            return message
        if action == "parkour_jail":
            commands = [f"tp {player} 1001 101 1001",
                        "fill 1000 100 1000 1002 100 1002 minecraft:stone",
                        "fill 1000 101 1000 1002 104 1002 minecraft:glass hollow",
                        f"effect give {player} minecraft:slowness 20 255 true",
                        f"title {player} actionbar {{\"text\":\"WIĘZIENIE · 20 s\",\"color\":\"red\"}}"]
            for command in commands:
                self._rcon(command)
            diagnostics.info("Gracz %s został przeniesiony do parkour-jail na 20 sekund.", player)
            return "Gracz trafił na 20 sekund do lobby-więzienia."
        if action in {"parkour_reset", "parkour_antireset"}:
            raise RuntimeError("Reset z odliczaniem jest koordynowany przez GiftStream.")
        raise ValueError("Nieobsługiwana akcja parkour.")

    def start_reset(self, player: str) -> str:
        if not PLAYER_RE.fullmatch(player or ""):
            raise ValueError("Niepoprawny nick gracza.")
        self._rcon(f"title {player} title {{\"text\":\"RESET ZA 60 SEKUND\",\"color\":\"gold\"}}")
        self._rcon(f"title {player} subtitle {{\"text\":\"Wyślij prezent przypisany do Anty-resetu, aby anulować\",\"color\":\"white\"}}")
        return "Rozpoczęto 60-sekundowe odliczanie resetu."

    def update_reset_countdown(self, player: str, remaining: int) -> None:
        if not PLAYER_RE.fullmatch(player or ""):
            raise ValueError("Niepoprawny nick gracza.")
        remaining = max(0, min(60, int(remaining)))
        self._rcon(f"title {player} actionbar {{\"text\":\"RESET ZA {remaining} s · podaruj Anty-reset, aby anulować\",\"color\":\"gold\"}}")

    def finish_reset(self, player: str) -> str:
        return self._rcon(f"execute as {player} run trigger restart set 1")

    def cancel_reset(self, player: str) -> str:
        if not PLAYER_RE.fullmatch(player or ""):
            raise ValueError("Niepoprawny nick gracza.")
        self._rcon(f"title {player} clear")
        self._rcon(f"title {player} actionbar {{\"text\":\"RESET ANULOWANY\",\"color\":\"green\"}}")
        return "Reset anulowany przez anty-reset."

    def release_jail(self, player: str) -> str:
        if not PLAYER_RE.fullmatch(player or ""):
            raise ValueError("Niepoprawny nick gracza.")
        self._rcon(f"effect clear {player} minecraft:slowness")
        return self._rcon(f"tp {player} -4 89 1")
