"""GiftStream — modele danych (Pydantic).

Odpowiadają strukturze StreamToEarn:
  Preset = lista reguł (trigger -> akcja w grze)
  Trigger = prezent / lajki / follow / słowo w komentarzu
  Akcja   = spawn, heal, boss, ruch, teleport itd.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Presety (konfiguracja streamera)
# ---------------------------------------------------------------------------

TriggerType = Literal["gift", "like", "follow", "comment", "join", "share", "subscribe"]


class Trigger(BaseModel):
    """Co wywołuje akcję."""
    type: TriggerType = "gift"
    gift_name: Optional[str] = None          # np. "Rose", "Lion"
    gift_id: Optional[int] = None            # id prezentu z TikToka
    min_diamonds: Optional[int] = None       # próg wartości prezentu
    min_coins: Optional[int] = Field(default=None, ge=0)  # verified viewer purchase price
    max_coins: Optional[int] = Field(default=None, ge=0)
    keyword: Optional[str] = None            # dla triggera "comment"
    like_threshold: Optional[int] = None     # co ile lajków odpalić (np. co 100)


class Action(BaseModel):
    """Co dzieje się w grze po triggerze."""
    type: str = "spawn_zombie"               # patrz docs/BRIDGE.md
    params: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    """Jedna reguła presetów: IF trigger THEN akcja."""
    id: str
    name: str = ""
    enabled: bool = True
    trigger: Trigger = Field(default_factory=Trigger)
    action: Action = Field(default_factory=Action)
    cooldown_sec: float = 0.0                # minimalny odstęp między odpaleniami
    interrupt: bool = False                  # czy to "paid interrupt" (nadpisuje wszystko)
    alert_text: str = ""                     # tekst alertu na overlayu
    sound: Optional[str] = None              # nazwa dźwięku / URL


class Preset(BaseModel):
    id: str
    name: str
    description: str = ""
    rules: list[Rule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Eventy z TikTok Live (znormalizowane)
# ---------------------------------------------------------------------------

EventType = Literal["gift", "like", "follow", "comment", "join", "share", "subscribe", "system"]


class TikTokEvent(BaseModel):
    """Normalized event from TikTok LIVE or a trusted webhook."""
    type: EventType
    user: str = "unknown"                    # @unique_id widza
    nickname: str = ""
    gift_name: Optional[str] = None
    gift_id: Optional[int] = None
    gift_image_url: Optional[str] = None
    diamonds: int = 0
    diamonds_estimated: bool = False
    gift_coins: int = 0
    gift_coins_estimated: bool = True
    gift_coins_source: str = ""
    repeat_count: int = 1
    repeat_end: bool = True                  # True = koniec streaka (akcja dopiero wtedy)
    comment: Optional[str] = None
    like_count: int = 0
    total_likes: int = 0
    viewer_count: int = 0
    source: str = "tiktok"                   # tiktok | webhook


class GameAction(BaseModel):
    """Akcja wysyłana do gry (przez WebSocket / webhook / UDP / plik)."""
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    triggered_by: Optional[TikTokEvent] = None
    rule_name: str = ""
    ts: float = 0.0


# ---------------------------------------------------------------------------
# Konfiguracja aplikacji
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    tiktok_username: str = ""
    # `simulator` remains accepted only to migrate older config.json files.
    mode: Literal["tiktok", "managed", "disconnected", "simulator"] = "disconnected"
    active_preset_id: str = "default"
    tts_enabled: bool = False
    tts_voice: str = "pl-PL"
    sound_alerts: bool = False
    sound_volume: float = Field(default=0.55, ge=0.0, le=1.0)
    sound_url: str = ""
    sound_label: str = Field(default="", max_length=100)
    overlay_template: Literal["grid", "list", "minimal", "streamer", "ticker", "spotlight"] = "grid"
    overlay_theme: Literal["mint", "violet", "amber", "ocean"] = "mint"
    overlay_accent: str = Field(default="#8cf0bc", pattern=r"^#[0-9A-Fa-f]{6}$")
    overlay_scale: float = Field(default=1.0, ge=0.7, le=1.4)
    overlay_title: str = Field(default="", max_length=60)
    overlay_fields: str = Field(default="earnings,gifts,actions,viewers", max_length=80)
    bridge_webhook_secret: str = ""


# ---------------------------------------------------------------------------
# Przykładowe prezenty (wartości orientacyjne; prawdziwe ID/grafiki
# dopisywane są z katalogu pokoju i eventów TikTok LIVE).
# ---------------------------------------------------------------------------

GIFT_CATALOG: list[dict[str, Any]] = [
    # Monety to cena orientacyjna dla widza; diamenty to odrębna wartość zdarzenia.
    # Ceny mogą się różnić regionalnie; prawdziwe ID i grafiki uzupełnia katalog TikToka.
    {"name": "Rose", "label_pl": "Róża", "id": None, "diamonds": 1, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "TikTok", "label_pl": "TikTok", "id": None, "diamonds": 5, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Finger Heart", "label_pl": "Serduszko z palców", "id": None, "diamonds": 5, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Doughnut", "label_pl": "Donut", "id": None, "diamonds": 30, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Ice Cream", "label_pl": "Lody", "id": None, "diamonds": 50, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Panda", "label_pl": "Panda", "id": None, "diamonds": 100, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Sunglasses", "label_pl": "Okulary", "id": None, "diamonds": 199, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Corgi", "label_pl": "Corgi", "id": None, "diamonds": 299, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Money Gun", "label_pl": "Pistolet na pieniądze", "id": None, "diamonds": 599, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Dragon", "label_pl": "Smok", "id": None, "diamonds": 999, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Castle", "label_pl": "Zamek", "id": None, "diamonds": 2888, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "Lion", "label_pl": "Lew", "id": None, "diamonds": 29999, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
    {"name": "TikTok Universe", "label_pl": "Wszechświat TikToka", "id": None, "diamonds": 34999, "coins": 0, "coins_estimated": True, "image_url": None, "source": "sample"},
]

# Akcje gier — dokumentacja w docs/BRIDGE.md
KNOWN_ACTIONS: list[str] = [
    "parkour_push", "parkour_level_forward", "parkour_level_back",
    "parkour_jail", "parkour_reset", "parkour_antireset",
    "parkour_speed", "parkour_slow", "parkour_blind", "parkour_jump",
    "parkour_shield", "parkour_cleanse",
]
