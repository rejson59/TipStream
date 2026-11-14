import io
import json
import zipfile
from pathlib import Path

import pytest

from app.parkour_server import MAP_FILENAME, MAP_VERSION, ParkourServer, _extract_map, map_level_areas


def test_minecraft_version_options_offer_latest_stable_and_exclude_snapshots(monkeypatch):
    from app import parkour_server

    monkeypatch.setattr(parkour_server, "_version_manifest", lambda **_kwargs: {
        "latest": {"release": "26.3"},
        "versions": [
            {"id": "26.3", "type": "release", "releaseTime": "2026-11-01T00:00:00Z"},
            {"id": "26.4-snapshot", "type": "snapshot"},
            {"id": "26.2", "type": "release", "releaseTime": "2026-10-01T00:00:00Z"},
        ],
    })
    result = parkour_server.minecraft_versions()
    assert result["latest"] == "26.3"
    assert [item["id"] for item in result["versions"]] == ["26.3", "26.2"]
    assert parkour_server._resolve_release("latest")[0] == "26.3"
    assert parkour_server._resolve_release("26.2")[0] == "26.2"
    with pytest.raises(ValueError, match="stabilnym wydaniem"):
        parkour_server._resolve_release("26.4-snapshot")


def test_setup_stores_each_minecraft_version_in_a_separate_world(tmp_path, monkeypatch):
    from app import parkour_server

    server = ParkourServer()
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(parkour_server, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(parkour_server, "_resolve_release", lambda version: (version, {}))
    monkeypatch.setattr(parkour_server.shutil, "which", lambda _name: "/usr/bin/java")
    monkeypatch.setattr(parkour_server, "_map_archive", lambda: tmp_path / "map.zip")

    def fake_download(root, version):
        jar = root / "server.jar"
        jar.write_bytes(b"test server jar")
        return jar, 21, version, Path("/usr/bin/java")

    def fake_extract(_archive, stage):
        # _extract_map removes the ZIP's top-level "100 Levels" prefix.
        (stage / "level.dat").write_bytes(b"map data")
        glint = stage / "datapacks/main/data/common/item_modifier/training_mode/glint"
        glint.mkdir(parents=True)
        for name in ("disable", "enable"):
            (glint / f"{name}.json").write_text(json.dumps([{
                "function": "minecraft:set_components",
                "components": {"minecraft:enchantment_glint_override": name == "enable"},
            }]), encoding="utf-8")
        timer = stage / "datapacks/main/data/common/function/timer/display.mcfunction"
        timer.parent.mkdir(parents=True)
        timer.write_text('title @s actionbar [{"text":"timer"}]\n', encoding="utf-8")

    monkeypatch.setattr(parkour_server, "_download_server_jar", fake_download)
    monkeypatch.setattr(parkour_server, "_extract_map", fake_extract)
    monkeypatch.setattr(server, "start", lambda: {})
    monkeypatch.setattr(server, "status", lambda: {"configured": True, "minecraft_version": server.settings()["minecraft_version"]})

    old = server.setup("", "Streamer", True, "26.2")
    old_world = tmp_path / "MinecraftParkour" / "servers" / "26.2" / "world" / "level.dat"
    assert old["minecraft_version"] == "26.2" and old_world.read_bytes() == b"map data"
    latest = server.setup("", "Streamer", True, "26.3")
    latest_world = tmp_path / "MinecraftParkour" / "servers" / "26.3" / "world" / "level.dat"
    assert latest["minecraft_version"] == "26.3" and latest_world.read_bytes() == b"map data"
    installed_modifier = json.loads((latest_world.parent / "datapacks/main/data/common/item_modifier/training_mode/glint/enable.json").read_text())
    assert installed_modifier["type"] == "minecraft:set_components" and "function" not in installed_modifier
    assert old_world.exists()


def test_bundled_map_has_metadata_and_level_regions():
    areas = map_level_areas()
    assert set(range(1, 101)).issubset(areas)
    assert all(dx > 0 and dz > 0 and dy in {69, 150} for _x, _z, dx, dz, dy in areas.values())
    import zipfile as zf
    from app.paths import bundle_root
    with zf.ZipFile(bundle_root() / MAP_FILENAME) as archive:
        assert "100 Levels/level.dat" in archive.namelist()
        assert "100 Levels/datapacks/main/pack.mcmeta" in archive.namelist()


def test_map_extract_strips_archive_root_into_world_directory(tmp_path):
    archive_path = tmp_path / "map.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("100 Levels/level.dat", "metadata")
        archive.writestr("100 Levels/datapacks/main/pack.mcmeta", "metadata")
    destination = tmp_path / ".giftstream-world-stage"
    _extract_map(archive_path, destination)
    assert (destination / "level.dat").read_text() == "metadata"
    assert (destination / "datapacks/main/pack.mcmeta").is_file()
    assert not (destination / "100 Levels").exists()


def test_map_extract_rejects_traversal_and_symlinks(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("100 Levels/level.dat", "metadata")
        archive.writestr("100 Levels/datapacks/main/pack.mcmeta", "metadata")
        archive.writestr("../outside.txt", "not safe")
    with pytest.raises(ValueError, match="ścieżkę"):
        _extract_map(bad, tmp_path / "world")


def test_server_diagnostic_tail_includes_jvm_output_and_redacts_secrets(monkeypatch, tmp_path):
    from app import parkour_server

    server_dir = tmp_path / "server"
    server_dir.mkdir()
    (server_dir / "giftstream-server.log").write_text(
        "Starting minecraft server\nrcon.password=private-secret\n", encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"server_dir": str(server_dir)}), encoding="utf-8")
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings)
    lines = ParkourServer().diagnostic_lines()
    assert any("Starting minecraft server" in line for line in lines)
    assert any("rcon.password=[REDACTED]" in line for line in lines)
    assert all("private-secret" not in line for line in lines)


