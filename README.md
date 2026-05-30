# A2 Macro Controller

A *helper* that plays the skill-picking parts of the game for you. It watches
the game window, chooses skills based on what you tell it to prefer, and taps
through the menus on its own.

## What you need

- A Windows 10 or 11 PC.
- The game running in one of:
  - **BlueStacks** (an Android emulator), or
  - your real phone, mirrored to the PC with **scrcpy** *(it probably works
    with other emulators, but this is what I tested it with)*.
- Keep this folder together. `A2 Macro Controller.exe` needs the `skills` and
  `ref` folders sitting right next to it. Put the whole folder somewhere you
  can edit, like your Desktop, **not** inside `Program Files`.

**Downloads**

- scrcpy (easiest with Android + ADB enabled): <https://scrcpy.org/>
- BlueStacks (no phone needed): <https://www.bluestacks.com/>

## First-time setup (about 2 minutes)

Open the game and get it on screen first, then start the app:

1. **Double-click `A2 Macro Controller.exe`.**
2. **Tell it where the game is.** Click **Pick** next to *Capture region*,
   then click anywhere on your game window. A box appears over it; drag the
   edges in until the box covers just the game screen (no black bars or
   toolbars). Click the green check to save.
3. **Line up the matching.** Get to a skill-selection screen in the game,
   then click **Calibrate scale from screen**. Wait for the countdown. This
   sizes the image matching to your window. Do this again any time you resize
   the game window or switch between BlueStacks and scrcpy.
4. **Set the tap spots.** Use the **Pick** buttons for *First skill slot*,
   *Second skill slot* and *Game-over tap*; each pops a bullseye you drag
   onto the right spot, then click the green check.
   > **Tip:** Set this up in a Valkyrie skill selection. Place the first
   > tap-spot/bullseye on the **left edge of the first skill**, and the second
   > on the **right edge of the second skill**. This makes sure the macro can
   > click skills in both regular and Valkyrie skill selections.
5. **Choose what to grab.** Tick the skill categories you want on the left,
   and drag them so your favourite/best is on top. Optionally add specific
   must-have skills (*Custom priority*) or skills to never take (*Avoid*).

### Optional: custom buttons

If the game shows extra buttons you want auto-clicked (e.g. an event "Enter"
button), use **+ Add custom button** on the right. Name the button, then
drag the box over it on screen and click the green check. The macro will
click that button on sight, just like the built-in Play / Continue prompts.
Run **Calibrate scale from screen** first so the capture is saved at the
right size.

## Running it

- Make sure the game window is visible and on top.
- Press **Start**. The status in the top-right turns green (**Running**).
- To stop: press **Stop**, press **Esc**, or slam your mouse into any corner
  of the screen *(inconsistent, but maybe useful if it somehow goes
  haywire)*. The status turns red (**Stopped**).

## Other options (top bar)

- **Eternal Lode**: switches the Start button to the Eternal Lode minigame
  macro (digging the 6x8 board, buying pickaxes when empty, stopping when
  all resources are gone). Leave it off to play the regular game.
  **Warning:** this mode is currently untested and may not work properly.
- **Dark mode**: switch between the dark and light look.
- **Autosave**: when on, your settings save automatically after every change.
  When off, press **Save settings** to keep changes.
- **Lock window**: pins the app on top and stops it being moved or resized.

## Good to know

- Your setup is remembered in `settings.json` next to the `.exe`.
- The first launch can take a few seconds (the `.exe` unpacks itself).
- If Windows SmartScreen warns about an unknown publisher, click
  **More info**, then **Run anyway**.
- **Seeing a black capture?** In BlueStacks set the graphics renderer to
  OpenGL; on scrcpy make sure the mirror window is visible and the phone
  screen is on. Also make sure the screen region is properly chosen.
