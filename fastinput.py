"""
Low-latency touch injection via sendevent.

The normal tap path (`adb shell input tap x y`) spends ~35ms per tap: it boots
`app_process` (a JVM) and, on a cold call, opens a fresh adb connection.  For the
All-Star Cup mode, where the macro must tap a boss card the instant it is
detected, that latency is the dominant delay.

This module bypasses `input tap`.  It keeps ONE persistent `adb shell` open and
feeds it `sendevent` commands, which write input events straight to the device's
touchscreen node (for example `/dev/input/event4`).  A tap is one short write to
that shell plus the on-device cost of the `sendevent` calls, around 20ms total,
with no per-tap connection or JVM startup.

`tap()` is SYNCHRONOUS: it appends an `echo` marker and waits for the device to
echo it back before returning, so the tap has actually executed by the time the
call returns.  This is a faithful drop-in for the old blocking `adb input tap`:
when a spam loop is told to stop, tapping stops at once, with nothing queued to
keep firing on the device.  A fire-and-forget version (just writing and
returning) let a backlog build in the pipe and kept tapping after the loop
stopped, which broke the All-Star phase transitions.

Why sendevent and not raw bytes to the node: writing packed `input_event` structs
through `cat > <node>` is faster still, but adb can fragment a multi-event write
so the node receives a chunk that is not a whole number of events, and the kernel
rejects it with EINVAL (the `cat` writer then dies).  `sendevent` writes exactly
one 24-byte event per call, directly to the device fd, so it can never misalign.
This was validated live: raw multi-event writes died on certain coordinates;
sendevent survived hundreds of varying taps.

Requirements (all true on the user's rooted BlueStacks, validated live):
  - The shell user can write the event node.  BlueStacks runs with SELinux
    Disabled and the shell user in the `input` group, so the node is writable
    with no root.  A rooted phone with a permissive policy works too.
  - The touchscreen exposes ABS_MT_POSITION_X / ABS_MT_POSITION_Y.

If any part of setup fails, `available` stays False and the caller falls back to
the ordinary `ADBClient.tap`.  Nothing here is required for the macro to work; it
only makes taps faster when the device allows it.

Device profiles handled:
  - Type-A (BlueStacks Virtual Touch): only X/Y axes.  down = X, Y,
    SYN_MT_REPORT, SYN_REPORT; up = SYN_MT_REPORT, SYN_REPORT.
  - Type-B (real phones): ABS_MT_SLOT / ABS_MT_TRACKING_ID / BTN_TOUCH.

Coordinate transform:
  - "rotated": BlueStacks renders a portrait app on a landscape panel, so the
    touch grid is rotated 90 degrees from the screencap.  ev_x = (1 - sy/H)*xmax,
    ev_y = (sx/W)*ymax.  Validated by launching known launcher icons.
  - "direct": the touch grid matches the screen orientation (typical real
    phone).  ev_x = (sx/W)*xmax, ev_y = (sy/H)*ymax.
"""

from __future__ import annotations

import logging
import queue
import random
import re
import subprocess
import threading
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flash on Windows

# Marker the shell echoes back after a tap so we know it executed, and how long
# to wait for it.  The device taps in ~20ms; the timeout is a generous ceiling so
# a transient load spike does not raise (we just return, having paced the loop),
# while a genuinely hung shell cannot block the macro for long.
_ACK_MARKER = b"__ft_ack__"
_ACK_TIMEOUT = 0.4

# Linux input event type codes
_EV_SYN = 0x00
_EV_KEY = 0x01
_EV_ABS = 0x03

# Event codes
_SYN_REPORT = 0x00
_SYN_MT_REPORT = 0x02
_ABS_MT_SLOT = 0x2f
_ABS_MT_TOUCH_MAJOR = 0x30
_ABS_MT_POSITION_X = 0x35
_ABS_MT_POSITION_Y = 0x36
_ABS_MT_TRACKING_ID = 0x39
_ABS_MT_PRESSURE = 0x3a
_BTN_TOUCH = 0x14a

