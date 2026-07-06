# A2 Macro Controller

An automation tool for an Archero-style mobile game. It watches the game screen
via ADB, identifies UI states and skill choices using template matching, and
taps the optimal option automatically. Works with **BlueStacks**, other Android
emulators, or a **real Android phone** over USB.

## Prerequisites

- **Windows 10 or 11.**
- **ADB (Android Debug Bridge) enabled on your device:**
  - **BlueStacks:** Settings > Advanced > toggle "Android Debug Bridge (ADB)" on.
  - **Real phone:** Settings > Developer Options > enable "USB Debugging", then
    connect via USB cable. (Developer Options is unlocked by tapping Build Number
    7 times in Settings > About Phone.)
- Keep this folder together. `A2 Macro Controller.exe` needs the `skills/` and
  `ref/` folders sitting right next to it. Put the whole folder somewhere you
  can edit (e.g. Desktop), **not** inside `Program Files`.

## First-time setup

1. **Open the game** on BlueStacks or your phone, then launch `A2 Macro Controller.exe`.
2. **Connect your device.** The app auto-detects available ADB devices. Pick
   yours from the dropdown and click **Test Connection**. If no devices appear,
   hit **Refresh**.
3. **Run the Setup Wizard.** It connects to the device, reads the screen
   resolution, and writes all click-target coordinates automatically. No manual
   region picking or scale calibration needed.
4. **Choose your skills.** Tick the skill categories you want on the left and
   drag them so your favourite is on top. Optionally add specific must-have
   skills (Custom Priority) or skills to never take (Avoid).

That is it. Press **Start**.

## Features

- **Fully ADB-based.** No screen region picking, no window focus required. The
  game window does not need to be visible or on top.
- **Automatic device setup.** The Setup Wizard detects resolution and computes
  all coordinates; no manual calibration.
- **Smart skill selection.** Prioritise skill categories by drag order, pin
  specific must-have skills (Custom Priority), and blacklist skills to never
  take (Avoid list). Rerolls automatically when no wanted skill is showing.
- **Multiple game modes:** Chapter, Plant Defense, Shackled Jungle, Eternal
  Lode, and All-Star Cup, each with mode-specific options. All-Star Cup ONLY
  works on a rooted BlueStacks instance.
- **Automatic movement.** Configurable per-mode joystick movement after the
  first skill selection: timed chapter run, or directional Plant Defense
  positioning (top/bottom/left/right, with a Spawn Side toggle -- the game
  seats co-op players by username alphabetical order, so set your side once
  per partner).
- **Plant Defense co-op helpers.** Gives your partner a like on the results
  screen, and when hosting, taps the back button after every completed round
  so the game's level auto-advance never moves you off your chosen level.
- **Speed control.** Automatically cycles the in-game speed to max (3x) at the
  start of each match.
- **Tap-on-sight buttons.** Built-in detection for Play, Continue, Get Ready,
  Start Challenge, Devil Reject, Game Over, challenge-ended screens, spin wheel,
  and more. Clicked automatically whenever seen.
- **Custom buttons.** Capture any extra on-screen button (e.g. an event banner)
  via ADB screenshot crop; the macro clicks it on sight.
- **Streaming capture.** Frames come from a continuous `screenrecord` H.264
  stream (decoded with PyAV), not per-poll screenshots. Faster and avoids
  BlueStacks black-frame issues.
- **Eternal Lode mode.** Automates the mining minigame: reads the board, clicks
  optimal cells, buys pickaxes, uses tools, opens chests.
- **Run timeout.** Automatically stop after a set duration; optionally close the
  game window and/or sleep the phone.
- **Humanised input.** Tap jitter and timing randomisation.
- **Dark / light theme.**
- **Autosave.** Settings persist to `settings.json` automatically or on demand.
- **Global hotkey.** Press ``Ctrl+` `` at any time (any window focused) to
  start or stop the macro. Rebind it via the Hotkey button.
- **Auto-updates.** `A2 Updater.exe` fetches the latest release from GitHub.
- **Standalone .exe.** No Python install needed for end users.

## Running it

- Press **Start**. The status pill turns green (**Running**).
- To stop: press **Stop**, or use the global hotkey (``Ctrl+` `` by default).
- Your setup is remembered in `settings.json` next to the `.exe`.

## Updating

Run `A2 Updater.exe` (it sits next to the main exe). It checks GitHub for the
latest release, downloads it, and applies it in place. Your `settings.json`,
custom buttons, and match zones are never touched. Close the main app first.

## Good to know

- The first launch can take a few seconds (the `.exe` unpacks itself).
- If Windows SmartScreen warns about an unknown publisher, click
  **More info**, then **Run anyway**.
- **Seeing a black capture?** In BlueStacks, set the graphics renderer to
  OpenGL. On a real phone, make sure the screen is on. Stream capture
  (on by default) usually prevents this.

## Running from source

```
pip install -r requirements.txt
python gui.py
```

To build the standalone `.exe`:

```
build.bat
```

The output goes to `dist/`. Copy `skills/`, `ref/`, and `README.md` alongside
the `.exe` (the build script does this automatically).
