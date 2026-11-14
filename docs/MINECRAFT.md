# Minecraft Parkour and Dropper

GiftStream includes two local Minecraft game modes: Parkour uses the bundled `100-levels-5229-v1.7.4.zip` world; Dropper builds a compact five-shaft arena directly in a fresh vanilla world with controlled RCON commands.

## Dropper mode

Choose **Minecraft Dropper · 5 szybów** in the Minecraft panel and create the local server. GiftStream uses a separate `MinecraftDropper/servers/<version>` folder, then generates **Dropper Arena 1.0** in the vanilla world after RCON becomes ready: five colored-glass shafts, varied glass obstacle openings, water landing pools, and a stage title visible to the streamer. Map generation is bounded to those fixed coordinates and is marked complete in settings so it does not overwrite the arena on restart. The map is generated locally rather than copied from a third-party download; this avoids unknown archive contents and map-version mismatch. The vanilla server does not load Bukkit/Paper plugins, and no plugin or mod is needed for the audience actions.

The built-in **Reguły Dropper** preset includes push, jump to a higher/lower shaft, return to start, slow, and high jump. The action editor also offers the same timed effects as Parkour: speed, slow, darkness, jump plus slow-falling, resistance, and cleanse. Assign a gift/event or test locally after the arena is ready and game control is enabled. The five shafts are manually selected by level actions; reaching the water does not automatically advance the stage.

## One-click setup

In **Minecraft Parkour** or **Minecraft Dropper**, select **Najnowsza stabilna** or one of the recent official releases, enter your Minecraft nickname, read and accept the Minecraft EULA, then choose **Utwórz i uruchom serwer**. GiftStream queries Mojang's official version manifest; snapshots are not offered. Each selected release gets its own server and world folder, so switching versions does not overwrite another version's world. On first setup it:

- downloads the selected official Minecraft server JAR using Mojang's version manifest and verifies its published SHA-1;
- resolves **Najnowsza stabilna** at setup time and checks that installed Java meets that release's requirement;
- extracts the included 100 Levels world, which has map datapack regions for levels 1–100, then migrates the two training-mode glint item modifiers from the bundled array/`function` form to the typed object (`type`) resource format Minecraft 26.3 expects; the bundled archive is never modified;
- adds a per-player `LEVEL n / 100` prefix to the map's existing actionbar timer, preserving its timer display and leaving the map's optional scoreboard sidebar controls untouched;
- configures and starts the vanilla server bound only to `127.0.0.1:25565`, with Minecraft online authentication and the whitelist disabled. This avoids Mojang session-server 429 errors for this local-only server;
- creates a random RCON password and binds RCON only to `127.0.0.1`.

For servers installed by earlier GiftStream releases, startup migrates `online-mode`, `white-list` and `enforce-whitelist` to `false`; if that server is already running, GiftStream also sends the RCON `whitelist off` command. Restart the server for the authentication-mode change to take effect. Offline authentication is appropriate here only because the Minecraft server listens exclusively on loopback (`127.0.0.1`); do not expose its port to a LAN or the internet. Offline mode uses different player UUIDs than authenticated mode, so existing player-specific inventory/data may not carry over, though the parkour world and scoreboards remain stored locally.

The map works as a datapack and does not require mods, a mod loader, or third-party plugins. The official Minecraft server is downloaded at setup time and is not bundled with GiftStream. GiftStream searches PATH, `JAVA_HOME`, common launcher runtimes (including `.minecraft/runtime`, Horizon, Prism and MultiMC) and running Java game processes. If no compatible runtime is found on Windows, it installs a per-user Adoptium JRE into GiftStream's data folder, verifies its SHA-256, and uses it without requiring administrator rights. Internet access is needed for first setup and the Minecraft EULA must be accepted by the user. The server uses up to 2 GB of memory.

When the server is ready, join from Minecraft Multiplayer using **`localhost:25565`** (or use **Kopiuj adres** in GiftStream). This server is intentionally reachable only from the same computer. The dashboard shows the selected Minecraft version and connected player count when RCON is ready. Enable **Wysyłaj akcje presetu parkour do lokalnej gry** to allow gifts to trigger game actions. Use **Zatrzymaj** for a graceful shutdown. In the Minecraft view, **Logi aplikacji i Minecraft** captures setup stages, Java/server/map downloads, server start/stop, RCON failures, preset actions, API errors, and browser-side errors. Its copy button also includes the latest JVM output from `giftstream-server.log` in that version's server folder; **Wyczyść** clears both app diagnostics and the server log.

