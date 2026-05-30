"""
A2 Macro Controller: main loop.

Watches the game window (BlueStacks or a scrcpy phone mirror), identifies the
current state via template matching

Usage:
    python main.py        # head-less with saved settings
    python gui.py         # graphical control panel
"""

import random
import subprocess
import sys
import time
from pathlib import Path

import pyautogui

import config
from matcher import (
    crop_band,
    grab_screen_bgr,
    is_blank,
    list_skill_files,
    load_template,
    make_scales,
    multi_scale_match,
    skill_hash,
)

pyautogui.FAILSAFE = True   # mouse to a screen corner aborts
pyautogui.PAUSE = 0.05

# Keeps the subprocess helpers (taskkill, tasklist, powershell, adb) from
# flashing a console window in PyInstaller's --windowed build. 0 where absent.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# logging
# Optional sink that mirrors every log line elsewhere (the GUI shows it).
_log_sink = None


def set_log_sink(sink):
    """Install a callable that receives every formatted log line, or None."""
    global _log_sink
    _log_sink = sink


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # A --windowed build has no console (sys.stdout is None), so guard print;
    # the GUI sink still shows the line.
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass
    if _log_sink is not None:
        try:
            _log_sink(line)
        except Exception:
            pass   # a broken sink must never kill the macro


def click(x, y, label):
    """Move to (x, y) and click, with a few px of jitter and randomised
    timing so input is neither pixel-perfect nor perfectly periodic."""
    jx = x + random.randint(-config.CLICK_JITTER, config.CLICK_JITTER)
    jy = y + random.randint(-config.CLICK_JITTER, config.CLICK_JITTER)
    log(f"  click {label} @ ({jx}, {jy})")
    pyautogui.moveTo(jx, jy, duration=random.uniform(*config.MOVE_DURATION_RANGE))
    time.sleep(random.uniform(*config.CLICK_DELAY_RANGE))
    pyautogui.click()


def rand_delay(base):
    """`base` seconds randomised by +/- config.DELAY_JITTER fraction."""
    return base * random.uniform(1.0 - config.DELAY_JITTER,
                                 1.0 + config.DELAY_JITTER)


def _interruptible_sleep(seconds, stop_event):
    """Sleep `seconds`, waking immediately if `stop_event` is set."""
    if stop_event is not None:
        stop_event.wait(seconds)
    else:
        time.sleep(seconds)


def close_target():
    """Force-close the game-window processes in config.CLOSE_PROCESSES via
    taskkill. The list covers both backends, so a process not running is just
    reported and skipped."""
    for name in config.CLOSE_PROCESSES:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", name],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            log(f"  closed {name}")
        else:
            log(f"  {name} not running")   # taskkill non-zero = not running


# phone power (scrcpy)
# Killing scrcpy.exe leaves the mirrored phone awake on the game (overheating),
# so these reach the phone over adb to turn its screen off on timeout.