def test_parkour_setup_requires_eula_before_any_download(monkeypatch, tmp_path):
    server = ParkourServer()
    monkeypatch.setattr("app.parkour_server.data_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="EULA"):
        server.setup("", "Streamer", False)
    assert not (tmp_path / "MinecraftParkour").exists()


def test_switching_versions_refuses_to_orphan_a_detached_live_server(monkeypatch, tmp_path):
    from app import parkour_server

    server = ParkourServer()
    old_root = tmp_path / "old-server"
    old_root.mkdir()
    settings = tmp_path / "minecraft_parkour.json"
    settings.write_text(json.dumps({"server_dir": str(old_root), "minecraft_version": "26.2",
                                    "rcon_password": "secret", "rcon_host": "127.0.0.1", "rcon_port": 25575}))
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings)
    monkeypatch.setattr(parkour_server, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(parkour_server, "_resolve_release", lambda version: (version, {}))
    monkeypatch.setattr(server, "_rcon", lambda _command, **_kwargs: "There are 1 of a max of 8 players online")
    with pytest.raises(RuntimeError, match="Zatrzymaj"):
        server.setup("", "Streamer", True, "26.3")
    assert not (tmp_path / "MinecraftParkour" / "servers" / "26.3").exists()
    assert json.loads(settings.read_text())["server_dir"] == str(old_root)


def test_start_detects_detached_server_after_giftstream_restart(monkeypatch, tmp_path):
    server = ParkourServer()
    root = tmp_path / "server"
    root.mkdir()
    (root / "server.jar").write_bytes(b"jar")
    (root / "eula.txt").write_text("eula=true")
    settings = tmp_path / "minecraft_parkour.json"
    settings.write_text(json.dumps({"server_dir": str(root), "rcon_password": "secret",
                                                   "rcon_host": "127.0.0.1", "rcon_port": 25575}))
    monkeypatch.setattr("app.parkour_server.SETTINGS_PATH", settings)
    commands = []
    monkeypatch.setattr(server, "_rcon", lambda command, **_kwargs: commands.append(command) or "There are 1 of a max of 8 players online: Streamer")
    monkeypatch.setattr("app.parkour_server._running_minecraft_clients", lambda: [])
    monkeypatch.setattr("app.parkour_server.shutil.which", lambda _name: pytest.fail("must not launch duplicate server"))
    status = server.start()
    assert commands == ["list", "list", "whitelist off"]
    assert status["ready"] is True and status["players_online"] == 1
    assert status["players"] == ["Streamer"]


def test_running_third_party_launcher_client_is_reported(monkeypatch):
    from app import parkour_server

    monkeypatch.setattr(parkour_server, "_windows_java_processes", lambda: [{
        "path": r"C:\\HorizonLauncher\\runtime\\javaw.exe",
        "command": 'javaw -Dfoo=bar net.minecraft.client.main.Main --version 1.21.11',
        "pid": 123,
    }])
    clients = parkour_server._running_minecraft_clients()
    assert clients == [{"launcher": "Horizon Launcher", "version": "1.21.11", "pid": 123}]


def test_java_major_parser_handles_current_and_legacy_version_formats(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app import parkour_server

    outputs = iter(['openjdk version "21.0.13" 2026-10-15', 'java version "1.8.0_421"'])
    monkeypatch.setattr(parkour_server.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(stderr=next(outputs), stdout=""))
    assert parkour_server._java_major(tmp_path / "java.exe") == 21
    assert parkour_server._java_major(tmp_path / "java8.exe") == 8


def test_java_version_detection_and_candidate_selection(monkeypatch, tmp_path):
    from app import parkour_server

    java_21, java_25 = tmp_path / "java21.exe", tmp_path / "java25.exe"
    java_21.touch(); java_25.touch()
    monkeypatch.setattr(parkour_server, "_java_candidates", lambda: [java_21, java_25])
    monkeypatch.setattr(parkour_server, "_java_major", lambda path: 21 if path == java_21 else 25)
    assert parkour_server.find_java(21) == (java_21, 21)
    assert parkour_server.find_java(26) is None


def test_java_runtime_extraction_accepts_a_safe_runtime_zip(tmp_path):
    from app import parkour_server

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("temurin/bin/java.exe", b"java-runtime")
    executable = parkour_server._safe_extract_java(archive_bytes.getvalue(), tmp_path / "java")
    assert executable == tmp_path / "java" / "temurin" / "bin" / "java.exe"
    assert executable.read_bytes() == b"java-runtime"


def test_java_runtime_extraction_rejects_zip_path_traversal(tmp_path):
    from app import parkour_server

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("jdk/bin/java.exe", b"java")
        archive.writestr("../outside.txt", b"unsafe")
    with pytest.raises(RuntimeError, match="niebezpieczną ścieżkę"):
        parkour_server._safe_extract_java(archive_bytes.getvalue(), tmp_path / "java")


def test_non_windows_without_compatible_java_gets_actionable_setup_error(monkeypatch):
    from app import parkour_server

    monkeypatch.setattr(parkour_server, "_java_candidates", lambda: [])
    monkeypatch.setattr(parkour_server, "_install_java", lambda _major: (_ for _ in ()).throw(
        RuntimeError("Wymagana jest Java 21+. Zainstaluj ją i uruchom GiftStream ponownie.")))
    with pytest.raises(RuntimeError, match="Java 21"):
        parkour_server.ensure_java(21)


def test_push_action_uses_a_small_local_knockback_and_validates_player(monkeypatch):
    server = ParkourServer()
    commands = []
    monkeypatch.setattr(server, "settings", lambda: {"player": "Streamer"})
    monkeypatch.setattr(server, "_rcon", lambda command: commands.append(command) or "ok")
    message = server.execute_action("parkour_push")
    assert "3 bloki" in message
    assert commands == [
        "effect give Streamer minecraft:slow_falling 3 0 true",
        "execute as Streamer at Streamer run tp @s ^ ^0.6 ^-3",
    ]
    with pytest.raises(ValueError):
        server.execute_action("parkour_push", "bad player")


@pytest.mark.parametrize(("action", "commands", "message"), [
    ("parkour_speed", ["effect give Streamer minecraft:speed 8 1 true"], "Sprint przez 8 sekund!"),
    ("parkour_slow", ["effect give Streamer minecraft:slowness 7 1 true"], "Spowolnienie przez 7 sekund!"),
    ("parkour_blind", ["effect give Streamer minecraft:darkness 4 0 true"], "Ciemność przez 4 sekundy!"),
    ("parkour_jump", ["effect give Streamer minecraft:jump_boost 4 2 true",
                       "effect give Streamer minecraft:slow_falling 6 0 true"], "Wysoki skok — bezpieczne lądowanie!"),
    ("parkour_shield", ["effect give Streamer minecraft:resistance 6 1 true"], "Tarcza odporności na 6 sekund!"),
    ("parkour_cleanse", ["effect clear Streamer minecraft:slowness", "effect clear Streamer minecraft:darkness",
                         "effect clear Streamer minecraft:blindness", "effect clear Streamer minecraft:nausea",
                         "effect clear Streamer minecraft:weakness"], "Usunięto negatywne efekty!"),
])
def test_new_parkour_gameplay_actions_are_bounded_and_rcon_safe(monkeypatch, action, commands, message):
    server = ParkourServer()
    sent = []
    monkeypatch.setattr(server, "settings", lambda: {"player": "Streamer"})
    monkeypatch.setattr(server, "_rcon", lambda command: sent.append(command) or "ok")
    assert server.execute_action(action) == message
    assert sent == commands
    with pytest.raises(ValueError, match="nick gracza Minecraft"):
        server.execute_action(action, "bad player")


def test_chat_relay_encodes_text_as_tellraw_json_without_modifying_it(monkeypatch):
    server = ParkourServer()
    commands = []
    monkeypatch.setattr(server, "_rcon", lambda command: commands.append(command) or "ok")
    message = 'hi \\"; op @a <3 Привет\nsecond line'
    assert server.relay_chat(message) == "ok"
    command = commands[0]
    assert command.startswith("tellraw @a ")
    import json
    assert json.loads(command[len("tellraw @a "):])["text"] == message


def test_parkour_action_allowlist_is_fixed():
    from app.models import KNOWN_ACTIONS
    assert {"parkour_push", "parkour_level_forward", "parkour_level_back", "parkour_jail",
            "parkour_reset", "parkour_antireset", "parkour_speed", "parkour_slow",
            "parkour_blind", "parkour_jump", "parkour_shield", "parkour_cleanse"}.issubset(KNOWN_ACTIONS)


def test_map_datapack_migration_fixes_modifier_json_and_adds_level_timer_hud(tmp_path):
    from app.parkour_server import _patch_map_datapack

    glint = tmp_path / "datapacks/main/data/common/item_modifier/training_mode/glint"
    glint.mkdir(parents=True)
    original = [{"function": "minecraft:set_components",
                 "components": {"minecraft:enchantment_glint_override": False}}]
    # Repair the exact installed-world format left by 0.10.4: an unwrapped
    # array element which still uses the invalid legacy `function` key.
    (glint / "disable.json").write_text(json.dumps(original[0]), encoding="utf-8")
    original[0]["components"]["minecraft:enchantment_glint_override"] = True
    (glint / "enable.json").write_text(json.dumps(original), encoding="utf-8")
    timer = tmp_path / "datapacks/main/data/common/function/timer/display.mcfunction"
    timer.parent.mkdir(parents=True)
    timer.write_text('title @s actionbar [{"text":"timer"}]\n', encoding="utf-8")

    _patch_map_datapack(tmp_path)
    _patch_map_datapack(tmp_path)  # Reconfiguring an existing installation must not stack HUD prefixes.

    for name in ("disable", "enable"):
        resource = json.loads((glint / f"{name}.json").read_text(encoding="utf-8"))
        assert isinstance(resource, dict)
        assert resource["type"] == "minecraft:set_components"
        assert "function" not in resource
    line = timer.read_text(encoding="utf-8")
    assert line.count('"objective":"level"') == 1
    assert '"text":" / 100  ·  "' in line
    assert '"text":"timer"' in line


def test_stop_is_idempotent_when_tracked_server_process_has_exited(monkeypatch, tmp_path):
    from app import parkour_server

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"server_dir": str(tmp_path), "rcon_host": "127.0.0.1",
                                    "rcon_port": 25575, "rcon_password": "secret"}), encoding="utf-8")
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings)
    monkeypatch.setattr(parkour_server, "_running_minecraft_clients", lambda: [])
    server = ParkourServer()

    class ExitedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

    server._process = ExitedProcess()
    monkeypatch.setattr(server, "_rcon", lambda _command: pytest.fail("must not contact RCON for an exited process"))
    result = server.stop()
    assert result["running"] is False
    assert result["ready"] is False
    assert "już zatrzymany" in result["message"]


