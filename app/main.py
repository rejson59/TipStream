"""GiftStream — API i serwer webowy."""
from __future__ import annotations

import asyncio
import json
import ipaddress
import os
import re
import secrets
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import presets as preset_store
from . import earnings as earnings_tracker
from .bridge import Hub
from .engine import Engine
from .gifts import coin_price_for_gift, list_gifts, record_event, refresh_tiktok_catalog
from .games import get_game
from .parkour_server import ParkourServer, minecraft_versions
from .models import AppConfig, GameAction, Preset, TikTokEvent
from .paths import bundle_root, data_dir, installed_release_tag
from .tiktok_connector import TikTokConnector
from .version import VERSION
from . import update_state
from . import diagnostics
from .myinstants import search as search_myinstants

load_dotenv()
ROOT = bundle_root()
DATA = data_dir()
WEB = ROOT / "web"
CONFIG_PATH = DATA / "config.json"
GITHUB_RELEASES_URL = "https://github.com/rejson59/GiftStream/releases"
_DOWNLOAD_INFO_CACHE: dict[str, Any] = {"expires": 0.0, "data": None}

app = FastAPI(title="GiftStream", version=VERSION,
              description="Prezenty z TikTok LIVE sterują grą")


@app.middleware("http")
async def log_http_failures(request: Request, call_next):
    """Capture failed routes without logging query strings, bodies, or credentials."""
    try:
        response = await call_next(request)
    except Exception as exc:
        diagnostics.error("Nieobsłużony wyjątek HTTP %s %s: %s",
                          request.method, request.url.path, exc, exc_info=exc)
        raise
    if response.status_code >= 500:
        diagnostics.error("HTTP %s %s zakończyło się statusem %s.",
                          request.method, request.url.path, response.status_code)
    elif response.status_code >= 400:
        diagnostics.warning("HTTP %s %s zakończyło się statusem %s.",
                            request.method, request.url.path, response.status_code)
    return response


connector: TikTokConnector
config = AppConfig()


class ConnectRequest(BaseModel):
    mode: Literal["tiktok", "managed"] = "tiktok"
    username: str = ""


class EarningsSettingsRequest(BaseModel):
    usd_per_diamond: float = Field(gt=0, le=1)
    usd_to_pln: float | None = Field(default=None, gt=0, le=100)


class ParkourSetupRequest(BaseModel):
    server_dir: str = ""
    player: str
    eula_accepted: bool = False
    version: str = "latest"
    game_mode: Literal["parkour", "dropper"] = "parkour"


class ParkourActionRequest(BaseModel):
    action: str
    player: str | None = None


class ParkourChatRelayRequest(BaseModel):
    enabled: bool = False


class ClientDiagnosticRequest(BaseModel):
    source: str = Field(default="browser", max_length=80)
    message: str = Field(min_length=1, max_length=1500)


class BrowserEventRequest(BaseModel):
    event: TikTokEvent
    trigger_actions: bool = False


async def publish_game_action(action: GameAction) -> None:
    trigger = action.triggered_by
    trigger_hint = f" po zdarzeniu {trigger.type}" if trigger else ""
    diagnostics.info("Dopasowano regułę '%s': akcja '%s'%s.",
                     action.rule_name or "(bez nazwy)", action.action, trigger_hint)
    action_id = await hub.publish_action(action)
    if not action.action.startswith(("parkour_", "dropper_")):
        await hub.update_action(action_id, "sent", "Wysłano do lokalnego mostka; brak potwierdzenia wykonania przez grę.")
        return
    try:
        result = await _run_parkour_action(action)
        if result is None:
            await hub.update_action(
                action_id, "sent",
                "Mostek lokalny otrzymał akcję; lokalne sterowanie Minecraft jest wyłączone, więc wykonanie nie jest potwierdzone.")
        else:
            await hub.update_action(action_id, "succeeded", result)
    except Exception as exc:
        diagnostics.error("Akcja Parkour '%s' z reguły '%s' nie powiodła się: %s",
                          action.action, action.rule_name, exc, exc_info=exc)
        await hub.update_action(action_id, "failed", str(exc))
        await hub.publish_status({"parkour_result": {"ok": False, "message": str(exc)}})


hub = Hub()
parkour_server = ParkourServer()
parkour_reset_task: asyncio.Task | None = None
parkour_reset_player = ""
parkour_jail_tasks: dict[str, asyncio.Task] = {}


