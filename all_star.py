"""All-Star Cup macro mode (plus the Elim variant).

A heavily speed-optimised mode for the All-Star Cup event.  In place of the
usual skill picks, each level presents three boss-selection sets.  The macro
scans the three card slots, identifies which bosses are showing, and selects
the visible one with the LOWEST death-animation duration (fastest to despawn),
so the same logic works for every event with no per-event retargeting.  Every
boss template is scanned by default (ALL_STAR_SCAN_ALL_BOSSES): the per-level
line-ups drift between events (a color variant from another level's row can
swap in), so a pool-only scan would miss the drifted bosses.

Elim mode (config.ALL_STAR_LEVEL = "elim"): ten levels, each starting with the
same 3-boss popup.  The user pre-picks one boss per level in the GUI
(config.ALL_STAR_ELIM_BOSSES) and each level's scan looks ONLY for that boss;
a level without a pick falls back to the fastest-dying visible boss.

Why it is fast: instead of the normal whole-band, all-categories skill scan,
it matches pre-scaled boss templates against only the three fixed card slots,
at half resolution, reading every decoded frame of the screenrecord stream.
The FIRST frame that identifies any boss decides a set -- no settling delay.

Sequence per the user's spec:
  1. Find and tap the Challenge button to start the level.
  2. Spam-tap an empty spot to skip loading while scanning the slots; the
     moment the best visible boss is known, stop and briefly spam-tap its slot.
  3. WAIT (without tapping) for the screen to go black: the player fights the
     chosen boss (~10-20s) and the black screen marks the next selection.
  4. Repeat for the remaining sets (3 in All-Star, 10 in Elim).  After the
     last set, All-Star watches the boss health bar to pause on the kill;
     Elim just stops.
"""

import random
import threading
import time

import cv2

import capture
import config
import fastinput
from adb import ADBClient, ADBError, choose_server_port
from main import _interruptible_sleep, activity, log
from matcher import load_template, multi_scale_match

# How long to sleep when the stream has not produced a new frame yet, so the
# scan loop does not busy-spin a CPU core re-matching the same frame.  Well under
# one frame interval (~16ms at 60fps), so no new frame is ever missed.
_IDLE_SLEEP = 0.003


