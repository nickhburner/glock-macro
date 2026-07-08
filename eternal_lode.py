"""Eternal Lode minigame macro.

Detection is brightness-based: the game renders diggable cells at full
brightness and non-diggable (hidden) cells dimmed. A simple average-
brightness threshold cleanly separates the two (gap of ~30 points on every
tested device). Template matching then classifies the bright cells by type.

Board geometry is anchored to the green depth-frontier line each iteration
(stable, always visible). The line gives board left/right and cell width,
from which a device_scale is derived to pre-scale all templates once.
"""

import random
import time

import cv2
import numpy as np

import config
import fastinput
from adb import ADBClient, ADBError, choose_server_port
from main import (
    _interruptible_sleep,
    activity,
    close_target,
    log,
    rand_delay,
    sleep_phone,
)
from matcher import load_template

# ---------------------------------------------------------------------------
# Priority tiers
# ---------------------------------------------------------------------------
# High-value cells are dug ANYWHERE on the board before progressing downward.
# Progress cells (crystals, plain dirt, rocks) are dug deepest-first.
_HIGH_VALUE = [
    "chest", "dirt15-gem", "dirt10-gem",
    "dirt2-pickaxe", "dirt1-pickaxe", "dirt1-bomb", "dirt1-drill",
]
_PROGRESS = [
    "dirt40", "dirt30", "dirt20", "dirt15", "dirt10", "dirt5",
    "dirt1", "rock2", "rock1",
]
_ALL_TYPES = _HIGH_VALUE + _PROGRESS
_RANK = {name: i for i, name in enumerate(_ALL_TYPES)}
_HIGH_VALUE_SET = frozenset(_HIGH_VALUE)
_ROCK_TYPES = frozenset(("rock1", "rock2"))

_RESOURCE_KEYS = ("zero-pickaxes", "zero-bombs", "zero-drills")
_UI_KEYS = (
    "buy-pickaxe", "buy-button", "cancel-buy", "no-buy-button",
    "set-max-buy", "use-bomb", "use-drill",
)


def _load_optional(path):
    try:
        return load_template(path)
    except (FileNotFoundError, ValueError):
        return None