async def _run_parkour_action(action: GameAction, *, allow_disabled: bool = False) -> str | None:
    global parkour_reset_task, parkour_reset_player
    settings = parkour_server.settings()
    if not settings.get("enabled") and not allow_disabled:
        diagnostics.warning("Akcja Parkour '%s' pominięta: lokalne sterowanie grą jest wyłączone lub serwer nie jest skonfigurowany.",
                            action.action)
        return
    player = str(action.params.get("player") or settings.get("player") or "")
    if action.action == "parkour_reset":
        if parkour_reset_task and not parkour_reset_task.done():
            parkour_reset_task.cancel()
        await asyncio.to_thread(parkour_server.start_reset, player)
        diagnostics.info("Rozpoczęto reset Parkour z 60-sekundowym odliczaniem dla %s.", player)
        parkour_reset_player = player
        countdown_update_failed = False
        async def countdown() -> None:
            global parkour_reset_task, parkour_reset_player
            nonlocal countdown_update_failed
            try:
                for remaining in range(60, 0, -1):
                    await hub.publish_status({"parkour_reset_countdown": remaining, "parkour_player": player})
                    try:
                        await asyncio.to_thread(parkour_server.update_reset_countdown, player, remaining)
                    except Exception as exc:
                        # Preserve the countdown if the player is temporarily offline,
                        # but capture the first RCON failure in diagnostics.
                        if not countdown_update_failed:
                            diagnostics.warning("Nie udało się zaktualizować odliczania resetu przez RCON: %s", exc)
                            countdown_update_failed = True
                    await asyncio.sleep(1)
                result = await asyncio.to_thread(parkour_server.finish_reset, player)
                diagnostics.info("Odliczanie zakończone; wysłano reset Parkour dla %s.", player)
                await hub.publish_status({"parkour_reset_countdown": 0, "parkour_result": result})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                diagnostics.error("Odliczanie resetu Parkour nie powiodło się: %s", exc, exc_info=exc)
                await hub.publish_status({"parkour_reset_countdown": None, "parkour_result": str(exc)})
            finally:
                parkour_reset_task = None
        parkour_reset_task = asyncio.create_task(countdown())
        return "Rozpoczęto 60-sekundowe odliczanie resetu."
    if action.action == "parkour_antireset":
        if parkour_reset_task and not parkour_reset_task.done():
            parkour_reset_task.cancel()
            parkour_reset_task = None
            result = await asyncio.to_thread(parkour_server.cancel_reset, parkour_reset_player or player)
            diagnostics.info("Anulowano reset Parkour dla %s.", parkour_reset_player or player)
            await hub.publish_status({"parkour_reset_countdown": None, "parkour_result": result})
            return result
        diagnostics.info("Odebrano akcję anty-reset, ale nie trwało żadne odliczanie.")
        return "Nie trwa żadne odliczanie resetu do anulowania."
    if action.action == "parkour_jail":
        result = await asyncio.to_thread(parkour_server.execute_action, action.action, player)
        previous = parkour_jail_tasks.pop(player, None)
        if previous and not previous.done():
            previous.cancel()
        async def release_jail() -> None:
            try:
                await asyncio.sleep(20)
                released = await asyncio.to_thread(parkour_server.release_jail, player)
                diagnostics.info("Zwolniono gracza %s z parkour-jail.", player)
                await hub.publish_status({"parkour_result": released})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                diagnostics.error("Nie udało się zwolnić gracza %s z parkour-jail: %s", player, exc, exc_info=exc)
                await hub.publish_status({"parkour_result": str(exc)})
            finally:
                parkour_jail_tasks.pop(player, None)
        parkour_jail_tasks[player] = asyncio.create_task(release_jail())
        await hub.publish_status({"parkour_result": result, "parkour_jail_seconds": 20})
        return result
    if action.action.startswith(("parkour_", "dropper_")):
        result = await asyncio.to_thread(parkour_server.execute_action, action.action, player)
        await hub.publish_status({"parkour_result": result})
        return result


engine = Engine(on_action=publish_game_action)


async def process_event(ev: TikTokEvent, *, trigger_actions: bool = True) -> None:
    if ev.viewer_count:
        hub.viewer_count = ev.viewer_count
    if ev.type == "gift":
        if ev.diamonds <= 0:
            resolved, from_catalog = earnings_tracker.resolve_diamonds(ev.gift_id, ev.gift_name, ev.diamonds)
            if resolved > 0:
                ev.diamonds = resolved
                ev.diamonds_estimated = from_catalog
        if ev.gift_coins <= 0 or ev.gift_coins_estimated:
            coins, estimated = coin_price_for_gift(ev.gift_id, ev.gift_name)
            if coins > 0:
                ev.gift_coins = coins
                ev.gift_coins_estimated = estimated
                ev.gift_coins_source = "catalog"
    now = time.monotonic()
    gift_label = (ev.gift_name or "")[:100] if ev.type == "gift" else ""
    event_key = (ev.source[:40], ev.type, gift_label)
    if now - _event_diag_times.get(event_key, 0.0) >= 15:
        _event_diag_times[event_key] = now
        if len(_event_diag_times) > 1000:
            _event_diag_times.pop(next(iter(_event_diag_times)))
        diagnostics.info("Odebrano zdarzenie: source=%s type=%s%s%s.",
                         ev.source[:40], ev.type,
                         f" gift={gift_label!r}" if gift_label else "",
                         f" count={ev.repeat_count}" if ev.type == "gift" else "")
    try:
        record_event(ev)
        await hub.publish_event(ev)
        if (ev.type == "comment" and ev.source == "tiktok" and ev.comment
                and parkour_server.settings().get("chat_relay_enabled")):
            try:
                await asyncio.to_thread(parkour_server.relay_chat, ev.comment)
                diagnostics.info("Komentarz LIVE przekazano do czatu Minecraft (treść pominięta w logu).")
            except Exception as exc:
                diagnostics.error("Nie udało się przekazać komentarza LIVE do Minecraft: %s", exc, exc_info=exc)
                await hub.publish_status({"parkour_chat_relay_error": str(exc)})
        if trigger_actions:
            await engine.handle(ev)
    except Exception as exc:
        diagnostics.error("Błąd przetwarzania zdarzenia source=%s type=%s: %s",
                          ev.source, ev.type, exc, exc_info=exc)
        raise


connector = TikTokConnector(process_event)
_catalog_refresh_task: asyncio.Task | None = None
_catalog_refresh_started = False
_gift_refresh_loop_task: asyncio.Task | None = None
_currency_refresh_task: asyncio.Task | None = None
_event_diag_times: dict[tuple[str, str, str], float] = {}


async def _currency_refresh_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(earnings_tracker.refresh_usd_to_pln)
        except Exception as exc:
            # Keep the last successful NBP rate and never delay application startup.
            diagnostics.warning("Nie udało się odświeżyć kursu NBP; pozostawiam ostatni zapisany kurs: %s", exc)
        await asyncio.sleep(12 * 60 * 60)


async def _gift_catalog_refresh_loop() -> None:
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            await asyncio.to_thread(_refresh_catalog_safely)
        except Exception as exc:
            # Retain the last cached gift catalog if TikTok or the network is unavailable.
            diagnostics.warning("Nie udało się odświeżyć katalogu giftów; zachowuję cache: %s", exc)


