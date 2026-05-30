"""On-screen pickers for the A2 Macro Controller GUI.

Three widgets that let the user point at things on screen instead of typing
coordinates:

  * RectanglePicker : a translucent rectangle with draggable edges/corners;
    refines the capture region (and, with left/right locked, the skill band).
  * PointPicker     : a draggable bullseye; for the skill slots and game-over
    tap point.
  * pick_window(...): one-shot: wait for a click, return the client-area rect
    of the top-level window under it. Step 1 of the capture-region flow.

All three sit on top of the screen behind a borderless control bar (TopBar)
with Cancel / Confirm and a one-line instruction.

The overlay is a Toplevel sized to the whole virtual desktop. Its background
is a colour-key (Windows -transparentcolor): pixels of that colour are fully
transparent AND click-through, so only the drawn border/handles are
visible/clickable and the rest passes clicks through to the game.

Windows-only: uses user32 (ctypes) for window detection and Tk's
-transparentcolor for click-through.
"""

import ctypes
import threading
import time
import tkinter as tk


# Colour-key: pixels exactly this colour become transparent + click-through
# under wm_attributes("-transparentcolor", ...) on Windows.
_KEY = "#ff00fe"
_BORDER = "#e84118"
_HANDLE_FILL = "#e84118"
_HANDLE_OUTLINE = "white"
_BAR_BG = "#202124"
_HANDLE = 11           # half-side of an edge/corner handle, px
_MIN_SIZE = 20         # smallest allowed rectangle, each axis

_VK_LBUTTON = 0x01
_VK_ESCAPE = 0x1B
_GA_ROOT = 2

_user32 = ctypes.windll.user32


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _virtual_screen():
    """Bounding box of the whole virtual desktop, (x, y, w, h)."""
    return (_user32.GetSystemMetrics(76),    # SM_XVIRTUALSCREEN
            _user32.GetSystemMetrics(77),    # SM_YVIRTUALSCREEN
            _user32.GetSystemMetrics(78),    # SM_CXVIRTUALSCREEN
            _user32.GetSystemMetrics(79))    # SM_CYVIRTUALSCREEN


def _cursor_pos():
    pt = _POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _client_rect(hwnd):
    """Client area of hwnd in screen coords, (x, y, w, h)."""
    rect = _RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = _POINT(rect.left, rect.top)
    _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, rect.right - rect.left, rect.bottom - rect.top


def _window_under(x, y):
    """Client rect of the top-level window at screen point (x, y)."""
    hwnd = _user32.WindowFromPoint(_POINT(x, y))
    if not hwnd:
        return None
    root_hwnd = _user32.GetAncestor(hwnd, _GA_ROOT)
    if root_hwnd:
        hwnd = root_hwnd
    return _client_rect(hwnd)


def _wait_for_click(should_stop, timeout=30.0):
    """Block until the left button is pressed, then return (x, y). Returns None
    on Esc / `should_stop()` truthy / timeout."""
    deadline = time.monotonic() + timeout
    # Drain any press still down from the click that opened us.
    while _user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000:
        if time.monotonic() > deadline or should_stop():
            return None
        time.sleep(0.02)
    while True:
        if time.monotonic() > deadline or should_stop():
            return None
        if _user32.GetAsyncKeyState(_VK_ESCAPE) & 0x8000:
            return None
        if _user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000:
            return _cursor_pos()
        time.sleep(0.02)


