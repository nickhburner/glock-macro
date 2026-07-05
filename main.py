"""
A2 Macro Controller: main loop.

Watches the game via ADB screenshots, identifies the current state via
template matching, and taps the correct UI element.

Usage:
    python main.py        # headless with saved settings
    python gui.py         # graphical control panel
"""

import math
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import capture
import config
from adb import ADBClient, ADBError, BlackFrameError, choose_server_port
from matcher import (
    crop_band,
    is_blank,
    list_skill_files,
    load_template,
    multi_scale_match,
    resize_to_width,
    skill_hash,
)

# Keeps subprocess helpers from flashing a console window in --windowed builds.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Module-level reference to the active ADB client.  Set by run_macro() /
# run_eternal_lode() so sleep_phone() (imported by eternal_lode.py) works.
_active_adb: Optional[ADBClient] = None

# Buttons that begin a gamemode: the FIRST skill selection after one of these is
# the cue to run the set movement (matched by name prefix, so variants like
# start-hard-chapter-2 also count).  glory/level can be unreliable on entry, so
# we key off the start button instead of the banner type.  (start_challenge +
# get-ready begin Plant Defense; start-chapter/start-hard-chapter the chapters.)
MOVEMENT_START_BUTTONS = (
    "ad-start", "get-ready", "play_button",
    "start-hard-chapter", "start-chapter",
    "start_challenge", "start-challenge",
)

# Plant Defense spawn is determined by HOW the match was entered, not by image
# detection: the host starts the match (start_challenge) and always spawns in
# position 1 (left); a guest joins and enters via get-ready, spawning in
# position 2 (right).  Maps a start-button name prefix -> spawn; _maybe_arm_
# movement records it so _do_movement can pick the matching path.
PLANT_SPAWN_BY_START = (
    ("start_challenge", 1), ("start-challenge", 1),
    ("get-ready", 2),
)

# Max taps on the co-op Like button per round.  The pressed (inactive) button
# can still resemble the template, so without a cap the macro could sit on the
# results screen tapping Like forever instead of advancing.  Resets whenever a
# gamemode start button is tapped (= a new round began).
LIKE_TAP_LIMIT = 3


# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

_log_sink = None


def set_log_sink(sink):
    """Install a callable that receives every log line, or None to remove."""
    global _log_sink
    _log_sink = sink


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass
    if _log_sink is not None:
        try:
            _log_sink(line)
        except Exception:
            pass


# ------------------------------------------------------------------
# Timing helpers
# ------------------------------------------------------------------

def rand_delay(base):
    """`base` seconds varied by +/- config.DELAY_JITTER fraction."""
    return base * random.uniform(1.0 - config.DELAY_JITTER,
                                 1.0 + config.DELAY_JITTER)


def _interruptible_sleep(seconds, stop_event):
    """Sleep `seconds`, returning early if `stop_event` is set."""
    if stop_event is not None:
        stop_event.wait(seconds)
    else:
        time.sleep(seconds)


# ------------------------------------------------------------------
# Process / window management
# ------------------------------------------------------------------

def close_target():
    """Force-close game-window processes (both backends) via taskkill."""
    for name in config.CLOSE_PROCESSES:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", name],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            log(f"  closed {name}")
        else:
            log(f"  {name} not running")


def sleep_phone():
    """Turn the phone screen off via ADB (delegates to the active client).

    Imported by eternal_lode.py for its own timeout handler.  Non-fatal.
    """
    if _active_adb is not None:
        _active_adb.sleep_phone()
    else:
        log("  phone: ADB client not initialised, cannot sleep phone")


# ------------------------------------------------------------------
# Macro class
# ------------------------------------------------------------------