class EternalLodeMacro:
    def __init__(self, adb_client: ADBClient):
        self.adb = adb_client

        # Each template prefers its active-language variant (ref/<lang>/Eternal
        # Lode/<name>.png) if the user captured one; else the bundled English
        # ref. Most Eternal Lode cells are number/icon only (language-neutral),
        # but set-max-buy ("Max") is text-bearing, so route them all through the
        # resolver uniformly.
        self.cell_templates = []
        for name in _ALL_TYPES:
            tpl = _load_optional(config.ref_path(
                config.ETERNAL_LODE_DIR / f"{name}.png"))
            if tpl is not None:
                self.cell_templates.append((name, tpl))
        log(f"Eternal Lode: loaded {len(self.cell_templates)} cell template(s)")

        self.resource_refs = {}
        for name in _RESOURCE_KEYS:
            tpl = _load_optional(config.ref_path(
                config.ETERNAL_LODE_DIR / f"{name}.png"))
            if tpl is not None:
                self.resource_refs[name] = tpl

        self.ui_refs = {}
        for name in _UI_KEYS:
            tpl = _load_optional(config.ref_path(
                config.ETERNAL_LODE_DIR / f"{name}.png"))
            if tpl is not None:
                self.ui_refs[name] = tpl

        # Board geometry (set by _update_geometry on first good detection).
        self.board_left = None
        self.cell_w = None
        self.line_y = None
        self.device_scale = None
        self.calibrated = False

        # Pre-scaled templates, built once after calibration.
        self._scaled_cells = None    # [(name, resized_bgr), ...]
        self._ui_scales = None       # [scale_float, ...]

        self.buy_failed = False

        # Low-latency tap injection (sendevent), wired up only when the user
        # turns on ROOT_FAST_INPUT (run_eternal_lode calls setup_fast_tap once
        # the resolution is known). Off / on-failure, _tap uses adb.tap as
        # before. Independent of the All-Star FAST_TAP_ENABLED gate.
        self._tapper = fastinput.FastTapper(adb_client)
        self._fast_tap = False
        self._fast_warned = False

    def setup_fast_tap(self, width, height):
        """Bring up the sendevent fast tapper for this run if ROOT_FAST_INPUT is
        on; silent fallback to adb.tap on any failure."""
        if not getattr(config, "ROOT_FAST_INPUT", False):
            return
        self._fast_tap = self._tapper.setup(
            width, height, on_log=log,
            enabled=True,
            humanize=getattr(config, "HUMANIZED_TAPS", False))

    def close(self):
        """Release any held touch and tear down the fast tapper."""
        try:
            self._tapper.close()
        except Exception:                              # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _detect_green_line(self, image):
        """Find the green depth-frontier line.

        Returns (line_y, left_x, right_x) or None. The triangle marker and
        depth text on the left are trimmed so left_x aligns with column 0.
        """
        h, w = image.shape[:2]
        b = image[:, :, 0].astype(np.int16)
        g = image[:, :, 1].astype(np.int16)
        r = image[:, :, 2].astype(np.int16)
        green = ((g > config.EL_LINE_MIN_GREEN)
                 & (g - r > config.EL_LINE_MIN_DELTA)
                 & (g - b > config.EL_LINE_MIN_DELTA))

        rowcnt = green.sum(axis=1)
        y_lo = int(config.EL_LINE_SEARCH_TOP * h)
        y_hi = int(config.EL_LINE_SEARCH_BOT * h)
        search = rowcnt.copy()
        search[:y_lo] = 0
        search[y_hi:] = 0
        peak_y = int(search.argmax())
        peak_cnt = int(search[peak_y])
        if peak_cnt < config.EL_LINE_MIN_PIXELS * w:
            return None

        band = [y for y in range(max(0, peak_y - 12), min(h, peak_y + 13))
                if rowcnt[y] > peak_cnt * 0.5]
        if not band:
            return None
        line_y = int(round(sum(band) / len(band)))

        sub = green[band[0]:band[-1] + 1, :]
        colcnt = sub.sum(axis=0)
        need = max(1, int(0.5 * len(band)))
        cols = np.where(colcnt >= need)[0]
        if cols.size == 0:
            return None
        left_raw, right = int(cols.min()), int(cols.max())
        if (right - left_raw) < config.EL_LINE_MIN_SPAN * w:
            return None

        # Trim the triangle marker and depth text off the left: they are
        # taller vertically than the thin line itself.
        win_top = max(0, line_y - 30)
        win_bot = min(h, line_y + 30)
        heightcol = green[win_top:win_bot, :].sum(axis=0)
        left = left_raw
        while left < right and heightcol[left] > config.EL_TRIANGLE_MAX_THICK:
            left += 1
        # Small extra buffer to clear any residual marker pixels.
        left = min(left + 2, right)

        return line_y, left, right

    def _update_geometry(self, image):
        """Detect the green line and (re)derive board geometry.

        Returns True if the board is visible this frame, else False.
        """
        det = self._detect_green_line(image)
        if det is None:
            return False
        line_y, left, right = det
        self.board_left = left
        self.cell_w = (right - left) / config.EL_BOARD_COLS
        self.line_y = line_y

        if not self.calibrated:
            self.device_scale = self.cell_w / config.EL_TEMPLATE_CELL_PX
            self._prescale_templates()
            self.calibrated = True
            log(f"Eternal Lode: calibrated -- left={left}, "
                f"cell~{self.cell_w:.0f}px, scale={self.device_scale:.2f}")
        return True

    def _prescale_templates(self):
        """Pre-resize every cell template to the device scale (with a small
        sweep) so the per-cell matching loop avoids all cv2.resize calls.
        """
        self._scaled_cells = []
        s = self.device_scale
        for name, tpl in self.cell_templates:
            th, tw = tpl.shape[:2]
            nw, nh = max(1, int(round(tw * s))), max(1, int(round(th * s)))
            interp = cv2.INTER_AREA if nw < tw else cv2.INTER_LINEAR
            self._scaled_cells.append((name, cv2.resize(tpl, (nw, nh),
                                                        interpolation=interp)))
        # Fast-mode subset: only templates needed to decide the action
        # (chest needs special handling, rocks need bomb/drill).
        _FAST_TYPES = frozenset(("chest", "rock1", "rock2"))
        self._scaled_cells_fast = [(n, t) for n, t in self._scaled_cells
                                   if n in _FAST_TYPES]
        self._ui_scales = [round(self.device_scale * f, 4)
                           for f in (0.82, 0.91, 1.0, 1.09, 1.18)]

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_board(self, image, max_rows=None, fast=False):
        """Scan visible cells using brightness as the primary filter.

        max_rows: if set, only scan this many rows from the bottom.
        fast: if True, use the reduced template set (chest/rock only).

        Returns (diggable, empties) where:
          diggable = [(col, row_offset, type_name), ...]
          empties  = {(col, row_offset), ...}
        """
        h_img, w_img = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        board_top = config.EL_BOARD_TOP_FRAC * h_img
        inset = config.EL_CELL_INSET

        diggable = []
        empties = set()

        row_start = 0
        row_end = -config.EL_BOARD_ROWS
        if max_rows is not None:
            row_end = max(-config.EL_BOARD_ROWS, -max_rows)

        for ro in range(row_start, row_end, -1):
            top_y = self.line_y + ro * self.cell_w
            if top_y < board_top:
                break
            for col in range(config.EL_BOARD_COLS):
                x0 = int(self.board_left + col * self.cell_w + self.cell_w * inset)
                x1 = int(self.board_left + (col + 1) * self.cell_w - self.cell_w * inset)
                y0 = int(self.line_y + ro * self.cell_w + self.cell_w * inset)
                y1 = int(self.line_y + (ro + 1) * self.cell_w - self.cell_w * inset)
                x0, y0 = max(0, x0), max(0, y0)
                x1, y1 = min(w_img, x1), min(h_img, y1)
                if x1 - x0 < 4 or y1 - y0 < 4:
                    continue

                cell_gray = gray[y0:y1, x0:x1]
                avg = float(cell_gray.mean())

                if avg > config.EL_BRIGHT_THRESHOLD:
                    name = self._classify_cell(image, col, ro, fast=fast)
                    if name is not None:
                        diggable.append((col, ro, name))
                elif avg < config.EL_EMPTY_BRIGHT_MAX:
                    std = float(cell_gray.std())
                    if std < config.EL_EMPTY_STD_MAX:
                        empties.add((col, ro))

        return diggable, empties

    def _classify_cell(self, image, col, row_offset, fast=False):
        """Template-match a diggable cell against pre-scaled templates.

        fast=True uses a reduced set (chest/rock only) for speed.
        Returns the best-matching type name, or None if nothing exceeds
        the confidence threshold.
        """
        pad = 0.15
        cw = self.cell_w
        x0 = int(self.board_left + col * cw - cw * pad)
        y0 = int(self.line_y + row_offset * cw - cw * pad)
        x1 = int(self.board_left + (col + 1) * cw + cw * pad)
        y1 = int(self.line_y + (row_offset + 1) * cw + cw * pad)
        h_img, w_img = image.shape[:2]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w_img, x1), min(h_img, y1)
        crop = image[y0:y1, x0:x1]
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            return None

        templates = self._scaled_cells_fast if fast else self._scaled_cells
        best_name, best_conf = None, -1.0
        for name, tpl in templates:
            if tpl.shape[0] > crop.shape[0] or tpl.shape[1] > crop.shape[1]:
                continue
            res = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
            conf = float(res.max())
            if conf > best_conf:
                best_conf = conf
                best_name = name
            if conf > 0.90:
                break
        if fast and best_conf < config.EL_CELL_THRESHOLD:
            return "dirt1"
        return best_name if best_conf >= config.EL_CELL_THRESHOLD else None

    # ------------------------------------------------------------------
    # Target selection
    # ------------------------------------------------------------------

    def _pick_target(self, diggable):
        """Pick the best target.

        High-value cells (chest, gems, tools) are prioritised by value
        regardless of position. Progress cells (crystals, plain dirt, rocks)
        are dug deepest-first (row_offset closest to 0) to advance the board.
        """
        if not diggable:
            return None
        high = [t for t in diggable if t[2] in _HIGH_VALUE_SET]
        if high:
            return min(high, key=lambda t: (_RANK[t[2]], -t[1], t[0]))
        return min(diggable, key=lambda t: (-t[1], _RANK[t[2]], t[0]))

    # ------------------------------------------------------------------
    # Input helpers
    # ------------------------------------------------------------------

    def _tap(self, x, y, label):
        jx = x + random.randint(-config.CLICK_JITTER, config.CLICK_JITTER)
        jy = y + random.randint(-config.CLICK_JITTER, config.CLICK_JITTER)
        log(f"  tap {label} @ ({jx}, {jy})")
        time.sleep(random.uniform(*config.CLICK_DELAY_RANGE))
        # Fast tapper when ROOT_FAST_INPUT set one up; plain adb.tap otherwise or
        # on a fast-path failure (the fallback is a plain tap, no humanization).
        if self._fast_tap and self._tapper.available:
            try:
                self._tapper.tap(jx, jy)
                if self._fast_warned:
                    log("  fast-tap recovered")
                    self._fast_warned = False
                return
            except fastinput.FastTapError as exc:
                if not self._fast_warned:
                    log(f"  fast-tap failed ({exc}); falling back to adb input tap")
                    self._fast_warned = True
        self.adb.tap(jx, jy)

    def _cell_center(self, col, row_offset):
        cx = int(round(self.board_left + (col + 0.5) * self.cell_w))
        cy = int(round(self.line_y + (row_offset + 0.5) * self.cell_w))
        return cx, cy

    def _tap_cell(self, col, row_offset, label):
        cx, cy = self._cell_center(col, row_offset)
        self._tap(cx, cy, label)

    def _dismiss_popup(self):
        """Tap the pickaxe-count area (bottom-center, inactive) to dismiss
        any reward overlay that might be covering the board."""
        res = getattr(config, "PHONE_RESOLUTION", [1080, 2400])
        w, h = res[0], res[1]
        self._tap(int(w * 0.50), int(h * 0.965), "dismiss popup")

    # ------------------------------------------------------------------
    # UI / resource matching
    # ------------------------------------------------------------------

    def _match_ui(self, image, name, band=None):
        """Match a UI reference image. Returns (conf, (x, y)) or (conf, None).

        `band` is an optional (y_frac_top, y_frac_bot) to restrict the search.
        """
        tpl = self.ui_refs.get(name)
        if tpl is None:
            return -1.0, None

        y_off = 0
        search = image
        if band is not None:
            h_img = image.shape[0]
            yt = max(0, int(band[0] * h_img))
            yb = min(h_img, int(band[1] * h_img))
            if yb <= yt:
                return -1.0, None
            search = image[yt:yb]
            y_off = yt

        from matcher import multi_scale_match
        conf, center, _ = multi_scale_match(search, tpl, self._ui_scales)
        if center is None:
            return conf, None
        cx, cy = center[0], center[1] + y_off
        return conf, (cx, cy)

    def _find_ui(self, image, name, band=None):
        """Match a UI element; return (x, y) if above threshold, else None."""
        conf, pos = self._match_ui(image, name, band=band)
        if pos is not None and conf >= config.EL_UI_THRESHOLD:
            return pos
        return None

    def _check_resource_zero(self, image, name):
        """True if a resource badge reads zero."""
        tpl = self.resource_refs.get(name)
        if tpl is None:
            return False
        from matcher import multi_scale_match
        conf, center, _ = multi_scale_match(
            image, tpl, self._ui_scales)
        return conf >= config.EL_ZERO_THRESHOLD

    def _check_resources(self, image):
        """Return dict of resource-zero flags, restricted to the toolbar band."""
        h_img = image.shape[0]
        yt = int(config.EL_TOOLBAR_BAND[0] * h_img)
        yb = int(config.EL_TOOLBAR_BAND[1] * h_img)
        toolbar = image[yt:yb]
        return {name: self._check_resource_zero(toolbar, name)
                for name in _RESOURCE_KEYS}

    # ------------------------------------------------------------------
    # Buy flow
    # ------------------------------------------------------------------

    def _try_buy_pickaxes(self):
        log("Eternal Lode: out of pickaxes, attempting to buy")
        image = self.adb.screenshot()
        pos = self._find_ui(image, "buy-pickaxe")
        if pos is None:
            log("  buy-pickaxe not found; marking buy failed")
            self.buy_failed = True
            return
        self._tap(*pos, "buy-pickaxe")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

        image = self.adb.screenshot()
        pos = self._find_ui(image, "set-max-buy")
        if pos is None:
            log("  set-max-buy not found; cancelling")
            self._cancel_buy()
            self.buy_failed = True
            return
        self._tap(*pos, "set-max-buy")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

        image = self.adb.screenshot()
        if self._find_ui(image, "no-buy-button") is not None:
            log("  can't afford pickaxes; cancelling")
            self._cancel_buy()
            self.buy_failed = True
            return

        pos = self._find_ui(image, "buy-button")
        if pos is None:
            log("  buy-button not found; cancelling")
            self._cancel_buy()
            self.buy_failed = True
            return
        self._tap(*pos, "buy-button")
        log("  pickaxe purchase confirmed")
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

    def _cancel_buy(self):
        image = self.adb.screenshot()
        pos = self._find_ui(image, "cancel-buy")
        if pos is not None:
            self._tap(*pos, "cancel-buy")
            time.sleep(rand_delay(config.EL_ACTION_DELAY))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _use_pickaxe(self, col, row_offset, kind):
        self._tap_cell(col, row_offset,
                       f"pickaxe -> {kind} c{col} r{row_offset:+d}")

    def _use_bomb_or_drill(self, tool, col, row_offset, kind, empties):
        """Activate bomb/drill and place it in the empty cell above the target.

        Returns True if the placement was attempted, False if the tool button
        wasn't found or the cell above isn't empty.
        """
        if (col, row_offset - 1) not in empties:
            return False
        image = self.adb.screenshot()
        pos = self._find_ui(image, tool, band=config.EL_TOOLBAR_BAND)
        if pos is None:
            return False
        self._tap(*pos, tool)
        time.sleep(rand_delay(config.EL_ACTION_DELAY))
        self._tap_cell(col, row_offset - 1,
                       f"{tool} above c{col} r{row_offset - 1:+d}")
        return True

    def _handle_chest(self, col, row_offset, stop_event):
        duration = config.EL_CHEST_SPAM_SECS
        delay = config.EL_CHEST_TAP_DELAY
        log(f"Eternal Lode: chest at c{col} r{row_offset:+d}, "
            f"spam-tapping for {duration:.1f}s")
        deadline = time.monotonic() + duration
        taps = 0
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return
            self._tap_cell(col, row_offset, f"chest tap {taps + 1}")
            taps += 1
            _interruptible_sleep(delay, stop_event)
        log(f"  chest spam done ({taps} taps)")
        self._dismiss_popup()

    def _act_on(self, col, row_offset, kind, resources, empties, stop_event):
        zero_pick = resources.get("zero-pickaxes", False)
        zero_bomb = resources.get("zero-bombs", False)
        zero_drill = resources.get("zero-drills", False)

        if kind == "chest":
            self._handle_chest(col, row_offset, stop_event)
            return

        if kind in _ROCK_TYPES:
            if not zero_bomb:
                if self._use_bomb_or_drill("use-bomb", col, row_offset,
                                           kind, empties):
                    return
            if not zero_drill:
                if self._use_bomb_or_drill("use-drill", col, row_offset,
                                           kind, empties):
                    return
            if not zero_pick:
                self._use_pickaxe(col, row_offset, kind)
                if kind == "rock1":
                    time.sleep(rand_delay(config.EL_ACTION_DELAY))
                    self._use_pickaxe(col, row_offset, "rock1 2nd hit")
                return
            log(f"  no tools for {kind} at c{col}, skipping")
            return

        # Any dirt variant.
        if not zero_pick:
            self._use_pickaxe(col, row_offset, kind)
            return
        if not zero_bomb:
            if self._use_bomb_or_drill("use-bomb", col, row_offset,
                                       kind, empties):
                return
        if not zero_drill:
            if self._use_bomb_or_drill("use-drill", col, row_offset,
                                       kind, empties):
                return
        log(f"  no tools for {kind} at c{col}, skipping")

    # ------------------------------------------------------------------
    # One iteration
    # ------------------------------------------------------------------

    def step(self, stop_event=None):
        """Run one macro iteration. Returns a status string.

        Raises ADBError on unrecoverable device failure.
        """
        image = self.adb.screenshot()
        if image is None or float(np.asarray(image).std()) < 8.0:
            return "blank"

        if not self._update_geometry(image):
            # Board not visible; might be a popup. Tap to dismiss.
            self._dismiss_popup()
            _interruptible_sleep(rand_delay(config.EL_ACTION_DELAY),
                                 stop_event)
            return "no-board"

        if self._scaled_cells is None:
            return "no-board"

        resources = self._check_resources(image)
        zero_pick = resources["zero-pickaxes"]
        zero_bomb = resources["zero-bombs"]
        zero_drill = resources["zero-drills"]

        if zero_pick and zero_bomb and zero_drill:
            log("Eternal Lode: all resources empty; stopping")
            return "stop"
        if zero_pick and not self.buy_failed:
            self._try_buy_pickaxes()
            return "acted"

        fast = config.EL_FAST_MODE

        if fast:
            diggable, empties = self._scan_board(image, max_rows=2, fast=True)
            if not diggable:
                diggable, empties = self._scan_board(image, fast=True)
        else:
            diggable, empties = self._scan_board(image)

        if not diggable:
            return "no-cells"

        if fast:
            target = max(diggable, key=lambda t: (t[1], -t[0]))
        else:
            target = self._pick_target(diggable)
        col, row_offset, kind = target

        log(f"Eternal Lode{' [fast]' if fast else ''}: "
            f"{len(diggable)} diggable -- "
            + ", ".join(f"c{c}r{r:+d}:{k}" for c, r, k in
                        sorted(diggable, key=lambda t: (-t[1], t[0]))))
        log(f"  -> target c{col} r{row_offset:+d} = {kind}")

        self._act_on(col, row_offset, kind, resources, empties, stop_event)
        time.sleep(rand_delay(config.EL_ACTION_DELAY))

        # Dismiss any reward popup that appeared (bombs, drills, etc.).
        self._dismiss_popup()
        return "acted"