class AllStarMacro:
    """Plays one All-Star level's three boss-selection sets, or the ten Elim
    levels."""

    def __init__(self, adb_client: ADBClient, stream, level):
        self.adb = adb_client
        self._stream = stream
        self.level = level
        self.elim = (level == "elim")

        # Death anims for EVERY boss in every level: the line-ups drift
        # between events (a color variant from another level's row can swap
        # in), so any boss can show anywhere and its duration lives under its
        # own level's row.
        self._anim = {n: s for lp in config.ALL_STAR_LEVELS.values()
                      for n, s in lp.items()}

        # Boss templates (black-bg, pre-scaled to on-screen size at the
        # calibration width, built by make_boss_templates.py).  Names with no
        # template file simply cannot be identified, which the scan tolerates.
        self._templates = {}
        for name in self._anim:
            try:
                self._templates[name] = load_template(config.ALL_STAR_BOSS_DIR
                                                      / f"{name}.png")
            except (FileNotFoundError, ValueError):
                pass
        if not self._templates:
            raise ValueError(f"All-Star: no boss templates in "
                             f"{config.ALL_STAR_BOSS_DIR}")

        if self.elim:
            self.pool = {}
            self.n_sets = int(config.ALL_STAR_ELIM_SETS)
            self.targets = self._elim_targets()
        else:
            pool = config.ALL_STAR_LEVELS.get(level)
            if not pool:
                raise ValueError(f"All-Star: no boss pool for level {level}")
            self.pool = pool             # this level's boss name -> death-anim s
            self.n_sets = 3
            self.targets = None
            missing = [n for n in pool if n not in self._templates]
            if missing:
                log(f"All-Star WARNING: {len(missing)} of {len(pool)} "
                    f"level-{level} bosses have no template in "
                    f"{config.ALL_STAR_BOSS_DIR.name}/ and cannot be "
                    f"identified: {', '.join(sorted(missing))}")
        # The Challenge button renders its label in the game language; prefer the
        # active-language variant (ref/<lang>/all_star/challenge.png) if captured.
        self._challenge = load_template(config.ref_path(config.ALL_STAR_CHALLENGE))

        # Bosses already shown in earlier sets this run (All-Star only: a boss
        # appears in at most one of a level's three sets, so anything
        # identified in one set is excluded from later candidates.  Elim
        # levels may re-offer a boss, so nothing is excluded there).
        self._seen = set()

        # Geometry / scaling, resolved from the first real frame (the stream is
        # authoritative for the pixel grid).
        self._w = self._h = None
        self._factor = 1.0          # live_width / calibration_width
        self._boxes = None          # [(x0,y0,x1,y1), ...] one per slot
        self._cur = {}              # current set's candidates, pre-scaled + downscaled
        self._ds = config.ALL_STAR_SCAN_DOWNSCALE

        # Low-latency tapper (raw /dev/input writes).  Set up lazily once the
        # frame size is known; falls back to adb.tap if it cannot start.
        self._tapper = fastinput.FastTapper(adb_client)
        self._tapper_ready = False
        self._fast_warned = False   # dedupe the adb.tap fallback log

    def _elim_targets(self):
        """The user's per-level Elim boss picks, validated and padded to
        n_sets entries.  None = no pick for that level; the scan then falls
        back to the fastest-dying visible boss."""
        raw = [str(n).strip() for n in
               getattr(config, "ALL_STAR_ELIM_BOSSES", [])]
        targets = []
        for i in range(self.n_sets):
            name = raw[i] if i < len(raw) else ""
            if not name:
                targets.append(None)
            elif name in self._templates:
                targets.append(name)
            else:
                log(f"All-Star WARNING: Elim pick for level {i + 1} "
                    f"('{name}') has no template; falling back to the best "
                    "visible boss for that level")
                targets.append(None)
        return targets

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _grab(self):
        """Latest decoded stream frame (native BGR).  Bridges the brief gap
        between screenrecord segments with the last fresh frame; a longer stall
        triggers one stream-recovery attempt before giving up."""
        frame = self._stream.latest(max_age=2.0)
        if frame is not None:
            return frame
        deadline = time.time() + 2.0
        while time.time() < deadline:
            frame = self._stream.latest(max_age=2.0)
            if frame is not None:
                return frame
            time.sleep(0.05)
        frame = self._recover_stream()
        if frame is not None:
            return frame
        raise ADBError("capture stream lost and recovery failed (screen off "
                       "or USB/ADB unstable).")

    def _recover_stream(self):
        log("All-Star: capture stream stalled, attempting recovery...")
        self._stream.stop()
        self.adb.reconnect()
        stream = capture.open_stream(self.adb.adb_exe, self.adb.device,
                                     attempts=3, per_attempt_timeout=4.0,
                                     on_log=log)
        if stream is None:
            log("  stream recovery failed")
            return None
        self._stream = stream
        log("  stream recovered")
        return stream.latest(max_age=3.0)

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _ensure_geometry(self, frame):
        """Compute the slot boxes and the resolution scale factor once, from the
        live frame size."""
        h, w = frame.shape[:2]
        if self._w == w and self._h == h:
            return
        self._w, self._h = w, h
        self._factor = w / config.ALL_STAR_CALIB_WIDTH
        hw = round(config.ALL_STAR_SLOT_HALF_W_RATIO * w)
        hh = round(config.ALL_STAR_SLOT_HALF_H_RATIO * h)
        cy = round(config.ALL_STAR_SLOT_Y_RATIO * h)
        self._boxes = []
        for xr in config.ALL_STAR_SLOT_X_RATIOS:
            cx = round(xr * w)
            self._boxes.append((max(0, cx - hw), max(0, cy - hh),
                                min(w, cx + hw), min(h, cy + hh)))
        log(f"All-Star: frame {w}x{h}, scale {self._factor:.3f}, "
            f"slot x={[round(xr * w) for xr in config.ALL_STAR_SLOT_X_RATIOS]} "
            f"y={cy}")
        if not self._tapper_ready:
            self._tapper_ready = True
            # All-Star keeps its own FAST_TAP_ENABLED gate (unchanged by the new
            # ROOT_FAST_INPUT master toggle, which only extends fast taps to the
            # other modes).  Humanization is one global behavior, so All-Star
            # honours HUMANIZED_TAPS too (it slows the mode down; user's call).
            self._tapper.setup(w, h, on_log=log,
                               enabled=getattr(config, "FAST_TAP_ENABLED", True),
                               humanize=getattr(config, "HUMANIZED_TAPS", False))

    def _prep_candidates(self, names):
        """Pre-scale this set's candidate templates to (resolution factor *
        scan downscale) so the per-frame scan only resizes the small slot
        crops."""
        eff = self._factor * self._ds
        interp = cv2.INTER_AREA if eff < 1.0 else cv2.INTER_LINEAR
        self._cur = {}
        for name in names:
            t0 = self._templates[name]
            nw = max(1, round(t0.shape[1] * eff))
            nh = max(1, round(t0.shape[0] * eff))
            self._cur[name] = cv2.resize(t0, (nw, nh), interpolation=interp)

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _scan_slots(self, frame, found):
        """Try to identify the boss in each still-unresolved slot by matching
        every unassigned candidate against the slot crop at half resolution.
        found maps slot_index -> (name, conf) and is updated in place with any
        new identification at or above threshold (each boss can claim only one
        slot).  Returns the best confidence seen this frame, for diagnostics."""
        assigned = {name for name, _ in found.values()}
        best_seen = -1.0
        for i, (x0, y0, x1, y1) in enumerate(self._boxes):
            if i in found:
                continue
            crop = frame[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            small = cv2.resize(crop, None, fx=self._ds, fy=self._ds,
                               interpolation=cv2.INTER_AREA)
            best_name, best_c = None, -1.0
            for name, tpl in self._cur.items():
                if name in assigned:
                    continue
                if tpl.shape[0] > small.shape[0] or tpl.shape[1] > small.shape[1]:
                    continue
                conf = float(cv2.minMaxLoc(
                    cv2.matchTemplate(small, tpl, cv2.TM_CCOEFF_NORMED))[1])
                if conf > best_c:
                    best_c, best_name = conf, name
                    if best_c >= config.ALL_STAR_THRESHOLD:
                        # A threshold hit identifies reliably (the worst
                        # look-alike self-mismatch is ~0.81); skip the rest.
                        break
            best_seen = max(best_seen, best_c)
            if best_name is not None and best_c >= config.ALL_STAR_THRESHOLD:
                found[i] = (best_name, best_c)
                assigned.add(best_name)
        return best_seen

    def _is_black(self, frame):
        """True on a black transition screen.  Sampled (strided) for speed."""
        return float(frame[::16, ::16].mean()) < config.ALL_STAR_BLACK_MEAN

    def _find_challenge(self, frame):
        """Return the Challenge button centre (native px) if visible, else None."""
        scales = [round(self._factor * s, 4) for s in (0.94, 1.0, 1.06)]
        conf, center, _ = multi_scale_match(frame, self._challenge, scales,
                                             downscale=0.5)
        if conf >= config.ALL_STAR_CHALLENGE_THRESHOLD and center is not None:
            return center
        return None

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _tap(self, x, y):
        """A lean tap (small jitter, no pre-delay) so spam-tapping stays fast.

        Uses the raw /dev/input fast tapper when available (about 0.4ms vs ~35ms
        for `adb input tap`); falls back to adb.tap if the fast path fails."""
        j = config.CLICK_JITTER
        tx, ty = x + random.randint(-j, j), y + random.randint(-j, j)
        tapper = self._tapper
        if tapper is not None and tapper.available:
            try:
                tapper.tap(tx, ty)
                if self._fast_warned:
                    log("All-Star: fast-tap recovered")
                    self._fast_warned = False
                return
            except fastinput.FastTapError as exc:
                if not self._fast_warned:    # log once per failure episode, not per tap
                    log(f"All-Star: fast-tap failed ({exc}); falling back to "
                        "adb input tap")
                    self._fast_warned = True
        self.adb.tap(tx, ty)

    def close(self):
        """Release any held touch and tear down the fast tapper."""
        if self._tapper is not None:
            self._tapper.close()

    def _empty_point(self):
        return (round(config.ALL_STAR_TAP_X_RATIO * self._w),
                round(config.ALL_STAR_TAP_Y_RATIO * self._h))

    def _slot_center(self, slot):
        return (round(config.ALL_STAR_SLOT_X_RATIOS[slot] * self._w),
                round(config.ALL_STAR_SLOT_Y_RATIO * self._h))

    def _spam(self, get_point, stop_event):
        """Tap get_point() repeatedly on a BACKGROUND thread, so the main loop
        can scan / black-check at the full stream frame rate instead of being
        throttled to ~20Hz by the ~50ms each `adb input tap` blocks for.  That is
        what makes catching the brief (~5 frame) window the boss sits small in
        reliable: the scanner sees every decoded frame, not every ~3rd one.  Only
        this thread taps during a phase (the main thread just reads the stream),
        so there is never a concurrent tap.  Returns (stop_event, thread); set
        the event and join to end it."""
        done = threading.Event()

        def loop():
            while not done.is_set() and not stop_event.is_set():
                x, y = get_point()
                self._tap(x, y)
                done.wait(config.ALL_STAR_TAP_INTERVAL)

        thread = threading.Thread(target=loop, name="all-star-spam", daemon=True)
        thread.start()
        return done, thread

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    def _start_level(self, stop_event):
        """Find and tap the Challenge button.  Keep tapping while it is visible;
        once it disappears the level has started.  If it never appears within
        the timeout, assume the user already started the level and proceed."""
        log("All-Star: looking for the Challenge button...")
        deadline = time.time() + config.ALL_STAR_START_TIMEOUT
        saw = False
        while time.time() < deadline:
            if stop_event.is_set():
                return
            frame = self._grab()
            self._ensure_geometry(frame)
            center = self._find_challenge(frame)
            if center is not None:
                self._tap(*center)
                saw = True
                _interruptible_sleep(0.3, stop_event)
            elif saw:
                log("  Challenge tapped, level starting")
                return
            else:
                _interruptible_sleep(0.1, stop_event)
        if not saw:
            log("  Challenge button not found; assuming the level is already "
                "starting and continuing")

    def _finish_set(self, found):
        """Log the pick and return its slot index.  All-Star also records
        every identified boss as seen (off the table for later sets); Elim
        levels may re-offer a boss, so nothing is excluded there."""
        slot = min(found, key=lambda i: self._anim[found[i][0]])
        name, conf = found[slot]
        log(f"  picking '{name}' in slot {slot + 1} (conf {conf:.2f}, death "
            f"anim {self._anim[name]:.2f}s, {len(found)}/3 slots identified)")
        if not self.elim:
            for n, _ in found.values():
                self._seen.add(n)
            if name not in self.pool:
                log(f"  note: '{name}' is not in level {self.level}'s CSV "
                    "pool -- the event swapped in a variant; consider "
                    "updating allstarbossdata.csv")
        return slot

    def _scan_for_best(self, set_idx, stop_event):
        """Scan the three slots at the full stream frame rate while a
        background thread spam-taps an empty spot to skip loading.  The FIRST
        frame that identifies any boss decides the set: every slot gets its
        chance within that same frame's scan, then the best (lowest
        death-anim) identified boss is picked immediately.  There is no
        settling delay -- speed beats a perfect pick in this mode.

        Candidates: an Elim level with a pick scans ONLY that boss; otherwise
        every template (default) or the level's pool, minus All-Star's
        already-seen bosses.  Returns the chosen slot index, or None on
        timeout/stop."""
        if self.elim:
            target = self.targets[set_idx]
            if target is not None:
                candidates = [target]
            else:                       # no pick: take the best visible boss
                candidates = sorted(self._templates,
                                    key=self._anim.__getitem__)
        elif config.ALL_STAR_SCAN_ALL_BOSSES:
            candidates = sorted(
                (n for n in self._templates if n not in self._seen),
                key=self._anim.__getitem__)
        else:
            candidates = sorted(
                (n for n in self.pool
                 if n in self._templates and n not in self._seen),
                key=self._anim.__getitem__)
        if not candidates:
            log(f"All-Star: set {set_idx + 1}/{self.n_sets} -> no "
                "identifiable candidates left (missing templates)")
            return None
        self._prep_candidates(candidates)
        if len(candidates) == 1:
            log(f"All-Star: set {set_idx + 1}/{self.n_sets} -> scanning for "
                f"'{candidates[0]}' ({self._anim[candidates[0]]:.2f}s)")
        else:
            log(f"All-Star: set {set_idx + 1}/{self.n_sets} -> scanning "
                f"{len(candidates)} candidate(s), best possible "
                f"'{candidates[0]}' ({self._anim[candidates[0]]:.2f}s)")
        deadline = time.time() + config.ALL_STAR_SCAN_TIMEOUT
        done, thread = self._spam(self._empty_point, stop_event)
        found = {}                  # slot index -> (name, conf)
        best_conf = -1.0
        last = None
        try:
            while time.time() < deadline:
                if stop_event.is_set():
                    return None
                frame = self._grab()
                if frame is last:            # no new decoded frame yet
                    time.sleep(_IDLE_SLEEP)
                    continue
                last = frame
                self._ensure_geometry(frame)
                best_conf = max(best_conf, self._scan_slots(frame, found))
                if found:
                    for i in sorted(found):
                        name, conf = found[i]
                        log(f"  slot {i + 1}: '{name}' (conf {conf:.2f}, "
                            f"death anim {self._anim[name]:.2f}s)")
                    return self._finish_set(found)
        finally:
            done.set()
            thread.join(timeout=0.5)
        log(f"  timed out after {config.ALL_STAR_SCAN_TIMEOUT:.0f}s with no "
            f"identifiable boss (best conf {best_conf:.2f})")
        return None

    def _select(self, slot, stop_event):
        """Briefly spam-tap the chosen slot (background thread) to register the
        selection, then stop.  The slot centres are mid-screen, away from the
        joystick, so the few taps that land as the boss spawns are harmless."""
        log(f"  selecting slot {slot + 1}")
        done, thread = self._spam(lambda: self._slot_center(slot), stop_event)
        _interruptible_sleep(config.ALL_STAR_SELECT_BRIEF, stop_event)
        done.set()
        thread.join(timeout=0.5)

    def _wait_for_fight(self, stop_event):
        """After a boss is selected the player FIGHTS it (~10-20s) and the screen
        then goes black to transition to the next set.  Wait for that black screen
        WITHOUT tapping: the player is moving the character, so any tap (the empty
        spot is near the joystick especially) would interfere with their control.
        Returns when black is seen, the timeout elapses, or stop is requested."""
        log("  boss selected -- waiting for the fight to end "
            "(black screen, not tapping)")
        deadline = time.time() + config.ALL_STAR_BLACK_TIMEOUT
        last = None
        while time.time() < deadline:
            if stop_event.is_set():
                return
            frame = self._grab()
            if frame is last:
                time.sleep(_IDLE_SLEEP)
                continue
            last = frame
            if self._is_black(frame):
                log("  black transition detected (fight over)")
                return
        log(f"  no black transition within {config.ALL_STAR_BLACK_TIMEOUT:.0f}s; "
            "proceeding anyway")

    # ------------------------------------------------------------------
    # Boss death detection (3rd boss only)
    # ------------------------------------------------------------------

    def _hp_red_fraction(self, frame, box):
        """Fraction of the boss-health-bar crop that is saturated health-red.
        ~0.5-0.9 when the bar is full, ~0.00-0.01 when empty.  The strict
        saturated-red test ignores the warm castle background, so only the bar
        fill counts."""
        x0, y0, x1, y1 = box
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return 0.0
        b = crop[:, :, 0].astype("int16")
        g = crop[:, :, 1].astype("int16")
        r = crop[:, :, 2].astype("int16")
        mask = (r > 160) & (r - g > 75) & (r - b > 75)
        return float(mask.mean())

    def _watch_boss_death(self, stop_event):
        """After the 3rd boss is selected, rapidly poll its health bar and tap the
        pause button the INSTANT the bar empties on the kill.  Taps nothing else --
        the player is fighting.  Pauses once, then returns (the user exits
        manually).

        The 3rd part starts with a minions phase where the bar is EMPTY; the boss
        only spawns (and the bar fills) once the minions are cleared.  Both that
        early empty and the death empty look the same by red-fraction, so we ARM
        only after the bar has filled (boss spawned) and treat empty as the kill
        only after that.  This also covers the pre-fight transition (no bar)."""
        if not config.ALL_STAR_PAUSE_ON_3RD_DEATH:
            log("All-Star: 3rd boss selected -- auto-pause disabled, done")
            return
        bar_r, pause_r = config.all_star_hp_geometry(self._w, self._h)
        box = (round(bar_r[0] * self._w), round(bar_r[2] * self._h),
               round(bar_r[1] * self._w), round(bar_r[3] * self._h))
        px = round(pause_r[0] * self._w)
        py = round(pause_r[1] * self._h)
        log("All-Star: 3rd boss selected -- waiting for the boss to spawn (its "
            "health bar to fill), then will pause the instant it dies")

        # State A: wait for the boss to spawn, i.e. the bar to FILL.  The bar is
        # empty all through the minions phase (which can be long), so this must NOT
        # treat empty as the kill yet -- it only arms once the bar is clearly full.
        armed = False
        deadline = time.time() + config.ALL_STAR_HP_SPAWN_TIMEOUT
        last = None
        while time.time() < deadline:
            if stop_event.is_set():
                return
            frame = self._grab()
            if frame is last:
                time.sleep(_IDLE_SLEEP)
                continue
            last = frame
            if self._hp_red_fraction(frame, box) >= config.ALL_STAR_HP_FULL_FRAC:
                armed = True
                break
        if not armed:
            log(f"  boss never spawned (health bar never filled) within "
                f"{config.ALL_STAR_HP_SPAWN_TIMEOUT:.0f}s; skipping auto-pause "
                "(pause manually). If the boss DID spawn, check ALL_STAR_HP_BAR_* "
                "geometry for this device.")
            return
        log("  boss spawned (health bar full) -- now watching for the kill")

        # State B: watch the (now confirmed) bar deplete; pause when it empties.
        deadline = time.time() + config.ALL_STAR_HP_DEATH_TIMEOUT
        last = None
        while time.time() < deadline:
            if stop_event.is_set():
                return
            frame = self._grab()
            if frame is last:
                time.sleep(_IDLE_SLEEP)
                continue
            last = frame
            if self._hp_red_fraction(frame, box) <= config.ALL_STAR_HP_EMPTY_FRAC:
                log("  boss health bar EMPTY -- boss defeated, PAUSING")
                self._tap(px, py)
                return
        log(f"  no kill detected within {config.ALL_STAR_HP_DEATH_TIMEOUT:.0f}s; stopping")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, stop_event):
        """Play the run: start it, then pick every set in order (3 sets for
        an All-Star level, 10 for Elim)."""
        self._start_level(stop_event)
        if stop_event.is_set():
            return
        for set_idx in range(self.n_sets):
            slot = self._scan_for_best(set_idx, stop_event)
            if stop_event.is_set():
                return
            if slot is None:
                log(f"All-Star: giving up at set {set_idx + 1} (no boss "
                    "identified); stopping")
                return
            self._select(slot, stop_event)
            if stop_event.is_set():
                return
            # Every set but the last is followed by a fight + black
            # transition; wait it out without tapping.  After the final set
            # there is no next selection: All-Star watches the boss's health
            # bar and pauses the instant it dies, Elim just hands back.
            if set_idx < self.n_sets - 1:
                self._wait_for_fight(stop_event)
            elif not self.elim:
                self._watch_boss_death(stop_event)
            if stop_event.is_set():
                return
        log(f"All-Star: all {self.n_sets} bosses selected -- handing back "
            "to the player")