class Macro:
    def __init__(self, adb_client: ADBClient, stream=None):
        self.adb = adb_client
        # Optional capture.ScreenRecordStream; when present, frames come from it
        # (fast, keeps the surface composited). No screencap fallback.
        self._stream = stream
        # Match across a small scale range (not one value) so a slightly-off
        # CALIBRATED_SCALE still finds icons -- this is what makes detection
        # robust on devices that don't render at exactly the template baseline.
        self.skill_scales = config.skill_scales()
        self.ref_scales   = config.ref_scales()
        log(f"skill scales {[round(s, 3) for s in self.skill_scales]}  "
            f"ref scales {[round(s, 3) for s in self.ref_scales]}")

        # Every frame is normalised to this width before matching; _norm is the
        # per-frame scale (MATCH_WIDTH / device_width) used to convert matched
        # coords back to real device pixels for tapping.  See config.MATCH_WIDTH.
        self.match_width = config.MATCH_WIDTH
        self._norm = 1.0

        # Per-ref match zone (ref-relative path -> "top"/"middle"/"bottom"),
        # so each ref is searched in only ~half the frame. Default "full".
        self._zones = config.load_ref_zones()
        self._ref_zone = {}

        # refresh (reroll) + skill-screen banners go through self.refs/_find_ref,
        # since the skill-selection flow needs them by name. All other refs are
        # loaded as behavior GROUPS below (missing files are skipped, so you can
        # add them incrementally with the GUI "Capture ref" tool).
        self.refs = {}
        self.skill_banners = []
        for key, path in (("refresh",  config.REF_REFRESH),
                          ("valkyrie", config.REF_VALKYRIE),
                          ("level",    config.REF_LEVEL),
                          ("glory",    config.REF_GLORY),
                          ("angel",    config.REF_ANGEL)):
            if path.exists():
                self.refs[key] = load_template(path)
                self._ref_zone[key] = self._zones.get(path.name, "full")
                if key != "refresh":
                    self.skill_banners.append(key)
            elif key == "refresh":
                log("NOTE: refresh.png not captured; cannot reroll skills")
            else:
                log(f"NOTE: skill banner '{path.name}' not captured yet, skipping")
        if not self.skill_banners:
            log("WARNING: no skill-screen banners available, "
                "skill selection will not be detected")

        # Tap-on-sight buttons: clicked whenever seen. Names map to ref/<name>.png
        # (a trailing * globs variants, e.g. start-hard-chapter, -2, ...).
        self.tap_buttons = self._load_group(
            "devil_reject", "play_button", "continue", "get-ready",
            "start_challenge", "ad-start", "ready-hard-chapter",
            "start-chapter", "start-hard-chapter*")

        # Co-op Like button: checked BEFORE everything else so the other player
        # gets their like before any tap advances past the results screen.
        # Capped at LIKE_TAP_LIMIT per round.
        self.like_button = self._load_group("like")
        self._like_taps = 0

        # Plant Defense level correction (host only): the game auto-advances to
        # the next level after a completed round, so when the Start Challenge
        # button reappears AFTER any round activity, the Back button is tapped
        # once first to return to the level the user picked.  False at start so
        # the very first Start never triggers it.
        self.back_plant = self._load_group("back-plant-level")
        self._plant_round_done = False

        # Speed button (cycles 1x -> 2x -> 3x). Driven to max speed each match.
        self.speed_refs = {}
        for spd, nm in (("1x", "1x-speed"), ("2x", "2x-speed"), ("3x", "3x-speed")):
            g = self._load_group(nm)
            if g:
                self.speed_refs[spd] = g[0]          # (name, tpl, zone)

        # Challenge-ended screens (replace the old game-over/tap-to-close):
        # wait, then tap near the bottom centre to dismiss.
        # Exception: challenge-has-ended3 uses a Continue button -- load it
        # separately so it gets the tap_buttons / all-refs scan instead.
        self.challenge_ended_continue = self._load_group("challenge-has-ended3")
        # All tap-to-dismiss end screens EXCEPT the '3' continue-button variant.
        # The base "challenge-has-ended.png" must be listed explicitly: the glob
        # "challenge-has-ended[!3]*" can't match it (the [!3] class consumes the
        # dot, leaving nothing for the trailing .png), so it only catches the
        # numbered variants (challenge-has-ended2, -4, ...).
        self.challenge_ended = self._load_group(
            "challenge-has-ended", "challenge-has-ended[!3]*")
        # The Continue button shown on the challenge-has-ended3 end screen.  The
        # end banner stays on screen alongside this button, so step() must find
        # and tap it within the challenge-ended branch rather than deferring to
        # the tap_buttons scan (which that branch's early return never reaches).
        self.continue_button = self._load_group("continue")
        # Wheel spin: the spin button, and the "you won a skill" reward popup.
        self.spin_wheel   = self._load_group("spin-wheel")
        self.wheel_reward = self._load_group("wheel-reward")

        # Custom "press it when you see it" buttons (ref/custom/).  A custom
        # capture that shares a name with a built-in ref is skipped: older
        # releases shipped some now-built-in buttons (e.g. like.png) as custom
        # captures, and the updater preserves ref/custom/, so without this an
        # updated install would load the same button twice (and the uncapped
        # custom copy would bypass the built-in behavior, like the Like cap).
        builtin_names = {"refresh", "valkyrie", "level", "glory", "angel"}
        for group in (self.tap_buttons, self.like_button, self.back_plant,
                      self.challenge_ended_continue, self.challenge_ended,
                      self.continue_button, self.spin_wheel, self.wheel_reward,
                      *self.speed_refs.values()):
            if group and isinstance(group, tuple):    # a single (name, tpl, zone)
                builtin_names.add(group[0])
            else:
                builtin_names.update(name for name, *_ in group)
        self.custom_refs = []
        for path in sorted(config.REF_CUSTOM_DIR.glob("*.png")):
            if path.stem in builtin_names:
                log(f"NOTE: custom button '{path.name}' duplicates the "
                    f"built-in ref of the same name, skipping")
                continue
            try:
                zone = self._zones.get(f"custom/{path.name}", "full")
                self.custom_refs.append((path.stem, load_template(path), zone))
            except (FileNotFoundError, ValueError):
                log(f"NOTE: custom button '{path.name}' could not be loaded, skipping")
        if self.custom_refs:
            log(f"loaded {len(self.custom_refs)} custom button(s): "
                + ", ".join(n for n, *_ in self.custom_refs))
        log(f"behaviors: {len(self.tap_buttons)} tap-button(s), "
            f"{len(self.speed_refs)} speed, {len(self.challenge_ended)} "
            f"challenge-end, {len(self.spin_wheel)+len(self.wheel_reward)} wheel")

        # Speed-control state (see _adjust_speed): track last seen speed and
        # whether 3x turned out to be unavailable (so we hold at 2x, no oscillation).
        self._last_speed = None
        self._speed_max_2x = False

        # Movement sequence (see step()): a start button (only when a movement
        # mode is selected) arms it; when the FIRST skill selection's banner
        # disappears, run the movement in gameplay (force 1x first if the mode
        # has a speed button), then let speed return to max. _adjust_speed is
        # suppressed while armed so the forced 1x holds until the move is done.
        self._move_armed = False
        self._move_in_skill = False
        self._move_action = None        # None / "move"
        # Plant Defense spawn (1=host/left, 2=guest/right), set from the start
        # button in _maybe_arm_movement and consumed by _do_movement.
        self._plant_spawn = 1

        # Active skill categories with avoid filtering.
        avoid_hashes = set()
        for ident in config.AVOID_SKILLS:
            h = skill_hash(config.SKILLS_DIR / ident)
            if h is not None:
                avoid_hashes.add(h)

        self.skill_categories = []
        total = 0
        excluded = []
        for category in config.ACTIVE_CATEGORIES:
            files = list_skill_files(config.SKILLS_DIR / category)
            if not files:
                log(f"NOTE: skill category '{category}' has no icons, skipping")
                continue
            skills = []
            for p in files:
                if avoid_hashes and skill_hash(p) in avoid_hashes:
                    excluded.append(f"{category}/{p.name}")
                    continue
                skills.append((p.name, load_template(p)))
            if skills:
                self.skill_categories.append((category, skills))
                total += len(skills)
            else:
                log(f"NOTE: skill category '{category}' has no icons left "
                    f"after the avoid list, skipping")

        log(f"loaded {len(self.refs)} UI refs and {total} skill icons "
            f"across {len(self.skill_categories)} active categories")
        if excluded:
            log(f"avoid list excludes {len(excluded)} skill file(s): "
                + ", ".join(excluded))
        if total == 0:
            log("WARNING: no skill icons found, macro will always fall back "
                "to the first skill slot")

        # Custom priority skills (picked above all categories).
        self.custom_priority = []
        for ident in config.CUSTOM_PRIORITY_SKILLS:
            path = config.SKILLS_DIR / ident
            if not path.exists():
                log(f"NOTE: custom priority skill '{ident}' not found, skipping")
                continue
            if avoid_hashes and skill_hash(path) in avoid_hashes:
                log(f"NOTE: custom priority skill '{ident}' is on the avoid "
                    f"list; avoid wins, skipping")
                continue
            self.custom_priority.append((ident, load_template(path)))
        if self.custom_priority:
            log(f"loaded {len(self.custom_priority)} custom priority skill(s)")

        # Avoid skills checked against the first slot before the fallback.
        self.avoid_skills = []
        for ident in config.AVOID_SKILLS:
            path = config.SKILLS_DIR / ident
            if path.exists():
                self.avoid_skills.append((ident, load_template(path)))
            else:
                log(f"NOTE: avoid skill '{ident}' not found, skipping")
        if self.avoid_skills:
            log(f"loaded {len(self.avoid_skills)} avoid skill(s)")

        self._warn_blank_refs()

    def _warn_blank_refs(self):
        blank = [name for name, tpl in self.refs.items() if is_blank(tpl)]
        if blank:
            log("WARNING: these ref images look blank and will never match: "
                + ", ".join(blank))
            log("         re-capture them (run diagnose.py)")

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _tap(self, x, y, label):
        """Send a tap.  (x, y) are in the normalised match space (MATCH_WIDTH);
        convert to real device pixels via _norm, then add jitter + a pre-delay."""
        dx = round(x / self._norm)
        dy = round(y / self._norm)
        jx = dx + random.randint(-config.CLICK_JITTER, config.CLICK_JITTER)
        jy = dy + random.randint(-config.CLICK_JITTER, config.CLICK_JITTER)
        log(f"  tap {label} @ ({jx}, {jy})")
        time.sleep(random.uniform(*config.CLICK_DELAY_RANGE))
        self.adb.tap(jx, jy)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _grab(self):
        """Current frame. With a stream attached (streaming mode) read the
        latest decoded frame, effectively free. We NEVER fall back to
        screencap mid-run. A brief stall (the ~1s gap between screenrecord
        segments) is bridged by the last frame while it is still fresh; a
        longer stall triggers a full ADB reconnect + stream re-establish
        before giving up. Without a stream (USE_STREAM_CAPTURE off) capture
        is a deliberate screencap."""
        if self._stream is None:
            return self.adb.screenshot()
        frame = self._stream.latest(max_age=3.0)
        if frame is not None:
            return frame
        # No fresh frame for >3s: give the stream a moment to recover (it
        # auto-relaunches each segment) before escalating.
        frame = self._wait_for_stream_frame(timeout=3.0)
        if frame is not None:
            return frame
        # Stream is dead. Attempt a full ADB reconnect + stream rebuild.
        frame = self._recover_stream()
        if frame is not None:
            return frame
        raise ADBError(
            "capture stream lost and recovery failed. Check the device "
            "screen is on and the USB/ADB connection is stable.")

    def _wait_for_stream_frame(self, timeout=3.0):
        """After a >max_age stall, poll briefly for the stream to recover (the
        decode thread relaunches screenrecord each segment / on error). Returns
        a fresh frame, or None if none arrives within `timeout`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self._stream.latest(max_age=3.0)
            if frame is not None:
                return frame
            time.sleep(0.1)
        return None

    def _recover_stream(self):
        """Last resort: tear down the stream, reconnect ADB, and try to
        establish a fresh stream. Returns a frame on success, None on failure.
        Called at most once per stall; if recovery fails the macro stops."""
        log("capture stream stalled, attempting recovery...")
        self._stream.stop()

        if self.adb.reconnect():
            log("  ADB reconnected")
        else:
            log("  ADB reconnect failed, trying stream anyway...")

        log("  re-establishing capture stream...")
        stream = capture.open_stream(
            self.adb.adb_exe, self.adb.device,
            attempts=3, per_attempt_timeout=4.0, on_log=log)
        if stream is None:
            log("  stream recovery failed")
            return None

        self._stream = stream
        log("  stream recovered")
        return stream.latest(max_age=3.0)

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _match_in_zone(self, image, tpl, zone):
        """Match `tpl` over only its vertical zone (e.g. top/bottom 50%) for
        speed, mapping the result back to full-frame coords. zone 'full' (or
        unknown) searches the whole frame."""
        band = config.zone_band(zone, image.shape[0])
        if band is None:
            conf, center, _ = multi_scale_match(
                image, tpl, self.ref_scales, config.REF_DOWNSCALE)
            return conf, center
        sub, y_off = crop_band(image, band)
        conf, center, _ = multi_scale_match(
            sub, tpl, self.ref_scales, config.REF_DOWNSCALE)
        if center is not None:
            center = (center[0], center[1] + y_off)
        return conf, center

    def _find_ref(self, image, key):
        return self._match_in_zone(
            image, self.refs[key], self._ref_zone.get(key, "full"))

    def _load_group(self, *names):
        """Load ref PNGs by name/glob from ref/ -> list of (name, tpl, zone).
        A trailing '*' globs variants (start-hard-chapter -> -2, ...); missing
        files are skipped so refs can be added incrementally."""
        out = []
        for nm in names:
            pat = nm if nm.endswith(".png") else nm + ".png"
            if "*" in pat:
                paths = sorted(config.REF_DIR.glob(pat))
            else:
                p = config.REF_DIR / pat
                paths = [p] if p.is_file() else []
            for p in paths:
                try:
                    out.append((p.stem, load_template(p),
                                self._zones.get(p.name, "full")))
                except (FileNotFoundError, ValueError):
                    log(f"NOTE: ref '{p.name}' could not load, skipping")
        return out

    def _detect(self, image, group):
        """Best match across a group of (name, tpl, zone).  Returns
        (name, conf, center), or (None, -1.0, None) for an empty group."""
        best = (None, -1.0, None)
        for name, tpl, zone in group:
            conf, center = self._match_in_zone(image, tpl, zone)
            if conf > best[1]:
                best = (name, conf, center)
        return best

    def _best_skill_banner(self, image):
        best_label, best_conf = None, -1.0
        for key in self.skill_banners:
            conf, _ = self._find_ref(image, key)
            if conf > best_conf:
                best_label, best_conf = key, conf
        return best_label, best_conf

    def _find_best_skill(self, image, band):
        """Return (category, name, conf, center) of the best skill on screen,
        or (None, None, 0.0, None).  Centers are in the (normalised) match
        space; the caller's _tap converts them to device pixels."""
        band_img, y_offset = crop_band(image, band)

        for ident, tpl in self.custom_priority:
            conf, center, _ = multi_scale_match(
                band_img, tpl, self.skill_scales, config.SKILL_DOWNSCALE)
            if conf >= config.MATCH_THRESHOLD and center is not None:
                return ("custom", ident, conf,
                        (center[0], center[1] + y_offset))

        for category, skills in self.skill_categories:
            for name, tpl in skills:
                conf, center, _ = multi_scale_match(
                    band_img, tpl, self.skill_scales, config.SKILL_DOWNSCALE)
                if conf >= config.MATCH_THRESHOLD and center is not None:
                    return (category, name, conf,
                            (center[0], center[1] + y_offset))
        return None, None, 0.0, None

    def _fallback_slot(self, image, band, slots):
        """No wanted skill and no refresh: pick the leftmost slot NOT holding an
        avoid skill.  Avoid skills are located across the whole band (the same
        robust path wanted skills use) rather than in a fragile fixed box, then
        each slot's x is checked against them.  Returns (slot_xy, avoid_x_list).
        """
        avoid_x = []
        if self.avoid_skills:
            band_img, _ = crop_band(image, band)
            for ident, tpl in self.avoid_skills:
                conf, center, _ = multi_scale_match(
                    band_img, tpl, self.skill_scales, config.SKILL_DOWNSCALE)
                if conf >= config.MATCH_THRESHOLD and center is not None:
                    avoid_x.append((ident, center[0]))
        tol = image.shape[1] * 0.15
        for sx, sy in slots:
            if not any(abs(sx - ax) <= tol for _, ax in avoid_x):
                return (sx, sy), avoid_x
        return slots[0], avoid_x

    def _adjust_speed(self, image):
        """Detect the speed button and step toward max speed (3x, or 2x when 3x
        is unavailable on this match).  Returns True if a speed tap was issued.

        The button SHOWS the current speed and cycles 1x->2x->3x->1x; we only
        ever tap to go up.  If tapping at 2x drops us back to 1x, 3x is not
        available, so we remember that and hold at 2x instead of oscillating.
        """
        if not self.speed_refs:
            return False
        cur, cur_center, cur_conf = None, None, -1.0
        for spd, (name, tpl, zone) in self.speed_refs.items():
            conf, center = self._match_in_zone(image, tpl, zone)
            if (conf >= config.REF_THRESHOLD and conf > cur_conf
                    and center is not None):
                cur, cur_center, cur_conf = spd, center, conf
        if cur is None:
            return False
        if cur == "3x":
            self._last_speed = "3x"
            return False                          # already at max
        if cur == "2x" and self._speed_max_2x:
            self._last_speed = "2x"
            return False                          # 2x is this match's cap, hold
        if cur == "1x" and self._last_speed == "2x":
            self._speed_max_2x = True             # 2x->1x => 3x not available
        log(f"speed {cur} -> tapping to raise")
        self._tap(cur_center[0], cur_center[1], f"speed ({cur} up)")
        self._last_speed = cur
        time.sleep(rand_delay(config.ACTION_DELAY))
        return True

    # ------------------------------------------------------------------
    # Movement (ADB swipe)
    # ------------------------------------------------------------------

    def _drag_joystick(self, jx, jy, angle_deg, duration_s, dist):
        """Swipe-and-hold on the joystick.  (jx, jy, dist) are in the normalised
        match space; they're converted to device pixels for the ADB swipe, whose
        duration IS the hold time.  angle: 0=right, 90=up, 180=left, 270=down."""
        tx = jx + dist * math.cos(math.radians(angle_deg))
        ty = jy - dist * math.sin(math.radians(angle_deg))
        n = self._norm or 1.0
        x1, y1 = round(jx / n), round(jy / n)
        x2, y2 = round(tx / n), round(ty / n)
        duration_ms = max(100, int(duration_s * 1000))
        log(f"  swipe ({x1},{y1})->({x2},{y2})  angle {angle_deg}deg  "
            f"hold {duration_s:.2f}s")
        self.adb.swipe(x1, y1, x2, y2, duration_ms)

    def _detect_speed(self, image):
        """Return (speed_str, center) for the speed button currently shown
        ('1x'/'2x'/'3x'), or (None, None) if no speed button is visible."""
        cur, center, best = None, None, -1.0
        for spd, (name, tpl, zone) in self.speed_refs.items():
            conf, c = self._match_in_zone(image, tpl, zone)
            if conf >= config.REF_THRESHOLD and conf > best and c is not None:
                cur, center, best = spd, c, conf
        return cur, center

    def _ensure_speed_1x(self, max_wait=2.0, interval=0.25):
        """Fast-poll the speed button down to 1x.  The button cycles
        1x->2x->3x->1x, so we just tap until it reads 1x (works whether the
        match maxes at 2x or 3x).  Tight loop on purpose: speed is the most
        important thing to fix right after the first skill.  Returns True once 1x
        is confirmed; False if no speed button shows (no speed control) or it
        vanishes (a screen came up)."""
        if not self.speed_refs:
            return False
        deadline = time.time() + max_wait
        seen = False
        while time.time() < deadline:
            image, _ = resize_to_width(self._grab(), self.match_width)
            cur, center = self._detect_speed(image)
            if cur is None:
                if seen:
                    return False             # button left (e.g. a skill screen)
                time.sleep(interval)
                continue
            seen = True
            self._last_speed = cur
            if cur == "1x":
                return True
            self._tap(center[0], center[1], f"speed {cur}->1x")
            time.sleep(interval)
        return seen

    def _do_movement(self, w, h):
        """Run the configured movement: a swipe-and-hold on the joystick (started
        at the bottom centre) for each of the mode's two vectors.  Assumes the
        caller has already set 1x speed.  (w, h) are the normalised frame dims."""
        if config.MOVEMENT_MODE == 2:
            # Plant Defense: spawn was determined by the entry button (host vs
            # guest), recorded in self._plant_spawn -- no image detection needed.
            spawn = self._plant_spawn
            params = config.plant_movement(spawn)
            label = (f"plant defense  spawn {spawn} "
                     f"({'host/left' if spawn == 1 else 'guest/right'}) / "
                     f"{config.MOVEMENT_PLANT_PRESET}")
        else:
            params = {1: config.MOVEMENT_CHAPTER,
                      3: config.MOVEMENT_CUSTOM}.get(config.MOVEMENT_MODE)
            if not params:
                return
            label = {1: "chapter", 3: "custom"}.get(config.MOVEMENT_MODE, "?")

        angle1, dur1, angle2, dur2 = params
        jx = w * config.MOVEMENT_JOYSTICK_X_RATIO
        jy = h * config.MOVEMENT_JOYSTICK_Y_RATIO
        dist = h * config.MOVEMENT_SWIPE_LEN_RATIO
        log(f"movement ({label}): joystick @ bottom-centre, "
            f"swipe {dist:.0f}px (={config.MOVEMENT_SWIPE_LEN_RATIO:.0%} H)")
        if dur1 > 0:
            self._drag_joystick(jx, jy, angle1, dur1, dist)
        if dur1 > 0 and dur2 > 0:
            time.sleep(0.3)
        if dur2 > 0:
            self._drag_joystick(jx, jy, angle2, dur2, dist)

    def _maybe_arm_movement(self, name):
        """Arm the movement sequence if a movement mode is selected and `name` is
        a gamemode start button.  The move then runs once the first skill
        selection's banner disappears."""
        if config.MOVEMENT_MODE == 0:
            return
        if any(name.startswith(p) for p in MOVEMENT_START_BUTTONS):
            self._move_armed = True
            self._move_in_skill = False
            self._move_action = None
            # Plant Defense: the start button also tells us the spawn position
            # (host via start_challenge = 1/left, guest via get-ready = 2/right).
            for prefix, spawn in PLANT_SPAWN_BY_START:
                if name.startswith(prefix):
                    self._plant_spawn = spawn
                    log(f"  (plant spawn {spawn} = "
                        f"{'host/left' if spawn == 1 else 'guest/right'}, "
                        f"from '{name}')")
                    break
            log("  (armed: move when the first skill selection ends)")

    # ------------------------------------------------------------------
    # One iteration
    # ------------------------------------------------------------------

    def step(self):
        """Capture one frame, act on what's visible, return a state string.
        Raises ADBError on unrecoverable device failure."""
        image = self._grab()

        # Normalise to MATCH_WIDTH: one template set fits every device and
        # matching stays fast.  _norm carries the scale so _tap can convert the
        # match-space coords it gets back into the device's real pixels.
        image, self._norm = resize_to_width(image, self.match_width)

        if is_blank(image):
            return "blank"

        h, w = image.shape[:2]
        # Skill band / fallback slots in THIS frame's match space.
        band, _first_slot, _second_slot, _ = config.geometry_for(w, h)

        def bottom_tap(label):
            self._tap(round(w * config.BOTTOM_TAP_X_RATIO),
                      round(h * config.BOTTOM_TAP_Y_RATIO), label)

        # 0. Co-op Like button: checked before everything else so the like
        #    lands before any other tap advances past the screen it is on.
        #    Capped per round so an already-pressed button can't stall the loop.
        if self.like_button and self._like_taps < LIKE_TAP_LIMIT:
            lname, lconf, lcenter = self._detect(image, self.like_button)
            if lconf >= config.REF_THRESHOLD and lcenter is not None:
                self._like_taps += 1
                self._plant_round_done = True
                log(f"like button (conf {lconf:.2f}) -> tap "
                    f"({self._like_taps}/{LIKE_TAP_LIMIT} this round)")
                self._tap(lcenter[0], lcenter[1], "like")
                time.sleep(rand_delay(config.ACTION_DELAY))
                return "like"

        # 1. Skill selection, detected by any banner (valkyrie/level/glory/angel).
        indicator, iconf = self._best_skill_banner(image)
        if iconf >= config.REF_THRESHOLD:
            self._plant_round_done = True       # a round is being played
            if self._move_armed:
                self._move_in_skill = True      # a skill screen is up
            # The cards and the Refresh button animate in after the banner shows.
            # With a fast poll we can catch the banner before they finish, then
            # see no skill / no Refresh and wrongly fall back to a slot.  Wait a
            # beat and RE-GRAB so we scan the settled screen, not the half-drawn
            # one.  (Skipped when SKILL_SETTLE_DELAY is 0.)
            if config.SKILL_SETTLE_DELAY > 0:
                time.sleep(config.SKILL_SETTLE_DELAY)
                image, self._norm = resize_to_width(self._grab(), self.match_width)
                h, w = image.shape[:2]
                band, _first_slot, _second_slot, _ = config.geometry_for(w, h)
            category, name, sconf, scenter = self._find_best_skill(image, band)
            if name:
                log(f"skill select [{indicator} {iconf:.2f}] -> "
                    f"{category} / {name} (conf {sconf:.2f})")
                self._tap(scenter[0], scenter[1], f"skill {category}/{name}")
            else:
                rconf, rcenter = (self._find_ref(image, "refresh")
                                  if "refresh" in self.refs else (-1.0, None))
                if rconf >= config.REF_THRESHOLD and rcenter is not None:
                    log(f"skill select [{indicator} {iconf:.2f}] -> no wanted "
                        f"skill, refresh ({rconf:.2f}) -> reroll")
                    self._tap(rcenter[0], rcenter[1], "refresh")
                else:
                    # Slot layout depends on how many cards the screen shows:
                    # valkyrie/angel = 2 cards, level/glory = 3.  Tapping the
                    # 3-card positions on a 2-card screen lands in dead space.
                    n_cards = 2 if indicator in config.SKILL_BANNERS_2_CARD else 3
                    sy = round(h * config.SKILL_ROW_Y_RATIO)
                    slots = [(round(w * xr), sy)
                             for xr in config.SKILL_SLOT_X_BY_COUNT[n_cards]]
                    slot, avoid_x = self._fallback_slot(image, band, slots)
                    if avoid_x:
                        log(f"skill select [{indicator} {iconf:.2f}] -> no wanted "
                            f"skill; avoiding {[i for i, _ in avoid_x]} "
                            f"-> slot x={slot[0]}")
                    else:
                        log(f"skill select [{indicator} {iconf:.2f}] -> no wanted "
                            f"skill, no refresh -> first slot")
                    self._tap(*slot, "fallback skill slot")
            time.sleep(rand_delay(config.ACTION_DELAY))
            return "skill"

        # No banner: the first skill selection's banner just disappeared
        # (event-based, so it handles the variable co-op skill timer) -> queue
        # the movement, to run below in gameplay.
        if self._move_armed and self._move_in_skill:
            self._move_in_skill = False
            self._move_action = "move"

        # 2. Challenge ended (replaces game-over / tap-to-close): wait, then tap
        #    the dead centre of the screen to dismiss the results.  The end
        #    screens say "tap empty area to close"; the centre is reliably empty,
        #    whereas the bottom strip can land on a reward icon and not dismiss.
        #    Exception: challenge-has-ended3 shows a Continue button rather than
        #    a tap-to-dismiss area.  Find and tap Continue right here: the end
        #    banner stays on screen alongside the button, so this branch returns
        #    on every poll and the tap_buttons scan below is never reached.
        c3name, c3conf, _ = self._detect(image, self.challenge_ended_continue)
        if c3conf >= config.REF_THRESHOLD:
            self._plant_round_done = True
            bname, bconf, bcenter = self._detect(image, self.continue_button)
            if bconf >= config.REF_THRESHOLD and bcenter is not None:
                log(f"challenge ended [{c3name} {c3conf:.2f}] -> Continue "
                    f"({bconf:.2f}) -> tap")
                self._tap(bcenter[0], bcenter[1], "continue")
                time.sleep(rand_delay(config.ACTION_DELAY))
            else:
                log(f"challenge ended [{c3name} {c3conf:.2f}] -> waiting for "
                    f"Continue button")
            return "challenge_ended"
        cname, cconf, _ = self._detect(image, self.challenge_ended)
        if cconf >= config.REF_THRESHOLD:
            self._plant_round_done = True
            log(f"challenge ended [{cname} {cconf:.2f}] -> dismiss (centre tap)")
            time.sleep(rand_delay(config.ACTION_DELAY))
            self._tap(round(w * 0.50), round(h * 0.50), "dismiss results")
            time.sleep(rand_delay(config.ACTION_DELAY))
            return "challenge_ended"

        # 3. Wheel reward (won a skill from the spin): accept with a centre tap.
        rwname, rwconf, _ = self._detect(image, self.wheel_reward)
        if rwconf >= config.REF_THRESHOLD:
            self._plant_round_done = True
            log(f"wheel reward [{rwname} {rwconf:.2f}] -> accept (centre tap)")
            time.sleep(rand_delay(config.ACTION_DELAY))
            self._tap(round(w * 0.50), round(h * 0.50), "wheel reward accept")
            time.sleep(rand_delay(config.ACTION_DELAY))
            return "wheel_reward"

        # 4. Spin wheel: tap it, then clear any result popups near the bottom.
        swname, swconf, swcenter = self._detect(image, self.spin_wheel)
        if swconf >= config.REF_THRESHOLD and swcenter is not None:
            self._plant_round_done = True
            log(f"spin wheel [{swname} {swconf:.2f}] -> spin, dismiss popups")
            self._tap(swcenter[0], swcenter[1], "spin wheel")
            for i in range(2):
                time.sleep(1.0)
                bottom_tap(f"wheel popup dismiss {i + 1}")
            return "spin_wheel"

        # 5. Tap-on-sight buttons (devil reject, play, continue, start*, ...).
        bname, bconf, bcenter = self._detect(image, self.tap_buttons)
        if bconf >= config.REF_THRESHOLD and bcenter is not None:
            # Plant Defense level correction (host only): a completed round
            # auto-advances the game to the next level, so when Start Challenge
            # reappears after round activity, tap Back ONCE first to return to
            # the level the user picked.  The flag is cleared before tapping so
            # it can never fire twice, and it starts False so the very first
            # Start of a run never triggers it.
            if (config.GAME_MODE == "plant" and self._plant_round_done
                    and bname.startswith(("start_challenge", "start-challenge"))
                    and self.back_plant):
                self._plant_round_done = False
                pname, pconf, pcenter = self._detect(image, self.back_plant)
                if pconf >= config.REF_THRESHOLD and pcenter is not None:
                    log(f"round done -> back one level [{pname} {pconf:.2f}]")
                    self._tap(pcenter[0], pcenter[1], "back-plant-level")
                    time.sleep(rand_delay(config.ACTION_DELAY))
                    return "plant_back"
                log("round done but back-plant-level button not visible; "
                    "starting without the level correction")
            log(f"button '{bname}' (conf {bconf:.2f}) -> tap")
            self._tap(bcenter[0], bcenter[1], bname)
            self._maybe_arm_movement(bname)
            if any(bname.startswith(p) for p in MOVEMENT_START_BUTTONS):
                # A round is starting: reset the per-round like cap and the
                # round-completed flag.
                self._like_taps = 0
                self._plant_round_done = False
            else:
                self._plant_round_done = True
            time.sleep(rand_delay(config.ACTION_DELAY))
            return "button"

        # 6. User-captured custom buttons.
        for name, tpl, zone in self.custom_refs:
            conf, center = self._match_in_zone(image, tpl, zone)
            if conf >= config.REF_THRESHOLD and center is not None:
                log(f"custom button '{name}' (conf {conf:.2f}) -> tap")
                self._tap(center[0], center[1], f"custom: {name}")
                self._maybe_arm_movement(name)
                if any(name.startswith(p) for p in MOVEMENT_START_BUTTONS):
                    self._like_taps = 0
                    self._plant_round_done = False
                else:
                    self._plant_round_done = True
                time.sleep(rand_delay(config.ACTION_DELAY))
                return "custom"

        # 6.5 Movement: the first skill selection ended -> run the move once, in
        #     gameplay. Force 1x first (no-op if this mode/co-op has no speed
        #     button); the wheel that follows is handled by steps 3-4 above, and
        #     speed returns to max via _adjust_speed once we're unarmed.
        if self._move_action == "move":
            self._move_action = None
            self._move_armed = False
            if config.MOVEMENT_MODE != 0:
                time.sleep(0.5)             # let the skill UI finish closing
                self._ensure_speed_1x()     # 1x if the mode has a speed button
                self._do_movement(w, h)
                return "movement"

        # 7. In a match (not mid-movement-sequence): drive speed to max. Held off
        #    while armed so the forced 1x survives until the movement is done.
        if not self._move_armed and self._adjust_speed(image):
            return "speed"

        # 8. Nothing actionable; character is playing.
        return "idle"


