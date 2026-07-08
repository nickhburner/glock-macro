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
- **Per-mode skill profiles.** "Save to game mode" stores the current active
  categories, custom priority and avoid lists for the selected game mode, and
  reapplies them automatically whenever you switch back to that mode.
- **Multiple game modes:** Chapter, Plant Defense, Shackled Jungle, Eternal
  Lode, and All-Star Cup, each with mode-specific options. All-Star Cup ONLY
  works on a rooted BlueStacks instance. Switching mode while the macro is
  running takes effect on the next Start (the current run is never disturbed).
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
- **Languages.** English, French, and German. Pick the display language in
  Settings (the UI relabels on the next app start). If you play the game in
  French or German, the "Language refs" wizard walks you through recapturing
  the few text-bearing buttons in your language so detection keeps working.
- **Streaming capture.** Frames come from a continuous `screenrecord` H.264
  stream (decoded with PyAV), not per-poll screenshots. Faster and avoids
  BlueStacks black-frame issues.
- **Eternal Lode mode.** Automates the mining minigame: reads the board, clicks
  optimal cells, buys pickaxes, uses tools, opens chests.
- **Run timeout.** Automatically stop after a set duration; optionally close the
  game window and/or sleep the phone.
- **Humanised input.** Tap jitter and timing randomisation.
- **Root fast input (optional).** On a rooted BlueStacks (or any device with a
  writable `/dev/input`), route all taps through the low-latency `sendevent`
  path used by All-Star. An optional "Humanized taps" toggle adds gaussian
  position jitter, randomised hold time and micro-drift (it slows All-Star, so
  it is off by default). Both live under Settings > Fast input.
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
custom buttons, captured language refs (`ref/fr`, `ref/de`), match zones, and
remote pairing token are never touched. Close the main app first.

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

The output goes to `dist/`. The build script also copies `skills/`, `ref/`,
`lang/`, `version.txt`, and `README.md` alongside the `.exe` automatically, and
builds `A2 Updater.exe` and `A2 Remote.exe`.

## Remote status and control (optional)

Watch the macro's status and log from your phone, and press Start / Stop /
Sleep phone / Close BlueStacks from anywhere. **Off by default**; nothing
remote exists until you switch it on.

How it works: the main app never touches the internet. While the feature is
enabled, a small companion (`A2 Remote.exe`, sitting next to the main exe)
pushes the status **outbound over HTTPS** to a tiny relay running on *your
own* free Cloudflare account, and a static web page (hosted on *your own*
GitHub Pages) shows it. No ports are opened on your PC or router, and no
third-party service of ours is involved: you run both halves yourself.

One-time setup, three steps:

1. **Deploy the relay** to your Cloudflare account: follow
   `remote/worker/DEPLOY.md` (about 10 minutes, copy-paste commands).
2. **Deploy the web page** to GitHub Pages: follow `remote/pages/DEPLOY.md`
   (a single file upload). The `remote/` folder is in the source repository,
   not in the installed app folder.
3. **Enable and pair:** in the app's Remote panel, paste both URLs, switch
   the feature on, press **Copy pairing link**, and open that link once on
   your phone.

The security model in plain words:

- On first enable the app creates a random secret (`remote_token.txt`) that
  never leaves your machines: it travels only inside the pairing link, in
  the part after `#`, which browsers do not send to any server.
- Everything is signed with that secret, end to end. The relay only stores
  the latest status for a few minutes and a pending command for one minute;
  it never sees the secret, so it **cannot** control your PC, and neither
  can anyone else without your pairing link.
- Commands expire after 60 seconds and cannot be replayed; the PC only
  accepts the four built-in commands, nothing else.
- If a pairing link ever leaks, press **Regenerate token**: every old link
  stops working instantly.
- Turning the feature off stops the companion; the main app itself has no
  network code at all.