# ======================================================================
# Entry point
# ======================================================================

def run_all_star(stop_event=None):
    """Run the All-Star Cup macro for config.ALL_STAR_LEVEL until done or
    stopped.  Mirrors run_macro's ADB + required-streaming setup."""
    if stop_event is None:
        stop_event = threading.Event()

    activity.reset()            # clear any summary from a previous mode's run
    raw = getattr(config, "ALL_STAR_LEVEL", 1)
    if str(raw).strip().lower() == "elim":
        level = "elim"
        n_sets = int(config.ALL_STAR_ELIM_SETS)
        log(f"All-Star Cup mode -- Elim ({n_sets} levels)")
        picks = [str(n).strip() for n in
                 getattr(config, "ALL_STAR_ELIM_BOSSES", [])][:n_sets]
        picks += [""] * (n_sets - len(picks))
        log("boss picks: " + ", ".join(n or "(best visible)" for n in picks))
    else:
        level = int(raw or 1)
        log(f"All-Star Cup mode -- level {level}")
        pool = config.ALL_STAR_LEVELS.get(level, {})
        ranked = sorted(pool, key=pool.__getitem__)
        log("boss pool (fastest death anim first): "
            + ", ".join(f"{n} ({pool[n]:.2f}s)" for n in ranked))

    # Connect ADB (isolated server port dodges the BlueStacks version war).
    try:
        choose_server_port()
        adb_client = ADBClient()
        if config.ADB_DEVICE:
            adb_client.connect(config.ADB_DEVICE)
        else:
            config.ADB_DEVICE = adb_client.auto_connect()
    except ADBError as e:
        log(f"FATAL: ADB connection failed: {e}")
        return

    import main as _main
    _main._active_adb = adb_client

    try:
        probe = adb_client.screenshot()
        h, w = probe.shape[:2]
        config.PHONE_RESOLUTION = [w, h]
        log(f"device: {adb_client.device}  resolution: {w}x{h}")
    except ADBError as e:
        log(f"WARNING: could not capture a probe frame: {e}")

    if not adb_client.shell_works():
        log("WARNING: 'adb shell' is not responding -- taps will NOT register.")

    # Streaming is REQUIRED for this mode: the whole point is catching the boss
    # within a frame or two of it appearing, which needs the fast screenrecord
    # stream, not slow per-poll screencap.
    if not config.USE_STREAM_CAPTURE:
        log("FATAL: All-Star mode requires streaming capture; enable "
            "USE_STREAM_CAPTURE.")
        return
    if not capture.streaming_available():
        log(f"FATAL: {capture.unavailable_reason()}.")
        log("  Streaming capture is required. Install PyAV (pip install av).")
        return
    stream = capture.open_stream(adb_client.adb_exe, adb_client.device,
                                 attempts=3, per_attempt_timeout=4.0,
                                 on_log=log)
    if stream is None:
        log("FATAL: could not establish the screenrecord stream. Check the "
            "device screen is on and the USB/ADB connection is stable.")
        return
    log("capture: streaming via screenrecord")

    try:
        macro = AllStarMacro(adb_client, stream, level)
    except (FileNotFoundError, ValueError) as e:
        log(f"FATAL: {e}")
        stream.stop()
        return

    log(f"ADB ready, starting in {config.STARTUP_DELAY:.0f}s "
        "(press Stop to stop)")
    _interruptible_sleep(config.STARTUP_DELAY, stop_event)
    if stop_event.is_set():
        log("stopped before the run started")
        stream.stop()
        return

    try:
        macro.run(stop_event)
    except ADBError as e:
        log(f"ADB error: {e}")
        log("device disconnected; stopping")
    except KeyboardInterrupt:
        log("stopped by user")
    finally:
        macro.close()
        stream.stop()
    log("All-Star macro stopped")


def main():
    run_all_star()


if __name__ == "__main__":
    main()