def test_stop_after_app_restart_treats_refused_rcon_as_already_stopped(monkeypatch, tmp_path):
    from app import parkour_server

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"server_dir": str(tmp_path), "rcon_host": "127.0.0.1",
                                    "rcon_port": 25575, "rcon_password": "secret"}), encoding="utf-8")
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings)
    monkeypatch.setattr(parkour_server, "_running_minecraft_clients", lambda: [])
    server = ParkourServer()
    commands = []

    def refused(command, **_kwargs):
        commands.append(command)
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(server, "_rcon", refused)
    result = server.stop()
    assert result["running"] is False and result["ready"] is False
    assert "już zatrzymany" in result["message"]
    assert commands == ["stop", "list"]


def test_exited_startup_process_is_never_reported_as_rcon_ready(monkeypatch, tmp_path):
    from app import parkour_server

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"server_dir": str(tmp_path), "minecraft_version": "26.3",
                                    "rcon_host": "127.0.0.1", "rcon_port": 25575,
                                    "rcon_password": "secret"}), encoding="utf-8")
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings)
    monkeypatch.setattr(parkour_server, "_running_minecraft_clients", lambda: [])
    server = ParkourServer()

    class ExitedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

    server._process = ExitedProcess()
    monkeypatch.setattr(server, "_rcon", lambda _command: pytest.fail("dead process must not be queried"))
    result = server.status()
    assert result["running"] is False and result["ready"] is False
    assert "kod 0" in result["message"]