# sendevent cannot take a negative value; the type-B "lift" tracking id (-1) is
# sent as its unsigned 32-bit form.
_TRACKING_ID_UP = 4294967295

# ---- Humanization tuning (used only when the humanize flag is on) -------------
# A tap gets a small gaussian position jitter, a randomised press hold, a few
# intermediate "drift" move events between down and up, and a small random pause
# just before it fires.  These keep the injected taps from being pixel-perfect
# and metronome-timed.  The verdict from the interview was hold + jitter + timing
# only (no pressure curves / bezier paths), so these are all this needs.
_HUMAN_POS_SIGMA_PX  = 2.0     # gaussian stddev of the position jitter, in px
_HUMAN_POS_CLAMP_PX  = 5       # hard cap on the position jitter magnitude, in px
_HUMAN_HOLD_MIN      = 0.060   # shortest press hold (down -> up), seconds
_HUMAN_HOLD_MAX      = 0.120   # longest press hold, seconds
_HUMAN_DRIFT_MIN     = 1       # fewest intermediate move events during the hold
_HUMAN_DRIFT_MAX     = 3       # most intermediate move events during the hold
_HUMAN_DRIFT_PX      = 3       # max per-axis px offset of a drift move from target
_HUMAN_PRETAP_MIN    = 0.0     # shortest extra pause before a tap fires, seconds
_HUMAN_PRETAP_MAX    = 0.040   # longest extra pause before a tap fires, seconds


class FastTapError(Exception):
    """Raised when the fast-tap shell is unavailable or has failed."""


def _run(args, timeout=10):
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          creationflags=_NO_WINDOW)