# ------------------------------------------------------------------
# Plant Defense helpers
# ------------------------------------------------------------------

def run_plant_movement(adb_client, spawn=1, on_log=None):
    """Execute the configured Plant Defense movement for a given spawn, in real
    device pixels. Called by the GUI 'Conduct Movement' button so the user can
    test and tune the movement (and verify each spawn's path) outside of a full
    macro run. In a real run the spawn comes from the entry button instead.

    The joystick start and swipe length are derived from the device resolution
    (config.PHONE_RESOLUTION), so no frame capture is needed."""
    _log = on_log or log
    w, h = config.PHONE_RESOLUTION
    if not w or not h:
        _log("plant movement: device resolution unknown -- run Setup Wizard first")
        return

    params = config.plant_movement(spawn)
    angle1, dur1, angle2, dur2 = params
    _log(f"plant movement: preset={config.MOVEMENT_PLANT_PRESET} "
         f"spawn={spawn} ({'host/left' if spawn == 1 else 'guest/right'}) "
         f"T={config.MOVEMENT_PLANT_T}s  "
         f"({angle1:.0f}°×{dur1:.2f}s, {angle2:.0f}°×{dur2:.2f}s)")

    jx = w * config.MOVEMENT_JOYSTICK_X_RATIO
    jy = h * config.MOVEMENT_JOYSTICK_Y_RATIO
    dist = h * config.MOVEMENT_SWIPE_LEN_RATIO

    def _swipe(angle_deg, duration_s):
        # jx/jy/dist are already in device pixels (from PHONE_RESOLUTION).
        tx = jx + dist * math.cos(math.radians(angle_deg))
        ty = jy - dist * math.sin(math.radians(angle_deg))
        x1, y1 = round(jx), round(jy)
        x2, y2 = round(tx), round(ty)
        dur_ms = max(100, int(duration_s * 1000))
        _log(f"  swipe ({x1},{y1})->({x2},{y2})  angle {angle_deg:.0f}°  "
             f"hold {duration_s:.2f}s")
        adb_client.swipe(x1, y1, x2, y2, dur_ms)

    if dur1 > 0:
        _swipe(angle1, dur1)
    if dur1 > 0 and dur2 > 0:
        time.sleep(0.3)
    if dur2 > 0:
        _swipe(angle2, dur2)


