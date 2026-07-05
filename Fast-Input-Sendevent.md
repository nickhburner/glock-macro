# Fast Input: sendevent tapping

Goal: minimise tap latency for the All-Star Cup mode. The boss card must be
tapped the instant it is detected, and `adb shell input tap` (about 35ms per
tap) was the dominant delay. This documents the validated solution and the live
measurements behind it. (Project style: no em dashes.)

## TL;DR

`fastinput.FastTapper` keeps ONE persistent `adb shell` open and feeds it
`sendevent` commands. A tap is six sendevent calls plus an `echo` ack the shell
sends back, about 25ms total, versus ~35ms for `adb shell input tap` (which boots
`app_process` and, cold, opens a connection). There is no per-tap connection
setup or JVM boot. It auto-detects the device and falls back to `adb input tap`
if the fast path cannot be set up.

`tap()` is SYNCHRONOUS: it waits for the ack before returning, so the tap has
actually executed. This is deliberate and a faithful drop-in for the old blocking
`input tap` (see "Synchronous, not fire-and-forget" below).

## Why this is now possible (it was not before)

The earlier `BlueStacks-Input-Investigation.md` ruled out raw `/dev/input`
injection because, on an UNROOTED device, SELinux blocks the `shell` domain from
writing input nodes. The user has since ROOTED BlueStacks. Live probe results:

- `getenforce` -> **Disabled** (no SELinux enforcement at all).
- `id` for the shell user -> uid 2000, member of group **`input` (gid 1004)**.
- `/dev/input/event4` perms -> `crw-rw---- root input`, and `test -w` -> writable.

So the shell user can write the touch node directly. `adb root` does not even
stick on this BlueStacks (adbd stays `shell`), and it is not needed.

## Device profile (BlueStacks, validated)

From `getevent -pl`:

```
/dev/input/event4  "BlueStacks Virtual Touch"
  ABS_MT_POSITION_X  min 0 max 32767
  ABS_MT_POSITION_Y  min 0 max 32767
```

Type-A single touch: ONLY X and Y axes. No ABS_MT_TRACKING_ID, no ABS_MT_SLOT,
no BTN_TOUCH, no pressure. Screencap is 540x960 portrait; `wm size` reports the
physical landscape panel 960x540.

## Coordinate transform (90 degree rotation)

BlueStacks renders the portrait app on a landscape panel, so the touch grid is
rotated relative to the screencap. For screen pixel (sx, sy) in a WxH frame:

```
ev_x = round((1 - sy / H) * 32767)
ev_y = round((sx / W) * 32767)
```

Confirmed by launching known launcher icons at predicted coordinates: Store
(top-left) and Egg Inc (top-right) both opened. `FastTapper` selects this
"rotated" transform for BlueStacks automatically and uses a "direct"
(`ev_x = sx/W*xmax`, `ev_y = sy/H*ymax`) transform otherwise.

## Tap protocol (type-A)

down: `ABS_MT_POSITION_X x`, `ABS_MT_POSITION_Y y`, `SYN_MT_REPORT (0 2 0)`,
`SYN_REPORT (0 0 0)`.
up:   `SYN_MT_REPORT (0 2 0)`, `SYN_REPORT (0 0 0)`.

Sent as one batch of six `sendevent` calls per tap. The few ms of spacing between
the down and up calls is enough for BlueStacks to register the press (verified by
launching apps). `FAST_TAP_HOLD` can insert an explicit on-device `sleep` between
down and up if a device ever needs a longer press; the default 0 is fine here.

## Why sendevent and NOT raw bytes to the node

The faster-looking option is to write packed `struct input_event` bytes straight
to `/dev/input/event4` through a persistent `cat > <node>` (about 0.4ms per tap,
no process spawn). It was implemented and benchmarked, but it is NOT reliable:

- adb fragments a multi-event write (for example a 96 byte, 4-event "down"). The
  device-side `cat` then writes a chunk to the evdev node that is not a whole
  number of 24-byte events, the kernel rejects it with EINVAL, and `cat` dies
  (`cat: xwrite: Invalid argument`).
- This reproduced deterministically: a fixed coordinate survived thousands of
  taps, but tapping at VARYING coordinates (which real input always does) killed
  the writer within a tap or two.