class FastTapper:
    """Inject taps by feeding sendevent commands to a persistent adb shell.

    Lifecycle:
        tapper = FastTapper(adb_client)
        if tapper.setup(width, height, on_log=log):
            tapper.tap(sx, sy)      # screen pixels (screencap space)
        ...
        tapper.close()

    `tap` is thread-safe (a single lock serialises writes), so the macro's
    background spam thread can call it freely.  `available` is False whenever the
    fast path is not usable; callers should fall back to ADBClient.tap then.
    """

    def __init__(self, adb_client):
        self.adb = adb_client
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._tid = 0
        self.available = False
        # Per-writer ack plumbing: a daemon thread drains the shell's stdout and
        # puts one item per echoed marker, so tap() can wait for its tap to land.
        self._ack_q: Optional[queue.Queue] = None
        self._reader: Optional[threading.Thread] = None

        # Device profile (filled by setup()).
        self.node: Optional[str] = None
        self.name: Optional[str] = None
        self.type_b = False
        self.has_btn_touch = False
        self.has_slot = False
        self.has_pressure = False
        self.has_touch_major = False
        self.x_max = 0
        self.y_max = 0
        self.transform = "direct"
        self._w = 0
        self._h = 0
        # Optional extra press duration.  sendevent's own spacing (a few ms
        # between the down and up calls) already registers on BlueStacks, so the
        # default is 0.  A positive value inserts an on-device `sleep` between
        # down and up for devices that need a longer press.
        self._hold = 0.0
        # When True, tap() adds human-like variance (position jitter, randomised
        # hold, micro-drift move events, pre-tap timing jitter).  Set by setup()
        # from the caller's humanize flag; ignored on the adb.tap fallback path,
        # which is a plain tap.
        self._humanize = False

        # Self-healing: the persistent shell can occasionally be dropped (for
        # example when another adb shell command runs near it).  _write restarts
        # it transparently, rate-limited so a shell that will not stay up does not
        # thrash; callers just fall back to adb.tap meanwhile.
        self._last_restart = 0.0
        self._restart_cooldown = 0.5
        self.restarts = 0       # how many times the shell was transparently revived

    # ------------------------------------------------------------------
    # adb helpers (self-contained so this module does not depend on
    # ADBClient internals beyond the exe path and serial)
    # ------------------------------------------------------------------

    def _adb_base(self):
        base = [self.adb.adb_exe]
        if self.adb.device:
            base += ["-s", self.adb.device]
        return base

    def _shell_text(self, shell_cmd: str, timeout=10) -> str:
        out = _run(self._adb_base() + ["shell", shell_cmd], timeout=timeout)
        return out.stdout.decode(errors="replace")

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setup(self, width: int, height: int, on_log=None,
              enabled: Optional[bool] = None, humanize: bool = False) -> bool:
        """Probe the device, choose a transform, and start the shell.

        `enabled` is the caller's on/off decision for the fast path; when None it
        falls back to config.FAST_TAP_ENABLED.  This is how the two fast-tap
        toggles stay separate: All-Star passes config.FAST_TAP_ENABLED (its own
        gate, unchanged), while chapter / plant / eternal pass
        config.ROOT_FAST_INPUT.  `humanize` turns on per-tap human-like variance
        (see tap()); pass config.HUMANIZED_TAPS.

        Returns True if the fast path is ready, False to fall back to adb.tap.
        Never raises: any failure just disables the fast path.
        """
        say = on_log or (lambda *_: None)
        import config
        if enabled is None:
            enabled = getattr(config, "FAST_TAP_ENABLED", True)
        if not enabled:
            say("fast-tap: disabled in settings; using adb input tap")
            return False
        self._w, self._h = int(width), int(height)
        self._hold = float(getattr(config, "FAST_TAP_HOLD", 0.0))
        self._humanize = bool(humanize)
        try:
            if not self._probe_touch_device():
                say("fast-tap: no touchscreen event node found; using adb input tap")
                return False
            if not self._node_writable():
                say(f"fast-tap: {self.node} not writable (need root or the "
                    "`input` group + permissive SELinux); using adb input tap")
                return False
            self._choose_transform(config)
            self._start_writer()
        except Exception as exc:                       # noqa: BLE001
            say(f"fast-tap: setup failed ({exc}); using adb input tap")
            self._stop_writer()
            self.available = False
            return False
        self.available = True
        say(f"fast-tap: ON  node={self.node} name={self.name!r} "
            f"type={'B' if self.type_b else 'A'} "
            f"range=({self.x_max},{self.y_max}) transform={self.transform} "
            f"hold={self._hold * 1000:.0f}ms "
            f"humanize={'on' if self._humanize else 'off'}")
        return True

    def _probe_touch_device(self) -> bool:
        """Parse `getevent -pl` to find the touchscreen node and its profile."""
        text = self._shell_text("getevent -pl", timeout=10)
        best = None
        cur = None
        for raw in text.splitlines():
            m = re.match(r"add device \d+: (\S+)", raw)
            if m:
                if cur and cur.get("has_xy"):
                    best = self._prefer(best, cur)
                cur = {"node": m.group(1), "name": None, "x_max": 0,
                       "y_max": 0, "has_xy": False, "type_b": False,
                       "btn_touch": False, "slot": False, "pressure": False,
                       "touch_major": False}
                continue
            if cur is None:
                continue
            nm = re.match(r'\s*name:\s*"(.*)"', raw)
            if nm:
                cur["name"] = nm.group(1)
                continue
            if "ABS_MT_POSITION_X" in raw:
                cur["has_xy"] = True
                cur["x_max"] = self._parse_max(raw, cur["x_max"])
            elif "ABS_MT_POSITION_Y" in raw:
                cur["y_max"] = self._parse_max(raw, cur["y_max"])
            if "ABS_MT_TRACKING_ID" in raw or "ABS_MT_SLOT" in raw:
                cur["type_b"] = True
            if "ABS_MT_SLOT" in raw:
                cur["slot"] = True
            if "ABS_MT_PRESSURE" in raw:
                cur["pressure"] = True
            if "ABS_MT_TOUCH_MAJOR" in raw:
                cur["touch_major"] = True
            if "BTN_TOUCH" in raw:
                cur["btn_touch"] = True
        if cur and cur.get("has_xy"):
            best = self._prefer(best, cur)
        if not best:
            return False
        self.node = best["node"]
        self.name = best["name"]
        self.x_max = best["x_max"] or 32767
        self.y_max = best["y_max"] or 32767
        self.type_b = best["type_b"]
        self.has_btn_touch = best["btn_touch"]
        self.has_slot = best["slot"]
        self.has_pressure = best["pressure"]
        self.has_touch_major = best["touch_major"]
        return True

    @staticmethod
    def _parse_max(line: str, fallback: int) -> int:
        m = re.search(r"max\s+(-?\d+)", line)
        return int(m.group(1)) if m else fallback

    @staticmethod
    def _prefer(best, cand):
        """Prefer a device that looks like a touchscreen over a mouse/tablet.

        A name containing 'touch' wins; otherwise keep the first found."""
        if best is None:
            return cand
        bn = (best.get("name") or "").lower()
        cn = (cand.get("name") or "").lower()
        if "touch" in cn and "touch" not in bn:
            return cand
        return best

    def _node_writable(self) -> bool:
        out = self._shell_text(
            f"test -w {self.node} && echo __OK__ || echo __NO__")
        return "__OK__" in out

    def _choose_transform(self, config):
        mode = getattr(config, "FAST_TAP_TRANSFORM", "auto")
        if mode in ("rotated", "direct"):
            self.transform = mode
            return
        # auto: BlueStacks renders portrait apps rotated on a landscape panel and
        # uses a square normalised grid; a real phone touch grid matches the
        # screen orientation.
        name = (self.name or "").lower()
        square = self.x_max == self.y_max and self.x_max > 0
        if "bluestacks" in name or square:
            self.transform = "rotated"
        else:
            self.transform = "direct"

    # ------------------------------------------------------------------
    # Shell process
    # ------------------------------------------------------------------

    def _start_writer(self):
        """Open one persistent `adb shell` we feed sendevent commands to.

        stdout is piped and drained by a reader thread so tap() can wait for the
        per-tap echo marker (stderr is merged in so a sendevent error does not
        block the pipe)."""
        proc = subprocess.Popen(
            self._adb_base() + ["shell"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, creationflags=_NO_WINDOW)
        time.sleep(0.12)
        if proc.poll() is not None:
            raise FastTapError("adb shell exited immediately")
        self._proc = proc
        self._ack_q = queue.Queue()
        self._reader = threading.Thread(
            target=self._reader_loop, args=(proc, self._ack_q),
            name="all-star-fasttap-reader", daemon=True)
        self._reader.start()

    @staticmethod
    def _reader_loop(proc, ack_q):
        """Drain the shell's stdout; put one item per echoed ack marker."""
        try:
            for line in iter(proc.stdout.readline, b""):
                if _ACK_MARKER in line:
                    ack_q.put(1)
        except Exception:                              # noqa: BLE001
            pass

    def _maybe_restart(self) -> bool:
        """Bring the shell back, at most once per cooldown.

        Returns True if a fresh shell is running.  Returns False if we are within
        the cooldown (caller falls back to adb.tap for this tap but the fast path
        stays enabled for later) or if the shell will not start (fast path
        disabled).
        """
        now = time.time()
        if now - self._last_restart < self._restart_cooldown:
            return False
        self._last_restart = now
        self._stop_writer()
        try:
            self._start_writer()
            self.restarts += 1
            return True
        except Exception:                              # noqa: BLE001
            self.available = False
            return False

    def _stop_writer(self):
        proc, self._proc = self._proc, None
        self._ack_q = None
        self._reader = None        # the reader thread exits on stdout EOF below
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:                              # noqa: BLE001
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:                              # noqa: BLE001
            try:
                proc.kill()
            except Exception:                          # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Event / command building
    # ------------------------------------------------------------------

    def _to_event(self, sx: int, sy: int) -> Tuple[int, int]:
        sx = min(max(0, int(sx)), self._w - 1)
        sy = min(max(0, int(sy)), self._h - 1)
        if self.transform == "rotated":
            ex = round((1.0 - sy / self._h) * self.x_max)
            ey = round((sx / self._w) * self.y_max)
        else:
            ex = round((sx / self._w) * self.x_max)
            ey = round((sy / self._h) * self.y_max)
        ex = min(max(0, ex), self.x_max)
        ey = min(max(0, ey), self.y_max)
        return ex, ey

    def _down_events(self, ex: int, ey: int):
        if not self.type_b:
            return [(_EV_ABS, _ABS_MT_POSITION_X, ex),
                    (_EV_ABS, _ABS_MT_POSITION_Y, ey),
                    (_EV_SYN, _SYN_MT_REPORT, 0),
                    (_EV_SYN, _SYN_REPORT, 0)]
        self._tid = (self._tid % 65535) + 1
        ev = []
        if self.has_slot:
            ev.append((_EV_ABS, _ABS_MT_SLOT, 0))
        ev.append((_EV_ABS, _ABS_MT_TRACKING_ID, self._tid))
        if self.has_btn_touch:
            ev.append((_EV_KEY, _BTN_TOUCH, 1))
        ev.append((_EV_ABS, _ABS_MT_POSITION_X, ex))
        ev.append((_EV_ABS, _ABS_MT_POSITION_Y, ey))
        if self.has_touch_major:
            ev.append((_EV_ABS, _ABS_MT_TOUCH_MAJOR, 6))
        if self.has_pressure:
            ev.append((_EV_ABS, _ABS_MT_PRESSURE, 50))
        ev.append((_EV_SYN, _SYN_REPORT, 0))
        return ev

    def _up_events(self):
        if not self.type_b:
            return [(_EV_SYN, _SYN_MT_REPORT, 0), (_EV_SYN, _SYN_REPORT, 0)]
        ev = []
        if self.has_slot:
            ev.append((_EV_ABS, _ABS_MT_SLOT, 0))
        ev.append((_EV_ABS, _ABS_MT_TRACKING_ID, _TRACKING_ID_UP))
        if self.has_btn_touch:
            ev.append((_EV_KEY, _BTN_TOUCH, 0))
        ev.append((_EV_SYN, _SYN_REPORT, 0))
        return ev

    def _move_events(self, ex: int, ey: int):
        """Mid-touch position update (a drift move during the hold), reusing the
        active contact.  Type A re-reports X/Y then SYN_MT_REPORT/SYN_REPORT;
        type B updates the slot's X/Y (no new tracking id) then SYN_REPORT."""
        if not self.type_b:
            return [(_EV_ABS, _ABS_MT_POSITION_X, ex),
                    (_EV_ABS, _ABS_MT_POSITION_Y, ey),
                    (_EV_SYN, _SYN_MT_REPORT, 0),
                    (_EV_SYN, _SYN_REPORT, 0)]
        ev = []
        if self.has_slot:
            ev.append((_EV_ABS, _ABS_MT_SLOT, 0))
        ev.append((_EV_ABS, _ABS_MT_POSITION_X, ex))
        ev.append((_EV_ABS, _ABS_MT_POSITION_Y, ey))
        ev.append((_EV_SYN, _SYN_REPORT, 0))
        return ev

    def _cmd(self, events) -> str:
        node = self.node
        return "".join(f"sendevent {node} {t} {c} {v};" for (t, c, v) in events)

    def _write(self, line: str):
        data = line.encode()
        proc = self._proc
        if proc is None or proc.poll() is not None:
            if not self._maybe_restart():
                raise FastTapError("fast-tap shell is not running")
            proc = self._proc
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            if self._maybe_restart():
                try:
                    self._proc.stdin.write(data)
                    self._proc.stdin.flush()
                    return
                except (BrokenPipeError, OSError):
                    pass
            raise FastTapError(f"fast-tap shell pipe broke: {exc}") from exc

    # ------------------------------------------------------------------
    # Public tap API
    # ------------------------------------------------------------------

    def tap(self, sx: int, sy: int):
        """Tap at (sx, sy) in screencap pixels, and BLOCK until it has executed.

        Sends the press and release as one batch of sendevent calls (with an
        optional on-device hold between them), then waits for the shell to echo
        an ack marker so the tap has actually landed before returning.  This
        keeps the call synchronous like the old `adb input tap`, so a spam loop
        never builds a backlog and stops tapping the instant it is told to.

        When humanization is on (see setup(humanize=...)) the tap instead gets a
        small gaussian position jitter, a randomised 60-120ms hold with 1-3
        intermediate micro-drift move events, and a small random pre-tap pause,
        so the injected taps are not pixel-perfect or metronome-timed.

        Raises FastTapError if the shell has failed; the caller should then fall
        back to adb.tap.
        """
        if self._humanize:
            line = self._humanized_line(sx, sy)
        else:
            ex, ey = self._to_event(sx, sy)
            line = self._cmd(self._down_events(ex, ey))
            if self._hold > 0:
                line += f"sleep {self._hold:.3f};"
            line += self._cmd(self._up_events())
        line += "echo " + _ACK_MARKER.decode() + "\n"
        with self._lock:
            # Drop any stale ack from a prior tap that timed out, so we wait for
            # THIS tap's marker and not an old one.
            q = self._ack_q
            if q is not None:
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
            self._write(line)
            q = self._ack_q            # may be a fresh queue if _write restarted
            if q is not None:
                try:
                    q.get(timeout=_ACK_TIMEOUT)
                except queue.Empty:
                    pass               # best effort: the tap most likely landed

    def _humanized_line(self, sx: int, sy: int) -> str:
        """Build the sendevent command line for one human-like tap.

        Position gets a clamped gaussian jitter; the press is held for a random
        60-120ms with 1-3 intermediate drift move events, each nudged a couple of
        px off the (jittered) target; and a small pre-tap pause is inserted on the
        device before the press.  All timing is expressed as on-device `sleep`
        commands so the whole tap is still one write to the shell.  The ack marker
        is appended by the caller.
        """
        # Position jitter around the screen-space target, clamped, then mapped to
        # event space (the transform + bounds clamp live in _to_event).
        def jitter(v):
            off = int(round(random.gauss(0.0, _HUMAN_POS_SIGMA_PX)))
            off = max(-_HUMAN_POS_CLAMP_PX, min(_HUMAN_POS_CLAMP_PX, off))
            return v + off

        tx, ty = jitter(sx), jitter(sy)
        ex, ey = self._to_event(tx, ty)

        pretap = random.uniform(_HUMAN_PRETAP_MIN, _HUMAN_PRETAP_MAX)
        hold = random.uniform(_HUMAN_HOLD_MIN, _HUMAN_HOLD_MAX)
        n_drift = random.randint(_HUMAN_DRIFT_MIN, _HUMAN_DRIFT_MAX)

        line = ""
        if pretap > 0:
            line += f"sleep {pretap:.3f};"
        line += self._cmd(self._down_events(ex, ey))
        # Split the hold into n_drift+1 slices, emitting a drifted move between
        # each.  Each drift is a few px off the jittered target (in screen space
        # so it respects the transform), never leaving the tap area.
        slice_hold = hold / (n_drift + 1)
        for _ in range(n_drift):
            line += f"sleep {slice_hold:.3f};"
            dx = tx + random.randint(-_HUMAN_DRIFT_PX, _HUMAN_DRIFT_PX)
            dy = ty + random.randint(-_HUMAN_DRIFT_PX, _HUMAN_DRIFT_PX)
            mex, mey = self._to_event(dx, dy)
            line += self._cmd(self._move_events(mex, mey))
        line += f"sleep {slice_hold:.3f};"
        line += self._cmd(self._up_events())
        return line

    def release(self):
        """Send a touch-up (best effort), in case a press was left outstanding."""
        try:
            with self._lock:
                self._write(self._cmd(self._up_events()) + "\n")
        except Exception:                              # noqa: BLE001
            pass

    def close(self):
        self.release()
        self._stop_writer()
        self.available = False
