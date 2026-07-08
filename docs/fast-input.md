# Fast input (sendevent) and humanized taps

`fastinput.FastTapper` sends low-latency taps by writing `sendevent` commands
to the device touchscreen node through one persistent `adb shell`. A tap is
~25 ms (six sendevent calls) versus ~35 ms for `adb shell input tap`, and it is
synchronous: `tap()` appends an `echo` marker and waits for the shell to echo
it back, so the tap has executed before the call returns (a fire-and-forget
version let a backlog build in the pipe and kept firing after a spam loop
stopped, which broke All-Star phase transitions).

## How it sets up

`getevent -pl` auto-detects the node, type (A vs B), axis ranges, and the
screen -> event transform:
- "rotated" for BlueStacks' portrait-on-landscape 90-degree grid:
  `ev_x = (1 - sy/H) * xmax`, `ev_y = (sx/W) * ymax`.
- "direct" for a normal phone.

It uses `sendevent` (one clean 24-byte write per event) rather than raw
`cat > node` writes, because adb fragments multi-event writes and the kernel
kills the writer with EINVAL on misaligned chunks. If setup fails it falls back
to `adb.tap` (with self-healing shell restarts). Needs a writable `/dev/input`
node: true on rooted BlueStacks (SELinux Disabled + shell in the `input`
group; no root process needed). The type-B (real-phone) path is implemented but
not live-verified.

`fastinput_check.py [serial]` prints the detected profile and taps reference
points to confirm.

## Where it is used

- All-Star Cup taps always go through `FastTapper` (`FAST_TAP_ENABLED`), since
  that mode is speed-critical.
- Other modes route taps through `FastTapper` only when `ROOT_FAST_INPUT` is on
  (default off). `Macro` / `EternalLodeMacro` wire `setup_fast_tap` / `close`
  into `run_macro` / `run_eternal_lode`.

## Humanized taps

`HUMANIZED_TAPS` (default off, only selectable when `ROOT_FAST_INPUT` is on)
adds human-like variation inside `FastTapper._humanized_line`, applied on the
device in a single shell write so `tap()` stays synchronous:
- gaussian position jitter (sigma 2 px, clamped to 5 px),
- randomized hold 60-120 ms,
- 1-3 micro-drift move events between down and up,
- 0-40 ms pre-tap delay.

The GUI warns that humanization slows All-Star (it applies everywhere, one
global behavior) and that turning it off restores raw speed.

## Config keys

`FAST_TAP_ENABLED`, `FAST_TAP_HOLD`, `FAST_TAP_TRANSFORM` (All-Star, unchanged),
`ROOT_FAST_INPUT`, `HUMANIZED_TAPS`. All persisted.
