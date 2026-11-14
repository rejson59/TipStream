"""Built-in ready-to-run Minecraft game modes supported by GiftStream."""
from __future__ import annotations


def _rule(rule_id: str, name: str, trigger: dict, action: dict, alert: str = "", interrupt: bool = False) -> dict:
    return {
        "id": rule_id,
        "name": name,
        "enabled": True,
        "trigger": trigger,
        "action": action,
        "alert_text": alert or name,
        "interrupt": interrupt,
        "cooldown_sec": 0,
    }


GAMES = [
    {
        "id": "minecraft-parkour",
        "title": "Minecraft: parkour na żywo",
        "category": "Parkour",
        "status": "bridge",
        "integration": "GiftStream Local Minecraft",
        "icon": "🧗",
        "tagline": "100 poziomów parkour sterowanych przez widzów.",
        "description": "Gotowe akcje dla mapy 100 Levels. Push jest najtańszy (1–29 monet); awans i cofnięcie poziomu wymagają prezentu 30–999 monet; reset i anty-reset — co najmniej 1000 monet. Ceny monet są weryfikowane z katalogu TikToka, bez przeliczania diamentów.",
        "ideas": [
            "Róża · 1–29 monet → push",
            "Donut · 30–999 monet → następny poziom",
            "Ice Cream · 30–999 monet → poprzedni poziom",
            "Castle · 1000+ monet → reset",
            "Lion · 1000+ monet → anty-reset",
        ],
        "preset_id": "minecraft-parkour",
        "rules": [
            _rule("mp-push", "Róża · 1–29 monet → push", {"type": "gift", "gift_name": "Rose", "min_coins": 1, "max_coins": 29}, {"type": "parkour_push"}, "🌹 Push streamera!"),
            _rule("mp-forward", "Donut · 30–999 monet → następny poziom", {"type": "gift", "gift_name": "Donut", "min_coins": 30, "max_coins": 999}, {"type": "parkour_level_forward"}, "🍩 Awans o poziom!"),
            _rule("mp-back", "Lody · 30–999 monet → poprzedni poziom", {"type": "gift", "gift_name": "Ice Cream", "min_coins": 30, "max_coins": 999}, {"type": "parkour_level_back"}, "↩ Cofnij streamera!"),
            _rule("mp-follow", "Follow → więzienie na 20 s", {"type": "follow"}, {"type": "parkour_jail"}, "🔒 Do więzienia!"),
            _rule("mp-reset", "Castle · 1000+ monet → reset za 60 s", {"type": "gift", "gift_name": "Castle", "min_coins": 1000}, {"type": "parkour_reset"}, "🏰 Reset za 60 sekund!", True),
            _rule("mp-antireset", "Lion · 1000+ monet → anty-reset", {"type": "gift", "gift_name": "Lion", "min_coins": 1000}, {"type": "parkour_antireset"}, "🦁 Reset uratowany!", True),
        ],
    },
    {
        "id": "minecraft-dropper",
        "title": "Minecraft: Dropper",
        "category": "Dropper",
        "status": "bridge",
        "integration": "GiftStream Local Minecraft",
        "icon": "🪂",
        "tagline": "Pięć pionowych poziomów, widzowie sterują zrzutem.",
        "description": "Dropper Arena to lokalnie generowana mapa vanilla z pięcioma pionowymi szybami i wodnymi lądowiskami. Akcje widzów obejmują push, skok między poziomami, reset oraz krótkie efekty. Nie wymaga pluginów ani modów.",
        "ideas": ["Róża → push", "Donut → wyższy poziom", "Ice Cream → niższy poziom", "Castle → reset"],
        "preset_id": "minecraft-dropper",
        "rules": [
            _rule("md-push", "Róża → push", {"type": "gift", "gift_name": "Rose"}, {"type": "dropper_push"}, "🌹 Push w szybie!"),
            _rule("md-forward", "Donut → wyższy poziom", {"type": "gift", "gift_name": "Donut"}, {"type": "dropper_level_forward"}, "🍩 Wyższy szyb!"),
            _rule("md-back", "Ice Cream → niższy poziom", {"type": "gift", "gift_name": "Ice Cream"}, {"type": "dropper_level_back"}, "↩ Niższy szyb!"),
            _rule("md-reset", "Castle → powrót na start", {"type": "gift", "gift_name": "Castle"}, {"type": "dropper_reset"}, "🏰 Powrót na górę!", True),
            _rule("md-follow", "Follow → spowolnienie", {"type": "follow"}, {"type": "dropper_slow"}, "🐢 Spowolnienie w locie!"),
            _rule("md-jump", "Panda → wysoki skok", {"type": "gift", "gift_name": "Panda"}, {"type": "dropper_jump"}, "⬆ Wysoki skok!"),
        ],
    },
]


def get_game(game_id: str) -> dict | None:
    return next((game for game in GAMES if game["id"] == game_id), None)
