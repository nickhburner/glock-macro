"""Eternal Lode minigame macro.
The board is a 6-col x 8-row grid of clickable squares. A green level marker
sits on the top border of the primary row (the row below it); the macro only
acts on that primary row.

Board location is calibrated once on startup; the level marker drives
per-iteration vertical alignment.
"""

import time

import pyautogui

import config
from main import (
    _interruptible_sleep,
    click,
    log,
    rand_delay,
)
from matcher import (
    grab_screen_bgr,
    is_blank,
    load_template,
    make_scales,
    multi_scale_match,
)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


# Cell types best-first.
_PRIORITY = [
    "chest",
    "dirt15-gem",
    "dirt10-gem",
    "dirt2-pickaxe",
    "dirt1-pickaxe",
    "dirt1-bomb",
    "dirt1-drill",
    "dirt40",
    "dirt30",
    "dirt20",
    "dirt15",
    "dirt10",
    "dirt5",
    "dirt1",
    "rock2",
    "rock1",
]
_CELL_TYPES = list(_PRIORITY)

# "You have zero of X" badges below the board. None showing means that
# resource is still available.
_RESOURCE_KEYS = ("zero-pickaxes", "zero-bombs", "zero-drills")

# Buy-flow and action-button references.
_UI_KEYS = (
    "buy-pickaxe", "buy-button", "cancel-buy", "no-buy-button",
    "set-max-buy", "use-bomb", "use-drill", "level",
)


def _load_optional(path):
    """Load a reference template, or return None (with a log note) if missing."""
    try:
        return load_template(path)
    except (FileNotFoundError, ValueError) as e:
        log(f"NOTE: Eternal Lode ref '{path.name}' could not be loaded ({e})")
        return None