def read_config() -> AppConfig:
    if CONFIG_PATH.exists():
        try:
            result = AppConfig(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            diagnostics.info("Wczytano konfigurację GiftStream z %s.", CONFIG_PATH)
            return result
        except Exception as exc:
            diagnostics.error("Nie udało się wczytać konfiguracji %s: %s; używam wartości domyślnych.",
                              CONFIG_PATH, exc, exc_info=exc)
    return AppConfig()


def save_config() -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(config.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        diagnostics.info("Zapisano konfigurację GiftStream.")
    except Exception as exc:
        diagnostics.error("Nie udało się zapisać konfiguracji GiftStream: %s", exc, exc_info=exc)
        raise


def public_config() -> dict:
    data = config.model_dump()
    data.pop("bridge_webhook_secret", None)
    data["bridge_has_secret"] = bool(config.bridge_webhook_secret or os.getenv("GIFTSTREAM_BRIDGE_WEBHOOK_SECRET", ""))
    return data


def _refresh_catalog_safely() -> int:
    try:
        count = refresh_tiktok_catalog()
        diagnostics.info("Katalog giftów TikTok odświeżony; dostępnych wpisów: %s.", count)
        return count
    except Exception as exc:
        # Keep the last valid catalog and let the local gallery work offline.
        diagnostics.warning("Nie udało się odświeżyć katalogu giftów TikTok; zachowuję cache: %s", exc)
        return 0


_LEGACY_PARKOUR_DESCRIPTION = "Gotowe akcje dla dołączonej mapy 100 Levels: przesuwanie poziomu, więzienie, reset z 60-sekundowym odliczaniem i anty-reset. Wymaga lokalnego serwera GiftStream."
_LEGACY_PARKOUR_RULES = {
    "mp-rose": ("Róża → następny poziom", "gift", "Rose", "parkour_level_forward"),
    "mp-tiktok": ("TikTok → poprzedni poziom", "gift", "TikTok", "parkour_level_back"),
    "mp-follow": ("Follow → więzienie", "follow", None, "parkour_jail"),
    "mp-donut": ("Donut → awans o poziom", "gift", "Donut", "parkour_level_forward"),
    "mp-dragon": ("Smok → reset za 60 s", "gift", "Dragon", "parkour_reset"),
    "mp-antireset": ("Anty-reset → anuluj reset", "gift", "Panda", "parkour_antireset"),
}


def _migrate_builtin_parkour_preset() -> bool:
    """Update only the untouched built-in v0.6.4 parkour preset, never user-edited rules."""
    old = preset_store.get_preset("minecraft-parkour")
    if old is None or old.description != _LEGACY_PARKOUR_DESCRIPTION or len(old.rules) != len(_LEGACY_PARKOUR_RULES):
        return False
    for rule in old.rules:
        expected = _LEGACY_PARKOUR_RULES.get(rule.id)
        actual = (rule.name, rule.trigger.type, rule.trigger.gift_name, rule.action.type)
        if expected is None or actual != expected or rule.trigger.min_coins is not None or rule.trigger.max_coins is not None:
            return False
    game = get_game("minecraft-parkour")
    if game is None:
        return False
    preset_store.save_preset(Preset(id=game["preset_id"], name=game["title"],
                                    description=game["description"], rules=game["rules"]))
    return True


@app.on_event("startup")
async def startup() -> None:
    global config, _catalog_refresh_task, _catalog_refresh_started, _currency_refresh_task, _gift_refresh_loop_task
    diagnostics.info("Uruchamiam GiftStream %s; dane użytkownika: %s.", VERSION, DATA)
    config = read_config()
    if config.mode == "simulator":
        # Older builds persisted the demo as the startup mode, which generated
        # fabricated viewers/events on every launch. Migrate to an inert state.
        config.mode = "disconnected"
        save_config()
        diagnostics.info("Wyłączono dawny tryb symulatora; GiftStream czeka na ręczne połączenie TikTok LIVE.")
    if config.tiktok_username:
        connector.username = config.tiktok_username
    for game_id in ("minecraft-parkour", "minecraft-dropper"):
        game = get_game(game_id)
        if game and preset_store.get_preset(game["preset_id"]) is None:
            preset_store.save_preset(Preset(id=game["preset_id"], name=game["title"],
                                            description=game["description"], rules=game["rules"]))
    _migrate_builtin_parkour_preset()
    if config.active_preset_id not in preset_store.PRESET_IDS:
        config.active_preset_id = "minecraft-parkour"
        save_config()
    engine.load_preset(config.active_preset_id)
    diagnostics.info("Załadowano preset '%s'; tryb źródła: %s.", config.active_preset_id, config.mode)
    # TikTok's public catalog is loaded once in the background; the panel remains usable offline.
    if not _catalog_refresh_started:
        _catalog_refresh_started = True
        _catalog_refresh_task = asyncio.create_task(asyncio.to_thread(_refresh_catalog_safely))
    if _gift_refresh_loop_task is None or _gift_refresh_loop_task.done():
        _gift_refresh_loop_task = asyncio.create_task(_gift_catalog_refresh_loop())
    if _currency_refresh_task is None or _currency_refresh_task.done():
        _currency_refresh_task = asyncio.create_task(_currency_refresh_loop())
    if config.mode == "tiktok" and config.tiktok_username:
        # Restore the last explicitly selected public LIVE channel after an app restart.
        result = await connector.start("tiktok", config.tiktok_username)
        if not result.get("ok"):
            diagnostics.warning("Automatyczne wznowienie TikTok LIVE nie powiodło się: %s", result.get("message", "błąd"))


@app.on_event("shutdown")
async def shutdown() -> None:
    diagnostics.info("Zamykanie GiftStream; sprzątam zadania i połączenia.")
    await connector.stop()
    global parkour_reset_task
    if parkour_reset_task and not parkour_reset_task.done():
        parkour_reset_task.cancel()
    for task in parkour_jail_tasks.values():
        if not task.done():
            task.cancel()
    if parkour_server.settings():
        try:
            await asyncio.to_thread(parkour_server.stop)
        except Exception as exc:
            diagnostics.warning("Nie udało się zatrzymać lokalnego Minecraft podczas zamykania GiftStream: %s", exc)
    if _currency_refresh_task and not _currency_refresh_task.done():
        _currency_refresh_task.cancel()
    if _gift_refresh_loop_task and not _gift_refresh_loop_task.done():
        _gift_refresh_loop_task.cancel()


# -- Web app ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard() -> Any:
    return FileResponse(WEB / "index.html")

@app.get("/overlay", response_class=HTMLResponse)
async def overlay() -> Any:
    return FileResponse(WEB / "overlay.html")

@app.get("/docs/minecraft")
async def minecraft_docs() -> Any:
    return FileResponse(ROOT / "docs" / "MINECRAFT.md", media_type="text/markdown")

@app.get("/docs/games")
async def games_docs() -> Any:
    return FileResponse(ROOT / "docs" / "GAME_IDEAS.md", media_type="text/markdown")

app.mount("/static", StaticFiles(directory=WEB), name="static")


# -- API -------------------------------------------------------------------
_TIKTOK_IMAGE_HOSTS = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com")
_SOUND_UPLOAD_DIR = DATA / "uploads" / "sounds"
_SOUND_UPLOAD_LIMIT = 10 * 1024 * 1024
_SOUND_MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
               ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac"}
_SOUND_FILE_RE = re.compile(r"^[a-f0-9]{32}\.(?:mp3|wav|ogg|m4a|aac|flac)$")


def _trusted_tiktok_image_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (parsed.scheme == "https" and parsed.port in (None, 443) and
                not parsed.username and not parsed.password and
                any(host == domain or host.endswith("." + domain) for domain in _TIKTOK_IMAGE_HOSTS))
    except ValueError:
        return False


class _TikTokImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if not _trusted_tiktok_image_url(target):
            raise urllib.error.HTTPError(target, code, "TikTok CDN redirected outside its trusted hosts", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _download_tiktok_image(url: str) -> tuple[bytes, str]:
    if not _trusted_tiktok_image_url(url):
        raise ValueError("Nieobsługiwany adres grafiki TikToka.")
    request = urllib.request.Request(url, headers={"User-Agent": "GiftStream/1", "Referer": "https://www.tiktok.com/"})
    opener = urllib.request.build_opener(_TikTokImageRedirect())
    with opener.open(request, timeout=8) as response:
        if not _trusted_tiktok_image_url(response.geturl()):
            raise ValueError("Przekierowanie grafiki wyszło poza CDN TikToka.")
        media_type = response.headers.get_content_type().lower()
        if media_type not in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}:
            raise ValueError("CDN TikToka nie zwrócił obsługiwanej grafiki rastrowej.")
        payload = response.read(4 * 1024 * 1024 + 1)
    if not payload or len(payload) > 4 * 1024 * 1024:
        raise ValueError("Grafika jest pusta albo przekracza limit 4 MB.")
    return payload, media_type


@app.get("/api/gifts/image")
async def gift_image(url: str) -> Response:
    if not _trusted_tiktok_image_url(url):
        raise HTTPException(400, "Adres grafiki musi należeć do zaufanego CDN TikToka.")
    try:
        payload, media_type = await asyncio.to_thread(_download_tiktok_image, url)
    except Exception as exc:
        # Missing CDN art is expected for gifts that TikTok no longer serves;
        # return a cacheable non-server error so a long feed cannot flood diagnostics.
        raise HTTPException(404, f"Grafika niedostępna z CDN TikToka: {exc}",
                            headers={"Cache-Control": "public, max-age=300"}) from exc
    return Response(payload, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"})


@app.post("/api/sounds/upload")
async def upload_sound(request: Request, filename: str = "") -> dict[str, Any]:
    safe_name = Path(filename.replace("\\", "/")).name[:160]
    suffix = Path(safe_name).suffix.lower()
    media_type = _SOUND_MIME.get(suffix)
    if not media_type:
        raise HTTPException(415, "Wybierz plik MP3, WAV, OGG, M4A, AAC lub FLAC.")
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        content_length = 0
    if content_length > _SOUND_UPLOAD_LIMIT:
        raise HTTPException(413, "Plik dźwiękowy przekracza limit 10 MB.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _SOUND_UPLOAD_LIMIT:
            raise HTTPException(413, "Plik dźwiękowy przekracza limit 10 MB.")
        chunks.append(chunk)
    payload = b"".join(chunks)
    signatures = {".wav": lambda b: len(b) > 12 and b[:4] == b"RIFF" and b[8:12] == b"WAVE",
                  ".ogg": lambda b: b.startswith(b"OggS"), ".flac": lambda b: b.startswith(b"fLaC"),
                  ".m4a": lambda b: len(b) > 8 and b[4:8] == b"ftyp",
                  ".aac": lambda b: len(b) > 2 and b[0] == 0xff and (b[1] & 0xf6) == 0xf0,
                  ".mp3": lambda b: b.startswith(b"ID3") or (len(b) > 1 and b[0] == 0xff and (b[1] & 0xe0) == 0xe0)}
    if not payload or not signatures[suffix](payload):
        raise HTTPException(415, "Plik nie wygląda na dźwięk w formacie zgodnym z jego rozszerzeniem.")
    _SOUND_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    asset = f"{uuid.uuid4().hex}{suffix}"
    (_SOUND_UPLOAD_DIR / asset).write_bytes(payload)
    diagnostics.info("Zapisano lokalny dźwięk użytkownika (%s, %s bajtów).", suffix, len(payload))
    return {"url": f"/api/sounds/uploads/{asset}", "name": safe_name, "size": len(payload), "content_type": media_type}


@app.get("/api/sounds/uploads/{filename}")
async def uploaded_sound(filename: str) -> FileResponse:
    if not _SOUND_FILE_RE.fullmatch(filename):
        raise HTTPException(404, "Nie znaleziono pliku audio.")
    path = _SOUND_UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "Nie znaleziono pliku audio.")
    return FileResponse(path, media_type=_SOUND_MIME[path.suffix],
                        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@app.get("/api/sounds/myinstants")
async def myinstants_search(q: str = "") -> dict[str, Any]:
    if len(q) > 100:
        raise HTTPException(400, "Wyszukiwana fraza może mieć maksymalnie 100 znaków.")
    if not q.strip():
        return {"results": [], "notice": "Wyszukiwarka MyInstants jest nieoficjalna i może okresowo przestać działać."}
    try:
        results = await asyncio.to_thread(search_myinstants, q)
    except Exception as exc:
        raise HTTPException(502, f"Nie udało się pobrać wyników MyInstants: {exc}") from exc
    return {"results": results, "notice": "Wyniki pochodzą z nieoficjalnego wyszukiwania MyInstants; dostępność plików może się zmieniać."}

@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "app": "GiftStream", "version": installed_release_tag()}

@app.get("/api/updates/status")
async def updates_status() -> dict:
    return update_state.status()

@app.post("/api/updates/check")
async def check_for_updates() -> dict:
    queued = update_state.request_check()
    status = update_state.status()
    return {"ok": True, "checking": True, "check_id": status["check_id"],
            "message": "Sprawdzanie aktualizacji rozpoczęte." if queued else "Sprawdzanie aktualizacji jest już w toku."}

@app.post("/api/updates/apply")
async def apply_downloaded_update() -> dict:
    if not update_state.request_apply():
        raise HTTPException(409, "Nie ma pobranej aktualizacji do zastosowania.")
    return {"ok": True, "message": "GiftStream uruchomi się ponownie z pobraną aktualizacją."}

@app.get("/api/download-info")
async def download_info() -> dict:
    """Return the single GiftStream executable download URL."""
    now = time.time()
    if _DOWNLOAD_INFO_CACHE["data"] is not None and now < _DOWNLOAD_INFO_CACHE["expires"]:
        return _DOWNLOAD_INFO_CACHE["data"]
    fallback = {"available": False, "url": GITHUB_RELEASES_URL,
                "releases_url": GITHUB_RELEASES_URL,
                "message": "Wydanie GiftStream dla Windows nie jest obecnie dostępne."}

    def check_github_release() -> dict:
        request = urllib.request.Request(
            "https://api.github.com/repos/rejson59/GiftStream/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "GiftStream/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                release = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return fallback
            raise
        assets = {asset.get("name"): asset for asset in release.get("assets", [])}
        asset = assets.get("GiftStream.exe")
        asset_url = str((asset or {}).get("browser_download_url", ""))
        if asset and asset_url.startswith("https://github.com/"):
            version = release.get("tag_name", "")
            return {"available": True, "kind": "application", "url": asset_url,
                    "releases_url": GITHUB_RELEASES_URL, "version": version,
                    "message": f"Pobierz GiftStream {version} dla Windows."}
        return fallback

    try:
        result = await asyncio.to_thread(check_github_release)
    except Exception:
        result = {**fallback, "message": "Nie udało się sprawdzić wydań. Otwórz stronę wydań GiftStream."}
    _DOWNLOAD_INFO_CACHE.update({"data": result, "expires": now + (300 if result["available"] else 60)})
    return result

@app.get("/api/status")
async def status() -> dict:
    return {
        "connector": connector.status(),
        "preset": engine.active_preset.model_dump() if engine.active_preset else None,
        "stats": engine.stats,
        "viewer_count": hub.viewer_count,
        "total_likes": hub.total_likes,
        "mode": config.mode,
        "tiktok_username": config.tiktok_username,
    }

@app.get("/api/snapshot")
async def snapshot() -> dict:
    return {**hub.snapshot(), "connector": connector.status(),
            "preset": engine.active_preset.model_dump() if engine.active_preset else None,
            "stats": engine.stats, "config": public_config()}

@app.post("/api/stats/clear-activity")
async def clear_activity() -> dict[str, Any]:
    hub.clear_activity()
    diagnostics.info("Użytkownik wyczyścił listę aktywności; liczniki pozostawiono bez zmian.")
    await hub.broadcast_snapshot()
    return {"ok": True, "message": "Wyczyszczono listę aktywności. Liczniki statystyk pozostały bez zmian."}

@app.post("/api/stats/reset")
async def reset_stats() -> dict[str, Any]:
    hub.reset_stats()
    engine.reset_stats()
    diagnostics.info("Użytkownik wyzerował wszystkie statystyki bieżącej sesji.")
    await hub.broadcast_snapshot()
    await hub.publish_status({"stats_reset": True})
    return {"ok": True, "message": "Wyzerowano wszystkie statystyki bieżącej sesji."}

@app.get("/api/config")
async def get_config() -> dict:
    return public_config()

@app.put("/api/config")
async def update_config(data: dict[str, Any]) -> dict:
    global config
    if "sound_url" in data:
        url = str(data["sound_url"] or "").strip()
        if url.startswith("/api/sounds/uploads/"):
            filename = url.rsplit("/", 1)[-1]
            if not _SOUND_FILE_RE.fullmatch(filename) or not (_SOUND_UPLOAD_DIR / filename).is_file():
                raise HTTPException(422, "Nie znaleziono przesłanego pliku dźwiękowego.")
        elif url:
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower().rstrip(".")
            try:
                ip = ipaddress.ip_address(hostname)
                unsafe_ip = ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            except ValueError:
                unsafe_ip = False
            if (len(url) > 2048 or parsed.scheme != "https" or not hostname or
                    parsed.username or parsed.password or unsafe_ip or
                    hostname in {"localhost", "metadata.google.internal"} or
                    hostname.endswith((".localhost", ".local", ".internal"))):
                raise HTTPException(422, "Dźwięk musi wskazywać publiczny adres HTTPS albo lokalnie wgrany plik.")
        data["sound_url"] = url
    merged = {**config.model_dump(), **data}
    try:
        config = AppConfig(**merged)
    except ValidationError as exc:
        raise HTTPException(422, detail=jsonable_encoder(exc.errors())) from exc
    save_config()
    diagnostics.info("Zaktualizowano ustawienia panelu GiftStream.")
    return public_config()

@app.get("/api/gifts")
async def gifts(q: str = "", sort: Literal["popular", "price_asc", "price_desc"] = "price_asc") -> list[dict]:
    return list_gifts(q, sort)

@app.post("/api/gifts/refresh")
async def refresh_gifts() -> dict:
    count = await asyncio.to_thread(_refresh_catalog_safely)
    return {"ok": count > 0, "count": count, "gifts": list_gifts()}

@app.get("/api/earnings/settings")
async def get_earnings_settings() -> dict[str, Any]:
    return earnings_tracker.get_settings()

@app.put("/api/earnings/settings")
async def update_earnings_settings(req: EarningsSettingsRequest) -> dict[str, Any]:
    return earnings_tracker.save_settings(req.usd_per_diamond, req.usd_to_pln)

@app.post("/api/earnings/refresh-rate")
async def refresh_earnings_rate() -> dict[str, Any]:
    try:
        return {"ok": True, "settings": await asyncio.to_thread(earnings_tracker.refresh_usd_to_pln)}
    except Exception as exc:
        return {"ok": False, "settings": earnings_tracker.get_settings(),
                "message": f"Nie udało się pobrać najnowszego kursu NBP: {exc}"}

@app.get("/api/minecraft/versions")
async def minecraft_version_options() -> dict[str, Any]:
    try:
        versions = await asyncio.to_thread(minecraft_versions)
        diagnostics.info("Pobrano listę stabilnych wersji Minecraft; latest=%s.", versions.get("latest"))
        return versions
    except Exception as exc:
        diagnostics.error("Nie udało się pobrać manifestu wersji Minecraft: %s", exc, exc_info=exc)
        raise HTTPException(502, f"Nie udało się pobrać wersji Minecraft z manifestu Mojang: {exc}") from exc

@app.get("/api/minecraft/parkour")
async def parkour_status() -> dict[str, Any]:
    settings = parkour_server.settings()
    status = await asyncio.to_thread(parkour_server.status)
    status["enabled"] = bool(settings.get("enabled"))
    status["chat_relay_enabled"] = bool(settings.get("chat_relay_enabled"))
    return status

@app.post("/api/minecraft/parkour/setup")
async def parkour_setup(req: ParkourSetupRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(parkour_server.setup, req.server_dir, req.player, req.eula_accepted, req.version, req.game_mode)
    except (ValueError, FileExistsError) as exc:
        diagnostics.warning("Konfiguracja Minecraft została odrzucona: %s", exc)
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        diagnostics.error("Endpoint konfiguracji Minecraft zakończył się błędem: %s", exc, exc_info=exc)
        raise HTTPException(502, str(exc)) from exc

@app.post("/api/minecraft/parkour/start")
async def parkour_start() -> dict[str, Any]:
    try:
        return await asyncio.to_thread(parkour_server.start)
    except Exception as exc:
        diagnostics.error("Nie udało się uruchomić serwera Minecraft przez API: %s", exc, exc_info=exc)
        raise HTTPException(400, str(exc)) from exc

@app.post("/api/minecraft/parkour/stop")
async def parkour_stop() -> dict[str, Any]:
    try:
        return await asyncio.to_thread(parkour_server.stop)
    except Exception as exc:
        diagnostics.error("Nie udało się zatrzymać serwera Minecraft przez API: %s", exc, exc_info=exc)
        raise HTTPException(400, str(exc)) from exc

@app.put("/api/minecraft/parkour/enabled")
async def parkour_enabled(request: dict[str, bool]) -> dict[str, Any]:
    state = parkour_server.settings()
    if not state.get("server_dir"):
        raise HTTPException(400, "Najpierw skonfiguruj lokalny serwer parkour.")
    state["enabled"] = bool(request.get("enabled", False))
    await asyncio.to_thread(parkour_server.save_settings, state)
    diagnostics.info("Sterowanie zdarzeniami w lokalnym Parkour %s.", "włączono" if state["enabled"] else "wyłączono")
    return {**await asyncio.to_thread(parkour_server.status),
            "enabled": state["enabled"],
            "chat_relay_enabled": bool(state.get("chat_relay_enabled"))}

@app.put("/api/minecraft/parkour/chat-relay")
async def parkour_chat_relay(req: ParkourChatRelayRequest) -> dict[str, Any]:
    state = parkour_server.settings()
    if not state.get("server_dir"):
        raise HTTPException(400, "Najpierw skonfiguruj lokalny serwer parkour.")
    state["chat_relay_enabled"] = req.enabled
    await asyncio.to_thread(parkour_server.save_settings, state)
    diagnostics.info("Przekazywanie komentarzy LIVE do Minecraft %s.", "włączono" if req.enabled else "wyłączono")
    return {"ok": True, "chat_relay_enabled": req.enabled}

@app.post("/api/minecraft/parkour/action")
async def parkour_test_action(req: ParkourActionRequest) -> dict[str, Any]:
    from .models import KNOWN_ACTIONS
    state = parkour_server.settings()
    mode = state.get("game_mode", "parkour")
    prefix = "dropper_" if mode == "dropper" else "parkour_"
    if req.action not in KNOWN_ACTIONS or not req.action.startswith(prefix):
        raise HTTPException(400, f"Akcja nie pasuje do aktywnego trybu {mode}.")
    if not state.get("server_dir"):
        raise HTTPException(409, "Najpierw skonfiguruj lokalny serwer Minecraft.")
    status = await asyncio.to_thread(parkour_server.status)
    if not status.get("ready"):
        raise HTTPException(409, status.get("message") or "Serwer Minecraft nie jest jeszcze gotowy przez RCON.")
    player = req.player or str(state.get("player", ""))
    if not player or player.casefold() not in {str(name).casefold() for name in status.get("players", [])}:
        raise HTTPException(409, f"Gracz {player or '(nieustawiony)'} nie jest online na serwerze Minecraft.")
    try:
        # A manual test is intentionally independent of the LIVE automation toggle.
        # That toggle controls incoming gifts; this endpoint is an explicit user action.
        message = await _run_parkour_action(
            GameAction(action=req.action, params={"player": player}), allow_disabled=True)
    except Exception as exc:
        diagnostics.warning("Test akcji Minecraft '%s' nie powiódł się: %s", req.action, exc)
        raise HTTPException(502, f"Nie udało się wykonać akcji Minecraft: {exc}") from exc
    return {"ok": True, "message": message or f"Akcja {req.action} wysłana dla {player}."}

@app.get("/api/actions")
async def actions() -> list[dict]:
    from .models import KNOWN_ACTIONS
    labels = {
        "parkour_push": "Parkour: delikatny push",
        "parkour_level_forward": "Parkour: kolejny poziom",
        "parkour_level_back": "Parkour: poprzedni poziom",
        "parkour_jail": "Parkour: więzienie",
        "parkour_reset": "Parkour: reset za 60 s",
        "parkour_antireset": "Parkour: anty-reset",
        "parkour_speed": "Parkour: sprint · 8 s",
        "parkour_slow": "Parkour: spowolnienie · 7 s",
        "parkour_blind": "Parkour: ciemność · 4 s",
        "parkour_jump": "Parkour: wysoki skok · 4 s",
        "parkour_shield": "Parkour: tarcza · 6 s",
        "parkour_cleanse": "Parkour: usuń negatywne efekty",
        "dropper_push": "Dropper: push",
        "dropper_level_forward": "Dropper: wyższy poziom",
        "dropper_level_back": "Dropper: niższy poziom",
        "dropper_reset": "Dropper: powrót na start",
        "dropper_speed": "Dropper: sprint · 8 s",
        "dropper_slow": "Dropper: spowolnienie · 7 s",
        "dropper_blind": "Dropper: ciemność · 4 s",
        "dropper_jump": "Dropper: wysoki skok",
        "dropper_shield": "Dropper: tarcza · 6 s",
        "dropper_cleanse": "Dropper: usuń negatywne efekty",
    }
    return [{"id": action, "label": labels[action]} for action in KNOWN_ACTIONS]

@app.get("/api/presets")
async def list_presets() -> list[dict]:
    return [preset.model_dump() for preset_id in sorted(preset_store.PRESET_IDS)
            if (preset := preset_store.get_preset(preset_id)) is not None]

@app.get("/api/presets/{preset_id}")
async def get_preset(preset_id: str) -> dict:
    if preset_id not in preset_store.PRESET_IDS:
        raise HTTPException(404, "Nie znaleziono presetu")
    p = preset_store.get_preset(preset_id)
    if p is None:
        raise HTTPException(404, "Nie znaleziono presetu")
    return p.model_dump()

def _validate_rule_sound_url(url: str) -> None:
    if url.startswith("/api/sounds/uploads/"):
        filename = url.rsplit("/", 1)[-1]
        if not _SOUND_FILE_RE.fullmatch(filename) or not (_SOUND_UPLOAD_DIR / filename).is_file():
            raise HTTPException(422, "Nie znaleziono przesłanego pliku dźwiękowego przypisanego do reguły.")
        return
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(hostname)
        unsafe_ip = ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        unsafe_ip = False
    if (len(url) > 2048 or parsed.scheme != "https" or not hostname or parsed.username or
            parsed.password or unsafe_ip or hostname in {"localhost", "metadata.google.internal"} or
            hostname.endswith((".localhost", ".local", ".internal"))):
        raise HTTPException(422, "Dźwięk reguły musi być publicznym linkiem HTTPS albo wgranym plikiem lokalnym.")


@app.put("/api/presets/{preset_id}")
async def update_preset(preset_id: str, preset: Preset) -> dict:
    if preset_id not in preset_store.PRESET_IDS:
        raise HTTPException(404, "Nie znaleziono presetu")
    if preset.id != preset_id:
        raise HTTPException(400, "ID w URL i body muszą być takie same")
    for rule in preset.rules:
        if rule.sound:
            _validate_rule_sound_url(rule.sound)
    saved = preset_store.save_preset(preset)
    if engine.active_preset and engine.active_preset.id == preset_id:
        engine.load_preset(preset_id)
    diagnostics.info("Zapisano preset Parkour '%s' (%s reguł).", saved.name, len(saved.rules))
    return saved.model_dump()

@app.post("/api/presets/{preset_id}/activate")
async def activate_preset(preset_id: str) -> dict:
    if preset_id not in preset_store.PRESET_IDS:
        raise HTTPException(404, "Nie znaleziono presetu")
    preset = engine.load_preset(preset_id)
    if preset is None:
        raise HTTPException(404, "Nie znaleziono presetu")
    config.active_preset_id = preset_id
    save_config()
    await hub.publish_status({"active_preset_id": preset_id, "active_preset_name": preset.name})
    diagnostics.info("Aktywowano preset Parkour '%s'.", preset.name)
    return {"ok": True, "preset": preset.model_dump()}

@app.get("/api/logs")
async def get_diagnostics(limit: int = 300) -> dict[str, Any]:
    lines = diagnostics.get_logs(limit)
    minecraft_lines = await asyncio.to_thread(parkour_server.diagnostic_lines, min(max(limit, 1), 150))
    return {"lines": lines + minecraft_lines}

@app.post("/api/logs/client")
async def report_client_diagnostic(req: ClientDiagnosticRequest) -> dict[str, bool]:
    diagnostics.error("Błąd interfejsu (%s): %s", req.source, req.message)
    return {"ok": True}

@app.post("/api/logs/clear")
async def clear_diagnostics() -> dict[str, bool]:
    diagnostics.clear_logs()
    await asyncio.to_thread(parkour_server.clear_diagnostic_logs)
    diagnostics.info("Dziennik aplikacji i log Minecraft wyczyszczono z panelu GiftStream.")
    return {"ok": True}

@app.post("/api/connect")
async def connect(req: ConnectRequest) -> dict:
    global config
    username = req.username.strip().lstrip("@").strip()
    if req.mode == "tiktok" and not re.fullmatch(r"[A-Za-z0-9._]{2,24}", username):
        raise HTTPException(400, "Podaj poprawny @username TikTok LIVE (bez spacji).")

    previous_username = config.tiktok_username.strip().lstrip("@").casefold()
    new_tiktok_user = req.mode == "tiktok" and username.casefold() != previous_username
    if new_tiktok_user:
        # Stop old-user events first, then clear every counter/history before the
        # new connector is allowed to receive data. This prevents mixed LIVE stats.
        await connector.stop()
        hub.reset_stats()
        engine.reset_stats()
        config.mode = "disconnected"
        config.tiktok_username = username
        save_config()
        await hub.broadcast_snapshot()
        await hub.publish_status({"stats_reset": True})
        diagnostics.info("Nowy kanał TikTok LIVE @%s; wyzerowano statystyki przed połączeniem.", username)
    try:
        result = await connector.start(req.mode, username)
    except Exception as exc:
        diagnostics.error("Błąd uruchamiania konektora (%s): %s", req.mode, exc, exc_info=exc)
        raise HTTPException(500, "Nie udało się uruchomić połączenia. Szczegóły są w panelu logów.") from exc
    if result.get("ok"):
        config.mode = req.mode
        config.tiktok_username = username if req.mode == "tiktok" else config.tiktok_username
        save_config()
    await hub.publish_status(connector.status())
    return result

@app.post("/api/disconnect")
async def disconnect() -> dict:
    global config
    await connector.stop()
    config.mode = "disconnected"
    save_config()
    await hub.publish_status(connector.status())
    diagnostics.info("Połączenie zatrzymano ręcznie; automatyczne wznowienie wyłączono.")
    return {"ok": True, "message": "Rozłączono"}

@app.post("/api/bridge/event")
async def bridge_event(ev: TikTokEvent, x_giftstream_secret: str | None = Header(default=None)) -> dict:
    secret = config.bridge_webhook_secret or os.getenv("GIFTSTREAM_BRIDGE_WEBHOOK_SECRET", "")
    if secret and not secrets.compare_digest(x_giftstream_secret or "", secret):
        raise HTTPException(401, "Invalid or missing X-GiftStream-Secret")
    await process_event(ev)
    return {"ok": True}

@app.post("/api/browser/event")
async def browser_event(req: BrowserEventRequest, request: Request) -> dict[str, Any]:
    """Accept normalized events from a local browser helper; never expose a public ingest."""
    remote = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(403, "Browser relay przyjmuje zdarzenia wyłącznie z tego komputera.")
    ev = req.event
    if ev.type == "system":
        raise HTTPException(422, "Browser relay nie przyjmuje zdarzeń systemowych.")
    if (len(ev.user) > 64 or len(ev.nickname) > 100 or len(ev.comment or "") > 1000
            or len(ev.gift_name or "") > 128 or len(ev.gift_image_url or "") > 2048
            or ev.repeat_count < 1 or ev.repeat_count > 1000
            or ev.like_count < 0 or ev.like_count > 10_000_000
            or ev.diamonds < 0 or ev.diamonds > 10_000_000):
        raise HTTPException(422, "Zdarzenie z przeglądarki przekracza dozwolony rozmiar lub zakres.")
    normalized = ev.model_copy(update={"source": "browser"})
    await process_event(normalized, trigger_actions=req.trigger_actions)
    return {"ok": True, "received": normalized.type,
            "trigger_actions": req.trigger_actions,
            "events_received": hub.browser_event_count}


@app.get("/api/browser/status")
async def browser_status(request: Request) -> dict[str, Any]:
    remote = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(403, "Browser relay jest dostępny wyłącznie lokalnie.")
    return {"events_received": hub.browser_event_count,
            "last_event_at": hub.browser_last_event_at or None}


@app.post("/api/bridge/action")
async def bridge_action(data: dict) -> dict:
    """Game -> GiftStream (e.g. report score/state); forwarded to dashboard/overlay."""
    data.setdefault("ts", __import__("time").time())
    await hub.publish_status({"game": data})
    return {"ok": True}


# -- WebSockets ------------------------------------------------------------
async def _ws_client(websocket: WebSocket, group: set[WebSocket]) -> None:
    await websocket.accept()
    group.add(websocket)
    await websocket.send_json({"kind": "snapshot", "data": hub.snapshot()})
    try:
        while True:
            # keepalive or client commands; all commands are intentionally inert
            await websocket.receive_text()
    except WebSocketDisconnect:
        group.discard(websocket)
    except Exception:
        group.discard(websocket)

@app.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket) -> None:
    await _ws_client(websocket, hub.feed_clients)

@app.websocket("/ws/game")
async def ws_game(websocket: WebSocket) -> None:
    await _ws_client(websocket, hub.game_clients)
