"""Custom Tkinter widgets for the A2 Macro Controller GUI.

Provides rounded buttons, sections, toggle switches, and other modern
UI elements built on tk.Canvas.
"""

import sys
import tkinter as tk
import tkinter.font as tkfont


# ---- Font resolution ----

_UI_FONT = None


def ui_font():
    """Return the preferred UI font family. Cached after first call."""
    global _UI_FONT
    if _UI_FONT is not None:
        return _UI_FONT
    try:
        families = set(tkfont.families())
    except Exception:
        _UI_FONT = "Segoe UI"
        return _UI_FONT
    for candidate in ("IBM Plex Sans", "Segoe UI", "Helvetica Neue",
                      "Helvetica"):
        if candidate in families:
            _UI_FONT = candidate
            return _UI_FONT
    _UI_FONT = "TkDefaultFont"
    return _UI_FONT


# ---- Dark title bar (Windows DWM) ----

def set_dark_titlebar(window, dark=True):
    """Toggle the Windows title-bar between dark and light via the DWM API.
    No-op on non-Windows platforms or if the API call fails."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


# ---- Canvas drawing helpers ----

def draw_rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """Draw a rounded rectangle on *canvas* using a smooth polygon."""
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [
        x1 + r, y1,  x2 - r, y1,
        x2, y1,  x2, y1 + r,
        x2, y2 - r,  x2, y2,
        x2 - r, y2,  x1 + r, y2,
        x1, y2,  x1, y2 - r,
        x1, y1 + r,  x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


# ---- RoundedSection ----

class RoundedSection(tk.Frame):
    """A section with a rounded border and optional title **inside** the
    border.  Pack children into ``section.inner``.

    Uses a Canvas (placed behind via ``place``) for the rounded border
    and ``pack`` for the inner content frame so geometry auto-sizes.
    """

    RADIUS = 8

    def __init__(self, parent, title="", bg="#1a1b1f",
                 border_color="#33363e", title_fg="#e7e8ec",
                 radius=None, parent_bg=None, **kw):
        if parent_bg is None:
            try:
                parent_bg = parent.cget("background")
            except Exception:
                parent_bg = bg
        super().__init__(parent, bg=parent_bg, highlightthickness=0,
                         bd=0, **kw)
        if radius is not None:
            self.RADIUS = radius
        self._bg = bg
        self._border_color = border_color
        self._title_fg = title_fg
        self._title = title
        self._parent_bg = parent_bg

        # Canvas occupies the full area via *place* (does not affect
        # pack-based sizing of children).
        self._canvas = tk.Canvas(self, highlightthickness=0,
                                 bg=parent_bg, bd=0)
        self._canvas.place(x=0, y=0, relwidth=1.0, relheight=1.0)

        title_h = 32 if title else self.RADIUS + 2
        self.inner = tk.Frame(self, bg=bg, highlightthickness=0, bd=0)
        self.inner.pack(fill="both", expand=True,
                        padx=(self.RADIUS + 6, self.RADIUS + 6),
                        pady=(title_h, self.RADIUS + 4))
        self.inner.lift()

        if title:
            self._title_lbl = tk.Label(
                self, text=title, bg=bg, fg=title_fg,
                font=(ui_font(), 10, "bold"),
                highlightthickness=0, bd=0)
            self._title_lbl.place(x=16, y=8)
            self._title_lbl.lift()
        else:
            self._title_lbl = None

        # Coalesce the storm of <Configure> events a window resize produces
        # into one redraw per idle cycle, and skip redraws when the size has
        # not actually changed (Configure also fires on position-only moves).
        self._last_wh = (0, 0)
        self._redraw_pending = False
        self.bind("<Configure>", self._on_configure)
        self.bind("<Map>", self._on_configure)
        self.after_idle(self._redraw)

    # -- drawing --

    def _on_configure(self, _event=None):
        if self._redraw_pending:
            return
        self._redraw_pending = True
        self.after_idle(self._do_redraw)

    def _do_redraw(self):
        self._redraw_pending = False
        self._redraw()

    def _redraw(self, force=False):
        c = self._canvas
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 20 or h < 20:
            return
        if not force and (w, h) == self._last_wh:
            return
        self._last_wh = (w, h)
        c.delete("border")
        r = self.RADIUS
        draw_rounded_rect(c, 1, 1, w - 2, h - 2, r,
                          fill=self._bg, outline=self._border_color,
                          width=1, tags="border")

    # -- re-theming --

    def update_colors(self, bg=None, border_color=None, title_fg=None,
                      parent_bg=None):
        if bg is not None:
            self._bg = bg
            self.inner.configure(bg=bg)
        if border_color is not None:
            self._border_color = border_color
        if title_fg is not None:
            self._title_fg = title_fg
            if self._title_lbl:
                self._title_lbl.configure(fg=title_fg, bg=bg or self._bg)
        if parent_bg is not None:
            self._parent_bg = parent_bg
            self.configure(bg=parent_bg)
            self._canvas.configure(bg=parent_bg)
        self._redraw(force=True)


# ---- RoundedButton ----

class RoundedButton(tk.Canvas):
    """A button drawn with rounded corners on a Canvas."""

    def __init__(self, parent, text="", command=None,
                 bg="#46b66e", fg="#ffffff", hover_bg=None,
                 font=None, radius=6, padx=16, pady=6,
                 width=None, **kw):
        canvas_bg = kw.pop("canvas_bg", None)
        if canvas_bg is None:
            try:
                canvas_bg = parent.cget("background")
            except Exception:
                canvas_bg = "#1a1b1f"
        super().__init__(parent, highlightthickness=0, bg=canvas_bg,
                         cursor="hand2", bd=0, **kw)
        self._text = text
        self._command = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or _lighten(bg, 15)
        self._font = font or (ui_font(), 10, "bold")
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self._state = "normal"
        self._hovering = False

        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._auto_size(width)
        self._redraw()

    def _auto_size(self, explicit_w=None):
        f = tkfont.Font(font=self._font)
        tw = f.measure(self._text)
        th = f.metrics("linespace")
        w = explicit_w if explicit_w else tw + 2 * self._padx
        h = th + 2 * self._pady
        self.configure(width=w, height=h)

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            w = self.winfo_reqwidth()
        h = self.winfo_height()
        if h <= 1:
            h = self.winfo_reqheight()
        if w < 4 or h < 4:
            return
        r = self._radius
        if self._state == "disabled":
            bg, fg = "#555", "#888"
        elif self._hovering:
            bg, fg = self._hover_bg, self._fg
        else:
            bg, fg = self._bg, self._fg
        draw_rounded_rect(self, 1, 1, w - 1, h - 1, r,
                          fill=bg, outline="")
        self.create_text(w // 2, h // 2, text=self._text,
                         fill=fg, font=self._font)

    def _on_enter(self, _e):
        self._hovering = True
        if self._state != "disabled":
            self._redraw()

    def _on_leave(self, _e):
        self._hovering = False
        self._redraw()

    def _on_click(self, _e):
        if self._state != "disabled" and self._command:
            self._command()

    def set_canvas_bg(self, color):
        """Recolour the Canvas backing (visible at the rounded corners and the
        1px margin around the fill). Distinct from ``background``, which the
        overridden ``configure`` treats as the drawn fill -- without this a
        dark->light switch leaves a dark frame around the button."""
        super().configure(bg=color)
        self._redraw()

    def configure(self, **kw):
        redraw = False
        for key in ("state", "text", "background", "activebackground",
                     "foreground", "activeforeground", "font",
                     "relief", "padx", "pady"):
            if key not in kw:
                continue
            val = kw.pop(key)
            if key == "state":
                self._state = val
            elif key == "text":
                self._text = val
            elif key == "background":
                self._bg = val
            elif key == "activebackground":
                self._hover_bg = val
            elif key == "foreground":
                self._fg = val
            elif key == "font":
                self._font = val
            redraw = True
        if "command" in kw:
            self._command = kw.pop("command")
        if kw:
            super().configure(**kw)
        if redraw:
            self._redraw()

    config = configure


# ---- StatusPill ----

class StatusPill(tk.Canvas):
    """Pill-shaped status indicator: LED dot + text.

    The dot is always static.  A low-opacity glow ring slowly pulses
    outward around the dot when ``start_pulse`` is called.
    """

    def __init__(self, parent, text_var=None, bg="#2a2c32",
                 outline="#3a3d45", canvas_bg=None, **kw):
        if canvas_bg is None:
            try:
                canvas_bg = parent.cget("background")
            except Exception:
                canvas_bg = "#1a1b1f"
        super().__init__(parent, highlightthickness=0, bg=canvas_bg,
                         height=30, bd=0, **kw)
        self._pill_bg = bg
        self._pill_outline = outline
        self._dot_color = "#e2575c"
        self._text_color = "#e7e8ec"
        self._text_var = text_var
        self._pulse_phase = 0.0
        self._pulse_after = None
        self._pulsing = False

        if text_var:
            text_var.trace_add("write",
                               lambda *_: self.after_idle(self._redraw))
        self.bind("<Configure>", lambda e: self._redraw())
        self.after(50, self._auto_width)

    def _auto_width(self):
        text = self._text_var.get() if self._text_var else ""
        f = tkfont.Font(font=(ui_font(), 10, "bold"))
        tw = f.measure(text)
        self.configure(width=max(tw + 44, 80))
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 20 or h < 10:
            return
        r = h // 2
        draw_rounded_rect(self, 0, 0, w, h, r,
                          fill=self._pill_bg,
                          outline=self._pill_outline, width=1)

        dot_x, dot_y, dot_r = 16, h // 2, 4
        # Pulse glow ring
        if self._pulsing and self._pulse_phase > 0.01:
            glow_r = dot_r + 2 + int(self._pulse_phase * 6)
            # Approximate opacity via stipple patterns
            if self._pulse_phase < 0.25:
                stipple = "gray12"
            elif self._pulse_phase < 0.5:
                stipple = "gray25"
            elif self._pulse_phase < 0.75:
                stipple = "gray25"
            else:
                stipple = "gray12"
            self.create_oval(dot_x - glow_r, dot_y - glow_r,
                             dot_x + glow_r, dot_y + glow_r,
                             fill="", outline=self._dot_color,
                             width=1, stipple=stipple)
        # Solid dot
        self.create_oval(dot_x - dot_r, dot_y - dot_r,
                         dot_x + dot_r, dot_y + dot_r,
                         fill=self._dot_color, outline="")
        # Text
        text = self._text_var.get() if self._text_var else ""
        self.create_text(dot_x + dot_r + 10, h // 2,
                         text=text, anchor="w",
                         fill=self._text_color,
                         font=(ui_font(), 10, "bold"))

    # -- public API --

    def set_colors(self, dot_color=None, text_color=None,
                   bg=None, outline=None, canvas_bg=None):
        if dot_color:
            self._dot_color = dot_color
        if text_color:
            self._text_color = text_color
        if bg:
            self._pill_bg = bg
        if outline:
            self._pill_outline = outline
        if canvas_bg:
            super().configure(bg=canvas_bg)
        self._auto_width()

    def start_pulse(self):
        self._pulsing = True
        self._pulse_phase = 0.0
        self._pulse_tick()

    def stop_pulse(self):
        self._pulsing = False
        if self._pulse_after:
            try:
                self.after_cancel(self._pulse_after)
            except Exception:
                pass
            self._pulse_after = None
        self._redraw()

    def _pulse_tick(self):
        if not self._pulsing:
            return
        self._pulse_phase += 0.04
        if self._pulse_phase > 1.0:
            self._pulse_phase = 0.0
        self._redraw()
        self._pulse_after = self.after(50, self._pulse_tick)


# ---- ToggleSwitch ----

class ToggleSwitch(tk.Canvas):
    """A modern toggle switch bound to a BooleanVar."""

    def __init__(self, parent, variable=None, command=None,
                 on_color="#5b86e8", off_color="#41454e",
                 knob_color="#ffffff", width=44, height=24,
                 canvas_bg=None, labels=None, **kw):
        if canvas_bg is None:
            try:
                canvas_bg = parent.cget("background")
            except Exception:
                canvas_bg = "#1a1b1f"
        self._sw = width
        self._sh = height
        self._labels = labels  # (off_label, on_label) or None
        label_w = 0
        if labels:
            label_w = max(len(labels[0]), len(labels[1])) * 8 + 16
        super().__init__(parent, width=width + label_w, height=height,
                         highlightthickness=0, bg=canvas_bg,
                         cursor="hand2", bd=0, **kw)
        self._var = variable
        self._command = command
        self._on_color = on_color
        self._off_color = off_color
        self._knob_color = knob_color
        self._label_fg = "#a6abb4"
        self._label_w = label_w

        if variable:
            variable.trace_add("write", lambda *_: self._redraw())
        self.bind("<Button-1>", self._toggle)
        self.bind("<Configure>", lambda e: self._redraw())
        self.after(10, self._redraw)

    def _redraw(self):
        self.delete("all")
        w = self._sw
        h = self._sh
        r = h // 2
        on = self._var.get() if self._var else False
        bg = self._on_color if on else self._off_color
        draw_rounded_rect(self, 0, 0, w, h, r, fill=bg, outline="")
        knob_r = r - 3
        knob_x = w - r if on else r
        self.create_oval(knob_x - knob_r, h // 2 - knob_r,
                         knob_x + knob_r, h // 2 + knob_r,
                         fill=self._knob_color, outline="")
        if self._labels:
            label = self._labels[1] if on else self._labels[0]
            self.create_text(w + 8, h // 2, text=label, anchor="w",
                             fill=self._label_fg,
                             font=(ui_font(), 9))

    def _toggle(self, _event=None):
        if self._var:
            self._var.set(not self._var.get())
        if self._command:
            self._command()

    def update_colors(self, on_color=None, off_color=None,
                      canvas_bg=None, label_fg=None):
        if on_color:
            self._on_color = on_color
        if off_color:
            self._off_color = off_color
        if canvas_bg:
            self.configure(bg=canvas_bg)
        if label_fg:
            self._label_fg = label_fg
        self._redraw()


# ---- RoundedCheckbox ----

class RoundedCheckbox(tk.Canvas):
    """A rounded checkbox with dark filled appearance."""

    def __init__(self, parent, text="", variable=None, command=None,
                 checked_color="#5b86e8", unchecked_color="#41454e",
                 text_color="#a6abb4", check_color="#ffffff",
                 font=None, canvas_bg=None, **kw):
        if canvas_bg is None:
            try:
                canvas_bg = parent.cget("background")
            except Exception:
                canvas_bg = "#1a1b1f"
        super().__init__(parent, highlightthickness=0, bg=canvas_bg,
                         cursor="hand2", bd=0, height=22, **kw)
        self._text = text
        self._var = variable
        self._command = command
        self._checked_color = checked_color
        self._unchecked_color = unchecked_color
        self._text_color = text_color
        self._check_color = check_color
        self._font = font or (ui_font(), 9)

        if variable:
            variable.trace_add("write", lambda *_: self._redraw())
        self.bind("<Button-1>", self._toggle)
        self.bind("<Configure>", lambda e: self._redraw())
        f = tkfont.Font(font=self._font)
        self.configure(width=f.measure(text) + 32, height=22)
        self._redraw()

    def _redraw(self):
        self.delete("all")
        h = self.winfo_height()
        if h <= 1:
            h = self.winfo_reqheight()
        checked = self._var.get() if self._var else False
        box_size = 16
        bx, by = 2, (h - box_size) // 2
        r = 4
        fill = self._checked_color if checked else self._unchecked_color
        draw_rounded_rect(self, bx, by, bx + box_size, by + box_size,
                          r, fill=fill, outline="")
        if checked:
            cx, cy = bx + box_size // 2, by + box_size // 2
            self.create_line(cx - 4, cy, cx - 1, cy + 3, cx + 4, cy - 3,
                             fill=self._check_color, width=2,
                             capstyle="round", joinstyle="round")
        self.create_text(bx + box_size + 8, h // 2,
                         text=self._text, anchor="w",
                         fill=self._text_color, font=self._font)

    def _toggle(self, _event=None):
        if self._var:
            self._var.set(not self._var.get())
        if self._command:
            self._command()

    def update_colors(self, checked_color=None, unchecked_color=None,
                      text_color=None, canvas_bg=None):
        if checked_color:
            self._checked_color = checked_color
        if unchecked_color:
            self._unchecked_color = unchecked_color
        if text_color:
            self._text_color = text_color
        if canvas_bg:
            self.configure(bg=canvas_bg)
        self._redraw()


# ---- DirectionPad (2x2 grid of direction buttons) ----

class DirectionPad(tk.Frame):
    """A 2x2 grid of Top/Bottom/Left/Right buttons.

    ``labels`` maps the internal direction ids ("top" / "bottom" / "left" /
    "right", which is what the bound variable stores) to the text drawn on
    each button, so callers can pass translated labels without the stored
    ids ever changing. Defaults to English.
    """

    def __init__(self, parent, variable=None, command=None,
                 bg="#1a1b1f", selected_color="#5b86e8",
                 unselected_color="#31343c", fg="#e7e8ec",
                 radius=6, labels=None, **kw):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, **kw)
        self._var = variable
        self._command = command
        self._selected_color = selected_color
        self._unselected_color = unselected_color
        self._fg = fg
        self._radius = radius
        self._bg = bg
        self._buttons = {}
        self._dir_labels = labels or {"top": "Top", "bottom": "Bottom",
                                      "left": "Left", "right": "Right"}

        dirs = [("top", 0, 0), ("bottom", 0, 1),
                ("left", 1, 0), ("right", 1, 1)]
        for val, r, c in dirs:
            btn = tk.Canvas(self, width=80, height=36,
                            highlightthickness=0, bg=bg,
                            cursor="hand2", bd=0)
            btn.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
            btn.bind("<Button-1>",
                     lambda e, v=val: self._select(v))
            # Without these the buttons stayed blank until the first resize /
            # click, because the after(10) draw ran before they were laid out.
            btn.bind("<Configure>", lambda e: self._redraw())
            btn.bind("<Map>", lambda e: self._redraw())
            self._buttons[val] = btn
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        if variable:
            variable.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    def _select(self, val):
        if self._var:
            self._var.set(val)
        if self._command:
            self._command()

    def _redraw(self):
        current = self._var.get() if self._var else ""
        labels = self._dir_labels
        for val, btn in self._buttons.items():
            btn.delete("all")
            w = btn.winfo_width()
            if w <= 1:
                w = btn.winfo_reqwidth()
            h = btn.winfo_height()
            if h <= 1:
                h = btn.winfo_reqheight()
            if w < 4 or h < 4:
                continue
            sel = (val == current)
            bg = self._selected_color if sel else self._unselected_color
            draw_rounded_rect(btn, 1, 1, w - 1, h - 1, self._radius,
                              fill=bg, outline="")
            btn.create_text(w // 2, h // 2, text=labels[val],
                            fill="#ffffff" if sel else self._fg,
                            font=(ui_font(), 9, "bold" if sel else ""))

    def update_colors(self, bg=None, selected_color=None,
                      unselected_color=None, fg=None):
        if bg:
            self._bg = bg
            self.configure(bg=bg)
            for btn in self._buttons.values():
                btn.configure(bg=bg)
        if selected_color:
            self._selected_color = selected_color
        if unselected_color:
            self._unselected_color = unselected_color
        if fg:
            self._fg = fg
        self._redraw()


# ---- SegmentToggle (two-segment labelled slider switch) ----

class SegmentToggle(tk.Canvas):
    """A two-option switch that shows both labels side by side.

    Unlike ToggleSwitch (a round knob), the active option is covered by a
    translucent coloured block that slides between the two sides and resizes
    to fit whichever label is active -- so the two halves are intentionally
    uneven, each just wider than its own text.

    Bound to a BooleanVar: ``False`` selects the left option, ``True`` the
    right.  ``left_color`` / ``right_color`` let the highlight differ per side
    (e.g. green "Priority" vs red "Avoid"); pass the same colour for a static
    look (e.g. the blue movement switch).
    """

    def __init__(self, parent, variable=None, command=None,
                 labels=("Off", "On"),
                 left_color="#5b86e8", right_color="#5b86e8",
                 container_bg="#16171b", label_fg="#a6abb4",
                 outline="#33363e", height=30, pad=15, alpha=0.5,
                 font=None, canvas_bg=None, **kw):
        if canvas_bg is None:
            try:
                canvas_bg = parent.cget("background")
            except Exception:
                canvas_bg = "#1a1b1f"
        super().__init__(parent, highlightthickness=0, bg=canvas_bg,
                         cursor="hand2", bd=0, height=height, **kw)
        self._var = variable
        self._command = command
        self._labels = labels
        self._left_color = left_color
        self._right_color = right_color
        self._container_bg = container_bg
        self._label_fg = label_fg
        self._outline = outline
        self._h = height
        self._pad = pad
        self._alpha = alpha
        self._font = font or (ui_font(), 9, "bold")
        self._anim = None
        self._block = None          # current (x, w); None -> snap to target
        self._measure()

        if variable:
            variable.trace_add("write", lambda *_: self._on_var_change())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Map>", lambda e: self._draw())
        self._draw()

    # -- geometry --

    def _measure(self):
        f = tkfont.Font(font=self._font)
        self._left_seg = f.measure(self._labels[0]) + 2 * self._pad
        self._right_seg = f.measure(self._labels[1]) + 2 * self._pad
        self.configure(width=self._left_seg + self._right_seg, height=self._h)

    def _seg_geom(self, right):
        """Block (x, w) for the active side, inset slightly inside the seam."""
        m = 3
        if right:
            return (self._left_seg + m, self._right_seg - 2 * m)
        return (m, self._left_seg - 2 * m)

    # -- interaction --

    def _on_var_change(self):
        self._animate_to(bool(self._var.get()) if self._var else False)

    def _on_click(self, event):
        right = event.x >= self._left_seg
        if self._var is not None:
            if bool(self._var.get()) == right:
                return
            self._var.set(right)        # trace fires -> _animate_to
        else:
            self._animate_to(right)
        if self._command:
            self._command()

    def _animate_to(self, right):
        target = self._seg_geom(right)
        if self._block is None:
            self._block = target
            self._draw()
            return
        start = self._block
        steps = 6
        if self._anim:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None

        def frame(i):
            t = i / steps
            t = 1 - (1 - t) ** 3        # ease-out cubic
            self._block = (start[0] + (target[0] - start[0]) * t,
                           start[1] + (target[1] - start[1]) * t)
            self._draw()
            if i < steps:
                self._anim = self.after(12, lambda: frame(i + 1))
            else:
                self._block = target
                self._anim = None
                self._draw()

        frame(1)

    # -- drawing --

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            w = self.winfo_reqwidth()
        h = self.winfo_height()
        if h <= 1:
            h = self.winfo_reqheight()
        if w < 4 or h < 4:
            return
        right = bool(self._var.get()) if self._var else False
        r = h // 2
        draw_rounded_rect(self, 1, 1, w - 1, h - 1, r,
                          fill=self._container_bg,
                          outline=self._outline, width=1)
        if self._block is None:
            self._block = self._seg_geom(right)
        bx, bw = self._block
        color = self._right_color if right else self._left_color
        draw_rounded_rect(self, bx, 3, bx + bw, h - 3, max(4, r - 3),
                          fill=_blend(color, self._container_bg, self._alpha),
                          outline="")
        self.create_text(self._left_seg / 2, h / 2, text=self._labels[0],
                         fill=self._label_fg, font=self._font)
        self.create_text(self._left_seg + self._right_seg / 2, h / 2,
                         text=self._labels[1], fill=self._label_fg,
                         font=self._font)

    def update_colors(self, container_bg=None, label_fg=None, canvas_bg=None,
                      outline=None, left_color=None, right_color=None):
        if container_bg:
            self._container_bg = container_bg
        if label_fg:
            self._label_fg = label_fg
        if outline:
            self._outline = outline
        if left_color:
            self._left_color = left_color
        if right_color:
            self._right_color = right_color
        if canvas_bg:
            self.configure(bg=canvas_bg)
        self._draw()


# ---- helpers ----

def _lighten(hex_color, amount):
    """Brighten a hex colour by *amount* per channel."""
    try:
        r = min(255, int(hex_color[1:3], 16) + amount)
        g = min(255, int(hex_color[3:5], 16) + amount)
        b = min(255, int(hex_color[5:7], 16) + amount)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


def _blend(c1, c2, t):
    """Blend hex colour *c1* over *c2*; t=1.0 is pure c1, t=0.0 is pure c2.

    Used to fake translucency on a Canvas (no real alpha): a colour at t~0.5
    over a near-black or white container reads as a soft tinted block."""
    try:
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        r = round(r1 * t + r2 * (1 - t))
        g = round(g1 * t + g2 * (1 - t))
        b = round(b1 * t + b2 * (1 - t))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return c1