`sendevent` avoids this entirely: each call does one `write(fd, &ev, 24)` directly
to the device fd, so the kernel always receives exactly one whole event and can
never misalign. Live test: 200 varying-coordinate taps with zero writer deaths.
The speed cost (about 25ms vs 0.4ms per tap) buys reliability, and a tap is still
faster than `input tap`.

## Synchronous, not fire-and-forget

The first cut made `tap()` fire-and-forget: write the sendevent batch to the
shell pipe and return immediately (under 1ms). That LOOKED great but broke the
All-Star phase transitions. Because the call returned before the device executed
the tap, a spam loop running every ~40ms could enqueue faster than the device
drained, building a backlog in the pipe. When a phase ended and the spam thread
was stopped, the device KEPT executing the queued taps, so it "kept spam tapping"
after a boss was selected, and during a scan a backlog of empty-spot taps could
disrupt the boss screen settling and detection.

The fix: `tap()` appends `echo __ft_ack__` and waits (with a 0.4s ceiling) for a
reader thread to see that marker come back on the shell's stdout. So the call
blocks until the tap has executed, exactly like the old blocking `adb input tap`.
No backlog can build, and when `_spam` is told to stop, tapping stops at once.
Verified: stopping a 1s spam phase returns in ~5ms with nothing left queued.

## Measurements (live, server port 5038, device 127.0.0.1:5555)

| method | per tap | notes |
|---|---|---|
| `adb shell input tap` | ~35ms, blocking | boots app_process + connection |
| raw write to persistent `cat > node` | ~0.4ms host | FAST but FRAGILE (EINVAL deaths) -> rejected |
| **synchronous persistent-shell `sendevent`** | ~25ms, blocking | robust (0 deaths over the varying pattern), no backlog |

~25ms is six sendevent calls plus the echo round-trip. Still faster than
`input tap` and, unlike it, no per-tap connection or JVM cost.

## Implementation

- `fastinput.py` `FastTapper`:
  - `setup(w, h)`: parse `getevent -pl` for the touch node + ranges + type, check
    the node is writable, choose the transform, start the persistent `adb shell`.
  - `tap(sx, sy)`: write one batch of sendevent calls (down + up) plus an `echo`
    ack, then block until a reader thread sees the ack on stdout. Thread-safe.
  - `release()` / `close()`: send a touch-up so a press is never left held.
  - Self-healing: if the shell is dropped, `_write` restarts it (rate-limited);
    any failure -> `available = False`, caller falls back to `adb.tap`.
- `all_star.py`: `AllStarMacro` creates the tapper, sets it up lazily in
  `_ensure_geometry` (needs the live frame W/H), and `_tap()` uses it when
  available (logging the adb.tap fallback only once per failure episode).
  `run_all_star`'s finally calls `macro.close()`.
- `config.py`: `FAST_TAP_ENABLED` (True), `FAST_TAP_HOLD` (0.0),
  `FAST_TAP_TRANSFORM` ("auto"/"rotated"/"direct"), all persisted.
- `fastinput_check.py [serial]`: prints the detected profile and taps reference
  points (saving marked before/after screenshots) to verify on any device.

## Stability notes

- With the capture stream running (the real run always has it), the persistent
  shell is stable: 250 taps, 0 drops. A stray one-shot `adb shell` command (for
  example `input keyevent`, which the macro never issues during a run) can briefly
  drop the persistent shell; self-healing restarts it, and adb.tap covers any gap.
- The capture stream relaunches its own `screenrecord` shell every ~170s. If that
  ever drops the tap shell, self-healing handles it; All-Star runs are short
  anyway.

## Real-phone (type-B) status

`FastTapper` implements the standard type-B sequence (ABS_MT_SLOT /
ABS_MT_TRACKING_ID / BTN_TOUCH, "direct" transform), but only BlueStacks was
connected for testing, so the phone path is UNVERIFIED. The Pixel
(serial 59100DLCH002FP, 1080x2410) touch node `/dev/input/event1` is type-B,
X max 12799, Y max 28559. On a phone, run `fastinput_check.py` first; if taps
miss, adjust `FAST_TAP_TRANSFORM` or set `FAST_TAP_ENABLED=False`.