# ======================================================================
# Entry point
# ======================================================================

def run_eternal_lode(stop_event=None):
    """Run the Eternal Lode macro until stopped or timed out."""
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    activity.reset()            # clear any summary from a previous mode's run
    log("Eternal Lode mode")

    try:
        choose_server_port()
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

    dev_wh = None
    try:
        w, h = adb_client.resolution()
        dev_wh = (w, h)
        log(f"device: {adb_client.device}  resolution: {w}x{h}")
        config.PHONE_RESOLUTION = [w, h]
    except ADBError as e:
        log(f"WARNING: could not query resolution: {e}")

    try:
        macro = EternalLodeMacro(adb_client)
    except (FileNotFoundError, ValueError) as e:
        log(f"FATAL: {e}")
        return

    # Bring up the sendevent fast tapper if ROOT_FAST_INPUT is on (silent
    # fallback to adb.tap otherwise or if the resolution is unknown).
    if dev_wh is not None:
        macro.setup_fast_tap(dev_wh[0], dev_wh[1])
    elif getattr(config, "ROOT_FAST_INPUT", False):
        log("root fast input: skipped (device resolution unknown); using adb input tap")

    log(f"ADB ready, starting in {config.STARTUP_DELAY:.0f}s")
    _interruptible_sleep(config.STARTUP_DELAY, stop_event)
    if stop_event.is_set():
        log("stopped before the run started")
        macro.close()
        return

    timeout_hours = config.RUN_TIMEOUT_HOURS or 0
    deadline = (time.monotonic() + timeout_hours * 3600
                if timeout_hours else None)
    if deadline:
        log(f"running, timeout in {timeout_hours:g}h")
    else:
        log("running with no timeout")

    timed_out = False
    self_stopped = False
    last_quiet = None
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

            if state in ("no-board", "no-cells", "blank"):
                if state != last_quiet:
                    if state == "blank":
                        log("WARNING: ADB screencap is blank")
                    elif state == "no-board":
                        log("Eternal Lode: board not visible (popup?), "
                            "will retry")
                    else:
                        log("Eternal Lode: no diggable cells, waiting")
                last_quiet = state
            else:
                last_quiet = None

            _interruptible_sleep(rand_delay(config.POLL_INTERVAL), stop_event)
    except KeyboardInterrupt:
        log("stopped by user")

    macro.close()

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