The bundled 100 Levels map was prepared for Minecraft **26.2**. Its `training_mode/glint/disable` and `enable` item modifier files use a JSON array that Minecraft **26.3** rejects; setup now safely converts those two known one-element resources to the expected typed object format (`type: minecraft:set_components`) before launching. This fixes the reported datapack-load failure, but does not certify every map mechanic on every later Minecraft release. If the server exits during startup, GiftStream reports it as stopped/not ready rather than RCON-ready. Stop is safe to repeat when the process has already exited or RCON is unavailable because the server is gone. The launcher checks the server download's hash and never overwrites a non-empty version folder. First-time setup can take longer because GiftStream may need to download the server JAR or install a compatible Java runtime; later starts reuse both. During JVM world/registry warm-up, the dashboard now reports “Minecraft uruchamia świat i rejestry…” instead of repeatedly opening RCON connections, and the server uses a smaller initial heap to reduce memory pressure on launch.

## Preset and coin prices

The built-in **Parkour** preset maps:

- Rose (1–29 Coins) to a small push;
- Donut (30–999 Coins) to the next level;
- Ice Cream (30–999 Coins) to the previous level;
- Castle (at least 1,000 Coins) to a 60-second reset countdown;
- Lion (at least 1,000 Coins) to cancel the countdown;
- Follow to a 20-second jail.

The rule editor also offers six timed audience effects: **Sprint** (speed for 8 s), **Slow** (slowness for 7 s), **Darkness** (4 s), **High jump** (jump boost plus slow falling), **Shield** (resistance for 6 s), and **Cleanse** (clears selected negative effects without removing other buffs). They affect only the configured local player and expire automatically. Assign any gift/event to these actions in **Reguły Parkour**. In the Minecraft panel, start the server, enable Parkour control, then use **Testuj akcję** to try an effect locally without waiting for a LIVE event.

Rules can be edited in GiftStream. Coin thresholds use only an explicit coin price from TikTok's gift catalog. If a catalog entry exposes only `diamond_count` or has no verified coin price, the coin-based rule will not fire for that gift. TikTok gift catalog data and prices can vary by region.

## Transparent LIVE Arena overlay

In **Nakładka OBS**, choose **LIVE Arena** for a stream HUD inspired by Minecraft gift overlays. It composes the active rule set into left/right gift-action cards, places live counters and the current top donor in the center, and shows the latest real gifts in a bottom strip. The page background remains transparent, so Minecraft footage stays visible behind the modules. It reports actual LIVE connection state and actual session counts; it does not invent win/loss points or viewer activity. Choose landscape (16:9) or portrait (9:16), then use **Przesuń elementy** in the preview to drag modules; pull the lower-right handle to resize. Positions are saved independently per canvas and included in the shareable OBS URL.

The overlay editor can hide/show counters, rules, gift strip and top donor, set accent/theme and custom title, and adjust scale, card opacity and density. It adapts to wide and portrait Browser Source canvases. When styling is changed, copy the refreshed URL into OBS/TikTok LIVE Studio if the source does not reload its address automatically. Use the transparency checker in the editor preview to judge card opacity before going live.

## Optional TikTok chat relay

The option **Przekazuj komentarze TikTok LIVE do czatu Minecraft bez zmian** is off by default. When enabled, GiftStream passes the comment text as a JSON-escaped `tellraw` component. It does not prefix a username, trim or otherwise rewrite the message, and never interprets the message as a Minecraft command. The relay is best-effort: it only handles comments actually received by the unofficial TikTokLive connector, and messages over 2,000 characters are rejected rather than truncated.

## Network safety

This is a local streamer-PC feature. `127.0.0.1` is not reachable by viewers or another host. Do not forward or publicly expose Minecraft RCON or the server port. Third-party plugins are not required or automatically installed for either mode. Dropper uses the vanilla server and its own generated world; adding an unrelated Bukkit/Paper plugin would require replacing the server type and a separate compatibility/security review.
