# Architecture

A2 Macro Controller automates an Archero-style mobile game running in a
BlueStacks emulator or on a real phone mirrored with scrcpy. It watches the
game screen over ADB, recognises UI states and skill choices with OpenCV
template matching, and taps the best option. A Tkinter GUI is the primary
control surface; the macro can also run headless with `python main.py`.

## Runtime flow

1. `gui.py` (`App`) is the control panel: device selection, game-mode and
   skill configuration, settings, log, and Start/Stop. It runs the macro on a
   background thread and pumps log lines to a queue drained on the UI thread.
2. On Start, `run_macro` (or `run_eternal_lode` / `run_all_star`) opens an ADB
   connection and a streaming capture, then loops: grab a frame, match refs
   and skills, tap.
3. Capture is a continuous `screenrecord` H.264 stream decoded by PyAV
   (`capture.ScreenRecordStream`), NOT per-poll screencap. Streaming is
   required: it keeps the emulator surface composited (no black frames) and
   `_grab()` returns the latest frame in <1 ms. See `capture.py` and the
   stall-recovery notes in that module and in `adb.py`.

## Modules

- `gui.py` - Tkinter control panel. All user-visible strings go through
  `i18n.t()`. Also owns the optional remote IPC loop and companion lifecycle
  (see [remote.md](remote.md)).
- `main.py` - `Macro` state machine (lobby -> playing -> skill select -> devil
  reject -> game over) plus `run_macro`. `close_target()` / `sleep_phone()` are
  the timeout/remote shutdown helpers.
- `all_star.py` - All-Star Cup / Elimination boss-pick mode (`run_all_star`).
- `eternal_lode.py` - Eternal Lode minigame (`run_eternal_lode`).
- `adb.py` - `ADBClient` (screenshot, tap, swipe, resolution, sleep_phone,
  connect/reconnect), `find_adb`, `list_devices`, `choose_server_port`. Uses a
  dedicated adb server port to dodge the BlueStacks HD-Adb vs SDK version war.
- `capture.py` - `ScreenRecordStream`: screenrecord -> latest decoded frame.
- `matcher.py` - multi-scale `TM_CCOEFF_NORMED` template matching + helpers
  (`load_template`, `list_skill_files`, `skill_hash`, `crop_band`).
- `fastinput.py` - `FastTapper`: low-latency taps via `sendevent` to a
  writable `/dev/input` node. See [fast-input.md](fast-input.md).
- `config.py` - defaults + `settings.json` load/save. Only keys in
  `PERSISTED_KEYS` round-trip. `BASE_DIR` is the config folder (or the exe
  folder when frozen).
- `i18n.py` - stdlib translation helper. See [localization.md](localization.md).
- `widgets.py` - custom themed Tkinter widgets.
- `updater.py` / `A2 Updater.exe` - standalone GitHub-Releases updater (the
  only network-touching component of the shipped app besides the opt-in remote
  companion).
- `remote_agent.py` / `A2 Remote.exe` - opt-in remote-status companion. See
  [remote.md](remote.md).

## Image matching

- OpenCV `matchTemplate`, `TM_CCOEFF_NORMED`, threshold ~0.78.
- One `CALIBRATED_SCALE` constant (set by the Setup Wizard) drives all template
  scales: `skill_scale = SKILL_SCALE_BASELINE (0.55) * CALIBRATED_SCALE`,
  `ref_scale = REF_SCALE_BASELINE (1.0) * CALIBRATED_SCALE`.
- Frames are normalised to `MATCH_WIDTH` before matching; matched coordinates
  map straight back to device pixels for tapping (no window/region conversion).

## Skill selection

Skills live in per-category subfolders of `skills/`, numbered `1.png` (best)
.. `N.png`. Pick order: custom-priority list -> active categories in priority
order (best-to-worst within each) -> reroll (Refresh) -> first/second slot
fallback. The avoid list wins over everything and is matched by file-content
hash (`matcher.skill_hash`) so avoiding one copy avoids the skill everywhere.
Per-mode skill profiles (`MODE_SKILL_PROFILES`) snapshot the active
categories / custom / avoid lists per game mode and auto-apply on mode switch.

## Constraints

- Windows only. No OCR, no ML: template matching only.
- The main app (gui.py and everything it imports) never touches the network.
  Only `updater.py` and `remote_agent.py` do, both opt-in and outbound-only.
- The game window need not be visible or focused; ADB drives the device
  directly.

## Distribution

`build.bat` builds `A2 Macro Controller.exe` (from the checked-in spec),
`A2 Updater.exe`, and `A2 Remote.exe` (both stdlib-only onefile), then copies
`skills/`, `ref/`, `lang/`, `version.txt`, and `README.md` into `dist/`.
`ref/custom`, `ref/fr`, `ref/de` ship empty (user data). The whole `dist/`
folder is the shippable artifact. See the release flow in the parent
`CLAUDE.md`.