def _is_running(image_name):
    """True if a process with this .exe image name is running."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return image_name.lower() in result.stdout.lower()


def _running_process_dir(image_name):
    """Folder of a running process by .exe image name, or None."""
    stem = image_name[:-4] if image_name.lower().endswith(".exe") else image_name
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Name '{stem}' -ErrorAction SilentlyContinue "
             f"| Select-Object -First 1).Path"],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = result.stdout.strip()
    if path and Path(path).exists():
        return Path(path).parent
    return None


def _find_adb():
    """Locate adb: prefer the adb.exe bundled next to a running scrcpy.exe,
    then fall back to config.ADB_PATH (default "adb" on PATH)."""
    scrcpy_dir = _running_process_dir("scrcpy.exe")
    if scrcpy_dir is not None:
        adb = scrcpy_dir / "adb.exe"
        if adb.exists():
            return str(adb)
    return config.ADB_PATH


def sleep_phone():
    """Turn the scrcpy-connected phone's screen off via adb (KEYCODE_SLEEP).
    Failures are logged, never raised."""
    adb = _find_adb()
    log("  phone: turning the screen off via adb (KEYCODE_SLEEP)")
    try:
        result = subprocess.run(
            [adb, "shell", "input", "keyevent", "223"],  # 223 = KEYCODE_SLEEP
            capture_output=True, text=True, timeout=20,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        log(f"  phone: adb not found ('{adb}'), add scrcpy's folder to "
            f"PATH, or set ADB_PATH in config.py")
        return
    except (OSError, subprocess.SubprocessError) as e:
        log(f"  phone: adb call failed, {e!r}")
        return
    if result.returncode == 0:
        log("  phone: screen off")
    else:
        err = (result.stderr or result.stdout or "").strip()
        log(f"  phone: adb could not sleep the device, {err or 'unknown'}")


class Macro:
    def __init__(self):
        self.skill_scales = make_scales(config.SCALE_RANGE)
        self.ref_scales = make_scales(config.REF_SCALE_RANGE)

        # Required UI refs.
        self.refs = {}
        for key, path in (("devil", config.REF_DEVIL),
                          ("game_over", config.REF_GAME_OVER),
                          ("play", config.REF_PLAY),
                          ("get_ready", config.REF_GET_READY),
                          ("continue", config.REF_CONTINUE),
                          ("start_challenge", config.REF_START_CHALLENGE),
                          ("refresh", config.REF_REFRESH)):
            self.refs[key] = load_template(path)

        # Skill-screen banners, any one detects the screen. Optional: a banner
        # not captured yet is skipped.
        self.skill_banners = []
        for key, path in (("valkyrie", config.REF_VALKYRIE),
                          ("level", config.REF_LEVEL),
                          ("glory", config.REF_GLORY)):
            if path.exists():
                self.refs[key] = load_template(path)
                self.skill_banners.append(key)
            else:
                log(f"NOTE: skill banner '{path.name}' not captured yet, skipping")
        if not self.skill_banners:
            log("WARNING: no skill-screen banners available, "
                "skill selection will not be detected")

        # Custom press-it buttons (user-captured in ref/custom/), clicked on
        # sight like the built-in buttons.
        self.custom_refs = []   # [(name, template), ...]
        for path in sorted(config.REF_CUSTOM_DIR.glob("*.png")):
            try:
                self.custom_refs.append((path.stem, load_template(path)))
            except (FileNotFoundError, ValueError):
                log(f"NOTE: custom button '{path.name}' could not be loaded"
                    f", skipping")
        if self.custom_refs:
            log(f"loaded {len(self.custom_refs)} custom button(s): "
                + ", ".join(n for n, _ in self.custom_refs))

        # Priority skills grouped by active category, checked in
        # ACTIVE_CATEGORIES order, best-to-worst within each. Avoided skills
        # (matched by content hash, so all copies count) are excluded here.
        avoid_hashes = set()
        for ident in config.AVOID_SKILLS:
            h = skill_hash(config.SKILLS_DIR / ident)
            if h is not None:
                avoid_hashes.add(h)

        self.skill_categories = []   # [(category, [(name, template), ...]), ...]
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
            log(f"avoid list excludes {len(excluded)} skill file(s) from "
                f"priority picks: {', '.join(excluded)}")
        if total == 0:
            log("WARNING: no skill icons found, the macro will always fall "
                "back to the first skill slot")

        # Custom priority skills, picked above ALL categories in added order.
        # Avoided skills are excluded (the avoid list still wins).
        self.custom_priority = []   # [(identifier, template), ...]
        for ident in config.CUSTOM_PRIORITY_SKILLS:
            path = config.SKILLS_DIR / ident
            if not path.exists():
                log(f"NOTE: custom priority skill '{ident}' image not found"
                    f", skipping")
                continue
            if avoid_hashes and skill_hash(path) in avoid_hashes:
                log(f"NOTE: custom priority skill '{ident}' is also on the "
                    f"avoid list, the avoid list wins, skipping")
                continue
            self.custom_priority.append((ident, load_template(path)))
        if self.custom_priority:
            log(f"loaded {len(self.custom_priority)} custom priority skill(s)")

        # Avoid skills, matched against the first slot before the fallback.
        self.avoid_skills = []   # [(identifier, template), ...]
        for ident in config.AVOID_SKILLS:
            path = config.SKILLS_DIR / ident
            if path.exists():
                self.avoid_skills.append((ident, load_template(path)))
            else:
                log(f"NOTE: avoid skill '{ident}' image not found, skipping")
        if self.avoid_skills:
            log(f"loaded {len(self.avoid_skills)} avoid skill(s)")

        self._warn_blank_refs()

    def _warn_blank_refs(self):
        """Flag reference images that look like blank/placeholder captures."""
        blank = [name for name, tpl in self.refs.items() if is_blank(tpl)]
        if blank:
            log("WARNING: these ref images look blank and will never match: "
                + ", ".join(blank))
            log("         re-capture them (see SETUP.md step 5 / run diagnose.py)")

    # detection
    def _find_ref(self, image, key):
        conf, center, _ = multi_scale_match(image, self.refs[key], self.ref_scales)
        return conf, center

    def _best_skill_banner(self, image):
        """Return (label, confidence) for the best-matching banner. The label
        is only for logging; any banner over threshold means the skill screen
        is up."""
        best_label, best_conf = None, -1.0
        for key in self.skill_banners:
            conf, _ = self._find_ref(image, key)
            if conf > best_conf:
                best_label, best_conf = key, conf
        return best_label, best_conf

    def _find_best_skill(self, image):
        """Return (category, name, confidence, center) of the best skill on
        screen, or (None, None, 0.0, None) if none clear the threshold.

        Custom priority skills first, then active categories in priority order,
        best-to-worst within each. Stops at the first icon over threshold.
        Centres are in full-region coordinates.
        """
        band_img, y_offset = crop_band(image, config.SKILL_MATCH_BAND)

        # 1. Custom priority skills, above every category.
        for ident, tpl in self.custom_priority:
            conf, center, _ = multi_scale_match(
                band_img, tpl, self.skill_scales, config.SKILL_DOWNSCALE)
            if conf >= config.MATCH_THRESHOLD and center is not None:
                return ("custom", ident, conf,
                        (center[0], center[1] + y_offset))

        # 2. Active categories, in priority order.
        for category, skills in self.skill_categories:
            for name, tpl in skills:
                conf, center, _ = multi_scale_match(
                    band_img, tpl, self.skill_scales, config.SKILL_DOWNSCALE)
                if conf >= config.MATCH_THRESHOLD and center is not None:
                    return (category, name, conf,
                            (center[0], center[1] + y_offset))
        return None, None, 0.0, None

    def _avoid_skill_in_slot(self, image, slot_screen):
        """Identifier of the avoid skill shown in the slot at absolute coords
        `slot_screen`, or None. Only a card-sized box (SKILL_SLOT_BOX) around
        the slot is checked."""
        if not self.avoid_skills:
            return None
        img_h, img_w = image.shape[:2]
        cx = slot_screen[0] - config.REGION_X
        cy = slot_screen[1] - config.REGION_Y
        bw, bh = config.SKILL_SLOT_BOX
        x0, x1 = max(0, cx - bw // 2), min(img_w, cx + bw // 2)
        y0, y1 = max(0, cy - bh // 2), min(img_h, cy + bh // 2)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        crop = image[y0:y1, x0:x1]
        for ident, tpl in self.avoid_skills:
            conf, center, _ = multi_scale_match(
                crop, tpl, self.skill_scales, config.SKILL_DOWNSCALE)
            if conf >= config.MATCH_THRESHOLD and center is not None:
                return ident
        return None

    def _to_screen(self, center):
        """Region-relative point -> absolute screen coordinates."""
        return config.REGION_X + center[0], config.REGION_Y + center[1]

    # one iteration
    def step(self):
        """Inspect the screen once, act on it, and return a state string."""
        image = grab_screen_bgr(config.BLUESTACKS_REGION)

        if is_blank(image):
            return "blank"

        # 1. Devil offer, checked first so the skill-pick fallback never
        #    accidentally accepts the deal.
        conf, center = self._find_ref(image, "devil")
        if conf >= config.REF_THRESHOLD:
            log(f"devil offer (conf {conf:.2f}) -> reject")
            click(*self._to_screen(center), "devil reject")
            time.sleep(rand_delay(config.ACTION_DELAY))
            return "devil"

        # 2. Skill selection, recognised by any banner.
        indicator, iconf = self._best_skill_banner(image)
        if iconf >= config.REF_THRESHOLD:
            category, name, sconf, scenter = self._find_best_skill(image)
            if name:
                log(f"skill select [{indicator} {iconf:.2f}] -> "
                    f"{category} / {name} (conf {sconf:.2f})")
                click(*self._to_screen(scenter), f"skill {category}/{name}")
            else:
                # No wanted skill. Reroll while refresh is available; once it
                # vanishes (refreshes gone), take a slot.
                rconf, rcenter = self._find_ref(image, "refresh")
                if rconf >= config.REF_THRESHOLD:
                    log(f"skill select [{indicator} {iconf:.2f}] -> no wanted "
                        f"skill, refresh available ({rconf:.2f}) -> reroll")
                    click(*self._to_screen(rcenter), "refresh")
                else:
                    # Take the first slot, unless it shows an avoid skill, then
                    # the second.
                    avoided = self._avoid_skill_in_slot(
                        image, config.FIRST_SKILL_SLOT)
                    if avoided:
                        log(f"skill select [{indicator} {iconf:.2f}] -> no "
                            f"wanted skill, first slot is avoid skill "
                            f"({avoided}) -> second slot")
                        click(*config.SECOND_SKILL_SLOT, "second skill slot")
                    else:
                        log(f"skill select [{indicator} {iconf:.2f}] -> no "
                            f"wanted skill, no refresh left -> first slot")
                        click(*config.FIRST_SKILL_SLOT, "first skill slot")
            time.sleep(rand_delay(config.ACTION_DELAY))
            return "skill"

        # 3. Game over / results.
        conf, center = self._find_ref(image, "game_over")
        if conf >= config.REF_THRESHOLD:
            log(f"game over (conf {conf:.2f}) -> dismiss")
            click(*config.GAME_OVER_TAP, "dismiss")
            time.sleep(rand_delay(config.ACTION_DELAY))
            return "game_over"

        # 4. "Press it when you see it" buttons (Play + get-ready / continue /
        #    start-challenge), checked in order; first hit wins.
        for key, label in (("play", "lobby play"),
                           ("get_ready", "get ready"),
                           ("continue", "continue"),
                           ("start_challenge", "start challenge")):
            conf, center = self._find_ref(image, key)
            if conf >= config.REF_THRESHOLD:
                log(f"{label} (conf {conf:.2f}) -> click")
                click(*self._to_screen(center), label)
                time.sleep(rand_delay(config.ACTION_DELAY))
                return key

        # 4b. Custom press-it buttons (user-captured in ref/custom/).
        for name, tpl in self.custom_refs:
            conf, center, _ = multi_scale_match(image, tpl, self.ref_scales)
            if conf >= config.REF_THRESHOLD and center is not None:
                log(f"custom button '{name}' (conf {conf:.2f}) -> click")
                click(*self._to_screen(center), f"custom: {name}")
                time.sleep(rand_delay(config.ACTION_DELAY))
                return "custom"

        # 5. Nothing actionable, the character is playing.
        return "idle"


# scale calibration
# The bundled templates were captured in BlueStacks fullscreen. At any other
# window size every template renders (and matches) at a different scale.
# measure_game_scale() recovers the current scale from one screenshot so the
# GUI can retune SCALE_RANGE / REF_SCALE_RANGE.

# Coarse brute-force sweep (min, max, step): small scrcpy window through large.
# The step is coarse on purpose; the refine step pins it down afterwards.
_SCAN_RANGE = (0.20, 1.80, 0.05)

# Reduced resolution for the scan (multi_scale_match maps sizes back).
_SCAN_DOWNSCALE = 0.5

# Fine local sweep step after the coarse scan, so the runtime can trust a
# single scale per match instead of sweeping a margin every frame.
_REFINE_STEP = 0.0125

# A UI ref at or above this confidence is trusted outright, skipping the
# (larger) skill-icon scan, the common fast path on a skill screen.
_REF_EARLY_EXIT = 0.80

# The final best match must clear this, or calibration is rejected.
_SCAN_MIN_CONF = 0.60


def _load_all_skill_templates():
    """Load every UNIQUE skill icon across all categories, de-duplicated by
    content (the same skill is copied byte-identically into several folders).
    Active/avoid lists are ignored: any on-screen icon works for calibration.
    Returns [(label, template), ...]."""
    out = []
    seen = set()
    for category in config.SKILL_CATEGORIES:
        for path in list_skill_files(config.SKILLS_DIR / category):
            h = skill_hash(path)
            if h is not None:
                if h in seen:
                    continue
                seen.add(h)
            try:
                out.append((f"{category}/{path.name}", load_template(path)))
            except (FileNotFoundError, ValueError):
                pass
    return out


def _load_calibration_refs():
    """Load the UI reference templates usable as scale references. Returns
    [(label, template), ...]; missing/unreadable files are skipped."""
    out = []
    for label, path in (("valkyrie", config.REF_VALKYRIE),
                        ("level", config.REF_LEVEL),
                        ("glory", config.REF_GLORY),
                        ("refresh", config.REF_REFRESH),
                        ("play", config.REF_PLAY),
                        ("game_over", config.REF_GAME_OVER),
                        ("devil", config.REF_DEVIL),
                        ("get_ready", config.REF_GET_READY),
                        ("continue", config.REF_CONTINUE),
                        ("start_challenge", config.REF_START_CHALLENGE)):
        try:
            out.append((label, load_template(path)))
        except (FileNotFoundError, ValueError):
            pass
    return out


def _scan_templates(image, templates, scales, label):
    """Brute-match every template against `image`.

    `templates` is [(kind, name, tpl), ...]. Returns the best
    (conf, scale, name, kind, tpl), or None. Progress is logged under `label`.
    """
    best = None
    progress_every = max(1, len(templates) // 4)
    for i, (kind, name, tpl) in enumerate(templates, start=1):
        conf, _, size = multi_scale_match(image, tpl, scales, _SCAN_DOWNSCALE)
        if size is not None and (best is None or conf > best[0]):
            best = (conf, size[0] / tpl.shape[1], name, kind, tpl)
        if i % progress_every == 0 or i == len(templates):
            seen = f"{best[2]} {best[0]:.2f}" if best else "none"
            log(f"  {label}: scanned {i}/{len(templates)} (best: {seen})")
    return best


def _refine_match_scale(image, tpl, coarse_scale):
    """Pin a coarse scale down with a fine local sweep on one template.
    Sweeps +/- one coarse step at _REFINE_STEP. Returns (confidence, scale)
    of the fine peak, or None."""
    coarse_step = _SCAN_RANGE[2]
    lo = max(_REFINE_STEP, coarse_scale - coarse_step)
    hi = coarse_scale + coarse_step
    conf, _, size = multi_scale_match(
        image, tpl, make_scales((lo, hi, _REFINE_STEP)), _SCAN_DOWNSCALE)
    if size is None:
        return None
    return conf, size[0] / tpl.shape[1]


def measure_game_scale(image):
    """Work out the game's on-screen scale from a single screenshot.

    `image` is a BGR capture, ideally an active skill-selection screen. UI refs
    are matched first (a confident one is used outright); otherwise skill icons
    are scanned too and the highest-confidence match wins. The matched scale
    and its family's baseline give the zoom, hence the scale both families
    should match at.

    Returns a dict: on success ok=True with conf, name, kind ("skill"/"ref"),
    matched_scale, zoom, skill_scale, ref_scale; on failure ok=False + reason.
    """
    if is_blank(image):
        return {"ok": False, "reason": "the screen capture is blank/black"}

    refs = [("ref", n, t) for n, t in _load_calibration_refs()]
    skills = [("skill", n, t) for n, t in _load_all_skill_templates()]
    if not refs and not skills:
        return {"ok": False,
                "reason": "no skill icons or reference images found to match"}

    scales = make_scales(_SCAN_RANGE)
    log(f"scale calibration: sweeping {len(scales)} scales "
        f"({_SCAN_RANGE[0]:g}-{_SCAN_RANGE[1]:g}); "
        f"{len(refs)} UI ref(s), {len(skills)} skill icon(s)...")

    # UI refs first; a confident match skips the skill scan.
    best = _scan_templates(image, refs, scales, "UI refs") if refs else None
    if (best is None or best[0] < _REF_EARLY_EXIT) and skills:
        log("  no confident UI-ref match, also scanning skill icons...")
        skill_best = _scan_templates(image, skills, scales, "skill icons")
        if skill_best is not None and (best is None
                                       or skill_best[0] > best[0]):
            best = skill_best

    if best is None:
        return {"ok": False, "reason": "no template could be matched at all"}

    conf, matched_scale, name, kind, tpl = best

    # Pin the coarse scale down so the derived zoom is accurate enough to drop
    # the safety margin.
    refined = _refine_match_scale(image, tpl, matched_scale)
    if refined is not None:
        rconf, rscale = refined
        log(f"  refined scale {matched_scale:.3f} -> {rscale:.3f} "
            f"(conf {rconf:.2f})")
        matched_scale = rscale
        conf = max(conf, rconf)

    if conf < _SCAN_MIN_CONF:
        return {"ok": False,
                "reason": (f"best match '{name}' only reached confidence "
                           f"{conf:.2f} (need >= {_SCAN_MIN_CONF:.2f}); "
                           f"make sure a skill-selection screen fills the "
                           f"capture region")}

    baseline = (config.SKILL_SCALE_BASELINE if kind == "skill"
                else config.REF_SCALE_BASELINE)
    if not baseline or baseline <= 0:
        return {"ok": False, "reason": "invalid scale baseline in config"}

    zoom = matched_scale / baseline
    return {
        "ok": True, "conf": conf, "name": name, "kind": kind,
        "matched_scale": matched_scale, "zoom": zoom,
        "skill_scale": config.SKILL_SCALE_BASELINE * zoom,
        "ref_scale": config.REF_SCALE_BASELINE * zoom,
    }


def run_macro(stop_event=None):
    """Run the macro loop until stopped, timed out, or the fail-safe fires.

    `stop_event` is an optional threading.Event; when set the loop exits at the
    next check. Config is read here, so the GUI applies settings before a run.
    """
    log("A2 Macro Controller")
    log(f"region={config.BLUESTACKS_REGION}  first-slot={config.FIRST_SKILL_SLOT}")
    log("active skill categories: "
        + (", ".join(config.ACTIVE_CATEGORIES) or "(none)"))

    try:
        macro = Macro()
    except (FileNotFoundError, ValueError) as e:
        log(f"FATAL: {e}")
        return

    log(f"focus the game window (BlueStacks or scrcpy) now, starting in "
        f"{config.STARTUP_DELAY:.0f}s")
    _interruptible_sleep(config.STARTUP_DELAY, stop_event)
    if stop_event is not None and stop_event.is_set():
        log("stopped before the run started")
        return

    # Run timeout: stop (and close the window if CLOSE_ON_TIMEOUT) after
    # RUN_TIMEOUT_HOURS. 0 / None disables it.
    timeout_hours = config.RUN_TIMEOUT_HOURS or 0
    deadline = time.monotonic() + timeout_hours * 3600 if timeout_hours else None
    if deadline:
        log(f"running, timeout in {timeout_hours:g}h; Ctrl+C, the Stop "
            "button, or the mouse in a screen corner stops it")
    else:
        log("running, Ctrl+C, the Stop button, or the mouse in a screen "
            "corner stops it")

    timed_out = False
    last_state = None
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                log("stop requested, stopping")
                break
            if deadline and time.monotonic() >= deadline:
                log(f"run timeout reached ({timeout_hours:g}h), stopping")
                timed_out = True
                break

            try:
                state = macro.step()
            except pyautogui.FailSafeException:
                log("fail-safe triggered, stopping")
                break

            # Log quiet states only when they first occur.
            if state in ("idle", "blank") and state != last_state:
                if state == "blank":
                    log("WARNING: capture is blank/black, on BlueStacks set "
                        "the graphics renderer to OpenGL; on scrcpy make sure "
                        "the mirror window is visible and the phone screen is "
                        "on")
                else:
                    log("in-game, waiting")
            last_state = state

            _interruptible_sleep(rand_delay(config.POLL_INTERVAL), stop_event)
    except KeyboardInterrupt:
        log("stopped by user")

    if timed_out:
        # Sleep the phone (scrcpy only) before closing the window; the two are
        # independent timeout actions.
        if config.SLEEP_PHONE_ON_TIMEOUT and _is_running("scrcpy.exe"):
            sleep_phone()
        if config.CLOSE_ON_TIMEOUT:
            log("closing the game window")
            close_target()
    log("macro stopped")


def main():
    run_macro()


if __name__ == "__main__":
    main()
