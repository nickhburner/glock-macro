"""
ADB abstraction layer.

All communication with the Android device (BlueStacks emulator or real phone
via scrcpy/USB) goes through this module. Nothing here uses pyautogui or any
screen-coordinate concept -- all coordinates are in the device's native
display space.

Usage:
    adb = ADBClient()           # auto-locate adb binary
    adb.connect("localhost:5555")
    frame = adb.screenshot()    # BGR numpy array at phone native resolution
    adb.tap(540, 960)
"""

from __future__ import annotations

import os
import os
import subprocess
import time
import logging
import re
import shutil
import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Subprocess helpers
# ------------------------------------------------------------------

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW -- suppresses console flash on Windows

_DEFAULT_TIMEOUT = 10    # seconds for most adb commands
_SCREENCAP_TIMEOUT = 15  # screencap can take longer on slower devices


def _run(args: list[str], timeout: int = _DEFAULT_TIMEOUT,
         capture_output: bool = True) -> subprocess.CompletedProcess:
    """Run a command, suppress the console window on Windows."""
    return subprocess.run(
        args,
        capture_output=capture_output,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def _run_raw(args: list[str], timeout: int = _SCREENCAP_TIMEOUT) -> bytes:
    """Run a command and return raw stdout bytes (for screencap)."""
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )
    return result.stdout


# ------------------------------------------------------------------
# ADB binary discovery
# ------------------------------------------------------------------