def test_concurrent_start_requests_spawn_only_one_server_process(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app import parkour_server

    server_dir = tmp_path / "server"
    server_dir.mkdir()
    (server_dir / "server.jar").write_bytes(b"jar")
    (server_dir / "eula.txt").write_text("eula=true")
    java = tmp_path / "java"
    java.touch()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"server_dir": str(server_dir), "java_required": 21,
                                    "java_executable": str(java), "rcon_host": "127.0.0.1",
                                    "rcon_port": 25575, "rcon_password": "secret"}), encoding="utf-8")
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings)
    monkeypatch.setattr(parkour_server, "_java_major", lambda _path: 21)
    monkeypatch.setattr(parkour_server, "_patch_map_datapack", lambda _world: None)
    monkeypatch.setattr(parkour_server, "_running_minecraft_clients", lambda: [])
    server = ParkourServer()
    calls = []

    class RunningProcess:
        pid = 456
        returncode = None

        @staticmethod
        def poll():
            return None

    def fake_popen(*_args, **_kwargs):
        calls.append("spawn")
        return RunningProcess()

    def no_rcon(command):
        raise ConnectionRefusedError(f"RCON unavailable for {command}")

    monkeypatch.setattr(server, "_rcon", no_rcon)
    monkeypatch.setattr(parkour_server.subprocess, "Popen", fake_popen)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: server.start(), range(2)))
    assert calls == ["spawn"]
    assert all(result["running"] and not result["ready"] for result in results)