class TopBar:
    """Borderless instruction bar pinned to the top of the screen.

    Shows a message and optional Cancel / Confirm buttons. Pass None for a
    callback to omit that button.
    """

    def __init__(self, root, message, on_confirm=None, on_cancel=None):
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.wm_attributes("-topmost", True)
        try:
            self.top.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.top.configure(background=_BAR_BG)

        frame = tk.Frame(self.top, background=_BAR_BG, padx=10, pady=8)
        frame.pack()

        self.label = tk.Label(frame, text=message, background=_BAR_BG,
                              foreground="white", font=("Segoe UI", 10),
                              padx=8)
        self.label.pack(side="left")

        if on_cancel is not None:
            tk.Button(frame, text="✗  Cancel", command=on_cancel,
                      background="#aa3333", foreground="white",
                      activebackground="#cc4444", activeforeground="white",
                      font=("Segoe UI", 10, "bold"), relief="flat",
                      borderwidth=0, padx=10, pady=2
                      ).pack(side="right", padx=(8, 0))
        if on_confirm is not None:
            tk.Button(frame, text="✓  Confirm", command=on_confirm,
                      background="#2e9e44", foreground="white",
                      activebackground="#3eaf55", activeforeground="white",
                      font=("Segoe UI", 10, "bold"), relief="flat",
                      borderwidth=0, padx=10, pady=2
                      ).pack(side="right", padx=(8, 0))

        # Esc cancels. bind_all so it fires even when focus is on the overlay
        # canvas; removed in destroy().
        if on_cancel is not None:
            self.top.bind("<Escape>", lambda e: on_cancel())
            self._esc_tag = self.top.bind_all(
                "<Escape>", lambda e: on_cancel(), add="+")
        else:
            self._esc_tag = None

        self.top.update_idletasks()
        vsx, vsy, vsw, _ = _virtual_screen()
        w = self.top.winfo_reqwidth()
        x = vsx + max(0, (vsw - w) // 2)
        y = vsy + 14
        self.top.geometry(f"+{x}+{y}")
        self.top.focus_force()

    def set_message(self, message):
        self.label.config(text=message)

    def destroy(self):
        if self._esc_tag is not None:
            try:
                self.top.unbind_all("<Escape>")
            except tk.TclError:
                pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class _Overlay:
    """Click-through Toplevel covering the whole virtual desktop. Canvas pixels
    left at the colour-key background are transparent + click-through; only the
    drawn items (border, handles) are visible and grab the mouse."""

    def __init__(self, root):
        vsx, vsy, vsw, vsh = _virtual_screen()
        self.vsx, self.vsy = vsx, vsy
        self.vsw, self.vsh = vsw, vsh

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.wm_attributes("-topmost", True)
        try:
            self.top.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.top.wm_attributes("-transparentcolor", _KEY)
        self.top.configure(background=_KEY)
        self.top.geometry(f"{vsw}x{vsh}+{vsx}+{vsy}")

        self.canvas = tk.Canvas(self.top, width=vsw, height=vsh,
                                background=_KEY, highlightthickness=0,
                                borderwidth=0)
        self.canvas.pack(fill="both", expand=True)

    def to_canvas(self, x, y):
        """Screen point -> overlay-local canvas coords."""
        return x - self.vsx, y - self.vsy

    def destroy(self):
        try:
            self.top.destroy()
        except tk.TclError:
            pass


_CURSORS = {
    "top":    "sb_v_double_arrow",
    "bottom": "sb_v_double_arrow",
    "left":   "sb_h_double_arrow",
    "right":  "sb_h_double_arrow",
    "nw":     "top_left_corner",
    "se":     "bottom_right_corner",
    "ne":     "top_right_corner",
    "sw":     "bottom_left_corner",
    "move":   "fleur",
}


class RectanglePicker:
    """An on-screen rectangle the user resizes by dragging edges or corners.

    Arguments:
      root:        the Tk root window
      initial:     starting (x, y, w, h) in screen coords
      on_done:     called with the final (x, y, w, h) on confirm, None on
                   cancel. Runs on the Tk main thread.
      message:     instruction shown in the TopBar
      lock_edges:  edge names {"top","bottom","left","right"} the user can't
                   drag. Corners touching a locked edge are hidden. The band
                   picker locks left + right.
      move_axis:   centre move-grip behaviour: "free" / "x" / "y" / None
                   (hide it).
    """

    def __init__(self, root, initial, on_done,
                 message="Drag the edges to refine. ✓ to save, "
                         "✗ to cancel.",
                 lock_edges=None, move_axis="free"):
        self._on_done = on_done
        self._done = False
        self._drag_mode = None
        self._drag_origin = None
        self._drag_start_rect = None
        self.lock_edges = set(lock_edges or ())
        self.move_axis = move_axis
        x, y, w, h = (int(v) for v in initial)
        self.x, self.y, self.w, self.h = x, y, max(_MIN_SIZE, w), max(_MIN_SIZE, h)

        self.overlay = _Overlay(root)
        # Motion/release bind to the canvas, not the handle items: _draw()
        # deletes and recreates every item each frame, so item-level bindings
        # would vanish on the first redraw. Canvas-level bindings survive.
        self.overlay.canvas.bind("<B1-Motion>", self._motion)
        self.overlay.canvas.bind("<ButtonRelease-1>", self._end)
        self.bar = TopBar(root, message,
                          on_confirm=self._confirm, on_cancel=self._cancel)
        self._draw()

    def _draw(self):
        c = self.overlay.canvas
        c.delete("all")
        ax, ay = self.overlay.to_canvas(self.x, self.y)
        bx, by = ax + self.w, ay + self.h

        # White halo under the red border, so it stays visible on any colour.
        c.create_rectangle(ax - 1, ay - 1, bx + 1, by + 1,
                           outline="white", width=2)
        c.create_rectangle(ax, ay, bx, by, outline=_BORDER, width=2)

        mx, my = (ax + bx) // 2, (ay + by) // 2

        # Edge handles (skip locked edges).
        candidates = []
        if "top" not in self.lock_edges:
            candidates.append(("top", mx, ay))
        if "bottom" not in self.lock_edges:
            candidates.append(("bottom", mx, by))
        if "left" not in self.lock_edges:
            candidates.append(("left", ax, my))
        if "right" not in self.lock_edges:
            candidates.append(("right", bx, my))

        # Corner handles (only when both adjoining edges are unlocked).
        for tag, hx, hy, edges in [
                ("nw", ax, ay, ("top", "left")),
                ("ne", bx, ay, ("top", "right")),
                ("sw", ax, by, ("bottom", "left")),
                ("se", bx, by, ("bottom", "right"))]:
            if not (edges[0] in self.lock_edges
                    or edges[1] in self.lock_edges):
                candidates.append((tag, hx, hy))

        for tag, hx, hy in candidates:
            self._handle(hx, hy, tag)

        if self.move_axis is not None:
            self._move_grip(mx, my)

        # Size readout outside the box, below the bottom edge; flips above the
        # top edge when too near the bottom of the screen.
        readout = f"{self.w} x {self.h}"
        pad = 4
        text_gap = _HANDLE + 14   # box edge to text centre
        below_y = by + text_gap
        above_y = ay - text_gap
        # 22 ~= text bbox height + pad.
        if below_y + 22 > self.overlay.vsh and above_y - 22 >= 0:
            ty = above_y
        else:
            ty = below_y
        text_id = c.create_text(mx, ty, text=readout, fill="white",
                                font=("Segoe UI", 9, "bold"))
        tb = c.bbox(text_id)
        if tb:
            bg = c.create_rectangle(tb[0] - pad, tb[1] - pad,
                                    tb[2] + pad, tb[3] + pad,
                                    fill=_BAR_BG, outline="")
            c.tag_lower(bg, text_id)

    def _handle(self, hx, hy, tag):
        c = self.overlay.canvas
        s = _HANDLE
        item = c.create_rectangle(hx - s, hy - s, hx + s, hy + s,
                                  fill=_HANDLE_FILL, outline=_HANDLE_OUTLINE,
                                  width=2)
        cursor = _CURSORS.get(tag, "")
        c.tag_bind(item, "<ButtonPress-1>",
                   lambda e, t=tag: self._begin(t, e))
        c.tag_bind(item, "<Enter>",
                   lambda e, cur=cursor: c.config(cursor=cur))
        c.tag_bind(item, "<Leave>", lambda e: c.config(cursor=""))

    def _move_grip(self, mx, my):
        c = self.overlay.canvas
        r = 14
        item = c.create_oval(mx - r, my - r, mx + r, my + r,
                             fill=_HANDLE_FILL, outline=_HANDLE_OUTLINE,
                             width=2)
        # White cross so it reads as "move".
        c.create_line(mx - 7, my, mx + 7, my, fill="white", width=2)
        c.create_line(mx, my - 7, mx, my + 7, fill="white", width=2)
        cursor = _CURSORS["move"]
        c.tag_bind(item, "<ButtonPress-1>",
                   lambda e: self._begin("move", e))
        c.tag_bind(item, "<Enter>",
                   lambda e: c.config(cursor=cursor))
        c.tag_bind(item, "<Leave>", lambda e: c.config(cursor=""))

    def _begin(self, mode, event):
        self._drag_mode = mode
        self._drag_origin = (event.x_root, event.y_root)
        self._drag_start_rect = (self.x, self.y, self.w, self.h)

    def _motion(self, event):
        if self._drag_mode is None:
            return
        ox, oy, ow, oh = self._drag_start_rect
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        m = self._drag_mode
        left = m in ("left", "nw", "sw")
        right = m in ("right", "ne", "se")
        top = m in ("top", "nw", "ne")
        bottom = m in ("bottom", "sw", "se")

        x, y, w, h = ox, oy, ow, oh
        if m == "move":
            if self.move_axis in ("free", "x"):
                x = ox + dx
            if self.move_axis in ("free", "y"):
                y = oy + dy
        else:
            if left:
                x = ox + dx
                w = ow - dx
            if right:
                w = ow + dx
            if top:
                y = oy + dy
                h = oh - dy
            if bottom:
                h = oh + dy
            if w < _MIN_SIZE:
                if left:
                    x = ox + ow - _MIN_SIZE
                w = _MIN_SIZE
            if h < _MIN_SIZE:
                if top:
                    y = oy + oh - _MIN_SIZE
                h = _MIN_SIZE

        self.x, self.y, self.w, self.h = x, y, w, h
        self._draw()

    def _end(self, _event):
        self._drag_mode = None

    def _confirm(self):
        if self._done:
            return
        self._done = True
        result = (self.x, self.y, self.w, self.h)
        self._cleanup()
        self._on_done(result)

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self._cleanup()
        self._on_done(None)

    def _cleanup(self):
        self.bar.destroy()
        self.overlay.destroy()


class PointPicker:
    """A draggable bullseye for picking a single screen point.

    `initial` is the starting (x, y), or None to centre on the virtual desktop.
    `on_done(point_or_none)` gets (x, y) on confirm, None on cancel.
    """

    def __init__(self, root, initial, on_done,
                 message="Drag the bullseye to the click target. "
                         "✓ to save, ✗ to cancel."):
        self._on_done = on_done
        self._done = False
        self._drag_origin = None
        self._drag_start_point = None

        self.overlay = _Overlay(root)
        vsx, vsy = self.overlay.vsx, self.overlay.vsy
        vsw, vsh = self.overlay.vsw, self.overlay.vsh
        if initial is None:
            self.x, self.y = vsx + vsw // 2, vsy + vsh // 2
        else:
            self.x, self.y = int(initial[0]), int(initial[1])

        # Motion/release on the canvas so they survive _draw() rebuilds; press
        # stays on the items so we know what was grabbed.
        self.overlay.canvas.bind("<B1-Motion>", self._motion)
        self.overlay.canvas.bind("<ButtonRelease-1>", self._end)
        self.bar = TopBar(root, message,
                          on_confirm=self._confirm, on_cancel=self._cancel)
        self._draw()

    def _draw(self):
        c = self.overlay.canvas
        c.delete("all")
        cx, cy = self.overlay.to_canvas(self.x, self.y)

        # Translucent stipple disk over the whole bullseye, so clicks land
        # reliably anywhere on it (the gaps between rings aren't dead zones).
        c.create_oval(cx - 38, cy - 38, cx + 38, cy + 38,
                      fill="white", stipple="gray25", outline="")

        # Concentric rings: white halo -> red -> white -> red -> red dot.
        c.create_oval(cx - 28, cy - 28, cx + 28, cy + 28,
                      outline="white", width=2)
        c.create_oval(cx - 26, cy - 26, cx + 26, cy + 26,
                      outline=_BORDER, width=3)
        c.create_oval(cx - 18, cy - 18, cx + 18, cy + 18,
                      outline="white", width=2)
        c.create_oval(cx - 12, cy - 12, cx + 12, cy + 12,
                      outline=_BORDER, width=2)
        c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4,
                      fill=_BORDER, outline="white", width=1)
        # Crosshair tics past the outer ring.
        for x1, y1, x2, y2 in [
                (cx - 38, cy, cx - 28, cy),
                (cx + 28, cy, cx + 38, cy),
                (cx, cy - 38, cx, cy - 28),
                (cx, cy + 28, cx, cy + 38)]:
            c.create_line(x1, y1, x2, y2, fill=_BORDER, width=3)

        # Screen-coords readout.
        readout = f"({self.x}, {self.y})"
        pad = 4
        text_id = c.create_text(cx, cy + 56,
                                text=readout, fill="white",
                                font=("Segoe UI", 9, "bold"))
        tb = c.bbox(text_id)
        if tb:
            bg = c.create_rectangle(tb[0] - pad, tb[1] - pad,
                                    tb[2] + pad, tb[3] + pad,
                                    fill=_BAR_BG, outline="")
            c.tag_lower(bg, text_id)

        for item in c.find_all():
            c.tag_bind(item, "<ButtonPress-1>", self._begin)
            c.tag_bind(item, "<Enter>",
                       lambda e: c.config(cursor="fleur"))
            c.tag_bind(item, "<Leave>", lambda e: c.config(cursor=""))

    def _begin(self, event):
        self._drag_origin = (event.x_root, event.y_root)
        self._drag_start_point = (self.x, self.y)

    def _motion(self, event):
        if self._drag_origin is None:
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        ox, oy = self._drag_start_point
        self.x, self.y = ox + dx, oy + dy
        self._draw()

    def _end(self, _event):
        self._drag_origin = None

    def _confirm(self):
        if self._done:
            return
        self._done = True
        result = (self.x, self.y)
        self._cleanup()
        self._on_done(result)

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self._cleanup()
        self._on_done(None)

    def _cleanup(self):
        self.bar.destroy()
        self.overlay.destroy()


def pick_window(root, on_done,
                message="Click anywhere on the game window. "
                        "Press Esc to cancel."):
    """Wait for a left-click, then return the client-area rect of the
    top-level window under it.

    `on_done(rect_or_none)` runs on the Tk main thread with (x, y, w, h) on
    success or None if cancelled.
    """
    cancelled = {"v": False}

    def on_cancel():
        cancelled["v"] = True

    bar = TopBar(root, message, on_confirm=None, on_cancel=on_cancel)

    def worker():
        pos = _wait_for_click(should_stop=lambda: cancelled["v"])
        rect = None
        if pos is not None and not cancelled["v"]:
            rect = _window_under(*pos)

        def finish():
            bar.destroy()
            on_done(rect)
        try:
            root.after(0, finish)
        except tk.TclError:
            pass

    threading.Thread(target=worker, daemon=True).start()
