"""
Fast, black-frame-proof capture via Android `screenrecord` (H.264 stream).

Two problems this solves over `adb exec-out screencap`:
  1. On some BlueStacks setups screencap returns all-black frames during active
     gameplay, because nothing is consuming the display surface so the emulator
     stops compositing it to a readable buffer.
  2. screencap is slow (a fresh PNG encode + transfer per frame, ~0.6-1s).

A continuous `screenrecord` stream is itself a display consumer (keeps the
surface composited -> no black frames) AND yields already-decoded frames on
demand at ~native fps. The macro reads the latest frame instead of taking a
screenshot each poll.

Requires PyAV (`av`) for H.264 decode. Streaming is REQUIRED when enabled:
the macro refuses to start without it and never silently degrades to screencap.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

import numpy as np

try:
    import av  # PyAV: ffmpeg bindings for incremental H.264 decode
    try:
        # Quiet ffmpeg's own logging: the --windowed build has no stderr for it
        # to write to, and stream startup emits benign "missing picture" noise.
        av.logging.set_level(av.logging.ERROR)
    except Exception:
        pass
    _AV_IMPORT_ERROR = None
except Exception as _exc:                       # pragma: no cover - env dependent
    av = None
    _AV_IMPORT_ERROR = _exc

log = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW
_SEGMENT_SECONDS = 170           # screenrecord hard-caps at 180s; restart before
_DEFAULT_BITRATE = 4_000_000     # plenty for template matching, light on the bus


class _PipeReader:
    """Read-only, non-seekable wrapper around a subprocess pipe.

    PyAV inspects the file object and, if it exposes `seek`, will try to fseek it
    while probing -- which raises `OSError [Errno 22]` on a live pipe and kills
    the stream. A plain `read`-only object (no `seek`) forces PyAV into pure
    forward-streaming mode, which is what a screenrecord pipe is.
    """

    def __init__(self, fileobj):
        self._f = fileobj

    def read(self, size):
        return self._f.read(size)

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


def streaming_available() -> bool:
    """True if PyAV imported, so an H.264 stream can be decoded."""
    return av is not None


def unavailable_reason() -> str:
    return "" if av is not None else f"PyAV not available ({_AV_IMPORT_ERROR})"


def open_stream(adb_exe: str, serial: Optional[str], attempts: int = 3,
                per_attempt_timeout: float = 4.0, on_log=None
                ) -> Optional["ScreenRecordStream"]:
    """Create, start and wait for a stream to decode its first frame, retrying a
    full teardown+relaunch up to `attempts` times.

    `screenrecord` can occasionally launch but never emit a decodable frame (a
    transient encoder hiccup, the device just waking, or USB renegotiation).
    Rather than silently degrade to slow per-poll screencap, we relaunch the
    whole stream a few times. Returns a ready stream, or None if no attempt
    produced a frame -- the caller decides whether that is fatal.
    """
    if not streaming_available():
        return None

    def _say(msg: str) -> None:
        if on_log is not None:
            on_log(msg)
        else:
            log.info(msg)

    for attempt in range(1, attempts + 1):
        s = ScreenRecordStream(adb_exe, serial)
        try:
            s.start()
            if s.wait_ready(timeout=per_attempt_timeout):
                if attempt > 1:
                    _say(f"capture: stream connected on attempt {attempt}")
                return s
        except Exception as exc:                    # pragma: no cover - env dependent
            _say(f"capture: stream attempt {attempt}/{attempts} error: {exc}")
        s.stop()
        if attempt < attempts:
            _say(f"capture: no stream frame in {per_attempt_timeout:.0f}s, "
                 f"retrying ({attempt}/{attempts})")
    return None


class ScreenRecordStream:
    """Background `screenrecord` -> H.264 -> latest decoded BGR frame.

    Usage::

        s = ScreenRecordStream(adb_exe, "127.0.0.1:5555")
        s.start()
        s.wait_ready(timeout=6)
        frame = s.latest(max_age=3.0)   # BGR ndarray or None
        ...
        s.stop()

    Thread-safe: one decode thread writes the latest frame, readers get the most
    recent one. Frames are never mutated in place (each is a fresh ndarray), so
    `latest()` can hand back the reference without copying.
    """

    def __init__(self, adb_exe: str, serial: Optional[str],
                 bitrate: int = _DEFAULT_BITRATE):
        self._adb = adb_exe
        self._serial = serial
        self._bitrate = bitrate

        self._frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None

        self.frames_seen = 0
        self.restarts = 0
        self._last_frame_time = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not streaming_available():
            raise RuntimeError(unavailable_reason())
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="screenrecord-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_proc()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def wait_ready(self, timeout: float = 6.0) -> bool:
        """Block until the first frame is decoded (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.latest() is not None:
                return True
            if self._stop.is_set():
                return False
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------------
    # Frame access
    # ------------------------------------------------------------------

    def latest(self, max_age: Optional[float] = None) -> Optional[np.ndarray]:
        """Most recent frame, or None if none yet / older than `max_age` sec.

        A `max_age` guard means a stalled or restarting stream returns None so
        the caller can detect the gap and attempt recovery.
        """
        with self._lock:
            if self._frame is None:
                return None
            if max_age is not None and (time.time() - self._last_frame_time) > max_age:
                return None
            return self._frame

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cmd(self) -> list:
        base = [self._adb]
        if self._serial:
            base += ["-s", self._serial]
        # Device-default size: the macro normalises to MATCH_WIDTH itself, and a
        # forced --size can be rejected by the AVC encoder on odd resolutions.
        return base + [
            "exec-out", "screenrecord",
            "--output-format=h264",
            f"--time-limit={_SEGMENT_SECONDS}",
            f"--bit-rate={self._bitrate}",
            "-",
        ]

    def _run(self) -> None:
        first_segment = True
        consec_errors = 0
        while not self._stop.is_set():
            if not first_segment:
                self.restarts += 1
            first_segment = False
            container = None
            got_frame = False
            try:
                self._proc = subprocess.Popen(
                    self._cmd(), stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
                container = av.open(
                    _PipeReader(self._proc.stdout), format="h264", mode="r")
                for frame in container.decode(video=0):
                    if self._stop.is_set():
                        break
                    img = frame.to_ndarray(format="bgr24")
                    with self._lock:
                        self._frame = img
                        self.frames_seen += 1
                        self._last_frame_time = time.time()
                    got_frame = True
                    consec_errors = 0
            except Exception as exc:
                log.warning("screenrecord stream error: %s", exc)
                if not self._stop.is_set():
                    consec_errors += 1
                    if consec_errors >= 6:
                        log.warning("screenrecord: %d consecutive errors, "
                                    "giving up (caller will handle recovery)",
                                    consec_errors)
                        break
                    time.sleep(0.5)
            else:
                if got_frame:
                    consec_errors = 0
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:
                        pass
                self._kill_proc()
            # Loop: the segment ended (time-limit) -> start the next one. The
            # ~1s relaunch gap is bridged by the last frame's max_age.

    def _kill_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