def test_status_does_not_probe_rcon_during_server_startup_grace(monkeypatch, tmp_path):
    import time
    from app import parkour_server

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"server_dir": str(tmp_path), "rcon_host": "127.0.0.1",
                                    "rcon_port": 25575, "rcon_password": "secret"}), encoding="utf-8")
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings)
    monkeypatch.setattr(parkour_server, "_running_minecraft_clients", lambda: [])
    server = ParkourServer()

    class StartingProcess:
        @staticmethod
        def poll():
            return None

    server._process = StartingProcess()
    server._process_started_at = time.monotonic()
    monkeypatch.setattr(server, "_rcon", lambda *_args, **_kwargs: pytest.fail("RCON must wait during warm-up"))
    status = server.status()
    assert status["running"] is True
    assert status["ready"] is False
    assert "uruchamia świat" in status["message"]


def test_server_properties_and_existing_install_disable_whitelist(tmp_path):
    from app.parkour_server import _disable_server_whitelist, _write_server_properties

    properties = tmp_path / "server.properties"
    _write_server_properties(properties, "secret")
    text = properties.read_text(encoding="utf-8")
    assert "online-mode=false" in text
    assert "white-list=false" in text
    assert "enforce-whitelist=false" in text

    properties.write_text("motd=GiftStream\nonline-mode=true\nwhite-list=true\nenforce-whitelist=true\n", encoding="utf-8")
    _disable_server_whitelist(properties)
    text = properties.read_text(encoding="utf-8")
    assert "motd=GiftStream" in text
    assert "online-mode=false" in text
    assert "white-list=false" in text
    assert "enforce-whitelist=false" in text
    assert "online-mode=true" not in text
    assert "white-list=true" not in text


