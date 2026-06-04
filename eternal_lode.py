"""Eternal Lode minigame macro.
The board is a 6-col x 8-row grid of clickable squares. A green level marker
sits on the top border of the primary row (the row below it); the macro only
acts on that primary row.

Board location is calibrated once on startup; the level marker drives
per-iteration vertical alignment.
"""

import time

import config
from adb import ADBClient, ADBError
from main import (
    _interruptible_sleep,
    _start_failsafe_listener,
    close_target,
    log,
    rand_delay,
    sleep_phone,
)
from matcher import (
    is_blank,
    load_template,
    multi_scale_match,
)

import random


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

_RESOURCE_KEYS = ("zero-pickaxes", "zero-bombs", "zero-drills")

_UI_KEYS = (
    "buy-pickaxe", "buy-button", "cancel-buy", "no-buy-button",
    "set-max-buy", "use-bomb", "use-drill", "level",
)


def _load_optional(path):
    try:
        return load_template(path)
    except (FileNotFoundError, ValueError) as e:
        log(f"NOTE: Eternal Lode ref '{path.name}' could not be loaded ({e})")
        return None


class EternalLodeMacro:
    def __init__(self, adb_client: ADBClient):
        self.adb = adb_client
        self.ref_scales = [config.REF_SCALE_BASELINE * config.CALIBRATED_SCALE]

        self.cell_templates = []
        for name in _CELL_TYPES:
            tpl = _load_optional(config.ETERNAL_LODE_DIR / f"{name}.png")
            if tpl is not None:
                self.cell_templates.append((name, tpl))
        log(f"Eternal Lode: loaded {len(self.cell_templates)} cell template(s)")

        self.resource_refs = {}
        for name in _RESOURCE_KEYS:
            tpl = _load_optional(config.ETERNAL_LODE_DIR / f"{name}.png")
            if tpl is not None:
                self.resource_refs[name] = tpl

        self.ui_refs = {}
        for name in _UI_KEYS:
            tpl = _load_optional(config.ETERNAL_LODE_DIR / f"{name}.png")
            if tpl is not None:
                self.ui_refs[name] = tpl

        self.board_template = _load_optional(
            config.ETERNAL_LODE_DIR / "8x6-fullboard.png")

        # Calibrated board geometry in phone-pixel space.
        self.board_x = None
        self.board_y = None
        self.board_w = None
        self.board_h = None
        self.cell_w  = None
        self.cell_h  = None

        self.buy_failed = False
        self.calibrated = False

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _tap(self, x, y, label):
        """Send a tap with jitter and a random pre-delay."""
        jx = x + random.randint(-config.CLICK_JITTER, config.CLICK_JITTER)
        jy = y + random.randint(-config.CLICK_JITTER, config.CLICK_JITTER)
        log(f"  tap {label} @ ({jx}, {jy})")
        time.sleep(random.uniform(*config.CLICK_DELAY_RANGE))
        self.adb.tap(jx, jy)

    # ------------------------------------------------------------------
    # Coordinate helpers  (phone-pixel space throughout)
    # ------------------------------------------------------------------

    def _cell_center(self, col, row):
        """Centre of cell (col, row) in phone-pixel coords."""
        if self.cell_w is None:
            return None
        cx = self.board_x + (col + 0.5) * self.cell_w
        cy = self.board_y + (row + 0.5) * self.cell_h
        return (int(round(cx)), int(round(cy)))

    def _cell_box(self, col, row, pad=0.10):
        """Crop box for cell (col, row) in phone-pixel coords."""
        if self.cell_w is None:
            return None
        mx = self.cell_w * pad
        my = self.cell_h * pad
        bx0 = self.board_x + col * self.cell_w - mx
        by0 = self.board_y + row * self.cell_h - my
        bx1 = self.board_x + (col + 1) * self.cell_w + mx
        by1 = self.board_y + (row + 1) * self.cell_h + my
        return (int(bx0), int(by0), int(bx1), int(by1))

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _calibrate_board(self, image):
        if self.board_template is None:
            log("Eternal Lode: 8x6-fullboard reference missing, cannot "
                "calibrate board, aborting")
            return False

        log("Eternal Lode: calibrating board position...")
        conf, center, size = multi_scale_match(
            image, self.board_template, self.ref_scales, 0.5)
        if center is not None and size is not None and conf >= 0.45:
            w, h = size
            self.board_w = w
            self.board_h = h
            self.board_x = center[0] - w // 2
            self.board_y = center[1] - h // 2
            self.cell_w  = self.board_w / config.EL_BOARD_COLS
            self.cell_h  = self.board_h / config.EL_BOARD_ROWS
            log(f"  board matched (conf {conf:.2f}) at ({self.board_x},"
                f" {self.board_y}), size {w}x{h}, "
                f"cell ~{self.cell_w:.1f}x{self.cell_h:.1f}px")
            return True

        log(f"  board match too weak (conf {conf:.2f}); "
            f"falling back to scale-derived size")
        img_h, img_w = image.shape[:2]
        scale = config.CALIBRATED_SCALE
        w = int(round(config.EL_BOARD_W * scale))
        h = int(round(config.EL_BOARD_H * scale))
        self.board_w = w
        self.board_h = h
        self.board_x = max(0, (img_w - w) // 2)
        self.board_y = max(0, (img_h - h) // 2)
        self.cell_w  = self.board_w / config.EL_BOARD_COLS
        self.cell_h  = self.board_h / config.EL_BOARD_ROWS
        log(f"  fallback board at ({self.board_x}, {self.board_y}), "
            f"size {w}x{h}, cell ~{self.cell_w:.1f}x{self.cell_h:.1f}px")
        return True

    def _find_primary_row(self, image):
        tpl = self.ui_refs.get("level")
        if tpl is None:
            log("Eternal Lode: level marker reference missing")
            return None
        conf, center, _ = multi_scale_match(image, tpl, self.ref_scales)
        if center is None or conf < config.EL_UI_THRESHOLD:
            log(f"  level marker not found (best conf {conf:.2f})")
            return None
        marker_y = center[1]
        row_top_relative = marker_y - self.board_y
        if self.cell_h <= 0:
            return None
        row = int(round(row_top_relative / self.cell_h))
        return max(0, min(config.EL_BOARD_ROWS - 1, row))

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _classify_cell(self, image, col, row):
        box = self._cell_box(col, row)
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
        return best_name if best_conf >= config.EL_CELL_THRESHOLD else None

    def _scan_primary_row(self, image, row):
        out = []
        for col in range(config.EL_BOARD_COLS):
            kind = self._classify_cell(image, col, row)
            if kind is not None:
                out.append((col, kind))
        return out

    def _pick_target(self, cells):
        if not cells:
            return None
        rank = {name: i for i, name in enumerate(_PRIORITY)}
        unknown = len(_PRIORITY)
        cells_sorted = sorted(cells,
                              key=lambda ct: (rank.get(ct[1], unknown), ct[0]))
        return cells_sorted[0]

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    def _check_resource(self, image, name):
        tpl = self.resource_refs.get(name)
        if tpl is None:
            return False
        conf, _, _ = multi_scale_match(image, tpl, self.ref_scales)
        return conf >= config.EL_UI_THRESHOLD

    def _find_ui(self, image, name):
        """Match a UI ref; return (conf, (x, y)) or (conf, None)."""
        tpl = self.ui_refs.get(name)
        if tpl is None:
            return -1.0, None
        conf, center, _ = multi_scale_match(image, tpl, self.ref_scales)
        if conf < config.EL_UI_THRESHOLD or center is None:
            return conf, None
        return conf, center   # already phone-pixel coords

    # ------------------------------------------------------------------
    # Buy flow
    # ------------------------------------------------------------------

    def _attempt_buy_pickaxes(self):
        log("Eternal Lode: out of pickaxes, attempting to buy")
        image = self.adb.screenshot()
        conf, pos = self._find_ui(image, "buy-pickaxe")
        if pos is None:
            log(f"  buy-pickaxe button not found (conf {conf:.2f}); "
                f"marking buy as failed for this session")
            self.buy_failed = True
            return
        self._tap(*pos, "buy-pickaxe")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

        image = self.adb.screenshot()
        conf, pos = self._find_ui(image, "set-max-buy")
        if pos is None:
            log(f"  set-max-buy button not found (conf {conf:.2f}); "
                f"cancelling and marking buy as failed")
            self._cancel_buy()
            self.buy_failed = True
            return
        self._tap(*pos, "set-max-buy")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

        image = self.adb.screenshot()
        nbconf, nbpos = self._find_ui(image, "no-buy-button")
        if nbpos is not None:
            log(f"  no-buy-button showing (conf {nbconf:.2f}), "
                f"can't afford; cancelling")
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
        self._tap(*bpos, "buy-button")
        log("  pickaxe purchase confirmed")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

    def _cancel_buy(self):
        image = self.adb.screenshot()
        cconf, cpos = self._find_ui(image, "cancel-buy")
        if cpos is not None:
            self._tap(*cpos, "cancel-buy")
            time.sleep(rand_delay(config.EL_ACTION_DELAY))
        else:
            log(f"  cancel-buy not found (conf {cconf:.2f}); "
                f"hoping the menu auto-dismisses")

    # ------------------------------------------------------------------
    # Tool usage
    # ------------------------------------------------------------------

    def _use_pickaxe(self, col, row, kind):
        cr = self._cell_center(col, row)
        if cr is None:
            return
        self._tap(*cr, f"pickaxe -> {kind} at ({col},{row})")

    def _use_bomb_or_drill(self, tool, col, row, kind):
        image = self.adb.screenshot()
        conf, pos = self._find_ui(image, tool)
        if pos is None:
            log(f"  {tool} button not found (conf {conf:.2f}), skipping")
            return False
        self._tap(*pos, tool)
        time.sleep(rand_delay(config.EL_ACTION_DELAY))
        above_row = row - 1
        if above_row < 0:
            log(f"  cannot place {tool} above row {row} (off the top), aborting")
            return False
        cr = self._cell_center(col, above_row)
        if cr is None:
            return False
        self._tap(*cr, f"{tool} at ({col},{above_row}) for {kind} at ({col},{row})")
        return True

    def _handle_chest(self, col, row, stop_event):
        cr = self._cell_center(col, row)
        if cr is None:
            return
        log(f"Eternal Lode: chest at ({col},{row}), tapping up to "
            f"{config.EL_CHEST_MAX_CLICKS} times")
        for i in range(config.EL_CHEST_MAX_CLICKS):
            if stop_event is not None and stop_event.is_set():
                return
            self._tap(*cr, f"chest tap {i + 1}")
            _interruptible_sleep(rand_delay(config.EL_CHEST_CLICK_DELAY), stop_event)
            image = self.adb.screenshot()
            kind = self._classify_cell(image, col, row)
            if kind != "chest":
                log(f"  chest cleared after {i + 1} tap(s)")
                return
        log(f"  chest still present after {config.EL_CHEST_MAX_CLICKS} taps, moving on")

    def _act_on(self, col, row, kind, resources, stop_event):
        zero_pick  = resources.get("zero-pickaxes", False)
        zero_bomb  = resources.get("zero-bombs",    False)
        zero_drill = resources.get("zero-drills",   False)

        if kind == "chest":
            self._handle_chest(col, row, stop_event)
            return

        if kind in ("rock1", "rock2"):
            if not zero_bomb:
                self._use_bomb_or_drill("use-bomb", col, row, kind)
                return
            if not zero_drill:
                self._use_bomb_or_drill("use-drill", col, row, kind)
                return
            if not zero_pick:
                self._use_pickaxe(col, row, kind)
                if kind == "rock1":
                    time.sleep(rand_delay(config.EL_ACTION_DELAY))
                    self._use_pickaxe(col, row, "rock1 (2nd hit)")
                return
            log(f"  no tools for {kind} at ({col},{row}), skipping")
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
        log(f"  no tools for {kind} at ({col},{row}), skipping")

    # ------------------------------------------------------------------
    # One iteration
    # ------------------------------------------------------------------

    def step(self, stop_event=None):
        """Run one iteration.  Returns a status string.
        Raises ADBError on unrecoverable device failure."""
        image = self.adb.screenshot()
        if is_blank(image):
            return "blank"

        if not self.calibrated:
            if not self._calibrate_board(image):
                return "stop"
            self.calibrated = True

        resources = {name: self._check_resource(image, name)
                     for name in _RESOURCE_KEYS}
        zero_pick  = resources["zero-pickaxes"]
        zero_bomb  = resources["zero-bombs"]
        zero_drill = resources["zero-drills"]
        if zero_pick and zero_bomb and zero_drill:
            log("Eternal Lode: out of pickaxes, bombs, AND drills; stopping")
            return "stop"
        if zero_pick and not self.buy_failed:
            self._attempt_buy_pickaxes()
            return "acted"

        row = self._find_primary_row(image)
        if row is None:
            return "no-row"

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

        self._act_on(col, row, kind, resources, stop_event)
        time.sleep(rand_delay(config.EL_ACTION_DELAY))
        return "acted"


def run_eternal_lode(stop_event=None):
    """Run the Eternal Lode macro until stopped, timed out, or the failsafe."""
    import threading
    from main import _active_adb

    if stop_event is None:
        stop_event = threading.Event()

    log("Eternal Lode mode")

    # Connect to ADB device.
    try:
        adb_client = ADBClient()
        if config.ADB_DEVICE:
            adb_client.connect(config.ADB_DEVICE)
        else:
            serial = adb_client.auto_connect()
            config.ADB_DEVICE = serial
    except ADBError as e:
        log(f"FATAL: ADB connection failed: {e}")
        return

    import main as _main
    _main._active_adb = adb_client

    try:
        w, h = adb_client.resolution()
        log(f"device: {adb_client.device}  resolution: {w}x{h}")
        config.PHONE_RESOLUTION = [w, h]
    except ADBError as e:
        log(f"WARNING: could not query resolution: {e}")

    _start_failsafe_listener(stop_event)

    try:
        macro = EternalLodeMacro(adb_client)
    except (FileNotFoundError, ValueError) as e:
        log(f"FATAL: {e}")
        return

    log(f"ADB ready, starting in {config.STARTUP_DELAY:.0f}s")
    _interruptible_sleep(config.STARTUP_DELAY, stop_event)
    if stop_event.is_set():
        log("stopped before the run started")
        return

    timeout_hours = config.RUN_TIMEOUT_HOURS or 0
    deadline = (time.monotonic() + timeout_hours * 3600
                if timeout_hours else None)
    if deadline:
        log(f"running, timeout in {timeout_hours:g}h")
    else:
        log("running with no timeout")

    timed_out   = False
    self_stopped = False
    last_quiet  = None
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
                state = macro.step(stop_event)
            except ADBError as e:
                log(f"ADB error: {e}")
                log("device disconnected; stopping")
                break

            if state == "stop":
                self_stopped = True
                break

            if state in ("no-row", "no-cells", "blank"):
                if state != last_quiet:
                    if state == "blank":
                        log("WARNING: ADB screencap is blank -- make sure "
                            "the device screen is on")
                    elif state == "no-row":
                        log("Eternal Lode: waiting for level marker...")
                    else:
                        log("Eternal Lode: primary row idle, waiting")
                last_quiet = state
            else:
                last_quiet = None

            _interruptible_sleep(rand_delay(config.POLL_INTERVAL), stop_event)
    except KeyboardInterrupt:
        log("stopped by user")

    if timed_out or self_stopped:
        if config.SLEEP_PHONE_ON_TIMEOUT:
            adb_client.sleep_phone()
        if config.CLOSE_ON_TIMEOUT:
            log("closing the game window")
            close_target()
    log("Eternal Lode macro stopped")


def main():
    run_eternal_lode()


if __name__ == "__main__":
    main()
