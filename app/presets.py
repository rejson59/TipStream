"""Local JSON persistence for the built-in Minecraft Parkour and Dropper presets."""
from __future__ import annotations

import json
from pathlib import Path

from .models import Preset
from .paths import data_dir

PRESETS_DIR = data_dir() / "presets"
PRESET_IDS = {"minecraft-parkour", "minecraft-dropper"}
PRESET_ID = "minecraft-parkour"  # Backward-compatible default preset id.


def _path(preset_id: str = PRESET_ID) -> Path:
    if preset_id not in PRESET_IDS:
        raise ValueError("Nieobsługiwany preset Minecraft.")
    return PRESETS_DIR / f"{preset_id}.json"


def get_preset(preset_id: str) -> Preset | None:
    if preset_id not in PRESET_IDS:
        return None
    path = _path(preset_id)
    if not path.is_file():
        return None
    try:
        return Preset(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def save_preset(preset: Preset) -> Preset:
    if preset.id not in PRESET_IDS:
        raise ValueError("GiftStream udostępnia wyłącznie presety Minecraft Parkour i Dropper.")
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(preset.id)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(preset.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return preset