def test_ready_existing_server_has_whitelist_disabled_through_rcon(monkeypatch, tmp_path):
    from app import parkour_server

    settings = tmp_path / "minecraft.json"
    settings.write_text(json.dumps({"server_dir": str(tmp_path), "minecraft_version": "26.3",
                                    "rcon_host": "127.0.0.1", "rcon_port": 25575,
                                    "rcon_password": "secret"}), encoding="utf-8")
    (tmp_path / "server.properties").write_text("white-list=true\n", encoding="utf-8")
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings)
    monkeypatch.setattr(parkour_server, "_running_minecraft_clients", lambda: [])
    server = ParkourServer()
    commands = []
    monkeypatch.setattr(server, "_rcon", lambda command, **_kwargs: commands.append(command) or "There are 0 of a max of 8 players online:")

    status = server.status()
    assert status["ready"] is True
    assert commands == ["list", "whitelist off"]
    assert "white-list=false" in (tmp_path / "server.properties").read_text(encoding="utf-8")


def test_dropper_setup_uses_separate_vanilla_world_without_external_map(tmp_path, monkeypatch):
    from app import parkour_server

    server = ParkourServer()
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(parkour_server, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(parkour_server, "_resolve_release", lambda version: (version, {}))
    monkeypatch.setattr(parkour_server.shutil, "which", lambda _name: "/usr/bin/java")
    monkeypatch.setattr(parkour_server, "_map_archive", lambda: (_ for _ in ()).throw(AssertionError("dropper must not need Parkour archive")))
    monkeypatch.setattr(parkour_server, "_extract_map", lambda *_args: (_ for _ in ()).throw(AssertionError("no archive extraction")))

    def fake_download(root, version):
        jar = root / "server.jar"
        jar.write_bytes(b"server")
        return jar, 21, version, Path("/usr/bin/java")

    monkeypatch.setattr(parkour_server, "_download_server_jar", fake_download)
    monkeypatch.setattr(server, "start", lambda: {})
    monkeypatch.setattr(server, "status", lambda: {"configured": True, "game_mode": server.settings()["game_mode"]})
    result = server.setup("", "Streamer", True, "26.2", "dropper")
    root = tmp_path / "MinecraftDropper" / "servers" / "26.2"
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert result["game_mode"] == "dropper"
    assert settings["game_mode"] == "dropper"
    assert settings["map_version"] == "Dropper Arena 1.0"
    assert (root / "server.jar").is_file()
    assert not (root / "world" / "level.dat").exists()


def test_dropper_map_generation_is_bounded_and_idempotent(tmp_path, monkeypatch):
    from app import parkour_server

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"game_mode": "dropper", "dropper_map_initialized": False,
                                        "dropper_player_levels": {}}))
    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", settings_path)
    server = ParkourServer()
    commands = []
    monkeypatch.setattr(server, "_rcon", lambda command, **_kwargs: commands.append(command) or "ok")
    server.initialize_dropper_map()
    stored = json.loads(settings_path.read_text())
    assert stored["dropper_map_initialized"] is True
    assert sum(command.startswith("fill ") for command in commands) == 60
    assert any("minecraft:water" in command for command in commands)
    assert any("minecraft:glass" in command for command in commands)
    assert any("minecraft:orange_stained_glass" in command for command in commands)
    server.initialize_dropper_map()
    assert len(commands) == 66


def test_dropper_level_actions_clamp_and_persist_stage(tmp_path, monkeypatch):
    from app import parkour_server

    monkeypatch.setattr(parkour_server, "SETTINGS_PATH", tmp_path / "settings.json")
    server = ParkourServer()
    state = {"game_mode": "dropper", "dropper_map_initialized": True,
             "dropper_player_levels": {"Streamer": 5}, "player": "Streamer"}
    commands = []
    monkeypatch.setattr(server, "settings", lambda: state)
    monkeypatch.setattr(server, "_rcon", lambda command: commands.append(command) or "ok")
    assert "szybu 5 z 5" in server.execute_action("dropper_level_forward")
    assert state["dropper_player_levels"]["Streamer"] == 5
    assert "szybu 4 z 5" in server.execute_action("dropper_level_back")
    assert state["dropper_player_levels"]["Streamer"] == 4
    assert "tp Streamer 58 127 4" in commands
    assert any('DROPPER · SZYB 4/5' in command for command in commands)