class EternalLodeMacro:
    def __init__(self):
        self.ref_scales = make_scales(config.REF_SCALE_RANGE)

        # Cell-content templates (dirt / rock / chest), in priority order.
        self.cell_templates = []   # [(name, template), ...]
        for name in _CELL_TYPES:
            tpl = _load_optional(config.ETERNAL_LODE_DIR / f"{name}.png")
            if tpl is not None:
                self.cell_templates.append((name, tpl))
        log(f"Eternal Lode: loaded {len(self.cell_templates)} cell template(s)")

        # Resource indicators.
        self.resource_refs = {}
        for name in _RESOURCE_KEYS:
            tpl = _load_optional(config.ETERNAL_LODE_DIR / f"{name}.png")
            if tpl is not None:
                self.resource_refs[name] = tpl

        # UI / button refs (buy flow + action buttons + level marker).
        self.ui_refs = {}
        for name in _UI_KEYS:
            tpl = _load_optional(config.ETERNAL_LODE_DIR / f"{name}.png")
            if tpl is not None:
                self.ui_refs[name] = tpl

        # Board-calibration reference, matched once at startup to locate the
        # grid.
        self.board_template = _load_optional(
            config.ETERNAL_LODE_DIR / "8x6-fullboard.png")

        # Calibrated board geometry, in CAPTURE-REGION coords (None until
        # _calibrate_board succeeds); converted to screen coords via _to_screen.
        self.board_x = None
        self.board_y = None
        self.board_w = None
        self.board_h = None
        self.cell_w = None   # cell pitch in px, region coords
        self.cell_h = None

        # Per-session state.
        self.buy_failed = False
        self.calibrated = False

    # coordinate helpers
    def _to_screen(self, point):
        """Region-relative (x, y) -> absolute screen (x, y)."""
        return (config.REGION_X + int(point[0]),
                config.REGION_Y + int(point[1]))

    def _cell_center_region(self, col, row):
        """Center of cell (col, row) in region coords. Row 0 is the top row."""
        if self.cell_w is None:
            return None
        cx = self.board_x + (col + 0.5) * self.cell_w
        cy = self.board_y + (row + 0.5) * self.cell_h
        return (int(round(cx)), int(round(cy)))

    def _cell_box_region(self, col, row, pad=0.10):
        """Crop box for cell (col, row) in region coords. `pad` expands the
        cell pitch by that fraction on each edge to absorb calibration
        rounding and leave room for the content template inside."""
        if self.cell_w is None:
            return None
        mx = self.cell_w * pad
        my = self.cell_h * pad
        bx0 = self.board_x + col * self.cell_w - mx
        by0 = self.board_y + row * self.cell_h - my
        bx1 = self.board_x + (col + 1) * self.cell_w + mx
        by1 = self.board_y + (row + 1) * self.cell_h + my
        return (int(bx0), int(by0), int(bx1), int(by1))

    # calibration
    def _calibrate_board(self, image):
        """Find the board on screen and derive cell positions.

        Matches the bundled 8x6-fullboard template; on failure, falls back to a
        centred board area derived from the capture region. Returns True on
        success.
        """
        if self.board_template is None:
            log("Eternal Lode: 8x6-fullboard reference missing, cannot "
                "calibrate board, aborting")
            return False

        log("Eternal Lode: calibrating board position...")
        conf, center, size = multi_scale_match(
            image, self.board_template, self.ref_scales, 0.5)
        # The board template includes changing cell contents, so a confident
        # structural match is not always possible; accept any reasonable one.
        if center is not None and size is not None and conf >= 0.45:
            w, h = size
            self.board_w = w
            self.board_h = h
            self.board_x = center[0] - w // 2
            self.board_y = center[1] - h // 2
            self.cell_w = self.board_w / config.EL_BOARD_COLS
            self.cell_h = self.board_h / config.EL_BOARD_ROWS
            log(f"  board matched (conf {conf:.2f}) at region ({self.board_x},"
                f" {self.board_y}), size {w}x{h}, cell ~{self.cell_w:.1f}x"
                f"{self.cell_h:.1f} px")
            return True

        log(f"  board template match too weak (best conf {conf:.2f}); "
            f"falling back to scale-derived size")
        # Fallback: a centred board area, width = EL_BOARD_W * current ref
        # scale (middle of REF_SCALE_RANGE).
        lo, hi, _ = config.REF_SCALE_RANGE
        scale = (lo + hi) / 2.0 or 1.0
        img_h, img_w = image.shape[:2]
        w = int(round(config.EL_BOARD_W * scale))
        h = int(round(config.EL_BOARD_H * scale))
        self.board_w = w
        self.board_h = h
        self.board_x = max(0, (img_w - w) // 2)
        self.board_y = max(0, (img_h - h) // 2)
        self.cell_w = self.board_w / config.EL_BOARD_COLS
        self.cell_h = self.board_h / config.EL_BOARD_ROWS
        log(f"  fallback board at region ({self.board_x}, {self.board_y}),"
            f" size {w}x{h}, cell ~{self.cell_w:.1f}x{self.cell_h:.1f} px")
        return True

    def _find_primary_row(self, image):
        """Match the level marker and return the primary row index (0-7), or
        None. The marker is centred on the row's top border, so the row's top
        is at marker_y."""
        tpl = self.ui_refs.get("level")
        if tpl is None:
            log("Eternal Lode: level marker reference missing, cannot "
                "locate primary row")
            return None
        conf, center, _ = multi_scale_match(image, tpl, self.ref_scales)
        if center is None or conf < config.EL_UI_THRESHOLD:
            log(f"  level marker not found (best conf {conf:.2f})")
            return None
        marker_y = center[1]   # the row's top border
        row_top_relative = marker_y - self.board_y
        if self.cell_h <= 0:
            return None
        row = int(round(row_top_relative / self.cell_h))
        # Clamp so a slightly-off match still picks a valid row.
        row = max(0, min(config.EL_BOARD_ROWS - 1, row))
        return row

    # scanning
    def _classify_cell(self, image, col, row):
        """Identify cell (col, row) by matching each cell template against its
        crop. Returns the highest-confidence type over threshold, or None."""
        box = self._cell_box_region(col, row)
        if box is None:
            return None
        x0, y0, x1, y1 = box
        img_h, img_w = image.shape[:2]
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(img_w, x1); y1 = min(img_h, y1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None
        crop = image[y0:y1, x0:x1]
        if is_blank(crop):
            return None

        best_name = None
        best_conf = -1.0
        for name, tpl in self.cell_templates:
            conf, _, _ = multi_scale_match(crop, tpl, self.ref_scales)
            if conf > best_conf:
                best_conf = conf
                best_name = name
        if best_conf >= config.EL_CELL_THRESHOLD:
            return best_name
        return None

    def _scan_primary_row(self, image, row):
        """[(col, type), ...] for the active cells in `row`, in column order."""
        out = []
        for col in range(config.EL_BOARD_COLS):
            kind = self._classify_cell(image, col, row)
            if kind is not None:
                out.append((col, kind))
        return out

    def _pick_target(self, cells):
        """Highest-priority cell from [(col, type), ...]; ties to the leftmost.
        Returns (col, type) or None."""
        if not cells:
            return None
        rank = {name: i for i, name in enumerate(_PRIORITY)}
        unknown = len(_PRIORITY)   # unknown types sort last
        cells_sorted = sorted(cells, key=lambda ct: (rank.get(ct[1], unknown),
                                                    ct[0]))
        return cells_sorted[0]

    # resources
    def _check_resource(self, image, name):
        """True when the 'zero-<resource>' indicator is on screen."""
        tpl = self.resource_refs.get(name)
        if tpl is None:
            return False
        conf, _, _ = multi_scale_match(image, tpl, self.ref_scales)
        return conf >= config.EL_UI_THRESHOLD

    def _find_ui(self, image, name):
        """Match a UI ref; return (conf, screen_xy) or (conf, None)."""
        tpl = self.ui_refs.get(name)
        if tpl is None:
            return -1.0, None
        conf, center, _ = multi_scale_match(image, tpl, self.ref_scales)
        if conf < config.EL_UI_THRESHOLD or center is None:
            return conf, None
        return conf, self._to_screen(center)

    # buy flow
    def _attempt_buy_pickaxes(self):
        """One-shot buy flow: open the buy menu, set max, then confirm or
        cancel depending on whether no-buy-button shows. Sets self.buy_failed
        on cancel so the session never retries."""
        log("Eternal Lode: out of pickaxes, attempting to buy")
        image = grab_screen_bgr(config.BLUESTACKS_REGION)
        conf, pos = self._find_ui(image, "buy-pickaxe")
        if pos is None:
            log(f"  buy-pickaxe button not found (conf {conf:.2f}); "
                f"marking buy as failed for this session")
            self.buy_failed = True
            return
        click(*pos, "buy-pickaxe")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

        image = grab_screen_bgr(config.BLUESTACKS_REGION)
        conf, pos = self._find_ui(image, "set-max-buy")
        if pos is None:
            log(f"  set-max-buy button not found (conf {conf:.2f}); "
                f"cancelling and marking buy as failed")
            self._cancel_buy()
            self.buy_failed = True
            return
        click(*pos, "set-max-buy")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

        image = grab_screen_bgr(config.BLUESTACKS_REGION)
        # no-buy-button showing = can't afford it; cancel + give up.
        nbconf, nbpos = self._find_ui(image, "no-buy-button")
        if nbpos is not None:
            log(f"  no-buy-button is showing (conf {nbconf:.2f}), "
                f"can't afford; cancelling and marking buy as failed")
            self._cancel_buy()
            self.buy_failed = True
            return

        bconf, bpos = self._find_ui(image, "buy-button")
        if bpos is None:
            log(f"  buy-button not found (conf {bconf:.2f}); "
                f"cancelling and marking buy as failed")
            self._cancel_buy()
            self.buy_failed = True
            return
        click(*bpos, "buy-button")
        log("  pickaxe purchase confirmed")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

    def _cancel_buy(self):
        """Best-effort: click cancel-buy if it can be found."""
        image = grab_screen_bgr(config.BLUESTACKS_REGION)
        cconf, cpos = self._find_ui(image, "cancel-buy")
        if cpos is not None:
            click(*cpos, "cancel-buy")
            time.sleep(rand_delay(config.EL_ACTION_DELAY))
        else:
            log(f"  cancel-buy button not found (conf {cconf:.2f}); "
                f"hoping the menu auto-dismisses")

    # tool usage
    def _use_pickaxe(self, col, row, kind):
        """Click the target cell once with the pickaxe."""
        cr = self._cell_center_region(col, row)
        if cr is None:
            return
        click(*self._to_screen(cr), f"pickaxe -> {kind} at ({col},{row})")

    def _use_bomb_or_drill(self, tool, col, row, kind):
        """Click the tool button, then the cell ONE ROW ABOVE the target.
        `tool` is 'use-bomb' or 'use-drill'."""
        image = grab_screen_bgr(config.BLUESTACKS_REGION)
        conf, pos = self._find_ui(image, tool)
        if pos is None:
            log(f"  {tool} button not found (conf {conf:.2f}), skipping")
            return False
        click(*pos, tool)
        time.sleep(rand_delay(config.EL_ACTION_DELAY))
        above_row = row - 1
        if above_row < 0:
            log(f"  cannot place {tool} above row {row} (off the top of the "
                f"board), aborting this action")
            return False
        cr = self._cell_center_region(col, above_row)
        if cr is None:
            return False
        click(*self._to_screen(cr),
              f"{tool} placement at ({col},{above_row}) for {kind} at "
              f"({col},{row})")
        return True

    def _handle_chest(self, col, row, stop_event):
        """Tap the chest cell until it disappears or the safety cap is hit."""
        cr = self._cell_center_region(col, row)
        if cr is None:
            return
        screen = self._to_screen(cr)
        log(f"Eternal Lode: chest at ({col},{row}), tapping up to "
            f"{config.EL_CHEST_MAX_CLICKS} times")
        for i in range(config.EL_CHEST_MAX_CLICKS):
            if stop_event is not None and stop_event.is_set():
                return
            click(*screen, f"chest tap {i + 1}")
            _interruptible_sleep(
                rand_delay(config.EL_CHEST_CLICK_DELAY), stop_event)
            # Re-check; stop once the chest is gone.
            image = grab_screen_bgr(config.BLUESTACKS_REGION)
            kind = self._classify_cell(image, col, row)
            if kind != "chest":
                log(f"  chest cleared after {i + 1} tap(s)")
                return
        log(f"  chest still present after {config.EL_CHEST_MAX_CLICKS} taps"
            f", treating as cleared and moving on")

    def _act_on(self, col, row, kind, resources, stop_event):
        """Dispatch the right tool for cell type `kind`. `resources` is the
        dict of zero-* indicator states ({name: True/False})."""
        zero_pick = resources.get("zero-pickaxes", False)
        zero_bomb = resources.get("zero-bombs", False)
        zero_drill = resources.get("zero-drills", False)

        if kind == "chest":
            self._handle_chest(col, row, stop_event)
            return

        if kind in ("rock1", "rock2"):
            # Bomb, then drill, then pickaxe fallback.
            if not zero_bomb:
                self._use_bomb_or_drill("use-bomb", col, row, kind)
                return
            if not zero_drill:
                self._use_bomb_or_drill("use-drill", col, row, kind)
                return
            if not zero_pick:
                # Rock1 needs 2 hits, Rock2 needs 1.
                self._use_pickaxe(col, row, kind)
                if kind == "rock1":
                    time.sleep(rand_delay(config.EL_ACTION_DELAY))
                    self._use_pickaxe(col, row, "rock1 (2nd hit)")
                return
            log(f"  no tools available for {kind} at ({col},{row}), skipping")
            return

        # Dirt of any kind.
        if not zero_pick:
            self._use_pickaxe(col, row, kind)
            return
        if not zero_bomb:
            self._use_bomb_or_drill("use-bomb", col, row, kind)
            return
        if not zero_drill:
            self._use_bomb_or_drill("use-drill", col, row, kind)
            return
        log(f"  no tools available for {kind} at ({col},{row}), skipping")

    # one iteration
    def step(self, stop_event=None):
        """Run one iteration. Returns a status string:
          'stop'     : session must end (all resources empty)
          'no-row'   : could not find the primary row this cycle
          'no-cells' : primary row had no active cells
          'acted'    : a cell was acted upon
          'blank'    : capture was blank/black
        """
        image = grab_screen_bgr(config.BLUESTACKS_REGION)
        if is_blank(image):
            return "blank"

        if not self.calibrated:
            if not self._calibrate_board(image):
                return "stop"
            self.calibrated = True

        # 1. Resource check (before acting on any square).
        resources = {name: self._check_resource(image, name)
                     for name in _RESOURCE_KEYS}
        zero_pick = resources["zero-pickaxes"]
        zero_bomb = resources["zero-bombs"]
        zero_drill = resources["zero-drills"]
        if zero_pick and zero_bomb and zero_drill:
            log("Eternal Lode: out of pickaxes, bombs, AND drills; stopping")
            return "stop"
        if zero_pick and not self.buy_failed:
            self._attempt_buy_pickaxes()
            return "acted"   # re-snapshot next cycle for post-buy state

        # 2. Locate the primary row.
        row = self._find_primary_row(image)
        if row is None:
            return "no-row"

        # 3. Scan and choose target.
        cells = self._scan_primary_row(image, row)
        if not cells:
            log(f"Eternal Lode: primary row {row} has no active cells")
            return "no-cells"
        log(f"Eternal Lode: primary row {row} cells = "
            + ", ".join(f"{c}:{k}" for c, k in cells))
        target = self._pick_target(cells)
        if target is None:
            return "no-cells"
        col, kind = target
        log(f"  -> target ({col},{row}) is {kind}")

        # 4. Act.
        self._act_on(col, row, kind, resources, stop_event)

        # 5. Wait for animation.
        time.sleep(rand_delay(config.EL_ACTION_DELAY))
        return "acted"


def run_eternal_lode(stop_event=None):
    """Run the Eternal Lode macro until stopped, timed out, or the fail-safe.
    Mirrors main.run_macro: applies saved settings, waits STARTUP_DELAY, then
    loops. Same timeout / close / sleep-phone behaviour."""
    # Lazy import to avoid pulling close_target/sleep_phone (and adb) at module
    # import, where they'd shadow re-imports during testing.
    from main import _is_running, close_target, sleep_phone

    log("Eternal Lode mode")
    log(f"region={config.BLUESTACKS_REGION}")

    try:
        macro = EternalLodeMacro()
    except (FileNotFoundError, ValueError) as e:
        log(f"FATAL: {e}")
        return

    log(f"focus the game window now, starting in "
        f"{config.STARTUP_DELAY:.0f}s")
    _interruptible_sleep(config.STARTUP_DELAY, stop_event)
    if stop_event is not None and stop_event.is_set():
        log("stopped before the run started")
        return

    timeout_hours = config.RUN_TIMEOUT_HOURS or 0
    deadline = (time.monotonic() + timeout_hours * 3600
                if timeout_hours else None)
    if deadline:
        log(f"running, timeout in {timeout_hours:g}h; Ctrl+C, the Stop "
            f"button, or the mouse in a screen corner stops it")
    else:
        log("running, Ctrl+C, the Stop button, or the mouse in a screen "
            "corner stops it")

    timed_out = False
    self_stopped = False
    last_quiet_state = None
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
                state = macro.step(stop_event)
            except pyautogui.FailSafeException:
                log("fail-safe triggered, stopping")
                break

            if state == "stop":
                self_stopped = True
                break

            # Log quiet/no-op states only when they first occur.
            if state in ("no-row", "no-cells", "blank"):
                if state != last_quiet_state:
                    if state == "blank":
                        log("WARNING: capture is blank/black, check the "
                            "graphics renderer / mirror window")
                    elif state == "no-row":
                        log("Eternal Lode: waiting for level marker...")
                    else:
                        log("Eternal Lode: primary row idle, waiting")
                last_quiet_state = state
            else:
                last_quiet_state = None

            _interruptible_sleep(rand_delay(config.POLL_INTERVAL), stop_event)
    except KeyboardInterrupt:
        log("stopped by user")

    if timed_out or self_stopped:
        if config.SLEEP_PHONE_ON_TIMEOUT and _is_running("scrcpy.exe"):
            sleep_phone()
        if config.CLOSE_ON_TIMEOUT:
            log("closing the game window")
            close_target()
    log("Eternal Lode macro stopped")


def main():
    run_eternal_lode()


if __name__ == "__main__":
    main()