# ------------------------------------------------------------------
# Run loop
# ------------------------------------------------------------------

def run_macro(stop_event=None):
    """Run the macro until stopped or timed out.

    `stop_event` is a threading.Event; pass None for headless use (one is
    created internally).  Config is read here, so the GUI applies settings
    before calling this.
    """
    global _active_adb

    if stop_event is None:
        stop_event = threading.Event()

    log("A2 Macro Controller")
    log("active skill categories: "
        + (", ".join(config.ACTIVE_CATEGORIES) or "(none)"))

    # Connect to ADB device.  Choose the adb server port first: an isolated port
    # dodges the BlueStacks "version war" (its old HD-Adb fights the modern adb
    # and drops USB phones from `adb devices`); it self-heals to the shared
    # server if something else already owns the device.
    try:
        port = choose_server_port()
        log(f"adb server: {'isolated port ' + str(port) if port else 'system default port'}")
        adb_client = ADBClient()
        if config.ADB_DEVICE:
            adb_client.connect(config.ADB_DEVICE)
        else:
            serial = adb_client.auto_connect()
            config.ADB_DEVICE = serial
    except ADBError as e:
        log(f"FATAL: ADB connection failed: {e}")
        return

    _active_adb = adb_client

    # The screenshot is the authoritative coordinate space: every tap lands in
    # the same pixel grid screencap returns.  Derive resolution from a probe
    # frame (not the flaky `wm size`, which returns empty on some emulators) and
    # rebuild the skill band / slot / game-over coords for the real size.  This
    # is what stops a stale PHONE_RESOLUTION from putting the band off-screen.
    try:
        probe = adb_client.screenshot()
        h, w = probe.shape[:2]
        config.PHONE_RESOLUTION = [w, h]
        config.resolve_geometry(w, h)   # device-space coords for the GUI/settings
        norm = config.MATCH_WIDTH / w if w else 1.0
        log(f"device: {adb_client.device}  resolution: {w}x{h} (from screencap)")
        log(f"  matching at {config.MATCH_WIDTH}px wide (scale {norm:.3f}); "
            f"taps map back to real device pixels")
    except ADBError as e:
        log(f"WARNING: could not capture a probe frame: {e}")
        log("  using cached geometry; detection may be off until reconnected")

    # Input sanity probe: detection (screencap) can work while `adb shell input`
    # is dead, so warn loudly rather than tapping into the void.
    if not adb_client.shell_works():
        log("WARNING: 'adb shell' is not responding -- taps will NOT register.")
        log("  If this is BlueStacks: turn ON 'Android Debug Bridge' in its")
        log("  Settings > Advanced, then restart BlueStacks and reconnect.")

    # Capture stream (screenrecord). When USE_STREAM_CAPTURE is on it is
    # REQUIRED, not best-effort: it keeps the surface composited (no black
    # frames) and is far faster than per-poll screencap. We retry to establish
    # it and refuse to run if it never connects -- a slow/black screencap loop
    # runs so poorly that not running at all is the better outcome.
    stream = None
    if config.USE_STREAM_CAPTURE:
        if not capture.streaming_available():
            log(f"FATAL: {capture.unavailable_reason()}.")
            log("  Streaming capture is required. Install PyAV (pip install av),")
            log("  or set USE_STREAM_CAPTURE = False to use screencap mode.")
            return
        stream = capture.open_stream(
            adb_client.adb_exe, adb_client.device,
            attempts=3, per_attempt_timeout=4.0, on_log=log)
        if stream is None:
            log("FATAL: could not establish the screenrecord stream after "
                "several attempts.")
            log("  Refusing to run on slow screencap. Check the device screen "
                "is on and the USB/ADB connection is stable, then start again.")
            return
        log("capture: streaming via screenrecord (fast, no black frames)")
    else:
        log("capture: screencap mode (USE_STREAM_CAPTURE is off) -- slower, "
            "and black on BlueStacks during gameplay")

    try:
        macro = Macro(adb_client, stream=stream)
    except (FileNotFoundError, ValueError) as e:
        log(f"FATAL: {e}")
        if stream is not None:
            stream.stop()
        return

    log(f"ADB ready, starting in {config.STARTUP_DELAY:.0f}s "
        f"(press the Stop button to stop)")
    _interruptible_sleep(config.STARTUP_DELAY, stop_event)
    if stop_event.is_set():
        log("stopped before the run started")
        return

    timeout_hours = config.RUN_TIMEOUT_HOURS or 0
    deadline = time.monotonic() + timeout_hours * 3600 if timeout_hours else None
    if deadline:
        log(f"running, timeout in {timeout_hours:g}h")
    else:
        log("running with no timeout")

    timed_out = False
    stuck_out = False
    last_state = None
    consec_black = 0
    BLACK_GIVE_UP = 40   # stop only after a long unbroken run of black frames
    stuck_state = None
    stuck_since = None
    stuck_timeout = (config.STUCK_TIMEOUT_MINUTES or 0) * 60
    try:
        while True:
            if stop_event.is_set():
                log("stop requested, stopping")
                break
            if deadline and time.monotonic() >= deadline:
                log(f"run timeout reached ({timeout_hours:g}h), stopping")
                timed_out = True
                break

            try:
                state = macro.step()
                consec_black = 0

                if stuck_timeout and state not in ("idle", "blank"):
                    now = time.monotonic()
                    if state == stuck_state:
                        if now - stuck_since >= stuck_timeout:
                            log(f"stuck timeout: '{state}' has repeated for "
                                f"{config.STUCK_TIMEOUT_MINUTES:g} min, stopping")
                            stuck_out = True
                            break
                    else:
                        stuck_state = state
                        stuck_since = now
                else:
                    stuck_state = None
                    stuck_since = None
            except BlackFrameError:
                # All-black capture: keep polling, it usually recovers. Do NOT
                # treat as a disconnect (the connection is fine).
                consec_black += 1
                if consec_black == 1:
                    log("WARNING: screen capture came back all-black.")
                    log("  If this is BlueStacks: open Settings > Graphics and "
                        "switch the Renderer (e.g. to OpenGL / Compatibility), "
                        "then restart BlueStacks -- the renderer can stop "
                        "exposing frames to screencap.")
                    log("  (Otherwise the device screen may be off.) Waiting...")
                elif consec_black % 10 == 0:
                    log(f"  still all-black ({consec_black} in a row)...")
                if consec_black >= BLACK_GIVE_UP:
                    log("giving up after a long run of black frames; stopping")
                    break
                _interruptible_sleep(rand_delay(config.POLL_INTERVAL), stop_event)
                continue
            except ADBError as e:
                log(f"ADB error: {e}")
                log("device disconnected; stopping")
                break

            if state in ("idle", "blank") and state != last_state:
                if state == "blank":
                    log("WARNING: ADB screencap is blank -- make sure the "
                        "device screen is on and the app is in the foreground")
                else:
                    log("in-game, waiting")
            last_state = state

            _interruptible_sleep(rand_delay(config.POLL_INTERVAL), stop_event)
    except KeyboardInterrupt:
        log("stopped by user")

    if stream is not None:
        stream.stop()

    if timed_out or stuck_out:
        if config.SLEEP_PHONE_ON_TIMEOUT:
            adb_client.sleep_phone()
        if config.CLOSE_ON_TIMEOUT:
            log("closing the game window")
            close_target()
    log("macro stopped")


def main():
    run_macro()


if __name__ == "__main__":
    main()