def _sdk_adb_candidates() -> list:
    """Likely locations of the Android SDK platform-tools adb.exe."""
    cands = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if root:
            cands.append(Path(root) / "platform-tools" / "adb.exe")
    for env in ("LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(env)
        if not base:
            continue
        sdk = Path(base)
        if env == "USERPROFILE":
            sdk = sdk / "AppData" / "Local"
        cands.append(sdk / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    return cands


def find_adb(config_path: str = "adb") -> str:
    """
    Locate the adb binary. Search order (modern adb first, on purpose):
      1. config_path (ADB_PATH from settings -- may be a full path)
      2. Android SDK platform-tools adb.exe
      3. System PATH
      4. Next to a running scrcpy.exe (scrcpy bundles adb.exe)
      5. BlueStacks's HD-Adb.exe  (LAST: it is adb 1.0.36, which kills and
         restarts newer adb servers -- the "version war" that makes the
         emulator go offline and `wm size` return empty. Only fall back to it
         when no modern adb exists.)

    Returns the executable string to use in subprocess calls.
    Raises RuntimeError if adb cannot be found anywhere.
    """
    # 1. Configured path
    if config_path and config_path != "adb":
        p = Path(config_path)
        if p.is_file():
            log.debug("adb found at configured path: %s", p)
            return str(p)

    # 2. Android SDK platform-tools (the modern adb; required by Android
    #    emulators and strictly newer than BlueStacks's HD-Adb).
    for p in _sdk_adb_candidates():
        if p.is_file():
            log.debug("adb found (Android SDK platform-tools): %s", p)
            return str(p)

    # 3. System PATH
    on_path = shutil.which("adb")
    if on_path:
        log.debug("adb found on PATH: %s", on_path)
        return on_path

    # 4. Next to running scrcpy.exe
    try:
        result = _run(
            ["tasklist", "/FI", "IMAGENAME eq scrcpy.exe", "/FO", "CSV", "/NH"],
            timeout=5,
        )
        if result.returncode == 0 and b"scrcpy.exe" in result.stdout:
            # Find scrcpy.exe path via wmic
            wmic = _run(
                ["wmic", "process", "where", "name='scrcpy.exe'",
                 "get", "ExecutablePath", "/VALUE"],
                timeout=5,
            )
            for line in wmic.stdout.decode(errors="replace").splitlines():
                if line.startswith("ExecutablePath="):
                    scrcpy_path = Path(line.split("=", 1)[1].strip())
                    candidate = scrcpy_path.parent / "adb.exe"
                    if candidate.is_file():
                        log.debug("adb found next to scrcpy: %s", candidate)
                        return str(candidate)
    except Exception:
        pass

    # 5. BlueStacks HD-Adb.exe (last resort -- see docstring)
    bs_candidates = [
        Path(r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe"),
        Path(r"C:\Program Files (x86)\BlueStacks\HD-Adb.exe"),
        Path(r"C:\ProgramData\BlueStacks\HD-Adb.exe"),
        Path(r"C:\ProgramData\BlueStacks_nxt\HD-Adb.exe"),
    ]
    for p in bs_candidates:
        if p.is_file():
            log.debug("adb found (BlueStacks HD-Adb -- old 1.0.36): %s", p)
            return str(p)

    raise RuntimeError(
        "adb not found. Install adb (comes with scrcpy or BlueStacks) and "
        "either add it to PATH or set ADB_PATH in settings."
    )


# ------------------------------------------------------------------
# Device listing and type detection
# ------------------------------------------------------------------

def _classify_serial(serial: str) -> str:
    """Return a human-readable type hint for a device serial."""
    if re.match(r"^(localhost|127\.0\.0\.1|emulator).*", serial):
        return "BlueStacks/emulator"
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$", serial):
        return "network device"
    return "USB phone"


# BlueStacks (and other TCP emulators) expose ADB on a localhost port that
# `adb devices` does NOT auto-detect -- you must `adb connect 127.0.0.1:<port>`
# first (USB phones and console emulators auto-detect; TCP ones don't). We read
# the port(s) from BlueStacks' own config, plus common defaults, and connect to
# them during discovery so the user never has to touch a terminal.

_COMMON_EMULATOR_PORTS = (5555, 5565, 5575, 5585)


def _bluestacks_confs() -> list:
    """Every bluestacks.conf across BlueStacks editions, found by globbing
    %ProgramData%\\BlueStacks* (covers BlueStacks_nxt / _X / 4 / future names,
    and any install drive -- the conf always lives in ProgramData)."""
    out = []
    pd = os.environ.get("ProgramData", r"C:\ProgramData")
    try:
        for d in Path(pd).glob("BlueStacks*"):
            conf = d / "bluestacks.conf"
            if conf.is_file():
                out.append(conf)
    except OSError:
        pass
    return out


def _bluestacks_adb_ports() -> list:
    """ADB ports to probe. Prefer the real port(s) from each user's own
    BlueStacks config (handles per-version/per-instance ports, so we probe just
    those and discovery stays fast); fall back to common defaults only when no
    config is found.  Note: the host is always 127.0.0.1 (loopback) -- BlueStacks
    runs locally, so this never depends on the user's network or region."""
    cfg = set()
    for conf in _bluestacks_confs():
        try:
            text = conf.read_text(errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'adb_port="?(\d+)"?', text):
            cfg.add(int(m.group(1)))
    return sorted(cfg) if cfg else list(_COMMON_EMULATOR_PORTS)


def connect_emulators(adb_exe: str) -> None:
    """`adb connect` to known localhost emulator ADB ports (BlueStacks etc.) so
    TCP emulators appear in `adb devices` without a manual connect. Runs the
    probes in parallel (a closed port can take ~1s to refuse) and is
    best-effort: failures are ignored. Already-connected ports return instantly,
    so repeat refreshes are fast."""
    def _try(port):
        try:
            _run([adb_exe, "connect", f"127.0.0.1:{port}"], timeout=3)
        except Exception:
            pass

    threads = [threading.Thread(target=_try, args=(p,), daemon=True)
               for p in _bluestacks_adb_ports()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3.5)


def list_devices(adb_exe: str, discover: bool = True) -> list[dict]:
    """
    Return a list of dicts describing each device visible to adb:
      {"serial": str, "state": str, "type": str, "label": str}

    state is "device" (ready), "offline", "unauthorized", etc.

    When `discover` (default), first `adb connect` to known localhost emulator
    ports so BlueStacks (TCP) shows up without the user running adb by hand.
    """
    if discover:
        connect_emulators(adb_exe)
    try:
        result = _run([adb_exe, "devices"], timeout=8)
    except Exception as exc:
        log.warning("adb devices failed: %s", exc)
        return []

    devices = []
    for line in result.stdout.decode(errors="replace").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        device_type = _classify_serial(serial)
        label = f"{serial} ({device_type})"
        if state != "device":
            label += f" [{state}]"
        devices.append({
            "serial": serial,
            "state": state,
            "type": device_type,
            "label": label,
        })
    return devices


def choose_server_port(adb_exe: Optional[str] = None) -> Optional[int]:
    """Decide which adb SERVER port this process should use and pin it via the
    ANDROID_ADB_SERVER_PORT env var (all adb subprocesses inherit it).

    Prefer the isolated port (``config.ADB_SERVER_PORT``), which sidesteps the
    BlueStacks "version war": BlueStacks ships an old HD-Adb (1.0.36) that squats
    on the shared default port 5037 and gets killed+restarted by the modern SDK
    adb on every command, briefly dropping USB phones from ``adb devices``. Our
    own server on a private port never fights it.

    Self-healing safety net: a USB device is exclusive to one adb server, so if
    the isolated server sees nothing while the system default server DOES own a
    device, share the system server instead -- otherwise we would be blind to a
    phone something else already holds. (With BlueStacks-only or USB-driven-by-us
    setups nothing else holds the phone, so the isolated port simply wins.)

    Returns the chosen port, or None when sharing the system default server.
    """
    import os
    import config
    pref = getattr(config, "ADB_SERVER_PORT", None)
    if not pref:
        os.environ.pop("ANDROID_ADB_SERVER_PORT", None)
        return None
    if adb_exe is None:
        adb_exe = find_adb(getattr(config, "ADB_PATH", "adb"))

    # 1) Try the isolated port. discover=True so a BlueStacks instance (TCP) is
    #    connected onto this server and shows up too.
    os.environ["ANDROID_ADB_SERVER_PORT"] = str(pref)
    if list_devices(adb_exe, discover=True):
        return int(pref)

    # 2) Isolated server is empty -- does the system default server own a device
    #    (e.g. another adb client already grabbed the USB phone)? If so, share it.
    os.environ.pop("ANDROID_ADB_SERVER_PORT", None)
    if list_devices(adb_exe, discover=True):
        log.warning("adb: a device is owned by the system adb server on the "
                    "default port; sharing it instead of isolated port %s.", pref)
        return None

    # 3) Nothing visible anywhere -- keep the isolated, war-proof port; a device
    #    will appear on it once connected.
    os.environ["ANDROID_ADB_SERVER_PORT"] = str(pref)
    return int(pref)


# ------------------------------------------------------------------
# Black frame detection
# ------------------------------------------------------------------

_BLACK_THRESHOLD = 8    # per-channel mean below this -> frame is black
_BLACK_FRACTION = 0.98  # fraction of pixels that must be near-black


def is_black_frame(frame: np.ndarray) -> bool:
    """
    Return True if the image is all-black or near-black (phone screen off,
    device disconnected, or screencap returned garbage).
    """
    if frame is None or frame.size == 0:
        return True
    # Fast path: per-channel mean
    if frame.mean() < _BLACK_THRESHOLD:
        return True
    # Slower path: check that almost all pixels are dark
    dark_mask = np.all(frame <= 16, axis=2)
    return dark_mask.mean() > _BLACK_FRACTION


# ------------------------------------------------------------------
# ADBClient
# ------------------------------------------------------------------

_RECONNECT_WAIT = 2.0    # seconds to wait after adb connect before retrying
_BLACK_RETRY_DELAY = 1.0
_BLACK_MAX_RETRIES = 3   # short: the macro loop retries again on the next poll


class ADBError(Exception):
    """Raised when an ADB operation fails after retries."""


class BlackFrameError(ADBError):
    """Screencap kept returning an all-black frame.

    Distinct from a real disconnect so the caller can keep polling (the screen
    may recover) instead of stopping. On BlueStacks this is usually the graphics
    renderer not exposing frames to screencap, not a dropped connection.
    """


class ADBClient:
    """
    Interface to one Android device (BlueStacks or USB phone) over ADB.

    Example::

        client = ADBClient()            # locate adb, no device yet
        client.connect("localhost:5555")
        frame = client.screenshot()     # BGR numpy array
        client.tap(540, 960)
        w, h = client.resolution()
    """

    def __init__(self, adb_exe: Optional[str] = None, device: Optional[str] = None):
        """
        adb_exe: path to adb binary; None = auto-locate via find_adb().
        device:  device serial to target; None = auto-select when one device
                 is present.
        """
        if adb_exe is None:
            # Lazy import to avoid circular dependency with config at module
            # level; config may not be loaded yet when adb.py is first imported.
            try:
                import config
                adb_exe = find_adb(getattr(config, "ADB_PATH", "adb"))
            except Exception:
                adb_exe = find_adb("adb")
        self._adb = adb_exe
        self._device: Optional[str] = device
        self._is_network_device: bool = False
        if device:
            self._is_network_device = bool(
                re.match(r"^[\d\.]+:\d+$|^localhost:\d+$", device)
            )

    # ------------------------------------------------------------------
    # Low-level command runners
    # ------------------------------------------------------------------

    def _cmd(self, args: list[str]) -> list[str]:
        """Prepend [adb, -s, serial] when a device is selected."""
        base = [self._adb]
        if self._device:
            base += ["-s", self._device]
        return base + args

    def _run_shell(self, shell_args: list[str],
                   timeout: int = _DEFAULT_TIMEOUT) -> str:
        """Run `adb shell <args>` and return stdout text."""
        result = _run(self._cmd(["shell"] + shell_args), timeout=timeout)
        return result.stdout.decode(errors="replace").strip()

    def _run_cmd(self, args: list[str],
                 timeout: int = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
        """Run an adb command (not shell) and return the CompletedProcess."""
        return _run(self._cmd(args), timeout=timeout)

    # ------------------------------------------------------------------
    # Auto-reconnect wrapper
    # ------------------------------------------------------------------

    def _with_reconnect(self, fn, *args, **kwargs):
        """
        Call fn(*args, **kwargs). If it raises, attempt reconnect once and
        retry. If the retry also fails, raise ADBError.

        Only network (TCP) devices are re-connected; USB devices cannot be
        re-connected this way.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as first_exc:
            if not self._device or not self._is_network_device:
                raise ADBError(f"ADB command failed: {first_exc}") from first_exc
            log.warning(
                "ADB command failed (%s), attempting reconnect to %s ...",
                first_exc, self._device,
            )
            try:
                self._adb_connect(self._device)
                time.sleep(_RECONNECT_WAIT)
                return fn(*args, **kwargs)
            except Exception as retry_exc:
                raise ADBError(
                    f"ADB command failed after reconnect: {retry_exc}"
                ) from retry_exc

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _adb_connect(self, serial: str) -> None:
        """Issue `adb connect <serial>` (for TCP/network devices)."""
        result = _run([self._adb, "connect", serial], timeout=10)
        output = result.stdout.decode(errors="replace").strip()
        log.debug("adb connect %s -> %s", serial, output)
        if "failed" in output.lower() or "refused" in output.lower():
            raise ADBError(f"adb connect {serial} failed: {output}")

    def connect(self, serial: str, ready_timeout: float = 6.0) -> None:
        """
        Select a device and, for network devices (BlueStacks / TCP), issue
        `adb connect` to ensure the connection is established.

        Polls `adb devices` for up to `ready_timeout` seconds waiting for the
        device to show up ready, instead of giving up on a single check: a
        freshly (re)started adb server takes ~1-3s to re-enumerate a USB phone,
        so a one-shot check can fail on a device that is perfectly fine. (The
        dedicated server port set by config.apply_adb_server_port() should keep
        the server from being restarted under us in the first place; this poll is
        belt-and-braces for slow enumeration and transient `offline` states.)

        Raises ADBError if the device stays offline/unauthorised or never appears.
        """
        self._device = serial
        self._is_network_device = bool(
            re.match(r"^[\d\.]+:\d+$|^localhost:\d+$", serial)
        )
        if self._is_network_device:
            self._adb_connect(serial)

        # Verify the device is ready, retrying until ready_timeout. discover is
        # off here: the explicit _adb_connect above already established a network
        # target, and a USB phone never needs the localhost emulator-port probing
        # (which only churns the server and slows each poll).
        deadline = time.time() + max(0.0, ready_timeout)
        last_state = None
        while True:
            for d in list_devices(self._adb, discover=False):
                if d["serial"] == serial:
                    last_state = d["state"]
                    if d["state"] == "device":
                        log.info("ADB connected to %s (%s)", serial, d["type"])
                        return
                    if d["state"] == "unauthorized":
                        raise ADBError(
                            f"Device {serial} is unauthorised. Enable USB "
                            "debugging and accept the host key on the device."
                        )
                    break  # offline / other transient state -> keep polling
            if time.time() >= deadline:
                break
            time.sleep(0.4)

        if last_state and last_state != "device":
            raise ADBError(f"Device {serial} is {last_state}, not ready.")
        raise ADBError(
            f"Device {serial} not found in `adb devices` after connect."
        )

    def auto_connect(self) -> str:
        """
        If exactly one device is connected and ready, select it automatically.
        Returns the serial that was selected.
        Raises ADBError if zero or multiple devices are found.
        """
        devices = [d for d in list_devices(self._adb) if d["state"] == "device"]
        if len(devices) == 1:
            serial = devices[0]["serial"]
            self.connect(serial)
            return serial
        if not devices:
            raise ADBError(
                "No ADB devices found. "
                "Connect a phone via USB, or start BlueStacks and enable ADB."
            )
        serials = [d["serial"] for d in devices]
        raise ADBError(
            f"Multiple ADB devices found: {serials}. "
            "Select one explicitly in the GUI."
        )

    def disconnect(self) -> None:
        """Disconnect from a TCP device (no-op for USB devices)."""
        if self._device and self._is_network_device:
            _run([self._adb, "disconnect", self._device], timeout=5)
        self._device = None

    def reconnect(self) -> bool:
        """Full disconnect + reconnect cycle for the current device.

        TCP devices: issues ``adb disconnect`` then ``adb connect`` so a
        stale/hung connection is torn down before retrying. USB devices:
        just re-verifies reachability (the OS handles the physical link).

        Returns True if the device came back online and is ready.
        """
        if not self._device:
            return False
        serial = self._device
        if self._is_network_device:
            try:
                _run([self._adb, "disconnect", serial], timeout=5)
            except Exception:
                pass
            time.sleep(_RECONNECT_WAIT)
            try:
                self._adb_connect(serial)
            except ADBError:
                return False
        return self.is_connected()

    @property
    def device(self) -> Optional[str]:
        return self._device

    @property
    def adb_exe(self) -> str:
        """Path/name of the adb binary this client uses (for the capture stream)."""
        return self._adb

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    def _do_screenshot(self) -> np.ndarray:
        """Take one screenshot; no retry logic. Returns BGR numpy array."""
        raw = _run_raw(self._cmd(["exec-out", "screencap", "-p"]),
                       timeout=_SCREENCAP_TIMEOUT)
        if not raw:
            raise ADBError("screencap returned empty bytes")
        arr = np.frombuffer(raw, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise ADBError("Failed to decode screencap PNG")
        return frame

    def screenshot(self, black_retries: int = _BLACK_MAX_RETRIES) -> np.ndarray:
        """
        Capture the device screen as a BGR numpy array at native resolution.

        Handles black frames (phone screen off / device busy) by retrying up
        to `black_retries` times with a short delay between attempts. If the
        frame is still black after all retries, raises ADBError.

        Uses the auto-reconnect wrapper for connection drops.
        """
        frame = self._with_reconnect(self._do_screenshot)

        if not is_black_frame(frame):
            return frame

        log.warning("Black frame detected -- phone screen may be off. Retrying...")
        for attempt in range(1, black_retries + 1):
            time.sleep(_BLACK_RETRY_DELAY)
            try:
                frame = self._with_reconnect(self._do_screenshot)
            except ADBError:
                continue
            if not is_black_frame(frame):
                log.info("Screen came back on after %d retry(s)", attempt)
                return frame
            log.warning("Still black (attempt %d/%d)", attempt, black_retries)

        raise BlackFrameError(
            "Screencap returned a black frame after all retries."
        )

    # ------------------------------------------------------------------
    # Input: tap
    # ------------------------------------------------------------------

    def _do_tap(self, x: int, y: int) -> None:
        out = self._run_shell(["input", "tap", str(x), str(y)])
        # adb shell input tap produces no stdout on success; any error message
        # appears on stderr (already captured by _run).
        _ = out  # intentionally ignored -- tap failure is silent by design

    def tap(self, x: int, y: int) -> None:
        """
        Send a tap at (x, y) in device display coordinates.

        Delivery failure is silent by design: if the tap doesn't register
        (wrong screen, game crashed) the macro's state machine will retry on
        the next poll cycle.
        """
        self._with_reconnect(self._do_tap, x, y)

    # ------------------------------------------------------------------
    # Input: swipe
    # ------------------------------------------------------------------

    def _do_swipe(self, x1: int, y1: int, x2: int, y2: int,
                  duration_ms: int) -> None:
        self._run_shell([
            "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration_ms),
        ])

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 300) -> None:
        """
        Send a swipe gesture from (x1, y1) to (x2, y2) over duration_ms ms.
        Used for joystick/drag movement in the game.
        """
        self._with_reconnect(self._do_swipe, x1, y1, x2, y2, duration_ms)

    # ------------------------------------------------------------------
    # Resolution query
    # ------------------------------------------------------------------

    def resolution(self) -> tuple[int, int]:
        """
        Return (width, height) in the screencap / tap coordinate space.

        The screenshot is authoritative: it is the exact pixel grid taps land in
        and matching runs on, and it always reflects the CURRENT rotation.
        `adb shell wm size` is deliberately NOT used -- it can report the
        physical, unrotated panel size (BlueStacks returns 960x540 while the app
        actually renders 540x960), which would be wrong for both geometry and
        taps.
        """
        frame = self.screenshot()
        h, w = frame.shape[:2]
        return w, h

    def shell_works(self) -> bool:
        """
        True if `adb shell` can actually execute commands on this device.

        BlueStacks returns `error: closed` for every shell command when its
        "Android Debug Bridge" setting is OFF -- yet `exec-out screencap` still
        works, so detection looks fine while taps silently never land. This
        probe lets the macro warn the user instead of failing mysteriously.
        """
        try:
            out = self._run_shell(["echo", "__adbok__"])
            return "__adbok__" in out
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Phone sleep (for run-timeout scrcpy shutdown)
    # ------------------------------------------------------------------

    def sleep_phone(self) -> None:
        """
        Turn the phone screen off via KEYCODE_SLEEP. Non-fatal: logs on
        failure rather than raising.
        """
        try:
            self._with_reconnect(
                lambda: self._run_shell(["input", "keyevent", "223"])
            )
            log.info("Phone screen turned off (KEYCODE_SLEEP)")
        except Exception as exc:
            log.warning("sleep_phone failed: %s", exc)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Return True if the selected device is reachable and ready."""
        if not self._device:
            return False
        devices = list_devices(self._adb)
        for d in devices:
            if d["serial"] == self._device and d["state"] == "device":
                return True
        return False

    def __repr__(self) -> str:
        return (
            f"ADBClient(adb={self._adb!r}, device={self._device!r})"
        )
