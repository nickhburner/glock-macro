"""
Graphical control panel for A2 Macro Controller.

Settings are saved to settings.json (next to config.py), which main.py and
diagnose.py also read. The macro runs in a background thread so the window
stays responsive.

Usage:
    python gui.py
"""

import ctypes
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

import all_star
import capture
import config
import eternal_lode
import main
from adb import ADBClient, ADBError, find_adb, list_devices, choose_server_port
from matcher import (
    list_skill_files,
    skill_hash,
)
from widgets import (
    ui_font, set_dark_titlebar, draw_rounded_rect,
    RoundedSection, RoundedButton, StatusPill, ToggleSwitch,
    RoundedCheckbox, DirectionPad, SegmentToggle,
)

# Optional pynput for the global Start/Stop hotkey.
try:
    from pynput import keyboard as _pynput_kb
    _PYNPUT_AVAILABLE = True
except ImportError:
    _pynput_kb = None
    _PYNPUT_AVAILABLE = False

# Pillow renamed the resampling enum; support old and new.
try:
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:                       # Pillow < 9.1
    _RESAMPLE = Image.LANCZOS

# Colour schemes

_AVOID_COLOR = "#cc3333"
_CUSTOM_COLOR = "#2e9e44"

_THEMES = {
    "light": {
        "bg": "#eef0f3",
        "surface": "#ffffff",
        "surface_2": "#f1f3f6",
        "surface_3": "#e6e9ee",
        "inset": "#f6f7f9",
        "border": "#dde0e6",
        "border_strong": "#c8cdd5",
        "fg": "#1c1e24",
        "fg_dim": "#565c66",
        "fg_muted": "#888e98",
        "ok": "#46b66e",
        "bad": "#e2575c",
        "warn": "#e0a534",
        "accent": "#5b86e8",
    },
    "dark": {
        "bg": "#1a1b1f",
        "surface": "#212329",
        "surface_2": "#282b32",
        "surface_3": "#31343c",
        "inset": "#16171b",
        "border": "#33363e",
        "border_strong": "#41454e",
        "fg": "#f0f1f5",
        "fg_dim": "#a0a5b0",
        "fg_muted": "#717680",
        "ok": "#46b66e",
        "bad": "#e2575c",
        "warn": "#e0a534",
        "accent": "#5b86e8",
    },
}

# Status text -> palette key for its colour.
_STATUS_COLORS = {"Running": "ok", "Stopped": "bad", "Stopping…": "warn"}

# Game modes -- dropdown values and bidirectional lookups.
_GAME_MODES = [
    ("chapter", "Chapter"),
    ("eternal", "Eternal Lode"),
    ("plant",   "Plant Defense"),
    ("jungle",  "Shackled Jungle"),
    ("allstar", "All-Star Cup"),
]
_GM_NAME_TO_ID = {name: gid for gid, name in _GAME_MODES}
_GM_ID_TO_NAME = {gid: name for gid, name in _GAME_MODES}
_GM_NAMES = [name for _, name in _GAME_MODES]

# Keys rendered on the main view (timeout card), skipped in the settings panel.
_MAIN_VIEW_KEYS = {"RUN_TIMEOUT_HOURS", "STUCK_TIMEOUT_MINUTES",
                    "CLOSE_ON_TIMEOUT", "SLEEP_PHONE_ON_TIMEOUT"}


# Settings form schema. Each entry is one of:
#   ("section", title)
#   ("button", label, App-method-name[, tooltip])
#   (config_key, label, kind, unit, hint)
# kind: "float" / "int" / "bool"  single scalar
#       "floats3"                  three floats (min / max / step)
#       "ints2" / "ints4"          N integers
SETTINGS_SCHEMA = [
    ("section", "Timing"),
    ("POLL_INTERVAL", "Poll interval", "float", "seconds",
     "How long the macro waits between screen checks."),
    ("ACTION_DELAY", "Action delay", "float", "seconds",
     "Pause after a tap so the screen can settle before the next check."),
    ("STARTUP_DELAY", "Startup delay", "float", "seconds",
     "Grace period after pressing Start before the first screen check. "
     "With ADB the game window no longer needs to be focused."),
    ("SKILL_SETTLE_DELAY", "Skill settle delay", "float", "seconds",
     "After a skill-selection screen is detected, wait this long and re-grab "
     "before reading the cards, so a fast poll doesn't scan before the cards "
     "and Refresh button finish animating in. 0.1-0.2s is plenty; 0 disables."),

    ("section", "Run timeout"),
    ("RUN_TIMEOUT_HOURS", "Run timeout", "float", "hours",
     "Stop the macro automatically after this long. Set to 0 to disable the "
     "timeout and run until stopped manually."),
    ("STUCK_TIMEOUT_MINUTES", "Stuck timeout", "float", "min",
     "Stop the macro if it keeps repeating the same action (e.g. tapping a "
     "button that won't dismiss) for this long. Set to 0 to disable."),
    ("CLOSE_ON_TIMEOUT", "Close game window on timeout", "bool", "",
     "When the timeout fires, also close the game window, the BlueStacks "
     "emulator (and its background services) or the scrcpy mirror window."),
    ("SLEEP_PHONE_ON_TIMEOUT", "Sleep phone on timeout", "bool", "",
     "When the timeout fires, turn the phone screen off via ADB so the game "
     "pauses and the phone stops overheating."),

    ("section", "Template matching"),
    ("MATCH_THRESHOLD", "Skill match threshold", "float", "0-1",
     "Minimum confidence to accept a skill-icon match. Higher is stricter, "
     "fewer false matches, but more misses."),
    ("REF_THRESHOLD", "UI match threshold", "float", "0-1",
     "Minimum confidence to accept a UI-element match (Play button, devil "
     "offer, game-over screen, refresh button)."),
    ("SKILL_DOWNSCALE", "Skill downscale", "float", "factor",
     "Skill matching runs at this fraction of full resolution for speed. "
     "1.0 = full res; 0.5 is roughly 4x faster."),
    ("REF_DOWNSCALE", "Ref downscale", "float", "factor",
     "UI refs/banners are matched over the whole frame, so they dominate the "
     "poll time. This runs them at a fraction of resolution. 0.6 is ~2.5x "
     "faster; 0.5 faster still but riskier for short refs (game-over, level)."),
    ("CALIBRATED_SCALE", "Calibrated scale", "float", "",
     "Zoom factor applied to all template scales. 1.0 means the bundled "
     "templates already match the phone's native resolution. Run Setup "
     "Wizard to determine the correct value for your device."),

    ("section", "Capture"),
    ("USE_STREAM_CAPTURE", "Stream capture (screenrecord)", "bool", "",
     "Capture from a continuous Android screenrecord H.264 stream instead of a "
     "screencap each poll. Keeps the display composited, which fixes BlueStacks "
     "all-black frames during gameplay, and is faster. Needs PyAV; "
     "automatically falls back to screencap if it is unavailable."),

    ("section", "Humanisation"),
    ("CLICK_JITTER", "Click jitter", "int", "px",
     "Each tap is nudged by up to this many random pixels so input is not "
     "pixel-perfect."),
    ("DELAY_JITTER", "Delay jitter", "float", "fraction",
     "Poll and action delays vary randomly by +/- this fraction "
     "(0.35 means +/-35%)."),
]

# Entry boxes per non-bool field kind.
_KIND_WIDTHS = {"float": 1, "int": 1, "floats3": 3, "ints2": 2, "ints4": 4}


def _fmt(value):
    """Format a stored number for display in an entry box."""
    return str(value)


class Tooltip:
    """A small hint box shown after hovering a widget briefly."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, justify="left",
                 background="#ffffe0", relief="solid", borderwidth=1,
                 wraplength=320, font=("", 8)).pack(ipadx=4, ipady=3)

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class _AdbCropWindow:
    """Show an ADB screenshot and let the user drag-select a crop region.

    Calls callback((x, y, w, h)) in phone-pixel space on confirm, or
    callback(None) on cancel/close.
    """

    _MAX_W = 800
    _MAX_H = 600

    def __init__(self, parent, screenshot_bgr, callback, title=None):
        self._callback = callback
        self._done = False
        self._start = None
        self._end = None
        self._rect_id = None
        self._title = title

        h_px, w_px = screenshot_bgr.shape[:2]
        scale = min(self._MAX_W / w_px, self._MAX_H / h_px, 1.0)
        disp_w = max(1, int(w_px * scale))
        disp_h = max(1, int(h_px * scale))
        self._scale = scale
        self._screenshot = screenshot_bgr

        rgb = screenshot_bgr[:, :, ::-1]
        pil = Image.fromarray(rgb.copy()).resize((disp_w, disp_h), _RESAMPLE)
        self._photo = ImageTk.PhotoImage(pil)

        win = tk.Toplevel(parent)
        win.title(self._title
                  or "Capture  --  drag to select, then click Confirm")
        win.transient(parent)
        win.resizable(False, False)
        self._win = win

        canvas = tk.Canvas(win, width=disp_w, height=disp_h,
                           cursor="crosshair", highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._canvas = canvas
        self._disp_w = disp_w
        self._disp_h = disp_h

        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)

        btn_row = ttk.Frame(win)
        btn_row.pack(fill="x", padx=8, pady=6)
        self._hint = ttk.Label(
            btn_row,
            text="Drag to select the button area on the screenshot",
            foreground="#888")
        self._hint.pack(side="left")
        ttk.Button(btn_row, text="Cancel", command=self._cancel).pack(
            side="right")
        self._confirm_btn = ttk.Button(btn_row, text="Confirm",
                                        state="disabled",
                                        command=self._confirm)
        self._confirm_btn.pack(side="right", padx=(0, 6))

        win.protocol("WM_DELETE_WINDOW", self._cancel)

    @staticmethod
    def _clamp(val, lo, hi):
        return max(lo, min(hi, val))

    def _on_press(self, event):
        self._start = (event.x, event.y)
        self._end = None
        if self._rect_id is not None:
            self._canvas.delete(self._rect_id)
            self._rect_id = None
        self._confirm_btn.config(state="disabled")

    def _on_drag(self, event):
        if self._start is None:
            return
        ex = self._clamp(event.x, 0, self._disp_w)
        ey = self._clamp(event.y, 0, self._disp_h)
        if self._rect_id is not None:
            self._canvas.delete(self._rect_id)
        self._rect_id = self._canvas.create_rectangle(
            self._start[0], self._start[1], ex, ey,
            outline="#00cc00", width=2)
        self._end = (ex, ey)
        w = abs(ex - self._start[0])
        h = abs(ey - self._start[1])
        self._confirm_btn.config(
            state="normal" if w > 4 and h > 4 else "disabled")

    def _on_release(self, event):
        if self._start is None:
            return
        ex = self._clamp(event.x, 0, self._disp_w)
        ey = self._clamp(event.y, 0, self._disp_h)
        self._end = (ex, ey)
        if abs(ex - self._start[0]) > 4 and abs(ey - self._start[1]) > 4:
            self._confirm_btn.config(state="normal")

    def _confirm(self):
        if self._done or self._start is None or self._end is None:
            return
        self._done = True
        x1 = min(self._start[0], self._end[0])
        y1 = min(self._start[1], self._end[1])
        x2 = max(self._start[0], self._end[0])
        y2 = max(self._start[1], self._end[1])
        s = self._scale
        px = round(x1 / s)
        py = round(y1 / s)
        pw = round((x2 - x1) / s)
        ph = round((y2 - y1) / s)
        self._win.destroy()
        self._callback((px, py, pw, ph))

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self._win.destroy()
        self._callback(None)


_ES_CONTINUOUS       = 0x80000000
_ES_SYSTEM_REQUIRED  = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def _set_keep_awake(on: bool):
    """Prevent (on=True) or allow (on=False) the OS from sleeping/dimming."""
    try:
        if on:
            ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED)
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass


# ------------------------------------------------------------------ Hotkey
# Global Start/Stop hotkey via pynput.

# Tkinter keysym -> canonical stored key name.
_KEYSYM_TO_KEY = {
    'grave': '`', 'asciitilde': '`', 'quoteleft': '`',
    'space': 'space', 'Return': 'enter', 'BackSpace': 'backspace',
    'Delete': 'delete', 'Escape': 'esc', 'Tab': 'tab',
    'minus': '-', 'underscore': '-',
    'equal': '=', 'plus': '=',
    'bracketleft': '[', 'braceleft': '[',
    'bracketright': ']', 'braceright': ']',
    'backslash': '\\', 'bar': '\\',
    'semicolon': ';', 'colon': ';',
    'apostrophe': "'", 'quotedbl': "'",
    'comma': ',', 'less': ',',
    'period': '.', 'greater': '.',
    'slash': '/', 'question': '/',
}
for _i in range(1, 13):
    _KEYSYM_TO_KEY[f'F{_i}'] = f'f{_i}'

# Named keys whose pynput representation is Key.<name>, not a KeyCode.
_NAMED_KEYS = frozenset(
    ['space', 'enter', 'tab', 'backspace', 'delete', 'esc']
    + [f'f{i}' for i in range(1, 13)]
)

# Display labels for non-obvious stored key names.
_KEY_DISPLAY = {
    'space': 'Space', 'enter': 'Enter', 'tab': 'Tab',
    'backspace': 'Bksp', 'delete': 'Del', 'esc': 'Esc',
}
for _i in range(1, 13):
    _KEY_DISPLAY[f'f{_i}'] = f'F{_i}'

# Character -> Windows virtual-key code (Ctrl masks the char, so
# the watcher falls back to VK to identify the physical key).
_CHAR_TO_VK = {
    '`': 0xC0, '-': 0xBD, '=': 0xBB,
    '[': 0xDB, ']': 0xDD, '\\': 0xDC,
    ';': 0xBA, "'": 0xDE, ',': 0xBC,
    '.': 0xBE, '/': 0xBF,
}
for _c in 'abcdefghijklmnopqrstuvwxyz':
    _CHAR_TO_VK[_c] = ord(_c.upper())
for _c in '0123456789':
    _CHAR_TO_VK[_c] = ord(_c)


def _hotkey_display(hotkey_str):
    """Format a stored hotkey for the UI: 'ctrl+`' -> 'Ctrl + `'."""
    parts = [p.strip() for p in hotkey_str.split('+')]
    out = []
    for p in parts:
        low = p.lower()
        if low in ('ctrl', 'alt', 'shift'):
            out.append(low.capitalize())
        elif low in _KEY_DISPLAY:
            out.append(_KEY_DISPLAY[low])
        elif len(low) == 1 and low.isalpha():
            out.append(low.upper())
        else:
            out.append(p)
    return ' + '.join(out)


class _HotkeyWatcher:
    """Global hotkey listener via pynput.  Fires *on_trigger* (from a
    background thread) each time the configured combination is pressed."""

    def __init__(self, hotkey_str, on_trigger):
        self._on_trigger = on_trigger
        self._listener = None
        self._held = set()          # currently-held modifier names
        self._target_mods = set()   # required modifiers
        self._target_char = None    # main key character (single char keys)
        self._target_vk = None      # fallback VK code (Windows)
        self._target_name = None    # pynput Key.name (named keys)
        self._mod_keys = {}
        if _PYNPUT_AVAILABLE:
            self._mod_keys = {
                _pynput_kb.Key.ctrl_l: 'ctrl',
                _pynput_kb.Key.ctrl_r: 'ctrl',
                _pynput_kb.Key.alt_l: 'alt',
                _pynput_kb.Key.alt_r: 'alt',
                _pynput_kb.Key.shift_l: 'shift',
                _pynput_kb.Key.shift_r: 'shift',
            }
            for attr in ('ctrl', 'alt', 'shift', 'alt_gr'):
                k = getattr(_pynput_kb.Key, attr, None)
                if k is not None and k not in self._mod_keys:
                    self._mod_keys[k] = attr.split('_')[0]
        self._parse(hotkey_str)

    def _parse(self, hotkey_str):
        parts = [p.strip().lower() for p in hotkey_str.split('+')]
        self._target_mods = set()
        for p in parts[:-1]:
            if p in ('ctrl', 'control'):
                self._target_mods.add('ctrl')
            elif p == 'alt':
                self._target_mods.add('alt')
            elif p == 'shift':
                self._target_mods.add('shift')
        key = parts[-1] if parts else ''
        if key in _NAMED_KEYS:
            self._target_name = key
        else:
            self._target_char = key
            self._target_vk = _CHAR_TO_VK.get(key)

    def start(self):
        if not _PYNPUT_AVAILABLE:
            return
        self._held = set()
        self._listener = _pynput_kb.Listener(
            on_press=self._on_press, on_release=self._on_release,
            daemon=True)
        self._listener.start()

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        mod = self._mod_keys.get(key)
        if mod:
            self._held.add(mod)
            return
        if self._held != self._target_mods:
            return
        # Named key (F1, Space, Enter, ...): match by pynput name.
        if self._target_name is not None:
            name = getattr(key, 'name', None)
            if name is not None and name == self._target_name:
                self._on_trigger()
            return
        # Character key: try the reported char first.
        ch = getattr(key, 'char', None)
        if ch is not None and ch.lower() == self._target_char:
            self._on_trigger()
            return
        # Ctrl masks the char on Windows; fall back to virtual-key code.
        if self._target_vk is not None:
            vk = getattr(key, 'vk', None)
            if vk == self._target_vk:
                self._on_trigger()

    def _on_release(self, key):
        mod = self._mod_keys.get(key)
        if mod:
            self._held.discard(mod)


class App:
    def __init__(self, root):
        self.root = root
        self._thread = None
        self._stop_event = None
        self._running = False
        self._drag_cat = None
        self._coords_busy = False
        self._coord_buttons = []  # disabled during ADB capture operations
        # Capture stream shared by GUI Test/ref-capture flows so they get
        # non-black frames on BlueStacks (screencap is black with no consumer).
        self._capture_stream = None
        self._capture_stream_serial = None
        self._capture_stream_lock = threading.Lock()
        self._autosave_after = None
        self._picker = None
        self._picker_body = None
        self._picker_head = None
        self._picker_cells = {}
        self._picker_mode = "avoid"
        self._locked = False
        self._locked_geometry = None
        self._log_queue = queue.Queue()
        self.active_rows = {}
        self.vars = {}
        self._thumb_cache = {}
        self._hash_cache = {}
        self._custom_thumb_refs = []
        self._adb_label_to_serial = {}   # combo label -> device serial
        self._compact = False
        self._compact_widgets = {}       # widgets in compact mode
        self._full_widgets = {}          # widgets in full mode
        self._status_pulse_after = None
        self._status_pulse_on = True
        self._picker_search_var = None   # search StringVar in skill picker
        self._elim_picker = None         # Elim boss-pick Toplevel
        self._elim_sel = 0               # level slot the next boss click fills
        self._boss_thumb_cache = {}

        self.active = [c for c in config.ACTIVE_CATEGORIES
                       if c in config.SKILL_CATEGORIES]
        self.custom = list(config.CUSTOM_PRIORITY_SKILLS)
        self.avoid = list(config.AVOID_SKILLS)
        self.elim_bosses = self._normalized_elim_picks()

        self.dark = bool(config.DARK_MODE)
        self.theme = _THEMES["dark" if self.dark else "light"]
        self.style = ttk.Style()
        self._settings_win = None
        self._log_open = True
        self._log_line_count = 0
        self._sp_custom_btns_body = None

        # Pre-create all settings tk variables before building any widgets.
        self._create_settings_vars()

        self._build_topbar(root)

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        # uniform keeps the column split fixed by weight regardless of content,
        # so collapsing the log (right column) no longer nudges the divider and
        # makes the whole window twitch to a slightly different width.
        body.columnconfigure(0, weight=115, uniform="bodycols")
        body.columnconfigure(1, weight=100, uniform="bodycols")
        body.rowconfigure(0, weight=1)
        self._body = body

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        # Left: device, game mode, skill categories
        self._build_device_card(left)
        self._build_game_mode_card(left)
        self._build_skills(left)

        # Right: custom priority, avoid, timeout, log
        self._build_custom(right)
        self._build_avoid(right)
        self._build_timeout_card(right)
        self._build_log(right)

        # Compact mode frame (built once, shown/hidden by _toggle_compact)
        self._compact_frame = ttk.Frame(root)
        self._build_compact_view(self._compact_frame)

        self._load_settings_into_form()
        self._wire_autosave_traces()
        self._apply_theme()

        main.set_log_sink(self._log_queue.put)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Configure>", self._on_configure)
        self.root.after(120, self._drain_log)

        if config.KEEP_AWAKE:
            _set_keep_awake(True)

        # Global hotkey listener (Start/Stop toggle).
        self._hotkey_watcher = None
        self._start_hotkey_watcher()

        # First-run: trigger setup wizard when no device is configured.
        if not config.ADB_DEVICE or not any(config.PHONE_RESOLUTION):
            self.root.after(600, self._run_setup_wizard)

    # top bar -- start/stop, status, save, autosave, theme, compact, settings
    def _build_topbar(self, parent):
        bar = tk.Frame(parent, background=self.theme["surface"],
                       highlightthickness=0)
        bar.pack(fill="x", padx=0, pady=0)
        self._topbar = bar

        sep = tk.Frame(parent, height=1, background=self.theme["border"])
        sep.pack(fill="x")
        self._topbar_sep = sep

        inner = tk.Frame(bar, background=self.theme["surface"])
        inner.pack(fill="x", padx=12, pady=8)
        self._topbar_inner = inner

        self.start_btn = RoundedButton(
            inner, text="▶  Start", command=self._start,
            bg=self.theme["ok"], fg="#ffffff",
            font=(ui_font(), 10, "bold"), radius=6,
            canvas_bg=self.theme["surface"])
        self.start_btn.pack(side="left")

        self.stop_btn = RoundedButton(
            inner, text="■  Stop", command=self._stop,
            bg=self.theme["bad"], fg="#ffffff",
            font=(ui_font(), 10, "bold"), radius=6,
            canvas_bg=self.theme["surface"])

        self.status_var = tk.StringVar(value="Stopped")
        self._status_pill = StatusPill(
            inner, text_var=self.status_var,
            bg=self.theme["surface_3"],
            outline=self.theme["border_strong"],
            canvas_bg=self.theme["surface"])
        self._status_pill.pack(side="left", padx=(12, 0))
        self.status_var.trace_add("write",
                                  lambda *_: self._update_status_color())

        # Right side: Settings, Compact, Theme, Autosave, Save
        self._settings_btn = tk.Button(
            inner, text="⚙", command=self._open_settings_panel,
            font=(ui_font(), 13), relief="flat", cursor="hand2",
            background=self.theme["surface"],
            foreground=self.theme["fg_dim"],
            activebackground=self.theme["surface_2"],
            activeforeground=self.theme["fg"], width=3)
        self._settings_btn.pack(side="right")
        Tooltip(self._settings_btn, "Settings")

        self._compact_btn = tk.Button(
            inner, text="Mini", command=self._toggle_compact,
            font=(ui_font(), 9), relief="flat", cursor="hand2",
            background=self.theme["surface"],
            foreground=self.theme["fg_dim"],
            activebackground=self.theme["surface_2"],
            activeforeground=self.theme["fg"], padx=4, pady=2)
        self._compact_btn.pack(side="right", padx=(0, 2))
        Tooltip(self._compact_btn, "Compact mini view")

        self.dark_var = tk.BooleanVar(value=self.dark)
        self._theme_btn = tk.Button(
            inner, text="Dark" if self.dark else "Light",
            command=self._toggle_theme,
            font=(ui_font(), 9), relief="flat", cursor="hand2",
            background=self.theme["surface"],
            foreground=self.theme["fg_dim"],
            activebackground=self.theme["surface_2"],
            activeforeground=self.theme["fg"], padx=6, pady=2)
        self._theme_btn.pack(side="right", padx=(0, 2))
        Tooltip(self._theme_btn, "Toggle dark/light theme")

        self._hotkey_btn = tk.Button(
            inner, text="Hotkey",
            command=self._open_hotkey_dialog,
            font=(ui_font(), 9), relief="flat", cursor="hand2",
            background=self.theme["surface"],
            foreground=self.theme["fg_dim"],
            activebackground=self.theme["surface_2"],
            activeforeground=self.theme["fg"], padx=6, pady=2)
        self._hotkey_btn.pack(side="right", padx=(0, 8))
        self._hotkey_tip = Tooltip(
            self._hotkey_btn,
            f"Start / Stop hotkey: {_hotkey_display(config.HOTKEY)}. "
            f"Click to change.")

        self.keep_awake_var = tk.BooleanVar(value=config.KEEP_AWAKE)
        self._keep_awake_cb = RoundedCheckbox(
            inner, text="Keep awake", variable=self.keep_awake_var,
            command=self._on_keep_awake_toggle,
            checked_color=self.theme["accent"],
            unchecked_color=self.theme["surface_3"],
            text_color=self.theme["fg_dim"],
            canvas_bg=self.theme["surface"])
        self._keep_awake_cb.pack(side="right", padx=(0, 8))
        Tooltip(self._keep_awake_cb,
                "Prevent the PC from sleeping or dimming the display "
                "while the macro is open.")

        self.autosave_var = tk.BooleanVar(value=config.AUTOSAVE)
        self._autosave_cb = RoundedCheckbox(
            inner, text="Autosave", variable=self.autosave_var,
            command=self._on_autosave_toggle,
            checked_color=self.theme["accent"],
            unchecked_color=self.theme["surface_3"],
            text_color=self.theme["fg_dim"],
            canvas_bg=self.theme["surface"])
        self._autosave_cb.pack(side="right", padx=(0, 8))
        Tooltip(self._autosave_cb, "Auto-save settings after every change.")

        self._save_btn = tk.Button(
            inner, text="Save", command=self._save,
            font=(ui_font(), 9), relief="flat", cursor="hand2",
            background=self.theme["surface"],
            foreground=self.theme["fg_dim"],
            activebackground=self.theme["surface_2"],
            activeforeground=self.theme["fg"], padx=8, pady=2)
        self._save_btn.pack(side="right", padx=(0, 4))

        self.lock_var = tk.BooleanVar(value=False)

    # device card (compact ADB selector)
    def _build_device_card(self, parent):
        self._device_section = RoundedSection(
            parent, title="Device", bg=self.theme["surface"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg"])
        self._device_section.pack(fill="x", padx=8, pady=(6, 3))
        frame = self._device_section.inner

        # Connected indicator (right-aligned in the header area)
        self._conn_frame = tk.Frame(self._device_section,
                                    bg=self.theme["surface"],
                                    highlightthickness=0, bd=0)
        self._conn_frame.place(relx=1.0, x=-16, y=8, anchor="ne")
        self._conn_frame.lift()
        self._conn_led = tk.Label(
            self._conn_frame, text="●", font=(ui_font(), 7),
            foreground=self.theme["fg_muted"], bg=self.theme["surface"])
        self._conn_led.pack(side="left")
        self._conn_label = tk.Label(
            self._conn_frame, text="Not connected",
            font=(ui_font(), 9), foreground=self.theme["fg_muted"],
            bg=self.theme["surface"])
        self._conn_label.pack(side="left", padx=(4, 0))

        row = tk.Frame(frame, bg=self.theme["surface"],
                       highlightthickness=0)
        row.pack(fill="x", pady=(2, 4))

        self.adb_device_var = tk.StringVar(value="")
        self.adb_combo = ttk.Combobox(
            row, textvariable=self.adb_device_var, state="readonly")
        self.adb_combo.pack(side="left", fill="x", expand=True)
        self.adb_combo.bind("<<ComboboxSelected>>",
                             self._on_adb_device_selected)

        refresh_btn = ttk.Button(row, text="⟳", width=3,
                                  command=self._refresh_adb_devices)
        refresh_btn.pack(side="left", padx=(6, 0))
        Tooltip(refresh_btn, "Scan for connected ADB devices.")

        locate_btn = ttk.Button(row, text="adb…", width=5,
                                 command=self._locate_adb)
        locate_btn.pack(side="left", padx=(4, 0))
        Tooltip(locate_btn,
                "Point the macro at adb yourself. Use this if scanning says "
                "'adb not found': pick HD-Adb.exe inside your BlueStacks "
                "folder (or any adb.exe).")

        self.adb_status_label = tk.Label(
            row, text="", font=(ui_font(), 8),
            foreground=self.theme["fg_muted"], bg=self.theme["surface"])
        self.adb_status_label.pack(side="left", padx=(8, 0))

        self.root.after(200, self._refresh_adb_devices)

    # game mode card with conditional disclosure
    def _build_game_mode_card(self, parent):
        self._gm_section = RoundedSection(
            parent, title="Game mode", bg=self.theme["surface"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg"])
        self._gm_section.pack(fill="x", padx=8, pady=3)
        frame = self._gm_section.inner

        top = tk.Frame(frame, bg=self.theme["surface"],
                       highlightthickness=0)
        top.pack(fill="x", pady=(2, 4))

        self.game_mode_var = tk.StringVar(
            value=_GM_ID_TO_NAME.get(config.GAME_MODE, "Chapter"))
        combo = ttk.Combobox(top, textvariable=self.game_mode_var,
                             values=_GM_NAMES, state="readonly")
        combo.pack(fill="x")
        combo.bind("<<ComboboxSelected>>", self._on_game_mode_changed)

        # Area for mode-specific options
        self._gm_disclosure = tk.Frame(frame, bg=self.theme["surface"],
                                       highlightthickness=0)
        self._gm_disclosure.pack(fill="x", pady=(0, 2))

        # Chapter: movement sub-section with toggle switch
        self._gm_chapter_frame = tk.Frame(self._gm_disclosure,
                                          bg=self.theme["surface"],
                                          highlightthickness=0)
        self._gm_chapter_sub = RoundedSection(
            self._gm_chapter_frame, title="MOVEMENT",
            bg=self.theme["surface_2"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg_dim"], radius=6)
        self._gm_chapter_sub.pack(fill="x", pady=(4, 0))
        self.chapter_move_var = tk.StringVar(value="dontmove")
        self._chapter_toggle_bool = tk.BooleanVar(value=False)
        self._chapter_toggle = SegmentToggle(
            self._gm_chapter_sub.inner,
            variable=self._chapter_toggle_bool,
            command=self._on_chapter_toggle,
            labels=("Don't move", "Timed Chapter"),
            left_color=self.theme["accent"],
            right_color=self.theme["accent"],
            container_bg=self.theme["inset"],
            label_fg=self.theme["fg_dim"],
            outline=self.theme["border"],
            canvas_bg=self.theme["surface_2"])
        self._chapter_toggle.pack(anchor="w", pady=(4, 4))

        # Plant Defense: ONE box holds all three controls side by side (the
        # direction pad, spawn side and round limit).  Stacking them as
        # separate boxes made the panel tall enough to push the skill
        # categories off the bottom of the window.
        self._gm_plant_frame = tk.Frame(self._gm_disclosure,
                                        bg=self.theme["surface"],
                                        highlightthickness=0)
        self._gm_plant_sub = RoundedSection(
            self._gm_plant_frame, title="PLANT DEFENSE",
            bg=self.theme["surface_2"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg_dim"], radius=6)
        self._gm_plant_sub.pack(fill="x", pady=(4, 0))
        plant_row = tk.Frame(self._gm_plant_sub.inner,
                             bg=self.theme["surface_2"])
        plant_row.pack(anchor="w", fill="x", pady=(4, 4))

        def _plant_caption(parent, text):
            return tk.Label(parent, text=text,
                            foreground=self.theme["fg_dim"],
                            bg=self.theme["surface_2"],
                            font=(ui_font(), 8, "bold"))

        # Defend direction
        dir_col = tk.Frame(plant_row, bg=self.theme["surface_2"])
        dir_col.pack(side="left", anchor="n")
        _plant_caption(dir_col, "DEFEND DIRECTION").pack(anchor="w")
        self.plant_dir_var = tk.StringVar(value=config.MOVEMENT_PLANT_PRESET)
        self._plant_dpad = DirectionPad(
            dir_col, variable=self.plant_dir_var,
            command=self._on_plant_dir,
            bg=self.theme["surface_2"],
            selected_color=self.theme["accent"],
            unselected_color=self.theme["surface_3"],
            fg=self.theme["fg_dim"])
        self._plant_dpad.pack(anchor="w", pady=(2, 0))

        # Spawn side: the game seats the two co-op players by username
        # alphabetical order, which the macro cannot read, so the user picks
        # their side here (constant for a session with the same partner).
        spawn_col = tk.Frame(plant_row, bg=self.theme["surface_2"])
        spawn_col.pack(side="left", anchor="n", padx=(16, 0))
        _plant_caption(spawn_col, "SPAWN SIDE").pack(anchor="w")
        self.plant_spawn_bool = tk.BooleanVar(value=config.PLANT_SPAWN == 2)
        self._plant_spawn_toggle = SegmentToggle(
            spawn_col,
            variable=self.plant_spawn_bool,
            command=self._on_plant_spawn,
            labels=("Left", "Right"),
            left_color=self.theme["accent"],
            right_color=self.theme["accent"],
            container_bg=self.theme["inset"],
            label_fg=self.theme["fg_dim"],
            outline=self.theme["border"],
            canvas_bg=self.theme["surface_2"])
        self._plant_spawn_toggle.pack(anchor="w", pady=(4, 0))
        Tooltip(self._plant_spawn_toggle,
                "Which side YOUR character spawns on. The game decides this "
                "by the alphabetical order of the two usernames (not by who "
                "hosts), so it stays the same all session with the same "
                "partner. If unsure, watch where you spawn in the first "
                "round and set it here.")

        # Round limit: play this many full rounds of the level, then stop.
        rounds_col = tk.Frame(plant_row, bg=self.theme["surface_2"])
        rounds_col.pack(side="left", anchor="n", padx=(16, 0))
        _plant_caption(rounds_col, "ROUNDS").pack(anchor="w")
        rounds_row = tk.Frame(rounds_col, bg=self.theme["surface_2"])
        rounds_row.pack(anchor="w", pady=(4, 0))
        self.plant_rounds_var = tk.StringVar(value=str(config.PLANT_ROUNDS))
        rounds_entry = ttk.Entry(rounds_row, width=5,
                                 textvariable=self.plant_rounds_var)
        rounds_entry.pack(side="left")
        tk.Label(rounds_row, text="0 = unlimited",
                 foreground=self.theme["fg_muted"], bg=self.theme["surface_2"],
                 font=(ui_font(), 9)).pack(side="left", padx=(6, 0))
        Tooltip(rounds_entry,
                "Stop the macro after this many completed rounds of the "
                "level. 0 keeps it running until stopped or the run "
                "timeout hits.")
        self.plant_rounds_var.trace_add(
            "write", lambda *_a: self._schedule_autosave())

        # Eternal Lode sub-frame
        self._gm_eternal_frame = tk.Frame(
            self._gm_disclosure, bg=self.theme["surface"])
        self._el_fast_var = tk.BooleanVar(value=config.EL_FAST_MODE)
        self._el_fast_cb = ttk.Checkbutton(
            self._gm_eternal_frame, text="Fast Mode",
            variable=self._el_fast_var,
            command=self._on_el_fast_toggle)
        self._el_fast_cb.pack(anchor="w", pady=(4, 2))
        self._gm_jungle_hint = tk.Label(
            self._gm_disclosure, foreground=self.theme["fg_muted"],
            bg=self.theme["surface"], font=(ui_font(), 9),
            text="Movement is disabled automatically for Shackled Jungle.")

        # All-Star Cup: level picker (1-7 or Elim).  Levels scan the 3 boss
        # cards and pick the fastest-dying visible boss; Elim runs 10 levels
        # against the user's per-level boss picks.
        self._gm_allstar_frame = tk.Frame(self._gm_disclosure,
                                           bg=self.theme["surface"],
                                           highlightthickness=0)
        tk.Label(self._gm_allstar_frame, text="Level",
                 foreground=self.theme["fg_dim"], bg=self.theme["surface"],
                 font=(ui_font(), 9)).pack(anchor="w", pady=(4, 0))
        self.allstar_level_var = tk.StringVar(
            value=self._allstar_level_label())
        allstar_combo = ttk.Combobox(
            self._gm_allstar_frame, textvariable=self.allstar_level_var,
            values=[str(i) for i in range(1, 8)] + ["Elim"],
            state="readonly", width=6)
        allstar_combo.pack(anchor="w", pady=(0, 2))
        allstar_combo.bind("<<ComboboxSelected>>", self._on_allstar_level)

        # Elim sub-options: per-level boss picks
        self._gm_elim_frame = tk.Frame(self._gm_allstar_frame,
                                       bg=self.theme["surface"],
                                       highlightthickness=0)
        self._elim_summary = tk.Label(
            self._gm_elim_frame, foreground=self.theme["fg_dim"],
            bg=self.theme["surface"], font=(ui_font(), 9))
        self._elim_summary.pack(anchor="w", pady=(2, 0))
        ttk.Button(self._gm_elim_frame, text="Choose bosses…",
                   command=self._open_elim_picker).pack(
                       anchor="w", pady=(2, 2))

        self._gm_allstar_hint = tk.Label(
            self._gm_allstar_frame, foreground=self.theme["fg_muted"],
            bg=self.theme["surface"], font=(ui_font(), 9), justify="left",
            wraplength=240)
        self._gm_allstar_hint.pack(anchor="w", pady=(0, 2))
        self._update_allstar_sub()

        self._update_game_mode_disclosure()

    def _on_chapter_toggle(self):
        on = self._chapter_toggle_bool.get()
        self.chapter_move_var.set("timed" if on else "dontmove")
        self._on_chapter_move()

    def _game_mode_id(self):
        """Current game mode as a config ID string."""
        name = self.game_mode_var.get()
        return _GM_NAME_TO_ID.get(name, "chapter")

    def _on_game_mode_changed(self, _event=None):
        self._update_game_mode_disclosure()
        self._schedule_autosave()
        gm = self._game_mode_id()
        main.log(f"game mode: {_GM_ID_TO_NAME.get(gm, gm)}")
        # One-time heads-up: All-Star mode depends on fast input via a
        # writable /dev/input node, which only a rooted BlueStacks instance
        # provides.  Shown once, then remembered in settings.
        if gm == "allstar" and not config.ALL_STAR_ROOT_WARNED:
            config.ALL_STAR_ROOT_WARNED = True
            messagebox.showwarning(
                "All-Star Cup",
                "All-Star Cup mode ONLY works on a rooted BlueStacks "
                "instance.\n\nIt will not work on a regular phone or an "
                "unrooted emulator. (This warning is shown once.)",
                parent=self.root)

    def _update_game_mode_disclosure(self):
        """Show/hide the mode-specific sub-options."""
        for w in (self._gm_chapter_frame, self._gm_plant_frame,
                  self._gm_eternal_frame, self._gm_jungle_hint,
                  self._gm_allstar_frame):
            w.pack_forget()
        gm = self._game_mode_id()
        if gm == "chapter":
            self._gm_chapter_frame.pack(fill="x")
            self._chapter_toggle_bool.set(
                self.chapter_move_var.get() == "timed")
        elif gm == "plant":
            self._gm_plant_frame.pack(fill="x")
        elif gm == "eternal":
            self._gm_eternal_frame.pack(fill="x")
        elif gm == "jungle":
            self._gm_jungle_hint.pack(anchor="w", pady=4)
        elif gm == "allstar":
            self._gm_allstar_frame.pack(fill="x")
            self._update_allstar_sub()

    def _on_chapter_move(self):
        self._schedule_autosave()

    def _on_el_fast_toggle(self):
        config.EL_FAST_MODE = self._el_fast_var.get()
        self._schedule_autosave()

    @staticmethod
    def _normalized_elim_picks():
        """config.ALL_STAR_ELIM_BOSSES padded/trimmed to exactly one entry per
        Elim level ("" = no pick)."""
        n = int(config.ALL_STAR_ELIM_SETS)
        picks = [str(b) for b in getattr(config, "ALL_STAR_ELIM_BOSSES", [])]
        return (picks + [""] * n)[:n]

    @staticmethod
    def _allstar_level_label():
        lvl = getattr(config, "ALL_STAR_LEVEL", 1)
        return "Elim" if str(lvl).strip().lower() == "elim" else str(lvl)

    def _allstar_is_elim(self):
        return self.allstar_level_var.get().strip().lower() == "elim"

    def _on_allstar_level(self, _event=None):
        val = self.allstar_level_var.get()
        if val.strip().lower() == "elim":
            config.ALL_STAR_LEVEL = "elim"
        else:
            try:
                config.ALL_STAR_LEVEL = int(val)
            except ValueError:
                return
        self._update_allstar_sub()
        self._schedule_autosave()

    def _update_allstar_sub(self):
        """Show/hide the Elim boss-pick row and swap the hint text to match
        the selected All-Star level."""
        if self._allstar_is_elim():
            self._render_elim_summary()
            self._gm_elim_frame.pack(fill="x", before=self._gm_allstar_hint)
            self._gm_allstar_hint.configure(
                text="Position the game on the Elim entry screen, then "
                     "Start. The macro picks your chosen boss at each of "
                     f"the {config.ALL_STAR_ELIM_SETS} levels.")
        else:
            self._gm_elim_frame.pack_forget()
            self._gm_allstar_hint.configure(
                text="Position the game on the level's Challenge screen, "
                     "then Start. The macro identifies the 3 bosses each "
                     "set and picks the fastest-dying one, then stops.")

    def _render_elim_summary(self):
        chosen = sum(1 for b in self.elim_bosses if b)
        self._elim_summary.configure(
            text=f"Bosses chosen: {chosen}/{len(self.elim_bosses)}"
                 + ("" if chosen == len(self.elim_bosses)
                    else "  (unset = best visible)"))

    # ------------------------------------------------------------------ Elim boss picker

    def _boss_thumb(self, name, size):
        """Square thumbnail of a boss template, letterboxed on black (the
        templates are black-bg crops of varying aspect)."""
        key = (name, size)
        if key in self._boss_thumb_cache:
            return self._boss_thumb_cache[key]
        photo = None
        path = config.ALL_STAR_BOSS_DIR / f"{name}.png"
        if path.exists():
            try:
                img = Image.open(path).convert("RGB")
                img.thumbnail((size, size), _RESAMPLE)
                square = Image.new("RGB", (size, size), "#000000")
                square.paste(img, ((size - img.width) // 2,
                                   (size - img.height) // 2))
                photo = ImageTk.PhotoImage(square)
            except Exception:
                photo = None
        self._boss_thumb_cache[key] = photo
        return photo

    def _open_elim_picker(self):
        if self._elim_picker is not None:
            try:
                if self._elim_picker.winfo_exists():
                    self._elim_picker.lift()
                    return
            except tk.TclError:
                pass
            self._elim_picker = None

        p = self.theme
        win = tk.Toplevel(self.root)
        self._elim_picker = win
        win.title("Elim boss picks")
        win.geometry("900x680")
        win.transient(self.root)
        win.configure(background=p["bg"])
        set_dark_titlebar(win, self.dark)

        head = tk.Frame(win, bg=p["bg"], highlightthickness=0)
        head.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(head, text="Elim boss picks", font=(ui_font(), 12, "bold"),
                 fg=p["fg"], bg=p["bg"]).pack(side="left")
        tk.Label(head,
                 text="Click a boss to assign it to the highlighted level. "
                      "Click a level to select it; right-click clears it.",
                 fg=p["fg_muted"], bg=p["bg"],
                 font=(ui_font(), 9)).pack(side="left", padx=(14, 0))

        # Level slot strip: stays visible above the scrolling boss grid.
        self._elim_slot_strip = tk.Frame(win, bg=p["bg"],
                                         highlightthickness=0)
        self._elim_slot_strip.pack(fill="x", padx=12, pady=(4, 6))
        self._elim_sel = next(
            (i for i, b in enumerate(self.elim_bosses) if not b), 0)
        self._render_elim_slots()

        # Scrollable boss grid
        canvas = tk.Canvas(win, borderwidth=0, highlightthickness=0,
                           background=p["bg"])
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        footer = tk.Frame(win, bg=p["bg"], highlightthickness=0)
        footer.pack(side="bottom", fill="x", padx=12, pady=(6, 10))
        self._elim_counts = tk.Label(footer, text="", fg=p["fg_muted"],
                                     bg=p["bg"], font=(ui_font(), 9))
        self._elim_counts.pack(side="left")
        RoundedButton(footer, text="Done", command=win.destroy,
                      bg=p["accent"], fg="#ffffff",
                      font=(ui_font(), 9, "bold"), radius=6,
                      canvas_bg=p["bg"]).pack(side="right")
        ttk.Button(footer, text="Clear all",
                   command=self._elim_clear_all).pack(
                       side="right", padx=(0, 8))

        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=p["bg"], highlightthickness=0)
        cwin = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(cwin, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<Enter>",
                    lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self._elim_body = body
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._render_elim_body()
        self._update_elim_footer()

    def _render_elim_slots(self):
        strip = getattr(self, "_elim_slot_strip", None)
        if strip is None:
            return
        try:
            if not strip.winfo_exists():
                return
        except tk.TclError:
            return
        for child in strip.winfo_children():
            child.destroy()
        p = self.theme
        size = 48
        for i, name in enumerate(self.elim_bosses):
            unit = tk.Frame(strip, bg=p["bg"], highlightthickness=0)
            unit.grid(row=0, column=i, padx=4)
            sel = (i == self._elim_sel)
            cell = tk.Canvas(unit, width=size + 6, height=size + 6,
                             highlightthickness=0, bg=p["bg"],
                             cursor="hand2", bd=0)
            draw_rounded_rect(cell, 1, 1, size + 4, size + 4, 6,
                              fill="#000000" if name else p["surface_3"],
                              outline=p["accent"] if sel
                              else p["border_strong"],
                              width=2 if sel else 1)
            photo = self._boss_thumb(name, size - 6) if name else None
            if photo is not None:
                cell.create_image((size + 6) // 2, (size + 6) // 2,
                                  image=photo)
                cell._photo = photo         # keep the PhotoImage alive
            else:
                cell.create_text((size + 6) // 2, (size + 6) // 2,
                                 text="?" if name else "+",
                                 fill=p["fg_muted"], font=(ui_font(), 16))
            cell.pack()
            lbl = tk.Label(unit, text=f"L{i + 1}",
                           fg=p["accent"] if sel else p["fg_dim"],
                           bg=p["bg"],
                           font=(ui_font(), 8, "bold" if sel else "normal"))
            lbl.pack()
            for w in (cell, lbl):
                w.bind("<Button-1>",
                       lambda e, idx=i: self._elim_select_slot(idx))
                w.bind("<Button-3>",
                       lambda e, idx=i: self._elim_clear_slot(idx))

    def _elim_select_slot(self, idx):
        self._elim_sel = idx
        self._render_elim_slots()

    def _elim_clear_slot(self, idx):
        self.elim_bosses[idx] = ""
        self._elim_sel = idx
        self._elim_changed()

    def _elim_assign(self, name):
        self.elim_bosses[self._elim_sel] = name
        self._elim_sel = min(self._elim_sel + 1, len(self.elim_bosses) - 1)
        self._elim_changed()

    def _elim_clear_all(self):
        self.elim_bosses = [""] * len(self.elim_bosses)
        self._elim_sel = 0
        self._elim_changed()

    def _elim_changed(self):
        self._render_elim_slots()
        self._render_elim_summary()
        self._update_elim_footer()
        self._schedule_autosave()

    def _update_elim_footer(self):
        lbl = getattr(self, "_elim_counts", None)
        if lbl is None:
            return
        try:
            chosen = sum(1 for b in self.elim_bosses if b)
            lbl.configure(text=f"● {chosen}/{len(self.elim_bosses)} levels "
                               "assigned")
        except tk.TclError:
            pass

    def _render_elim_body(self):
        """One flat grid of every boss, fastest death anim first.  Elim
        scatters bosses across its levels randomly per event, so the All-Star
        level groupings mean nothing here."""
        body = self._elim_body
        p = self.theme
        anim = {n: s for lp in config.ALL_STAR_LEVELS.values()
                for n, s in lp.items()}
        grid = tk.Frame(body, bg=p["bg"], highlightthickness=0)
        grid.pack(anchor="w", padx=8, pady=(8, 4))
        cols = 9
        for j, name in enumerate(sorted(anim, key=anim.__getitem__)):
            tile = self._elim_boss_tile(grid, name, anim[name])
            tile.grid(row=j // cols, column=j % cols, padx=3, pady=3,
                      sticky="n")

    def _elim_boss_tile(self, parent, name, secs):
        p = self.theme
        tile = tk.Frame(parent, bg=p["bg"], highlightthickness=0,
                        cursor="hand2")
        cell = tk.Canvas(tile, width=62, height=62, highlightthickness=0,
                         bg=p["bg"], bd=0)
        draw_rounded_rect(cell, 1, 1, 60, 60, 6, fill="#000000",
                          outline=p["border_strong"])
        photo = self._boss_thumb(name, 52)
        if photo is not None:
            cell.create_image(31, 31, image=photo)
            cell._photo = photo
        cell.pack()
        tk.Label(tile, text=name, fg=p["fg_dim"], bg=p["bg"],
                 font=(ui_font(), 7), wraplength=84,
                 justify="center").pack()
        tk.Label(tile, text=f"{secs:.2f}s", fg=p["fg"], bg=p["bg"],
                 font=(ui_font(), 8, "bold")).pack()
        handler = lambda e, n=name: self._elim_assign(n)
        tile.bind("<Button-1>", handler)
        for w in tile.winfo_children():
            w.bind("<Button-1>", handler)
        return tile

    def _on_plant_dir(self):
        self._schedule_autosave()

    def _on_plant_spawn(self):
        self._schedule_autosave()

    # timeout card (stays on main view)
    def _build_timeout_card(self, parent):
        self._timeout_section = RoundedSection(
            parent, title="Run timeout",
            bg=self.theme["surface"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg"])
        self._timeout_section.pack(fill="x", padx=8, pady=3)
        frame = self._timeout_section.inner

        row = tk.Frame(frame, bg=self.theme["surface"],
                       highlightthickness=0)
        row.pack(fill="x", pady=(2, 4))
        tk.Label(row, text="Stop after", bg=self.theme["surface"],
                 fg=self.theme["fg_dim"],
                 font=(ui_font(), 9)).pack(side="left")
        ttk.Entry(row, textvariable=self.vars["RUN_TIMEOUT_HOURS"][0],
                  width=6).pack(side="left", padx=(6, 4))
        tk.Label(row, text="hrs", bg=self.theme["surface"],
                 fg=self.theme["fg_muted"],
                 font=(ui_font(), 9)).pack(side="left")
        Tooltip(row, "Stop the macro automatically after this long. "
                     "Set to 0 to disable.")

        row_stuck = tk.Frame(frame, bg=self.theme["surface"],
                             highlightthickness=0)
        row_stuck.pack(fill="x", pady=(2, 4))
        tk.Label(row_stuck, text="Stuck after", bg=self.theme["surface"],
                 fg=self.theme["fg_dim"],
                 font=(ui_font(), 9)).pack(side="left")
        ttk.Entry(row_stuck, textvariable=self.vars["STUCK_TIMEOUT_MINUTES"][0],
                  width=6).pack(side="left", padx=(6, 4))
        tk.Label(row_stuck, text="min", bg=self.theme["surface"],
                 fg=self.theme["fg_muted"],
                 font=(ui_font(), 9)).pack(side="left")
        Tooltip(row_stuck, "Stop the macro if it keeps repeating the same "
                           "action for this long. Set to 0 to disable.")

        self._timeout_cb1 = RoundedCheckbox(
            frame, text="Close game window on timeout",
            variable=self.vars["CLOSE_ON_TIMEOUT"],
            checked_color=self.theme["accent"],
            unchecked_color=self.theme["surface_3"],
            text_color=self.theme["fg_dim"],
            canvas_bg=self.theme["surface"])
        self._timeout_cb1.pack(anchor="w", pady=2)
        Tooltip(self._timeout_cb1,
                "Close the emulator/scrcpy window when the timeout fires.")

        self._timeout_cb2 = RoundedCheckbox(
            frame, text="Sleep phone on timeout",
            variable=self.vars["SLEEP_PHONE_ON_TIMEOUT"],
            checked_color=self.theme["accent"],
            unchecked_color=self.theme["surface_3"],
            text_color=self.theme["fg_dim"],
            canvas_bg=self.theme["surface"])
        self._timeout_cb2.pack(anchor="w", pady=(2, 4))
        Tooltip(self._timeout_cb2,
                "Turn the phone screen off via ADB when the timeout fires.")

    def _locate_adb(self):
        """Let the user browse to an adb binary by hand (the rescue path when
        find_adb() cannot locate one automatically). Saves ADB_PATH to
        settings.json immediately, then rescans for devices."""
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Pick adb.exe or HD-Adb.exe",
            initialdir=r"C:\Program Files",
            filetypes=[("adb executable", "adb.exe HD-Adb.exe"),
                       ("Programs", "*.exe")])
        if not path:
            return
        name = Path(path).name.lower()
        if "adb" not in name:
            messagebox.showwarning(
                "Locate adb",
                f"{Path(path).name} does not look like adb.\n\n"
                "Pick HD-Adb.exe (inside your BlueStacks folder) or adb.exe.",
                parent=self.root)
            return
        config.ADB_PATH = path
        config.save_settings()
        main.log(f"adb path set manually: {path}")
        self._refresh_adb_devices()

    def _refresh_adb_devices(self):
        """Scan for ADB devices and update the dropdown."""
        self._adb_label_to_serial = {}
        try:
            adb_exe = find_adb(config.ADB_PATH)
            # Pick the right adb server port (isolated to dodge the BlueStacks
            # version war, or shared if something else owns the device) BEFORE
            # listing, so the dropdown and the pre-warmed stream use that server.
            choose_server_port(adb_exe)
            devices = list_devices(adb_exe)
        except Exception as e:
            self._update_adb_status(f"adb not found: {e}", ok=False)
            try:
                self.adb_combo["values"] = []
            except tk.TclError:
                pass
            return

        for d in devices:
            self._adb_label_to_serial[d["label"]] = d["serial"]
        labels = [d["label"] for d in devices]
        try:
            self.adb_combo["values"] = labels
        except tk.TclError:
            return

        # Restore the previously configured device if it is still present;
        # otherwise auto-select a lone device (or clear). Crucially, keep
        # config.ADB_DEVICE in lockstep with what the dropdown shows: setting the
        # StringVar does NOT fire the combobox-selected handler, so without this
        # an auto-selected device leaves config.ADB_DEVICE pointing at a stale,
        # now-absent serial -- and the macro then fails to connect ("Device ...
        # not found") even though Test Connection (which reads the dropdown) works.
        configured = config.ADB_DEVICE
        chosen_serial = None  # None = leave config untouched (user must pick)
        for label, serial in self._adb_label_to_serial.items():
            if serial == configured:
                self.adb_device_var.set(label)
                chosen_serial = serial
                break
        else:
            if len(labels) == 1:
                self.adb_device_var.set(labels[0])
                chosen_serial = self._adb_label_to_serial.get(labels[0], "")
            elif not labels:
                # No devices right now: blank the dropdown but KEEP the saved
                # serial, so a transient unplug/refresh doesn't forget the device
                # (it re-matches above when it reconnects).
                self.adb_device_var.set("")
            # multiple devices, none configured: leave the selection to the user;
            # _on_adb_device_selected updates config when they pick.
        if chosen_serial is not None and chosen_serial != config.ADB_DEVICE:
            config.ADB_DEVICE = chosen_serial
            self._schedule_autosave()

        count = len(devices)
        self._update_adb_status(
            f"{count} device(s) found" if count else "No devices found",
            ok=count > 0)
        # Warm the stream now so the first Test Connection / ref capture is
        # instant instead of paying the screenrecord cold start.
        self._prewarm_capture_stream()

    def _update_adb_status(self, text, ok=None):
        """Update the ADB status and connected indicator."""
        # Update device-count / status text (always gray, beside refresh btn)
        if getattr(self, "adb_status_label", None):
            try:
                self.adb_status_label.configure(
                    text=text, foreground=self.theme["fg_muted"])
            except tk.TclError:
                pass
        # Update the connected indicator in the header area
        if hasattr(self, "_conn_led") and hasattr(self, "_conn_label"):
            if ok is True:
                conn_color = self.theme["ok"]
                conn_text = "Connected"
            elif ok is False:
                conn_color = self.theme["bad"]
                conn_text = "Disconnected"
            else:
                conn_color = self.theme["fg_muted"]
                conn_text = "Not connected"
            try:
                self._conn_led.configure(foreground=conn_color)
                self._conn_label.configure(foreground=conn_color,
                                           text=conn_text)
            except tk.TclError:
                pass

    def _on_adb_device_selected(self, _event=None):
        """Persist the chosen device serial whenever the combo changes."""
        label = self.adb_device_var.get()
        serial = self._adb_label_to_serial.get(label, label)
        config.ADB_DEVICE = serial
        self._update_adb_status(f"Selected: {serial}", ok=None)
        self._schedule_autosave()
        self._prewarm_capture_stream()

    def _make_adb_client(self):
        """Return a connected ADBClient for the current device, or None."""
        label = self.adb_device_var.get()
        serial = self._adb_label_to_serial.get(label, label)
        if not serial:
            main.log("no ADB device selected -- use the ADB panel to pick one")
            return None
        try:
            adb_exe = find_adb(config.ADB_PATH)
        except RuntimeError as e:
            main.log(f"ADB binary not found: {e}")
            return None
        adb = ADBClient(adb_exe=adb_exe)
        try:
            adb.connect(serial)
        except ADBError as e:
            main.log(f"ADB connect failed: {e}")
            return None
        return adb

    # ------------------------------------------------------------------
    # Capture frames for GUI flows (Test connection, ref/custom capture).
    # Use a screenrecord stream so BlueStacks does not return black frames
    # (screencap is black when nothing consumes the display). The stream is
    # started lazily, kept alive for fast back-to-back captures, and stopped
    # when the macro starts (it makes its own) or the app closes.
    # ------------------------------------------------------------------

    def _ensure_capture_stream(self, adb):
        """Start (or reuse) the GUI capture stream for adb's device, retrying the
        screenrecord launch a few times. Returns a ready stream, or None if
        streaming is unavailable or no frame could be produced.

        The blocking establish (capture.open_stream) runs OUTSIDE the lock so a
        cold start / retry never freezes the UI thread when it stops the stream
        to hand off to the macro. Callers run this off the UI thread."""
        if not capture.streaming_available():
            return None
        if self._running:                       # macro owns the device's stream
            return None
        serial = adb.device
        with self._capture_stream_lock:
            if (self._capture_stream is not None
                    and self._capture_stream_serial == serial):
                return self._capture_stream

        s = capture.open_stream(adb.adb_exe, serial, attempts=3,
                                per_attempt_timeout=4.0, on_log=main.log)

        with self._capture_stream_lock:
            # Another thread may have built one meanwhile, or the macro may have
            # started -- in either case don't cache a competing second stream.
            if (self._capture_stream is not None
                    and self._capture_stream_serial == serial):
                if s is not None and s is not self._capture_stream:
                    s.stop()
                return self._capture_stream
            if self._running:
                if s is not None:
                    s.stop()
                return None
            self._stop_capture_stream_locked()
            if s is not None:
                self._capture_stream = s
                self._capture_stream_serial = serial
            return s

    def _stop_capture_stream(self):
        with self._capture_stream_lock:
            self._stop_capture_stream_locked()

    def _stop_capture_stream_locked(self):
        if self._capture_stream is not None:
            self._capture_stream.stop()
            self._capture_stream = None
            self._capture_stream_serial = None

    def _prewarm_capture_stream(self):
        """Start the capture stream in the background as soon as a device is
        known, so Test Connection / ref capture get an instant warm frame
        instead of paying the ~1-2s screenrecord cold start each time."""
        if not config.USE_STREAM_CAPTURE or not capture.streaming_available():
            return
        if self._running:
            return
        label = self.adb_device_var.get()
        serial = self._adb_label_to_serial.get(label, label)
        if not serial:
            return

        def worker():
            try:
                adb_exe = find_adb(config.ADB_PATH)
            except RuntimeError:
                return
            adb = ADBClient(adb_exe=adb_exe, device=serial)
            try:
                self._ensure_capture_stream(adb)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _capture_frame(self, adb):
        """Grab one frame for a GUI flow (Test connection, ref/custom capture).

        In streaming mode (the default) use the pre-warmed screenrecord stream,
        retrying to (re)establish it if needed. We surface a clear error rather
        than silently degrading to a slow / BlueStacks-black screencap -- that
        was the confusing 'why is this taking 2-3 seconds' path. Only when
        streaming is genuinely unavailable (PyAV missing) does a one-shot GUI
        screencap make sense as a last resort."""
        if config.USE_STREAM_CAPTURE and capture.streaming_available():
            stream = self._ensure_capture_stream(adb)
            if stream is not None and stream.wait_ready(timeout=6.0):
                frame = stream.latest(max_age=3.0)
                if frame is not None:
                    return frame
            raise RuntimeError(
                "screenrecord stream produced no frame. Check the device "
                "screen is on and the USB/ADB connection is stable.")
        return adb.screenshot(black_retries=2)

    def _test_adb_connection(self):
        """Take an ADB screenshot and show it in a preview window."""
        if self._coords_busy:
            return
        adb = self._make_adb_client()
        if adb is None:
            return
        self._coords_busy = True
        self._set_coord_buttons("disabled")
        self._update_adb_status("Capturing screenshot…", ok=None)

        def worker():
            screenshot = None
            try:
                screenshot = self._capture_frame(adb)
                h, w = screenshot.shape[:2]
                device = config.ADB_DEVICE or "device"
                main.log(f"ADB test: {w}x{h} screenshot from {device}")
                self._update_adb_status(f"Connected: {device}", ok=True)
            except Exception as e:
                main.log(f"ADB test failed: {e}")
                self._update_adb_status(f"Test failed: {e}", ok=False)

            def done():
                self._coords_busy = False
                self._set_coord_buttons("normal")
                if screenshot is not None:
                    self._show_adb_screenshot(screenshot)
            try:
                self.root.after(0, done)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _show_adb_screenshot(self, screenshot_bgr):
        """Display an ADB screenshot in a read-only preview window."""
        h_px, w_px = screenshot_bgr.shape[:2]
        max_w, max_h = 800, 600
        scale = min(max_w / w_px, max_h / h_px, 1.0)
        disp_w = max(1, int(w_px * scale))
        disp_h = max(1, int(h_px * scale))

        rgb = screenshot_bgr[:, :, ::-1]
        pil = Image.fromarray(rgb.copy()).resize((disp_w, disp_h), _RESAMPLE)
        photo = ImageTk.PhotoImage(pil)

        win = tk.Toplevel(self.root)
        win.title(f"ADB Screenshot  ({w_px} x {h_px})")
        win.transient(self.root)
        win.resizable(False, False)

        canvas = tk.Canvas(win, width=disp_w, height=disp_h,
                           highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas._photo_ref = photo   # prevent GC

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=6)

    def _run_setup_wizard(self):
        """First-time ADB setup dialog."""
        win = tk.Toplevel(self.root)
        win.title("ADB Setup Wizard")
        win.geometry("500x420")
        win.transient(self.root)
        win.grab_set()
        win.configure(background=self.theme["bg"])
        win.resizable(False, False)

        ttk.Label(win, text="ADB Setup Wizard",
                  font=("", 12, "bold")).pack(fill="x", padx=16, pady=(16, 4))
        ttk.Label(
            win, wraplength=466, justify="left",
            text="Connects an Android device or emulator via ADB, queries "
                 "its screen resolution, and sets sensible click-target "
                 "defaults. Run this once when first setting up or when "
                 "changing devices."
        ).pack(fill="x", padx=16, pady=(0, 8))
        ttk.Separator(win).pack(fill="x", padx=16, pady=4)

        # Device selector row
        dev_frame = ttk.Frame(win)
        dev_frame.pack(fill="x", padx=16, pady=6)
        ttk.Label(dev_frame, text="Device:").pack(side="left")
        dev_var = tk.StringVar()
        combo = ttk.Combobox(dev_frame, textvariable=dev_var, width=34)
        combo.pack(side="left", padx=(6, 0))

        label_to_serial: dict = {}

        def do_refresh_wiz():
            nonlocal label_to_serial
            try:
                exe = find_adb(config.ADB_PATH)
                devs = list_devices(exe)
                label_to_serial = {d["label"]: d["serial"] for d in devs}
                combo["values"] = list(label_to_serial)
                for lbl, ser in label_to_serial.items():
                    if ser == config.ADB_DEVICE:
                        dev_var.set(lbl)
                        return
                if len(devs) == 1:
                    dev_var.set(list(label_to_serial)[0])
            except Exception as e:
                messagebox.showerror("Refresh",
                                     f"Could not list devices:\n{e}",
                                     parent=win)

        do_refresh_wiz()
        ttk.Button(dev_frame, text="Refresh", width=8,
                   command=do_refresh_wiz).pack(side="left", padx=(6, 0))

        ttk.Separator(win).pack(fill="x", padx=16, pady=4)

        status_var = tk.StringVar(value="")
        ttk.Label(win, textvariable=status_var, wraplength=466,
                  justify="left").pack(fill="x", padx=16, pady=(4, 0))
        res_var = tk.StringVar(value="")
        ttk.Label(win, textvariable=res_var,
                  foreground="#888").pack(fill="x", padx=16)

        ttk.Separator(win).pack(fill="x", padx=16, pady=4)

        _result: dict = {"ok": False, "w": 0, "h": 0, "serial": ""}

        def do_connect():
            lbl = dev_var.get()
            serial = label_to_serial.get(lbl, lbl)
            if not serial:
                messagebox.showerror(
                    "Connect", "Select a device first.", parent=win)
                return
            connect_btn.config(state="disabled")
            finish_btn.config(state="disabled")
            status_var.set("Connecting…")
            res_var.set("")
            win.update_idletasks()

            def worker():
                try:
                    exe = find_adb(config.ADB_PATH)
                    adb = ADBClient(adb_exe=exe)
                    adb.connect(serial)
                    w, h = adb.resolution()
                    _result.update(ok=True, w=w, h=h, serial=serial)

                    def done():
                        status_var.set("Connected successfully.")
                        res_var.set(
                            f"Resolution: {w} x {h}  |  "
                            f"CALIBRATED_SCALE: {config.CALIBRATED_SCALE} "
                            f"(current, adjust in Settings if needed)")
                        finish_btn.config(state="normal")
                        connect_btn.config(state="normal")
                except Exception as e:
                    err = str(e)   # bind now; `e` is cleared after the except
                    def done():  # noqa: F811
                        status_var.set(f"Connection failed: {err}")
                        connect_btn.config(state="normal")
                try:
                    win.after(0, done)
                except tk.TclError:
                    pass

            threading.Thread(target=worker, daemon=True).start()

        connect_frame = ttk.Frame(win)
        connect_frame.pack(fill="x", padx=16, pady=6)
        connect_btn = ttk.Button(
            connect_frame, text="Connect & Detect Resolution",
            command=do_connect)
        connect_btn.pack(side="left")

        btn_frame = ttk.Frame(win)
        btn_frame.pack(side="bottom", fill="x", padx=16, pady=12)
        ttk.Button(btn_frame, text="Cancel",
                   command=win.destroy).pack(side="right")
        finish_btn = ttk.Button(btn_frame, text="Apply & Close",
                                 state="disabled")
        finish_btn.pack(side="right", padx=(0, 6))

        def do_finish():
            if not _result["ok"]:
                win.destroy()
                return
            w, h = _result["w"], _result["h"]
            serial = _result["serial"]
            config.ADB_DEVICE = serial
            config.PHONE_RESOLUTION = [w, h]
            # Derive the skill band / slot / game-over coords from this device's
            # real resolution via the shared resolver (single source of truth,
            # same one the macro re-applies from the live screenshot at start).
            config.resolve_geometry(w, h)
            config.save_settings()
            self._refresh_adb_devices()
            self._update_adb_status(f"Connected: {serial}", ok=True)
            main.log(f"ADB setup complete -- device={serial}, res={w}x{h}")
            main.log(f"  FIRST_SKILL_SLOT={config.FIRST_SKILL_SLOT}")
            main.log(f"  SECOND_SKILL_SLOT={config.SECOND_SKILL_SLOT}")
            main.log(f"  GAME_OVER_TAP={config.GAME_OVER_TAP}")
            main.log(f"  SKILL_MATCH_BAND={config.SKILL_MATCH_BAND}")
            main.log("  Adjust CALIBRATED_SCALE in Settings to tune "
                     "template matching if icons are not detected.")
            win.destroy()

        finish_btn.configure(command=do_finish)

    # pre-create settings variables
    def _create_settings_vars(self):
        """Build all tk variables used by settings fields so they exist before
        any widget that reads them (timeout card, settings panel, etc.)."""
        for entry in SETTINGS_SCHEMA:
            if entry[0] in ("section", "button"):
                continue
            key, _label, kind, _unit, _hint = entry[:5]
            if kind == "bool":
                self.vars[key] = tk.BooleanVar()
            else:
                self.vars[key] = [tk.StringVar()
                                  for _ in range(_KIND_WIDTHS[kind])]
        # Movement vector vars (4 floats each: angle1, dur1, angle2, dur2)
        for key in ("MOVEMENT_CHAPTER", "MOVEMENT_CUSTOM"):
            self.vars[key] = [tk.StringVar() for _ in range(4)]
        # Plant Defense movement vars.  The direction preset itself lives in
        # plant_dir_var (the D-pad in the game-mode card).
        self.movement_plant_t_var = tk.StringVar(
            value=str(config.MOVEMENT_PLANT_T))
        self.movement_plant_spawn_var = tk.IntVar(value=1)

    # theme
    def _apply_theme(self):
        """Apply the current palette to every widget and re-render the
        colour-baked strips. The single place colours are set."""
        p = self.theme
        _f = ui_font()
        s = self.style
        s.theme_use("clam")
        s.configure(".", background=p["bg"], foreground=p["fg_dim"],
                    fieldbackground=p["inset"], bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["surface"],
                    arrowcolor=p["fg_dim"], troughcolor=p["bg"],
                    font=(_f, 9))
        s.configure("TFrame", background=p["bg"])
        s.configure("TLabel", background=p["bg"], foreground=p["fg_dim"],
                    font=(_f, 9))
        s.configure("TLabelframe", background=p["bg"],
                    bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["surface"])
        s.configure("TLabelframe.Label", background=p["bg"],
                    foreground=p["fg"], font=(_f, 10, "bold"))
        s.configure("TButton", background=p["surface_2"],
                    foreground=p["fg_dim"],
                    bordercolor=p["border"], focuscolor=p["bg"],
                    font=(_f, 9))
        s.map("TButton",
              background=[("active", p["surface_3"]),
                          ("disabled", p["bg"])],
              foreground=[("disabled", p["border"])])
        s.configure("TCheckbutton", background=p["bg"],
                    foreground=p["fg_dim"], font=(_f, 9))
        s.map("TCheckbutton", background=[("active", p["bg"])],
              foreground=[("disabled", p["border"])])
        s.configure("TEntry", fieldbackground=p["inset"],
                    foreground=p["fg_dim"], insertcolor=p["fg_dim"],
                    bordercolor=p["border"], font=(_f, 9))
        s.configure("TCombobox", fieldbackground=p["inset"],
                    background=p["surface_2"], foreground=p["fg_dim"],
                    arrowcolor=p["fg_dim"], bordercolor=p["border"],
                    font=(_f, 9))
        s.map("TCombobox",
              fieldbackground=[("readonly", p["inset"]),
                               ("disabled", p["bg"])],
              foreground=[("readonly", p["fg_dim"]),
                          ("disabled", p["border"])],
              selectbackground=[("readonly", p["inset"])],
              selectforeground=[("readonly", p["fg_dim"])],
              arrowcolor=[("disabled", p["border"])])
        self.root.option_add("*TCombobox*Listbox.background", p["inset"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["fg_dim"])
        self.root.option_add("*TCombobox*Listbox.selectBackground",
                             p["surface_3"])
        self.root.option_add("*TCombobox*Listbox.selectForeground",
                             p["fg_dim"])
        s.configure("TScrollbar", background=p["surface_2"],
                    troughcolor=p["bg"], bordercolor=p["border"],
                    arrowcolor=p["fg_dim"])
        s.map("TScrollbar", background=[("active", p["surface_3"])])

        s.configure("TRadiobutton", background=p["bg"],
                    foreground=p["fg_dim"], font=(_f, 9))
        s.map("TRadiobutton", background=[("active", p["bg"])],
              foreground=[("disabled", p["border"])])
        s.configure("TSeparator", background=p["border"])

        self.root.configure(background=p["bg"])
        if getattr(self, "log_text", None) is not None:
            self.log_text.configure(background=p["inset"],
                                    foreground=p["fg_dim"],
                                    insertbackground=p["fg_dim"])
            self.log_text.tag_configure("timestamp", foreground=p["fg_muted"])
            self.log_text.tag_configure("ok", foreground=p["ok"])
            self.log_text.tag_configure("warn", foreground=p["warn"])
            self.log_text.tag_configure("err", foreground=p["bad"])
        # Re-theme the settings window if it is open
        if (self._settings_win is not None):
            try:
                if self._settings_win.winfo_exists():
                    self._settings_win.configure(background=p["surface"])
            except tk.TclError:
                pass
        # Re-theme RoundedSection widgets
        for attr in ("_device_section", "_gm_section", "_skills_section",
                     "_custom_section", "_avoid_section",
                     "_timeout_section", "_log_section"):
            sec = getattr(self, attr, None)
            if sec is not None:
                sec.update_colors(bg=p["surface"],
                                  border_color=p["border"],
                                  title_fg=p["fg"],
                                  parent_bg=p["bg"])
        for attr in ("_inactive_section", "_active_section",
                     "_gm_chapter_sub", "_gm_plant_sub"):
            sec = getattr(self, attr, None)
            if sec is not None:
                sec.update_colors(bg=p["surface_2"],
                                  border_color=p["border"],
                                  title_fg=p["fg_muted"],
                                  parent_bg=p["surface"])
        # Re-theme segmented switches and direction pad
        if hasattr(self, "_chapter_toggle"):
            self._chapter_toggle.update_colors(
                container_bg=p["inset"], label_fg=p["fg_dim"],
                outline=p["border"], canvas_bg=p["surface_2"],
                left_color=p["accent"], right_color=p["accent"])
        if hasattr(self, "_plant_spawn_toggle"):
            self._plant_spawn_toggle.update_colors(
                container_bg=p["inset"], label_fg=p["fg_dim"],
                outline=p["border"], canvas_bg=p["surface_2"],
                left_color=p["accent"], right_color=p["accent"])
        if getattr(self, "_picker_toggle", None) is not None:
            try:
                self._picker_toggle.update_colors(
                    container_bg=p["inset"], label_fg=p["fg_dim"],
                    outline=p["border"], canvas_bg=p["bg"],
                    left_color=_CUSTOM_COLOR, right_color=_AVOID_COLOR)
            except tk.TclError:
                pass
        if hasattr(self, "_plant_dpad"):
            self._plant_dpad.update_colors(
                bg=p["surface_2"], selected_color=p["accent"],
                unselected_color=p["surface_3"], fg=p["fg_dim"])
        # Re-theme timeout checkboxes
        for attr in ("_timeout_cb1", "_timeout_cb2"):
            cb = getattr(self, attr, None)
            if cb is not None:
                cb.update_colors(checked_color=p["accent"],
                                 unchecked_color=p["surface_3"],
                                 text_color=p["fg_dim"],
                                 canvas_bg=p["surface"])
        self._retheme_topbar()
        self._update_status_color()
        self._render_skills()
        self._render_custom()
        self._render_avoid()
        self._render_custom_buttons()
        if (self._picker is not None):
            try:
                if self._picker.winfo_exists():
                    self._render_picker()
            except tk.TclError:
                pass

    def _update_status_color(self):
        key = _STATUS_COLORS.get(self.status_var.get())
        color = self.theme[key] if key else self.theme["fg"]
        if getattr(self, "_status_pill", None) is not None:
            self._status_pill.set_colors(
                dot_color=color, text_color=color,
                bg=self.theme["surface_3"],
                outline=self.theme["border_strong"],
                canvas_bg=self.theme["surface"])
        # In mini mode the run status lives in the OS title bar.
        if self._compact:
            self._update_compact_title()

    def _toggle_theme(self):
        old = self.theme
        self.dark = not self.dark
        self.dark_var.set(self.dark)
        self.theme = _THEMES["dark" if self.dark else "light"]
        if hasattr(self, "_theme_btn"):
            self._theme_btn.configure(text="Dark" if self.dark else "Light")
        set_dark_titlebar(self.root, self.dark)
        self._apply_theme()
        # _apply_theme re-styles ttk widgets and the custom canvas widgets, but
        # the many plain tk.Frame/Label/Canvas backgrounds built once at startup
        # are left stranded on the old palette (the "black backgrounds in light
        # mode" bug). Remap the whole tree so a live switch looks identical to
        # launching fresh in that mode.
        self._remap_tree_colors(old, self.theme)
        main.log("dark mode on" if self.dark else "dark mode off")
        self._apply_and_save(quiet=True)

    # Palette roles split by where each colour is used: background-ish options
    # take the bg roles, text-ish options the fg roles. Keeping them separate
    # avoids a collision -- light "surface" is #ffffff, the same hex as white
    # button text, which must NOT be recoloured to a dark surface.
    _BG_ROLES = ("bg", "surface", "surface_2", "surface_3",
                 "inset", "border", "border_strong")
    _FG_ROLES = ("fg", "fg_dim", "fg_muted")

    @staticmethod
    def _remap_opt(widget, opt, cmap):
        try:
            cur = str(widget.cget(opt))
        except (tk.TclError, AttributeError):
            return            # ttk widget or option this class lacks
        repl = cmap.get(cur.lower())
        if repl:
            try:
                widget.configure(**{opt: repl})
            except tk.TclError:
                pass

    def _remap_tree_colors(self, old, new):
        """Walk every widget under root and recolour any plain tk widget still
        holding a previous-theme colour, so a dark<->light switch reaches the
        backgrounds the ttk style engine and update_colors() never touch."""
        bg_map, fg_map = {}, {}
        for role in self._BG_ROLES:
            o, n = old.get(role), new.get(role)
            if o and n and o.lower() != n.lower():
                bg_map[o.lower()] = n
        for role in self._FG_ROLES:
            o, n = old.get(role), new.get(role)
            if o and n and o.lower() != n.lower():
                fg_map[o.lower()] = n
        bg_opts = ("background", "highlightbackground", "highlightcolor",
                   "activebackground")
        fg_opts = ("foreground", "activeforeground", "insertbackground",
                   "disabledforeground")
        # The custom canvas widgets re-theme themselves via update_colors() in
        # _apply_theme, and several override .configure() so that "background"
        # means their drawn fill, not the canvas backing colour -- blindly
        # remapping that would, e.g., paint the green Start button white. Skip
        # their own options but still recurse so plain children get fixed.
        custom = (RoundedSection, RoundedButton, StatusPill, ToggleSwitch,
                  RoundedCheckbox, DirectionPad, SegmentToggle)

        def walk(w):
            if not isinstance(w, custom):
                for opt in bg_opts:
                    self._remap_opt(w, opt, bg_map)
                for opt in fg_opts:
                    self._remap_opt(w, opt, fg_map)
            for child in w.winfo_children():
                walk(child)

        walk(self.root)

    def _retheme_topbar(self):
        """Re-apply palette to the topbar widgets that the ttk style
        engine does not reach."""
        p = self.theme
        for w in (self._topbar, self._topbar_inner):
            try:
                w.configure(background=p["surface"])
            except tk.TclError:
                pass
        try:
            self._topbar_sep.configure(background=p["border"])
        except tk.TclError:
            pass
        self.start_btn.configure(background=p["ok"])
        self.start_btn.set_canvas_bg(p["surface"])
        self.stop_btn.configure(background=p["bad"])
        self.stop_btn.set_canvas_bg(p["surface"])
        if hasattr(self, "_status_pill"):
            self._status_pill.set_colors(
                bg=p["surface_3"], outline=p["border_strong"],
                canvas_bg=p["surface"])
        for attr in ("_autosave_cb", "_keep_awake_cb"):
            cb = getattr(self, attr, None)
            if cb is not None:
                cb.update_colors(
                    checked_color=p["accent"],
                    unchecked_color=p["surface_3"],
                    text_color=p["fg_dim"],
                    canvas_bg=p["surface"])
        for attr in ("_theme_btn", "_settings_btn", "_compact_btn",
                     "_save_btn", "_hotkey_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                try:
                    btn.configure(
                        background=p["surface"],
                        foreground=p["fg_dim"],
                        activebackground=p["surface_2"],
                        activeforeground=p["fg"])
                except tk.TclError:
                    pass

    # skill categories
    def _build_skills(self, parent):
        self._skills_section = RoundedSection(
            parent, title="Skill categories",
            bg=self.theme["surface"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg"])
        self._skills_section.pack(fill="x", padx=8, pady=6)
        skills = self._skills_section.inner

        cols = tk.Frame(skills, bg=self.theme["surface"],
                        highlightthickness=0)
        cols.pack(fill="both", expand=True, pady=(2, 0))
        cols.columnconfigure(0, weight=1, uniform="cols")
        cols.columnconfigure(1, weight=1, uniform="cols")
        cols.rowconfigure(0, weight=1)

        self._inactive_section = RoundedSection(
            cols, title="Inactive  -  Tick to Activate",
            bg=self.theme["surface_2"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg_muted"], radius=6)
        self._inactive_section.grid(row=0, column=0, sticky="nsew",
                                    padx=(0, 4))

        self._active_section = RoundedSection(
            cols, title="Active  -  Drag to Rank",
            bg=self.theme["surface_2"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg_muted"], radius=6)
        self._active_section.grid(row=0, column=1, sticky="nsew",
                                  padx=(4, 0))

        self.inactive_frame = tk.Frame(
            self._inactive_section.inner,
            bg=self.theme["surface_2"], highlightthickness=0)
        self.inactive_frame.pack(fill="both", expand=True)
        self.active_frame = tk.Frame(
            self._active_section.inner,
            bg=self.theme["surface_2"], highlightthickness=0)
        self.active_frame.pack(fill="both", expand=True)

    def _skill_count(self, category):
        return len(list_skill_files(config.SKILLS_DIR / category))

    def _render_skills(self):
        for frame in (self.inactive_frame, self.active_frame):
            for child in frame.winfo_children():
                child.destroy()
        self.active_rows = {}
        p = self.theme

        inactive = [c for c in config.SKILL_CATEGORIES if c not in self.active]

        if not inactive:
            tk.Label(self.inactive_frame, text="(none)",
                     foreground=p["fg_muted"], bg=p["surface_2"],
                     font=(ui_font(), 9)).pack(anchor="w")
        for cat in inactive:
            box = tk.Frame(self.inactive_frame, bg=p["surface_3"],
                           highlightthickness=0, bd=0)
            box.pack(fill="x", pady=2, padx=2)
            inner = tk.Frame(box, bg=p["surface_3"], highlightthickness=0)
            inner.pack(fill="x", padx=8, pady=6)
            cb = RoundedCheckbox(
                inner, text=cat, variable=tk.BooleanVar(value=False),
                command=lambda c=cat: self._activate(c),
                checked_color=p["accent"],
                unchecked_color=p["border_strong"],
                text_color=p["fg_dim"],
                canvas_bg=p["surface_3"])
            cb.pack(side="left")
            tk.Label(inner, text=str(self._skill_count(cat)),
                     foreground=p["fg_muted"], bg=p["surface_3"],
                     font=(ui_font(), 8)).pack(side="right")

        if not self.active:
            tk.Label(self.active_frame, text="(none)",
                     foreground=p["fg_muted"], bg=p["surface_2"],
                     font=(ui_font(), 9)).pack(anchor="w")
        for i, cat in enumerate(self.active, 1):
            self._make_active_row(cat, rank=i)

    def _make_active_row(self, cat, rank=None):
        p = self.theme
        # Individual gray box around each active category
        box = tk.Frame(self.active_frame, bg=p["surface_3"],
                       highlightthickness=1,
                       highlightbackground=p["border"],
                       highlightcolor=p["border"])
        row = tk.Frame(box, bg=p["surface_3"], highlightthickness=0)
        row.pack(fill="x", padx=8, pady=5)

        handle = tk.Label(row, text="⠿", width=2, cursor="fleur",
                          foreground=p["fg_muted"], bg=p["surface_3"],
                          font=(ui_font(), 9))
        handle.pack(side="left")

        badge = tk.Label(
            row, text=str(rank or ""), width=2,
            font=(ui_font(), 8, "bold"),
            foreground=p["accent"], background=p["surface_3"],
            anchor="center")
        badge.pack(side="left", padx=(0, 4))
        row._rank_badge = badge

        name = tk.Label(row, text=cat, cursor="fleur",
                        font=(ui_font(), 9), fg=p["fg_dim"],
                        bg=p["surface_3"])
        count = tk.Label(row, text=str(self._skill_count(cat)),
                         foreground=p["fg_muted"], bg=p["surface_3"],
                         font=(ui_font(), 8))

        # The X is always packed so toggling its visibility on hover never
        # reflows the row. The old code pack/pack_forget'd it on every <Enter>/
        # <Leave>, and because those fire repeatedly as the pointer crosses the
        # row's own child labels, the whole list "glitched out". Now hover only
        # swaps the border colour and the X's colour -- no layout change.
        x_btn = tk.Button(row, text="✕", font=(ui_font(), 8),
                          relief="flat", cursor="hand2", bd=0,
                          highlightthickness=0,
                          bg=p["surface_3"], fg=p["surface_3"],
                          activebackground=p["surface_3"],
                          activeforeground=p["bad"],
                          command=lambda c=cat: self._deactivate(c))
        name.pack(side="left", fill="x", expand=True)
        x_btn.pack(side="right", padx=(2, 0))
        count.pack(side="right", padx=(4, 0))

        def _hover_in(_e=None, b=box, xb=x_btn):
            b.configure(highlightbackground=p["fg_muted"])
            xb.configure(fg=p["bad"])

        def _hover_out(_e=None, b=box, xb=x_btn):
            # Ignore the <Leave> that fires when the pointer merely crosses onto
            # a child widget; only reset once it has really left the box.
            try:
                px, py = b.winfo_pointerxy()
                bx, by = b.winfo_rootx(), b.winfo_rooty()
                if (bx <= px < bx + b.winfo_width()
                        and by <= py < by + b.winfo_height()):
                    return
            except tk.TclError:
                pass
            if self._drag_cat == cat:
                return
            b.configure(highlightbackground=p["border"])
            xb.configure(fg=p["surface_3"])

        for w in (box, row, handle, badge, name, count):
            w.bind("<Enter>", _hover_in)
            w.bind("<Leave>", _hover_out)

        for widget in (row, handle, name):
            widget.bind("<ButtonPress-1>", lambda e, c=cat: self._drag_start(c))
            widget.bind("<B1-Motion>", self._drag_motion)
            widget.bind("<ButtonRelease-1>", self._drag_end)

        box.pack(fill="x", pady=2, padx=2)
        box._rank_badge = badge          # _repack_active updates the rank here
        self.active_rows[cat] = box

    def _activate(self, cat):
        if cat not in self.active:
            self.active.append(cat)
        self._render_skills()
        self._schedule_autosave()

    def _deactivate(self, cat):
        if cat in self.active:
            self.active.remove(cat)
        self._render_skills()
        self._schedule_autosave()

    def _drag_start(self, cat):
        self._drag_cat = cat

    def _drag_motion(self, event):
        cat = self._drag_cat
        if not cat or cat not in self.active:
            return
        row = self.active_rows.get(cat)
        if row is None:
            return
        row_h = row.winfo_height()
        if row_h < 2:
            row_h = 24
        row_h += 2

        y = event.y_root - self.active_frame.winfo_rooty()
        target = max(0, min(int(y // row_h), len(self.active) - 1))
        current = self.active.index(cat)
        if target != current:
            self.active.pop(current)
            self.active.insert(target, cat)
            self._repack_active()

    def _repack_active(self):
        for row in self.active_rows.values():
            row.pack_forget()
        for i, cat in enumerate(self.active, 1):
            row = self.active_rows[cat]
            if hasattr(row, "_rank_badge"):
                row._rank_badge.configure(text=str(i))
            row.pack(fill="x", pady=2, padx=2)

    def _drag_end(self, _event):
        self._drag_cat = None
        self._schedule_autosave()

    # custom priority / avoid skills
    def _build_custom(self, parent):
        self._custom_section = RoundedSection(
            parent, title="Custom priority skills",
            bg=self.theme["surface"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg"])
        self._custom_section.pack(fill="x", padx=8, pady=6)

        tk.Label(self._custom_section.inner,
                 text="taken above all categories",
                 foreground=self.theme["fg_muted"],
                 bg=self.theme["surface"],
                 font=(ui_font(), 8)).pack(anchor="w", pady=(0, 2))

        self.custom_frame = tk.Frame(self._custom_section.inner,
                                     bg=self.theme["surface"],
                                     highlightthickness=0)
        self.custom_frame.pack(fill="x", pady=(2, 2))

    def _build_avoid(self, parent):
        self._avoid_section = RoundedSection(
            parent, title="Avoid skills",
            bg=self.theme["surface"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg"])
        self._avoid_section.pack(fill="x", padx=8, pady=6)

        tk.Label(self._avoid_section.inner,
                 text="never taken",
                 foreground=self.theme["fg_muted"],
                 bg=self.theme["surface"],
                 font=(ui_font(), 8)).pack(anchor="w", pady=(0, 2))

        self.avoid_frame = tk.Frame(self._avoid_section.inner,
                                    bg=self.theme["surface"],
                                    highlightthickness=0)
        self.avoid_frame.pack(fill="x", pady=(2, 2))

    def _all_skills(self):
        out = []
        for cat in config.SKILL_CATEGORIES:
            files = list_skill_files(config.SKILLS_DIR / cat)
            out.append((cat, [f"{cat}/{p.name}" for p in files]))
        return out

    def _skill_hash(self, identifier):
        if identifier not in self._hash_cache:
            self._hash_cache[identifier] = skill_hash(
                config.SKILLS_DIR / identifier)
        return self._hash_cache[identifier]

    def _hashes_of(self, items):
        hashes = set()
        for identifier in items:
            h = self._skill_hash(identifier)
            if h is not None:
                hashes.add(h)
        return hashes

    def _thumb(self, identifier, size):
        key = (identifier, size)
        if key in self._thumb_cache:
            return self._thumb_cache[key]
        photo = None
        path = config.SKILLS_DIR / identifier
        if path.exists():
            try:
                image = Image.open(path).convert("RGB")
                image = image.resize((size, size), _RESAMPLE)
                photo = ImageTk.PhotoImage(image)
            except Exception:
                photo = None
        self._thumb_cache[key] = photo
        return photo

    # Sizes used by the skill picker grid (56) and the priority/avoid strips
    # (52). Preloaded behind the splash so the first picker open is instant.
    _PRELOAD_SIZES = (56, 52)

    def preload_assets(self, progress=None):
        """Warm the thumbnail + hash caches for every skill so opening the
        picker doesn't have to decode ~150 PNGs on first use. Called once,
        behind the loading splash. ``progress(done, total, msg)`` is invoked
        periodically so the splash bar can advance."""
        idents = []
        for _cat, cat_idents in self._all_skills():
            idents.extend(cat_idents)
        total = max(1, len(idents) * len(self._PRELOAD_SIZES))
        done = 0
        for i, size in enumerate(self._PRELOAD_SIZES):
            for ident in idents:
                self._thumb(ident, size)
                if i == 0:
                    self._skill_hash(ident)      # hash is size-independent
                done += 1
                if progress is not None and (done % 8 == 0 or done == total):
                    progress(done, total, "Loading skill icons…")
        if progress is not None:
            progress(total, total, "Ready")

    def _draw_skill_cell(self, cell, identifier, size, highlight=None):
        """(Re)draw a skill cell's rounded border + thumbnail. Split out from
        cell creation so a selection change can recolour the border in place
        rather than rebuilding the whole picker grid."""
        p = self.theme
        border_col = highlight or p["border_strong"]
        cell.delete("all")
        draw_rounded_rect(cell, 0, 0, size + 5, size + 5, 6,
                          fill=border_col, outline="")
        draw_rounded_rect(cell, 2, 2, size + 3, size + 3, 4,
                          fill="white", outline="")
        photo = self._thumb(identifier, size)
        if photo is not None:
            cell.create_image(3, 3, image=photo, anchor="nw")
            cell._photo_ref = photo
        else:
            cell.create_text((size + 6) // 2, (size + 6) // 2,
                             text="?", fill="#999",
                             font=(ui_font(), 10))

    def _skill_cell(self, parent, identifier, size, command, highlight=None):
        p = self.theme
        cell = tk.Canvas(parent, width=size + 6, height=size + 6,
                         highlightthickness=0,
                         bg=parent.cget("background") if hasattr(parent, "cget") else p["bg"],
                         cursor="hand2", bd=0)
        self._draw_skill_cell(cell, identifier, size, highlight)
        cell.bind("<Button-1>", lambda e: command())
        return cell

    def _render_skill_strip(self, frame, items, color, toggle, empty_text):
        for child in frame.winfo_children():
            child.destroy()
        p = self.theme
        flow = tk.Frame(frame, bg=p["surface"], highlightthickness=0)
        flow.pack(anchor="w")

        thumb_size = 52
        mode = "custom" if color == _CUSTOM_COLOR else "avoid"
        # "+" add button (same size as skill previews, rounded)
        add_btn = tk.Canvas(flow, width=thumb_size + 6,
                            height=thumb_size + 6,
                            highlightthickness=0, bg=p["surface"],
                            cursor="hand2", bd=0)
        draw_rounded_rect(add_btn, 0, 0, thumb_size + 5, thumb_size + 5,
                          6, fill=p["surface_3"],
                          outline=p["border_strong"])
        add_btn.create_text((thumb_size + 6) // 2, (thumb_size + 6) // 2,
                            text="+", fill=p["fg_muted"],
                            font=(ui_font(), 18))
        add_btn.grid(row=0, column=0, padx=3, pady=3)
        add_btn.bind("<Button-1>",
                     lambda e, m=mode: self._open_skill_picker(m))

        if not items:
            tk.Label(flow, text="none", foreground=p["fg_muted"],
                     bg=p["surface"],
                     font=(ui_font(), 9, "italic")).grid(
                         row=0, column=1, padx=6)
            return

        cols = 9
        for i, identifier in enumerate(items):
            cell = self._skill_cell(
                flow, identifier, thumb_size,
                command=lambda d=identifier: toggle(d), highlight=color)
            Tooltip(cell, identifier)
            cell.grid(row=(i + 1) // cols, column=(i + 1) % cols,
                      padx=3, pady=3)

    def _render_custom(self):
        self._render_skill_strip(self.custom_frame, self.custom, _CUSTOM_COLOR,
                                 self._toggle_custom,
                                 "(no custom priority skills)")

    def _render_avoid(self):
        self._render_skill_strip(self.avoid_frame, self.avoid, _AVOID_COLOR,
                                 self._toggle_avoid, "(no skills avoided)")

    def _toggle_in(self, items, identifier):
        h = self._skill_hash(identifier)
        if h is not None and any(self._skill_hash(d) == h for d in items):
            return [d for d in items if self._skill_hash(d) != h]
        if identifier in items:
            return items
        return items + [identifier]

    def _toggle_custom(self, identifier):
        self.custom = self._toggle_in(self.custom, identifier)
        self._render_custom()
        self._refresh_picker_after_toggle(identifier)
        self._schedule_autosave()

    def _toggle_avoid(self, identifier):
        self.avoid = self._toggle_in(self.avoid, identifier)
        self._render_avoid()
        self._refresh_picker_after_toggle(identifier)
        self._schedule_autosave()

    def _refresh_picker_after_toggle(self, identifier):
        """Recolour only the picker cells affected by toggling *identifier* (the
        skill plus any duplicate copies that share its image hash) instead of
        rebuilding the whole grid -- that full rebuild was the click/switch lag
        spike the user hit. Safe no-op if the picker is closed."""
        if self._picker is None:
            return
        try:
            if not self._picker.winfo_exists():
                return
        except tk.TclError:
            return
        cells = getattr(self, "_picker_cells", None)
        if not cells:
            return
        prio_hashes = self._hashes_of(self.custom)
        avoid_hashes = self._hashes_of(self.avoid)
        h0 = self._skill_hash(identifier)
        size = getattr(self, "_picker_cell_size", 56)
        for ident, cell in cells.items():
            h = self._skill_hash(ident)
            if h0 is not None and h != h0:
                continue          # this cell can't have changed
            if h is not None and h in prio_hashes:
                hl = _CUSTOM_COLOR
            elif h is not None and h in avoid_hashes:
                hl = _AVOID_COLOR
            else:
                hl = None
            try:
                self._draw_skill_cell(cell, ident, size, hl)
            except tk.TclError:
                pass
        self._update_picker_footer()

    # custom button helpers (rendering moved to settings panel)

    def _custom_button_thumb(self, path, box):
        try:
            image = Image.open(path).convert("RGB")
            image.thumbnail((box, box), _RESAMPLE)
            return ImageTk.PhotoImage(image)
        except Exception:
            return None

    def _custom_button_cell(self, parent, path):
        cell = tk.Frame(parent, background=self.theme["border_strong"])
        inner = tk.Frame(cell, background="white")
        inner.pack(padx=2, pady=2)
        photo = self._custom_button_thumb(path, 100)
        if photo is not None:
            self._custom_thumb_refs.append(photo)
            thumb = tk.Label(inner, image=photo, background="white")
        else:
            thumb = tk.Label(inner, text="(bad image)", width=12, height=3,
                             background="white", foreground="#999")
        thumb.pack()
        caption = tk.Label(inner, text=path.stem, background="white",
                           font=("", 7))
        caption.pack()
        for widget in (cell, inner, thumb, caption):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>",
                        lambda e, p=path: self._remove_custom_button(p))
        Tooltip(cell, f"{path.name} , click to remove")
        return cell

    def _remove_custom_button(self, path):
        if not messagebox.askyesno(
                "Remove custom button",
                f"Remove the custom button '{path.stem}'?"):
            return
        try:
            path.unlink()
            main.log(f"removed custom button '{path.stem}'")
        except OSError as e:
            messagebox.showerror("Remove custom button",
                                 f"Could not delete the file:\n{e}")
        self._render_custom_buttons()

    def _capture_crop_to(self, path, title, after_save=None, zone=None):
        """Shared capture flow: ADB screenshot, rubber-band crop, then save the
        crop downscaled to the MATCH_WIDTH baseline at `path`.  Used by both
        custom-button capture and built-in ref recapture, so every template is
        stored at the one scale the macro matches at, from ANY device.

        `path` is a pathlib.Path; `after_save` is an optional callable run on the
        UI thread once the file is written; `zone` (None / "full" / "top" /
        "middle" / "bottom") is recorded in ref/ref_zones.json so the macro only
        searches that vertical half of the frame for this template.
        """
        adb = self._make_adb_client()
        if adb is None:
            return

        self._start_picker()

        def worker():
            screenshot = None
            try:
                screenshot = self._capture_frame(adb)
            except Exception as e:
                main.log(f"capture: ADB screenshot failed: {e}")

            def show_crop(ss):
                if ss is None:
                    main.log("capture: screenshot failed, aborted")
                    self._finish_picker()
                    return

                def on_crop(rect):
                    if rect is None:
                        main.log("capture cancelled")
                        self._finish_picker()
                        return
                    x, y, w, h = rect
                    if w < 5 or h < 5:
                        main.log("capture: selection too small, aborted")
                        self._finish_picker()
                        return

                    def do_save():
                        try:
                            crop = ss[y:y + h, x:x + w]
                            pil  = Image.fromarray(crop[:, :, ::-1].copy())
                            # Downscale the crop to the MATCH_WIDTH baseline so the
                            # template matches in the macro's normalised match
                            # space no matter which device (540 emulator, 1080
                            # phone, ...) it was captured on.
                            dev_w = ss.shape[1]
                            norm = config.MATCH_WIDTH / dev_w if dev_w else 1.0
                            if abs(norm - 1.0) > 1e-3:
                                pil = pil.resize(
                                    (max(1, round(w * norm)),
                                     max(1, round(h * norm))),
                                    Image.LANCZOS)
                            path.parent.mkdir(parents=True, exist_ok=True)
                            pil.save(str(path))
                            main.log(
                                f"saved {path.name} ({pil.width}x{pil.height} px "
                                f"at the {config.MATCH_WIDTH}-wide baseline) "
                                f"-> {path.parent.name}/{path.name}")
                            if zone is not None:
                                self._save_ref_zone(path, zone)
                            if after_save is not None:
                                try:
                                    self.root.after(0, after_save)
                                except tk.TclError:
                                    pass
                        except Exception as e:
                            main.log(f"capture save failed: {e!r}")
                        finally:
                            try:
                                self.root.after(0, self._finish_picker)
                            except tk.TclError:
                                pass

                    threading.Thread(target=do_save, daemon=True).start()

                _AdbCropWindow(self.root, ss, on_crop, title=title)

            try:
                self.root.after(0, lambda: show_crop(screenshot))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _save_ref_zone(self, path, zone):
        """Record/clear a template's match zone in ref/ref_zones.json, keyed by
        the path relative to ref/ ('level.png' or 'custom/a.png')."""
        try:
            rel = path.resolve().relative_to(config.REF_DIR.resolve()).as_posix()
            zones = config.load_ref_zones()
            if zone in (None, "full"):
                zones.pop(rel, None)
            else:
                zones[rel] = zone
            config.save_ref_zones(zones)
            main.log(f"  match zone for {rel}: {zone}")
        except Exception as e:
            main.log(f"could not save match zone: {e!r}")

    def _zone_row(self, parent, initial="full"):
        """A 'Search zone:' combobox row; returns its StringVar."""
        zrow = ttk.Frame(parent)
        zrow.pack(fill="x", padx=14, pady=4)
        ttk.Label(zrow, text="Search zone:").pack(side="left")
        zvar = tk.StringVar(value=initial)
        ttk.Combobox(zrow, textvariable=zvar, values=list(config.ZONE_CHOICES),
                     state="readonly", width=10).pack(side="left", padx=(6, 0))
        ttk.Label(zrow, foreground="#888",
                  text="top / middle / bottom 50% (faster matching)").pack(
            side="left", padx=8)
        return zvar

    def _add_custom_button(self):
        """Capture a button via ADB screenshot and save it as a custom ref."""
        if self._coords_busy:
            return
        if self._running:
            main.log("stop the macro before adding a custom button")
            return

        win = tk.Toplevel(self.root)
        win.title("Add custom button")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        win.configure(background=self.theme["bg"])
        ttk.Label(
            win, wraplength=360, justify="left",
            text="Name the button and pick where on screen it appears, then drag "
                 "a box over it. The macro clicks it whenever it sees it."
        ).pack(fill="x", padx=14, pady=(14, 8))

        nrow = ttk.Frame(win)
        nrow.pack(fill="x", padx=14, pady=4)
        ttk.Label(nrow, text="Name:").pack(side="left")
        nvar = tk.StringVar()
        ttk.Entry(nrow, textvariable=nvar, width=24).pack(side="left", padx=(6, 0))
        zvar = self._zone_row(win)

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=14, pady=(10, 14))

        def go():
            safe = "".join(c for c in nvar.get()
                           if c.isalnum() or c in " -_").strip()
            if not safe:
                messagebox.showerror("Custom button",
                                     "Please enter a valid name.", parent=win)
                return
            path = config.REF_CUSTOM_DIR / f"{safe}.png"
            if path.exists() and not messagebox.askyesno(
                    "Custom button",
                    f"A custom button named '{safe}' already exists. Replace it?",
                    parent=win):
                return
            zone = zvar.get()
            win.destroy()
            self._capture_crop_to(
                path,
                f"Capture custom button '{safe}':  drag a box over it, then Confirm",
                after_save=self._render_custom_buttons, zone=zone)

        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Capture", command=go).pack(side="right", padx=(0, 6))

    def _recapture_ref(self):
        """Recapture a built-in reference image (ref/*.png) from a live ADB
        screenshot.  Saved at the MATCH_WIDTH baseline, so it can be captured
        from any device and still match (no need to use BlueStacks for this)."""
        if self._coords_busy:
            return
        if self._running:
            main.log("stop the macro before recapturing a ref")
            return

        refs = sorted(p.name for p in config.REF_DIR.glob("*.png") if p.is_file())
        existing = config.load_ref_zones()

        win = tk.Toplevel(self.root)
        win.title("Capture / recapture reference image")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        win.configure(background=self.theme["bg"])
        ttk.Label(
            win, wraplength=400, justify="left",
            text="Pick an existing reference to recapture, OR type a new name to "
                 "capture a brand-new ref. Choose where on screen it appears, "
                 "then drag a box tightly around it (saved at the standard match "
                 "scale, so any device works).\n\n"
                 "A new ref is only saved for later -- the macro uses it only "
                 "once you wire it up in code."
        ).pack(fill="x", padx=14, pady=(14, 8))

        row = ttk.Frame(win)
        row.pack(fill="x", padx=14, pady=4)
        ttk.Label(row, text="Reference:").pack(side="left")
        var = tk.StringVar(value=refs[0] if refs else "")
        # Editable on purpose: select an existing ref, or type a new name.
        ttk.Combobox(row, textvariable=var, values=refs,
                     width=24).pack(side="left", padx=(6, 0))
        zvar = self._zone_row(
            win, existing.get(refs[0], "full") if refs else "full")

        def on_ref(*_):
            z = existing.get(var.get())   # only prefill a known ref's saved zone;
            if z:                          # don't clobber the choice while typing
                zvar.set(z)
        var.trace_add("write", on_ref)

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=14, pady=(10, 14))

        def go():
            raw = var.get().strip()
            base = raw[:-4] if raw.lower().endswith(".png") else raw
            safe = "".join(c for c in base if c.isalnum() or c in " -_").strip()
            if not safe:
                messagebox.showerror("Reference",
                                     "Enter or pick a reference name.", parent=win)
                return
            fname = f"{safe}.png"
            path = config.REF_DIR / fname
            zone = zvar.get()
            verb = "Recapture" if path.exists() else "Capture new ref"
            if path.exists() and not messagebox.askyesno(
                    "Reference", f"'{fname}' already exists. Replace it?",
                    parent=win):
                return
            win.destroy()
            self._capture_crop_to(
                path,
                f"{verb} '{fname}':  drag a box over it, then Confirm",
                zone=zone)

        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Capture", command=go).pack(side="right", padx=(0, 6))

    # conduct movement (used by settings panel movement tuning)
    def _conduct_plant_movement(self):
        """Run the Plant Defense movement once for the selected test spawn."""
        if self._running:
            main.log("macro is running -- stop it before testing movement")
            return
        if not self._apply_and_save(quiet=True):
            main.log("conduct movement: could not save current settings")
            return
        adb = self._make_adb_client()
        if adb is None:
            return
        spawn = self.movement_plant_spawn_var.get()
        try:
            self._conduct_btn.state(["disabled"])
        except Exception:
            pass

        def worker():
            try:
                main.run_plant_movement(adb, spawn=spawn)
            except Exception as e:
                main.log(f"conduct movement: error: {e}")
            finally:
                try:
                    self.root.after(0,
                                    lambda: self._conduct_btn.state(["!disabled"]))
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    # skill picker (shared by custom priority + avoid)
    def _open_skill_picker(self, mode):
        self._picker_mode = mode
        if self._picker is not None:
            try:
                if self._picker.winfo_exists():
                    # Already open: just flip the mode (cheap) and raise it; the
                    # grid is already current, so no rebuild.
                    if getattr(self, "_picker_mode_var", None) is not None:
                        self._picker_mode_var.set(mode == "avoid")
                    self._set_picker_mode(mode)
                    self._picker.lift()
                    return
            except tk.TclError:
                pass
            self._picker = None

        win = tk.Toplevel(self.root)
        self._picker = win
        win.title("Add skills")
        win.geometry("820x580")
        win.transient(self.root)
        win.configure(background=self.theme["bg"])
        set_dark_titlebar(win, self.dark)
        self._picker_search_var = None

        # Header: title + mode toggle switch
        head = tk.Frame(win, bg=self.theme["bg"], highlightthickness=0)
        head.pack(fill="x", padx=12, pady=(10, 6))

        tk.Label(head, text="Add skills", font=(ui_font(), 12, "bold"),
                 fg=self.theme["fg"], bg=self.theme["bg"]).pack(
                     side="left")

        self._picker_mode_var = tk.BooleanVar(
            value=(mode == "avoid"))
        self._picker_toggle = SegmentToggle(
            head, variable=self._picker_mode_var,
            command=self._on_picker_toggle,
            labels=("Priority", "Avoid"),
            left_color=_CUSTOM_COLOR, right_color=_AVOID_COLOR,
            container_bg=self.theme["inset"],
            label_fg=self.theme["fg_dim"],
            outline=self.theme["border"],
            canvas_bg=self.theme["bg"])
        self._picker_toggle.pack(side="left", padx=(16, 0))

        # Scrollable body
        canvas = tk.Canvas(win, borderwidth=0, highlightthickness=0,
                           background=self.theme["bg"])
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        # Footer
        footer = tk.Frame(win, bg=self.theme["bg"], highlightthickness=0)
        footer.pack(side="bottom", fill="x", padx=12, pady=(6, 10))
        self._picker_counts = tk.Label(
            footer, text="", fg=self.theme["fg_muted"],
            bg=self.theme["bg"], font=(ui_font(), 9))
        self._picker_counts.pack(side="left")
        self._picker_hint = tk.Label(
            footer, text="", fg=self.theme["fg_muted"],
            bg=self.theme["bg"], font=(ui_font(), 8))
        self._picker_hint.pack(side="right", padx=(0, 8))
        self._picker_done_btn = RoundedButton(
            footer, text="Done", command=win.destroy,
            bg=self.theme["accent"], fg="#ffffff",
            font=(ui_font(), 9, "bold"), radius=6,
            canvas_bg=self.theme["bg"])
        self._picker_done_btn.pack(side="right")

        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=self.theme["bg"], highlightthickness=0)
        cwin = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(cwin, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<Enter>",
                    lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self._picker_body = body
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._render_picker()

    def _on_picker_toggle(self):
        avoid = self._picker_mode_var.get()
        self._set_picker_mode("avoid" if avoid else "custom")

    def _set_picker_mode(self, mode):
        """Switch the picker between 'custom' (priority) and 'avoid'.

        Cheap by design: flipping the mode does not change which skills are
        selected, only what a *new* click does, so we just update the footer
        hint -- no grid rebuild. (The rebuild was what made the switch stutter
        mid-animation.)"""
        self._picker_mode = mode
        self._update_picker_footer()

    def _picker_click(self, identifier):
        """Assign a clicked tile to the current mode's list. Reading the mode
        here (rather than baking the toggle fn into each cell) is what lets the
        Priority/Avoid switch flip without re-binding every cell."""
        if self._picker_mode == "avoid":
            self._toggle_avoid(identifier)
        else:
            self._toggle_custom(identifier)

    def _update_picker_footer(self):
        if getattr(self, "_picker_counts", None) is not None:
            try:
                self._picker_counts.configure(
                    text=f"● {len(self.custom)} priority   "
                         f"● {len(self.avoid)} avoid")
            except tk.TclError:
                pass
        if getattr(self, "_picker_hint", None) is not None:
            action = "prioritise" if self._picker_mode != "avoid" else "avoid"
            try:
                self._picker_hint.configure(
                    text=f"Click a tile to {action} it")
            except tk.TclError:
                pass

    def _render_picker(self):
        body = self._picker_body
        if body is None:
            return
        try:
            if not body.winfo_exists():
                return
        except tk.TclError:
            return

        p = self.theme

        for child in body.winfo_children():
            child.destroy()
        self._picker_cells = {}
        self._picker_cell_size = 56

        all_skills = self._all_skills()
        if not any(idents for _, idents in all_skills):
            tk.Label(body, foreground="#999", bg=p["bg"],
                     wraplength=580, font=(ui_font(), 9),
                     text="No skill images found. Add PNG icons to the "
                          "skills/ category subfolders, then reopen this "
                          "window.").pack(padx=12, pady=24)
            return

        prio_hashes = self._hashes_of(self.custom)
        avoid_hashes = self._hashes_of(self.avoid)
        cols = 10

        for category, idents in all_skills:
            if not idents:
                continue

            cat_label = f"{category.upper()}  ·  {len(idents)}"
            tk.Label(body, text=cat_label, font=(ui_font(), 8, "bold"),
                     foreground=p["fg_muted"], bg=p["bg"]).pack(
                         anchor="w", padx=8, pady=(10, 4))

            grid = tk.Frame(body, bg=p["bg"], highlightthickness=0)
            grid.pack(anchor="w", padx=8)
            for i, identifier in enumerate(idents):
                h = self._skill_hash(identifier)
                in_prio = h is not None and h in prio_hashes
                in_avoid = h is not None and h in avoid_hashes
                if in_prio:
                    highlight = _CUSTOM_COLOR
                elif in_avoid:
                    highlight = _AVOID_COLOR
                else:
                    highlight = None
                cell = self._skill_cell(
                    grid, identifier, self._picker_cell_size,
                    command=lambda d=identifier: self._picker_click(d),
                    highlight=highlight)
                cell.grid(row=i // cols, column=i % cols, padx=3, pady=3)
                self._picker_cells[identifier] = cell

        self._update_picker_footer()

    # ------------------------------------------------------------------ settings panel
    def _open_settings_panel(self):
        """Open the settings panel as a Toplevel window with accordion groups."""
        if self._settings_win is not None:
            try:
                if self._settings_win.winfo_exists():
                    self._settings_win.lift()
                    return
            except tk.TclError:
                pass
            self._settings_win = None

        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("Settings")
        win.geometry("480x640")
        win.transient(self.root)
        win.configure(background=self.theme["bg"])
        # Match the OS title bar to the theme (otherwise it's a bright white bar
        # in dark mode, since the dark DWM attribute was only set on the root).
        set_dark_titlebar(win, self.dark)

        head = ttk.Frame(win)
        head.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Label(head, text="⚙", font=("", 14),
                  foreground=self.theme["accent"]).pack(side="left")
        ttk.Label(head, text="Settings", font=("", 13, "bold")).pack(
            side="left", padx=(6, 0))
        ttk.Button(head, text="✕", width=3,
                   command=win.destroy).pack(side="right")

        # Scrollable body
        canvas = tk.Canvas(win, borderwidth=0, highlightthickness=0,
                           background=self.theme["bg"])
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = ttk.Frame(canvas)
        cwin = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(cwin, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<Enter>",
                    lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        btn_row = ttk.Frame(body)
        btn_row.pack(fill="x", padx=8, pady=(8, 6))
        wiz_btn = tk.Button(
            btn_row, text="✦  Setup Wizard",
            command=self._run_setup_wizard,
            font=("", 9, "bold"), foreground="#ffffff",
            background=self.theme["accent"],
            activebackground=self.theme["accent"],
            activeforeground="#ffffff", relief="flat", cursor="hand2",
            padx=10, pady=5)
        wiz_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        Tooltip(wiz_btn, "Connect a device, detect resolution, set defaults.")
        test_btn = ttk.Button(btn_row, text="⚡  Test connection",
                               command=self._test_adb_connection)
        test_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))
        Tooltip(test_btn, "Take an ADB screenshot to confirm the connection.")
        self._coord_buttons.append(test_btn)

        ttk.Separator(body).pack(fill="x", padx=8, pady=4)

        # Timing
        self._sp_group(body, "Timing", "How often the macro looks and acts",
                       default_open=True, fields=[
            ("POLL_INTERVAL", "Poll interval", "seconds"),
            ("ACTION_DELAY", "Action delay", "seconds"),
            ("STARTUP_DELAY", "Startup delay", "seconds"),
            ("SKILL_SETTLE_DELAY", "Skill settle delay", "seconds"),
        ])
        # Humanisation
        self._sp_group(body, "Humanisation",
                       "Add randomness so taps look human", fields=[
            ("CLICK_JITTER", "Click jitter", "px"),
            ("DELAY_JITTER", "Delay jitter", "fraction"),
        ])
        # Custom buttons
        self._sp_build_custom_buttons(body)
        # Reference images
        self._sp_build_ref_images(body)

        ttk.Separator(body).pack(fill="x", padx=8, pady=4)

        warn_bg = "#3a3020" if self.dark else "#fdf6e3"
        warn_frame = tk.Frame(body, background=warn_bg,
                              highlightthickness=0, padx=10, pady=7)
        warn_frame.pack(fill="x", padx=8, pady=(6, 4))
        tk.Label(warn_frame, text="⚠", font=("", 11),
                 foreground=self.theme["warn"],
                 background=warn_bg).pack(side="left")
        tk.Label(warn_frame, font=("", 8),
                 foreground=self.theme["warn"], background=warn_bg,
                 text="Advanced - only touch these if you know what you're "
                 "doing.").pack(side="left", padx=(8, 0))

        # Template matching (advanced)
        self._sp_group(body, "Template matching",
                       "Detection thresholds & scaling", fields=[
            ("MATCH_THRESHOLD", "Skill threshold", "0-1"),
            ("REF_THRESHOLD", "UI threshold", "0-1"),
            ("SKILL_DOWNSCALE", "Skill downscale", "factor"),
            ("REF_DOWNSCALE", "Ref downscale", "factor"),
            ("CALIBRATED_SCALE", "Calibrated scale", None),
        ])
        # Movement tuning (advanced)
        self._sp_build_movement_tuning(body)
        # Capture lives at the very bottom of the advanced section now, with a
        # yellow (advanced) icon to match the rest.
        self._sp_group(body, "Capture",
                       "How frames are pulled from the device", fields=[
            ("USE_STREAM_CAPTURE", "Stream capture (screenrecord)", None),
        ])

        win.protocol("WM_DELETE_WINDOW", win.destroy)

    _SP_ICONS = {
        "Timing": ("⏱", None),
        "Capture": ("⊙", "advanced"),
        "Humanisation": ("~", None),
        "Custom buttons": ("⊕", None),
        "Reference images": ("⊞", None),
        "Template matching": ("◎", "advanced"),
        "Movement tuning": ("↗", "advanced"),
    }

    def _sp_group(self, parent, title, sub, fields, default_open=False):
        """Build an accordion settings group with label+field rows."""
        p = self.theme
        outer = RoundedSection(
            parent, title="", bg=p["surface"],
            border_color=p["border"], radius=6,
            parent_bg=p["bg"])
        outer.pack(fill="x", padx=8, pady=3)

        header = tk.Frame(outer.inner, bg=p["surface"],
                          cursor="hand2", highlightthickness=0)
        header.pack(fill="x")

        icon_char, advanced = self._SP_ICONS.get(title, ("●", None))
        icon_color = p["warn"] if advanced else p["accent"]
        icon_lbl = tk.Label(
            header, text=icon_char, font=(ui_font(), 10),
            foreground=icon_color, background=p["surface"],
            width=3, cursor="hand2")
        icon_lbl.pack(side="left", padx=(0, 4))

        title_frame = tk.Frame(header, bg=p["surface"],
                               cursor="hand2", highlightthickness=0)
        title_frame.pack(side="left", fill="x", expand=True)
        tk.Label(title_frame, text=title, font=(ui_font(), 9, "bold"),
                 fg=p["fg"], bg=p["surface"],
                 cursor="hand2").pack(anchor="w")
        if sub:
            tk.Label(title_frame, text=sub, fg=p["fg_muted"],
                     bg=p["surface"], font=(ui_font(), 8),
                     cursor="hand2").pack(anchor="w")

        chev = tk.Label(header, text="▾" if default_open else "▸",
                        width=2, cursor="hand2", font=(ui_font(), 10),
                        fg=p["fg_dim"], bg=p["surface"])
        chev.pack(side="right")

        body = tk.Frame(outer.inner, bg=p["surface"],
                        highlightthickness=0)
        if default_open:
            body.pack(fill="x", padx=(16, 0), pady=(4, 4))

        def toggle(e=None):
            if body.winfo_ismapped():
                body.pack_forget()
                chev.configure(text="▸")
            else:
                body.pack(fill="x", padx=(16, 0), pady=(4, 4))
                chev.configure(text="▾")

        for w in (header,) + tuple(header.winfo_children()):
            w.bind("<Button-1>", toggle)

        for field in fields:
            key, label = field[0], field[1]
            unit = field[2] if len(field) > 2 else None
            var = self.vars.get(key)
            if var is None:
                continue

            row = tk.Frame(body, bg=p["surface"], highlightthickness=0)
            row.pack(fill="x", pady=2)
            lbl = tk.Label(row, text=label, fg=p["fg_dim"],
                           bg=p["surface"], font=(ui_font(), 9))
            lbl.pack(side="left")
            for entry in SETTINGS_SCHEMA:
                if entry[0] == key and len(entry) > 4 and entry[4]:
                    Tooltip(lbl, entry[4])
                    break

            if isinstance(var, tk.BooleanVar):
                RoundedCheckbox(
                    row, text="", variable=var,
                    checked_color=p["accent"],
                    unchecked_color=p["surface_3"],
                    text_color=p["fg_dim"],
                    canvas_bg=p["surface"]).pack(side="right")
            elif isinstance(var, list):
                cell = tk.Frame(row, bg=p["surface"],
                                highlightthickness=0)
                cell.pack(side="right")
                if unit:
                    tk.Label(cell, text=unit, fg=p["fg_muted"],
                             bg=p["surface"],
                             font=(ui_font(), 8)).pack(
                                 side="right", padx=(4, 0))
                for sv in var:
                    ttk.Entry(cell, textvariable=sv, width=6).pack(
                        side="left", padx=(0, 4))

    def _sp_build_custom_buttons(self, parent):
        """Custom buttons accordion in the settings panel."""
        p = self.theme
        outer = RoundedSection(
            parent, title="", bg=p["surface"],
            border_color=p["border"], radius=6,
            parent_bg=p["bg"])
        outer.pack(fill="x", padx=8, pady=2)
        header = tk.Frame(outer.inner, bg=p["surface"],
                          cursor="hand2", highlightthickness=0)
        header.pack(fill="x")

        icon_char, _ = self._SP_ICONS.get("Custom buttons", ("⊕", None))
        tk.Label(header, text=icon_char, font=(ui_font(), 10),
                 fg=p["accent"], bg=p["surface"], width=3,
                 cursor="hand2").pack(side="left", padx=(0, 4))
        title_f = tk.Frame(header, bg=p["surface"],
                           cursor="hand2", highlightthickness=0)
        title_f.pack(side="left", fill="x", expand=True)
        tk.Label(title_f, text="Custom buttons",
                 font=(ui_font(), 9, "bold"), fg=p["fg"],
                 bg=p["surface"], cursor="hand2").pack(anchor="w")
        tk.Label(title_f, text="Press-it-when-you-see-it taps",
                 fg=p["fg_muted"], bg=p["surface"],
                 font=(ui_font(), 8),
                 cursor="hand2").pack(anchor="w")
        chev = tk.Label(header, text="▸", width=2, cursor="hand2",
                        font=(ui_font(), 10), fg=p["fg_dim"],
                        bg=p["surface"])
        chev.pack(side="right")

        body = tk.Frame(outer.inner, bg=p["surface"],
                        highlightthickness=0)

        def toggle(e=None):
            if body.winfo_ismapped():
                body.pack_forget()
                chev.configure(text="▸")
                self._sp_custom_btns_body = None
            else:
                body.pack(fill="x", padx=(16, 0), pady=(4, 4))
                chev.configure(text="▾")
                self._sp_custom_btns_body = body
                self._render_custom_buttons_in(body)

        for w in (header,) + tuple(header.winfo_children()):
            w.bind("<Button-1>", toggle)

    def _render_custom_buttons(self):
        """Refresh custom buttons in the settings panel if it is open."""
        body = getattr(self, "_sp_custom_btns_body", None)
        if body is None:
            return
        try:
            if not body.winfo_exists():
                self._sp_custom_btns_body = None
                return
        except tk.TclError:
            self._sp_custom_btns_body = None
            return
        self._render_custom_buttons_in(body)

    def _render_custom_buttons_in(self, body):
        """Build the custom button list + capture buttons inside a body frame."""
        for child in body.winfo_children():
            child.destroy()
        self._custom_thumb_refs = []

        paths = sorted(config.REF_CUSTOM_DIR.glob("*.png"))
        if not paths:
            ttk.Label(body, text="No custom buttons yet.",
                      foreground="#888", font=("", 8, "italic")).pack(
                          anchor="w", pady=4)
        else:
            grid = ttk.Frame(body)
            grid.pack(anchor="w", pady=4)
            cols = 4
            for i, path in enumerate(paths):
                cell = self._custom_button_cell(grid, path)
                cell.grid(row=i // cols, column=i % cols, padx=3, pady=3)

        btn_row = ttk.Frame(body)
        btn_row.pack(fill="x", pady=(4, 2))
        add_btn = ttk.Button(btn_row, text="+ Add button",
                              command=self._add_custom_button)
        add_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._coord_buttons.append(add_btn)
        ref_btn = ttk.Button(btn_row, text="Capture ref",
                              command=self._recapture_ref)
        ref_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))
        self._coord_buttons.append(ref_btn)

    def _sp_build_ref_images(self, parent):
        """Reference images accordion in the settings panel."""
        p = self.theme
        outer = RoundedSection(
            parent, title="", bg=p["surface"],
            border_color=p["border"], radius=6,
            parent_bg=p["bg"])
        outer.pack(fill="x", padx=8, pady=2)
        header = tk.Frame(outer.inner, bg=p["surface"],
                          cursor="hand2", highlightthickness=0)
        header.pack(fill="x")

        icon_char, _ = self._SP_ICONS.get("Reference images", ("⊞", None))
        tk.Label(header, text=icon_char, font=(ui_font(), 10),
                 fg=p["accent"], bg=p["surface"], width=3,
                 cursor="hand2").pack(side="left", padx=(0, 4))
        title_f = tk.Frame(header, bg=p["surface"],
                           cursor="hand2", highlightthickness=0)
        title_f.pack(side="left", fill="x", expand=True)
        tk.Label(title_f, text="Reference images",
                 font=(ui_font(), 9, "bold"), fg=p["fg"],
                 bg=p["surface"], cursor="hand2").pack(anchor="w")
        tk.Label(title_f, text="Recapture built-in templates",
                 fg=p["fg_muted"], bg=p["surface"],
                 font=(ui_font(), 8),
                 cursor="hand2").pack(anchor="w")
        chev = tk.Label(header, text="▸", width=2, cursor="hand2",
                        font=(ui_font(), 10), fg=p["fg_dim"],
                        bg=p["surface"])
        chev.pack(side="right")

        body = tk.Frame(outer.inner, bg=p["surface"],
                        highlightthickness=0)

        def toggle(e=None):
            if body.winfo_ismapped():
                body.pack_forget()
                chev.configure(text="▸")
            else:
                body.pack(fill="x", padx=(16, 0), pady=(4, 4))
                chev.configure(text="▾")
                for child in body.winfo_children():
                    child.destroy()
                row = tk.Frame(body, bg=p["surface"],
                               highlightthickness=0)
                row.pack(fill="x", pady=2)
                tk.Label(row, text="Built-in references",
                         fg=p["fg_dim"], bg=p["surface"],
                         font=(ui_font(), 9)).pack(side="left")
                btn = ttk.Button(row, text="Recapture",
                                  command=self._recapture_ref)
                btn.pack(side="right")
                self._coord_buttons.append(btn)

        for w in (header,) + tuple(header.winfo_children()):
            w.bind("<Button-1>", toggle)

    def _sp_build_movement_tuning(self, parent):
        """Movement tuning accordion in the settings panel (advanced)."""
        p = self.theme
        outer = RoundedSection(
            parent, title="", bg=p["surface"],
            border_color=p["border"], radius=6,
            parent_bg=p["bg"])
        outer.pack(fill="x", padx=8, pady=2)
        header = tk.Frame(outer.inner, bg=p["surface"],
                          cursor="hand2", highlightthickness=0)
        header.pack(fill="x")

        icon_char, advanced = self._SP_ICONS.get("Movement tuning",
                                                   ("↗", "advanced"))
        icon_color = p["warn"] if advanced else p["accent"]
        tk.Label(header, text=icon_char, font=(ui_font(), 10),
                 fg=icon_color, bg=p["surface"], width=3,
                 cursor="hand2").pack(side="left", padx=(0, 4))
        title_f = tk.Frame(header, bg=p["surface"],
                           cursor="hand2", highlightthickness=0)
        title_f.pack(side="left", fill="x", expand=True)
        tk.Label(title_f, text="Movement tuning",
                 font=(ui_font(), 9, "bold"), fg=p["fg"],
                 bg=p["surface"], cursor="hand2").pack(anchor="w")
        tk.Label(title_f, text="Chapter, Plant & Custom vectors",
                 fg=p["fg_muted"], bg=p["surface"],
                 font=(ui_font(), 8),
                 cursor="hand2").pack(anchor="w")
        chev = tk.Label(header, text="▸", width=2, cursor="hand2",
                        font=(ui_font(), 10), fg=p["fg_dim"],
                        bg=p["surface"])
        chev.pack(side="right")

        body = tk.Frame(outer.inner, bg=p["surface"],
                        highlightthickness=0)

        def _build_body():
            for child in body.winfo_children():
                child.destroy()

            ttk.Label(body, foreground="#888", wraplength=380, justify="left",
                      font=("", 8),
                      text="Joystick starts bottom-centre; each vector swipes "
                           "~10% of screen height at the angle, held for the "
                           "duration. Set duration 0 to skip."
                      ).pack(anchor="w", pady=(0, 6))

            # Chapter vectors
            ttk.Label(body, text="Chapter movement",
                      font=("", 9, "bold")).pack(anchor="w", pady=(4, 2))
            self._sp_mvmt_fields(body, "MOVEMENT_CHAPTER")
            ttk.Separator(body).pack(fill="x", pady=6)

            # Custom vectors
            ttk.Label(body, text="Custom movement",
                      font=("", 9, "bold")).pack(anchor="w", pady=(4, 2))
            self._sp_mvmt_fields(body, "MOVEMENT_CUSTOM")
            ttk.Separator(body).pack(fill="x", pady=6)

            # Plant T-scale
            ttk.Label(body, text="Plant Defense",
                      font=("", 9, "bold")).pack(anchor="w", pady=(4, 2))
            t_row = ttk.Frame(body)
            t_row.pack(fill="x", pady=2)
            lbl = ttk.Label(t_row, text="T (time scale)")
            lbl.pack(side="left")
            Tooltip(lbl, "Seconds per unit. All Plant Defense durations are "
                         "T-multiples, so adjusting T scales everything.")
            cell = ttk.Frame(t_row)
            cell.pack(side="right")
            ttk.Label(cell, text="s/unit", foreground="#888").pack(
                side="right", padx=(4, 0))
            ttk.Entry(cell, textvariable=self.movement_plant_t_var,
                      width=6).pack(side="left")

            # Test spawn + conduct
            spawn_row = ttk.Frame(body)
            spawn_row.pack(fill="x", pady=(6, 2))
            slbl = ttk.Label(spawn_row, text="Test spawn:")
            slbl.pack(side="left")
            Tooltip(slbl, "Spawn to simulate for Conduct Movement. In a real "
                          "run the side comes from the Spawn Side toggle in "
                          "the Plant Defense panel.")
            for val, text in ((1, "1 Left"), (2, "2 Right")):
                ttk.Radiobutton(spawn_row, text=text,
                                variable=self.movement_plant_spawn_var,
                                value=val).pack(side="left", padx=(6, 0))
            self._conduct_btn = ttk.Button(
                body, text="Conduct Movement",
                command=self._conduct_plant_movement)
            self._conduct_btn.pack(anchor="w", pady=(4, 2))
            Tooltip(self._conduct_btn,
                    "Run movement for the selected test spawn immediately.")

        def toggle(e=None):
            if body.winfo_ismapped():
                body.pack_forget()
                chev.configure(text="▸")
            else:
                body.pack(fill="x", padx=(16, 0), pady=(4, 4))
                chev.configure(text="▾")
                _build_body()

        for w in (header,) + tuple(header.winfo_children()):
            w.bind("<Button-1>", toggle)

    def _sp_mvmt_fields(self, parent, config_key):
        """Build Move 1 / Move 2 angle+duration rows in the settings panel."""
        vars_list = self.vars[config_key]
        for i, label in enumerate(("Move 1", "Move 2")):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label).pack(side="left", padx=(8, 8))
            ttk.Label(row, text="Angle", foreground="#888").pack(side="left")
            ttk.Entry(row, textvariable=vars_list[i * 2], width=6).pack(
                side="left", padx=(4, 2))
            ttk.Label(row, text="°", foreground="#888").pack(
                side="left", padx=(0, 10))
            ttk.Label(row, text="Duration", foreground="#888").pack(
                side="left")
            ttk.Entry(row, textvariable=vars_list[i * 2 + 1], width=6).pack(
                side="left", padx=(4, 2))
            ttk.Label(row, text="s", foreground="#888").pack(side="left")

    # ------------------------------------------------------------------ settings load/save
    def _load_settings_into_form(self):
        for entry in SETTINGS_SCHEMA:
            if entry[0] in ("section", "button"):
                continue
            key, _label, kind, _unit, _hint = entry[:5]
            value = getattr(config, key)
            var = self.vars[key]
            if kind == "bool":
                var.set(bool(value))
            elif kind in ("float", "int"):
                var[0].set(_fmt(value))
            else:
                seq = list(value) if value is not None else []
                for i, box in enumerate(var):
                    box.set(_fmt(seq[i]) if i < len(seq) else "")

        # Game mode: infer from legacy settings if GAME_MODE is missing
        gm = config.GAME_MODE
        if not gm or gm not in _GM_ID_TO_NAME:
            if config.ETERNAL_LODE_MODE:
                gm = "eternal"
            elif config.MOVEMENT_MODE == 2:
                gm = "plant"
            else:
                gm = "chapter"
        self.game_mode_var.set(_GM_ID_TO_NAME.get(gm, "Chapter"))

        # Chapter sub-choice
        self.chapter_move_var.set(
            "timed" if config.MOVEMENT_MODE == 1 else "dontmove")

        # Eternal Lode options
        self._el_fast_var.set(config.EL_FAST_MODE)

        # All-Star level + Elim picks
        self.allstar_level_var.set(self._allstar_level_label())
        self.elim_bosses = self._normalized_elim_picks()

        # Plant direction + spawn side + round limit
        self.plant_dir_var.set(config.MOVEMENT_PLANT_PRESET)
        self.plant_spawn_bool.set(config.PLANT_SPAWN == 2)
        self.plant_rounds_var.set(str(config.PLANT_ROUNDS))

        # Movement vector fields
        for key in ("MOVEMENT_CHAPTER", "MOVEMENT_CUSTOM"):
            seq = list(getattr(config, key))
            for i, box in enumerate(self.vars[key]):
                box.set(_fmt(seq[i]) if i < len(seq) else "")
        self.movement_plant_t_var.set(_fmt(config.MOVEMENT_PLANT_T))

        self._update_game_mode_disclosure()

    def _collect_settings(self, quiet=False):
        """Build a settings dict from the form, or None if a field is bad."""
        out = {}
        label = ""
        try:
            for entry in SETTINGS_SCHEMA:
                if entry[0] in ("section", "button"):
                    continue
                key, label, kind, _unit, _hint = entry[:5]
                var = self.vars[key]
                if kind == "bool":
                    out[key] = bool(var.get())
                elif kind == "float":
                    out[key] = float(var[0].get())
                elif kind == "int":
                    out[key] = int(float(var[0].get()))
                elif kind == "floats3":
                    out[key] = [float(b.get()) for b in var]
                else:  # ints2 / ints4
                    out[key] = [int(float(b.get())) for b in var]
        except ValueError:
            if not quiet:
                messagebox.showerror(
                    "Invalid setting",
                    f"Could not read a number for '{label}'. Please check it.")
            return None

        out["ACTIVE_CATEGORIES"] = list(self.active)
        out["CUSTOM_PRIORITY_SKILLS"] = list(self.custom)
        out["AVOID_SKILLS"] = list(self.avoid)
        out["AUTOSAVE"] = bool(self.autosave_var.get())
        out["DARK_MODE"] = bool(self.dark_var.get())
        out["KEEP_AWAKE"] = bool(self.keep_awake_var.get())
        out["HOTKEY"] = config.HOTKEY

        # Game mode -> config keys
        gm = self._game_mode_id()
        out["GAME_MODE"] = gm
        out["ETERNAL_LODE_MODE"] = (gm == "eternal")
        out["EL_FAST_MODE"] = bool(self._el_fast_var.get())
        try:
            val = self.allstar_level_var.get()
            out["ALL_STAR_LEVEL"] = ("elim" if val.strip().lower() == "elim"
                                     else int(val))
        except (ValueError, AttributeError):
            out["ALL_STAR_LEVEL"] = config.ALL_STAR_LEVEL
        out["ALL_STAR_ELIM_BOSSES"] = list(self.elim_bosses)

        if gm == "chapter":
            out["MOVEMENT_MODE"] = (
                1 if self.chapter_move_var.get() == "timed" else 0)
        elif gm == "plant":
            out["MOVEMENT_MODE"] = 2
            out["MOVEMENT_PLANT_PRESET"] = self.plant_dir_var.get()
            out["PLANT_SPAWN"] = 2 if self.plant_spawn_bool.get() else 1
            # Mid-typing values ("" while editing) must not block a save;
            # fall back to the current setting instead of erroring.
            try:
                out["PLANT_ROUNDS"] = max(
                    0, int(float(self.plant_rounds_var.get())))
            except ValueError:
                out["PLANT_ROUNDS"] = config.PLANT_ROUNDS
        else:
            out["MOVEMENT_MODE"] = 0

        try:
            for key in ("MOVEMENT_CHAPTER", "MOVEMENT_CUSTOM"):
                out[key] = [float(b.get()) for b in self.vars[key]]
            out["MOVEMENT_PLANT_T"] = float(self.movement_plant_t_var.get())
        except ValueError:
            if not quiet:
                messagebox.showerror(
                    "Invalid setting",
                    "Could not read a number for a Movement field.")
            return None
        return out

    def _apply_and_save(self, quiet=False):
        data = self._collect_settings(quiet=quiet)
        if data is None:
            return False
        config.apply_settings(data)
        config.save_settings()
        return True

    def _save(self):
        if self._apply_and_save():
            main.log("settings saved to settings.json")

    # autosave
    def _on_autosave_toggle(self):
        on = self.autosave_var.get()
        main.log("autosave on, settings save after every change"
                 if on else "autosave off")
        self._apply_and_save(quiet=True)

    def _on_keep_awake_toggle(self):
        on = self.keep_awake_var.get()
        _set_keep_awake(on)
        main.log("keep awake on, PC will not sleep or dim"
                 if on else "keep awake off")
        self._apply_and_save(quiet=True)

    # hotkey
    def _start_hotkey_watcher(self):
        """Start (or restart) the global Start/Stop hotkey listener."""
        if self._hotkey_watcher is not None:
            self._hotkey_watcher.stop()
        self._hotkey_watcher = _HotkeyWatcher(
            config.HOTKEY, self._on_hotkey_pressed)
        self._hotkey_watcher.start()

    def _on_hotkey_pressed(self):
        """Fires from the pynput thread; schedule toggle on the UI thread."""
        try:
            self.root.after(0, self._toggle_macro)
        except tk.TclError:
            pass

    def _toggle_macro(self):
        """Toggle the macro between running and stopped."""
        if self._running:
            self._stop()
        else:
            self._start()

    def _open_hotkey_dialog(self):
        """Open a small dialog to set the global Start/Stop hotkey."""
        # Pause the watcher while the dialog captures keys.
        if self._hotkey_watcher is not None:
            self._hotkey_watcher.stop()

        p = self.theme
        win = tk.Toplevel(self.root)
        win.title("Set Hotkey")
        win.geometry("320x200")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        win.configure(background=p["bg"])
        set_dark_titlebar(win, self.dark)

        tk.Label(win, text="Set Start / Stop Hotkey",
                 font=(ui_font(), 11, "bold"),
                 fg=p["fg"], bg=p["bg"]).pack(pady=(16, 4))
        tk.Label(win, text="Click here first, then press a new key combination:",
                 fg=p["fg_dim"], bg=p["bg"],
                 font=(ui_font(), 9)).pack(pady=(8, 4))

        display_var = tk.StringVar(value=_hotkey_display(config.HOTKEY))
        tk.Label(win, textvariable=display_var,
                 font=(ui_font(), 14, "bold"),
                 fg=p["accent"], bg=p["bg"]).pack(pady=(4, 12))

        captured = [None]

        def on_key(event):
            mods = []
            if event.state & 0x4:
                mods.append('ctrl')
            if event.state & 0x20000:
                mods.append('alt')
            if event.state & 0x1:
                mods.append('shift')
            keysym = event.keysym
            if keysym in ('Control_L', 'Control_R', 'Alt_L', 'Alt_R',
                          'Shift_L', 'Shift_R'):
                return
            if not mods:
                return
            key = _KEYSYM_TO_KEY.get(keysym, keysym.lower())
            combo = '+'.join(mods + [key])
            captured[0] = combo
            display_var.set(_hotkey_display(combo))

        win.bind("<KeyPress>", on_key)

        btn_row = tk.Frame(win, bg=p["bg"], highlightthickness=0)
        btn_row.pack(fill="x", padx=16, pady=(0, 12))

        def _close():
            self._start_hotkey_watcher()
            win.destroy()

        def _apply():
            combo = captured[0] if captured[0] is not None else config.HOTKEY
            config.HOTKEY = combo
            self._hotkey_tip.text = (
                f"Start / Stop hotkey: {_hotkey_display(combo)}. "
                f"Click to change.")
            self._start_hotkey_watcher()
            self._schedule_autosave()
            main.log(f"hotkey set to {_hotkey_display(combo)}")
            win.destroy()

        def _reset():
            captured[0] = "ctrl+`"
            display_var.set(_hotkey_display("ctrl+`"))

        tk.Button(btn_row, text="Reset", command=_reset,
                  font=(ui_font(), 9), relief="flat", cursor="hand2",
                  bg=p["surface_2"], fg=p["fg_dim"],
                  activebackground=p["surface_3"],
                  activeforeground=p["fg"], padx=8, pady=4).pack(side="left")

        tk.Button(btn_row, text="Cancel", command=_close,
                  font=(ui_font(), 9), relief="flat", cursor="hand2",
                  bg=p["surface_2"], fg=p["fg_dim"],
                  activebackground=p["surface_3"],
                  activeforeground=p["fg"], padx=8, pady=4).pack(side="right")

        tk.Button(btn_row, text="Apply", command=_apply,
                  font=(ui_font(), 9), relief="flat", cursor="hand2",
                  bg=p["accent"], fg="#ffffff",
                  activebackground=p["accent"],
                  activeforeground="#ffffff", padx=8, pady=4).pack(
                      side="right", padx=(0, 6))

        win.protocol("WM_DELETE_WINDOW", _close)

    def _wire_autosave_traces(self):
        for var in self.vars.values():
            for v in (var if isinstance(var, list) else [var]):
                v.trace_add("write", lambda *_: self._schedule_autosave())

    def _schedule_autosave(self):
        if not self.autosave_var.get():
            return
        if self._autosave_after is not None:
            try:
                self.root.after_cancel(self._autosave_after)
            except tk.TclError:
                pass
        self._autosave_after = self.root.after(500, self._do_autosave)

    def _do_autosave(self):
        self._autosave_after = None
        if self.autosave_var.get():
            self._apply_and_save(quiet=True)

    # ADB capture helpers
    def _set_coord_buttons(self, state):
        for btn in self._coord_buttons:
            try:
                btn.config(state=state)
            except tk.TclError:
                pass

    def _start_picker(self):
        self._coords_busy = True
        self._set_coord_buttons("disabled")

    def _finish_picker(self):
        self._coords_busy = False
        self._set_coord_buttons("normal")

    # run / stop
    def _start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        gm = self._game_mode_id()
        if gm not in ("eternal", "allstar") and not self.active and not self.custom:
            if not messagebox.askyesno(
                    "Nothing to pick",
                    "No skill categories are active and no custom priority "
                    "skills are set, so the macro will always take a skill "
                    "slot.\n\nStart anyway?"):
                return
        if not self._apply_and_save():
            return

        # The macro creates its own capture stream; release the GUI one so two
        # screenrecords don't run on the same device.
        self._stop_capture_stream()

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._set_running(True)
        self._thread.start()

    def _run_thread(self):
        gm = self._game_mode_id()
        if gm == "eternal":
            runner = eternal_lode.run_eternal_lode
        elif gm == "allstar":
            runner = all_star.run_all_star
        else:
            runner = main.run_macro
        try:
            runner(self._stop_event)
        except Exception as e:
            main.log(f"ERROR: macro crashed; {e!r}")
        finally:
            try:
                self.root.after(0, lambda: self._set_running(False))
            except tk.TclError:
                pass

    def _stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._running:
            self.status_var.set("Stopping…")

    def _set_running(self, running):
        self._running = running
        if running:
            self.start_btn.pack_forget()
            self.stop_btn.pack(side="left", before=self._status_pill)
            self.stop_btn.configure(state="normal")
        else:
            self.stop_btn.pack_forget()
            self.start_btn.pack(side="left", before=self._status_pill)
            self.start_btn.configure(state="normal")
        self.status_var.set("Running" if running else "Stopped")
        if running:
            self._start_status_pulse()
        else:
            self._stop_status_pulse()
        if self._compact:
            self._sync_compact_state()

    # window lock
    def _toggle_lock(self):
        self._set_locked(self.lock_var.get())

    def _set_locked(self, locked):
        self._locked = locked
        self.lock_var.set(locked)
        self.root.attributes("-topmost", locked)
        self.root.resizable(not locked, not locked)
        if locked:
            self.root.update_idletasks()
            self._locked_geometry = self.root.geometry()
        main.log("GUI locked, window pinned on top"
                 if locked else "GUI unlocked")

    def _on_configure(self, event):
        if not self._locked or event.widget is not self.root:
            return
        if (self._locked_geometry is not None
                and self.root.geometry() != self._locked_geometry):
            self.root.geometry(self._locked_geometry)

    # log (collapsible)
    def _build_log(self, parent):
        self._log_section = RoundedSection(
            parent, title="",
            bg=self.theme["surface"],
            border_color=self.theme["border"],
            title_fg=self.theme["fg"])
        self._log_section.pack(fill="both", expand=True, padx=8,
                               pady=(0, 8))
        frame = self._log_section.inner

        bar = tk.Frame(frame, bg=self.theme["surface"],
                       highlightthickness=0)
        bar.pack(fill="x", pady=(0, 4))

        self._log_toggle_btn = tk.Label(
            bar, text="▼", font=(ui_font(), 9), cursor="hand2",
            fg=self.theme["fg_dim"], bg=self.theme["surface"])
        self._log_toggle_btn.pack(side="left")
        self._log_toggle_btn.bind("<Button-1>",
                                   lambda e: self._toggle_log())

        tk.Label(bar, text="Log", font=(ui_font(), 10, "bold"),
                 fg=self.theme["fg"], bg=self.theme["surface"]
                 ).pack(side="left", padx=(6, 0))

        self._log_count_label = tk.Label(
            bar, text="0 lines", font=(ui_font(), 8),
            fg=self.theme["fg_muted"], bg=self.theme["surface"])
        self._log_count_label.pack(side="left", padx=(8, 0))

        self._log_clear_btn = tk.Label(
            bar, text="Clear", font=(ui_font(), 9), cursor="hand2",
            fg=self.theme["fg_dim"], bg=self.theme["surface"])
        self._log_clear_btn.pack(side="right")
        self._log_clear_btn.bind("<Button-1>",
                                  lambda e: self._clear_log())

        self._log_body = tk.Frame(frame, bg=self.theme["surface"],
                                  highlightthickness=0)
        self._log_body.pack(fill="both", expand=True)

        # tk.Text + ttk.Scrollbar (not scrolledtext) so the scrollbar follows
        # the ttk TScrollbar style and matches the dark settings scrollbar,
        # instead of the bright native one ScrolledText ships with.
        log_wrap = tk.Frame(self._log_body, bg=self.theme["inset"],
                            highlightthickness=0)
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_wrap, height=8, width=20, state="disabled",
            wrap="word", font=("Consolas", 9),
            background=self.theme["inset"],
            foreground=self.theme["fg_dim"],
            insertbackground=self.theme["fg_dim"],
            relief="flat", borderwidth=0)
        log_vsb = ttk.Scrollbar(log_wrap, orient="vertical",
                                command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_vsb.set)
        log_vsb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        p = self.theme
        self.log_text.tag_configure("timestamp", foreground=p["fg_muted"])
        self.log_text.tag_configure("ok", foreground=p["ok"])
        self.log_text.tag_configure("warn", foreground=p["warn"])
        self.log_text.tag_configure("err", foreground=p["bad"])

    def _toggle_log(self):
        self._log_open = not self._log_open
        if self._log_open:
            self._log_body.pack(fill="both", expand=True)
            self._log_toggle_btn.configure(text="▼")
            try:
                self.log_text.see("end")
            except tk.TclError:
                pass
        else:
            self._log_body.pack_forget()
            self._log_toggle_btn.configure(text="▶")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._log_line_count = 0
        self._log_count_label.configure(text="0 lines")

    def _drain_log(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                self.log_text.config(state="normal")
                tag = self._log_line_tag(line)
                # Insert timestamp portion with muted colour
                ts_end = 0
                stripped = line.lstrip()
                if len(stripped) >= 8 and stripped[2] == ":" and stripped[5] == ":":
                    ts_end = line.index(stripped) + 8
                if ts_end > 0:
                    start = self.log_text.index("end-1c")
                    self.log_text.insert("end", line[:ts_end], "timestamp")
                    self.log_text.insert("end", line[ts_end:] + "\n", tag)
                else:
                    self.log_text.insert("end", line + "\n", tag)
                self.log_text.see("end")
                self.log_text.config(state="disabled")
                self._log_line_count += 1
                self._log_count_label.configure(
                    text=f"{self._log_line_count} lines")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    @staticmethod
    def _log_line_tag(line):
        low = line.lower()
        if any(k in low for k in ("started", "picked", "connected", "▶")):
            return "ok"
        if any(k in low for k in ("avoided", "stopping", "■", "warn")):
            return "warn"
        if any(k in low for k in ("stopped.", "error", "failed", "fatal")):
            return "err"
        return ""

    # ------------------------------------------------------------------ compact mode
    def _build_compact_view(self, parent):
        """Build the minimal mini view: a big Start/Stop with a small Expand
        icon beside it, then game-mode + timeout. The run status lives in the OS
        title bar (see _update_compact_title), so there is no in-window status
        text, LED, or connection label -- the window is just the controls."""
        parent.columnconfigure(0, weight=1)
        pad = 12

        # Row 1: Start/Stop fills the width; a small Expand icon (no text) sits
        # to its right at the same height. (Mini was otherwise a trap -- the
        # topbar with the Mini toggle is hidden in compact.)
        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", padx=pad, pady=(pad, 8))
        self._compact_expand = tk.Button(
            btn_row, text="⤢", command=self._toggle_compact,
            font=(ui_font(), 13), relief="flat", cursor="hand2", bd=0,
            highlightthickness=0, width=2,
            background=self.theme["surface_2"],
            foreground=self.theme["fg_dim"],
            activebackground=self.theme["surface_3"],
            activeforeground=self.theme["fg"])
        self._compact_expand.pack(side="right", fill="y", padx=(8, 0))
        Tooltip(self._compact_expand, "Back to the full window")
        self._compact_start = tk.Button(
            btn_row, text="▶  Start", command=self._start,
            font=(ui_font(), 12, "bold"), foreground="#ffffff",
            background=self.theme["ok"], activebackground=self.theme["ok"],
            activeforeground="#ffffff", relief="flat", cursor="hand2",
            bd=0, highlightthickness=0, height=2)
        self._compact_start.pack(side="left", fill="both", expand=True)
        self._compact_stop = tk.Button(
            btn_row, text="■  Stop", command=self._stop,
            font=(ui_font(), 12, "bold"), foreground="#ffffff",
            background=self.theme["bad"], activebackground=self.theme["bad"],
            activeforeground="#ffffff", relief="flat", cursor="hand2",
            bd=0, highlightthickness=0, height=2, state="disabled")

        # Row 2: game-mode selector (no label) + timeout entry + "hrs".
        row2 = ttk.Frame(parent)
        row2.pack(fill="x", padx=pad, pady=(0, pad))
        self._compact_gm = ttk.Combobox(
            row2, textvariable=self.game_mode_var,
            values=_GM_NAMES, state="readonly", width=15)
        self._compact_gm.pack(side="left", fill="x", expand=True)
        self._compact_gm.bind("<<ComboboxSelected>>",
                               self._on_game_mode_changed)
        ttk.Label(row2, text="hrs",
                  foreground=self.theme.get("fg_muted", "#888")).pack(
            side="right", padx=(4, 0))
        ttk.Entry(row2, textvariable=self.vars["RUN_TIMEOUT_HOURS"][0],
                  width=5).pack(side="right", padx=(8, 0))

    def _toggle_compact(self):
        """Switch between full and compact (mini) views."""
        self._compact = not self._compact
        if self._compact:
            self._topbar.pack_forget()
            self._topbar_sep.pack_forget()
            self._body.pack_forget()
            self._compact_frame.pack(fill="both", expand=True)
            self._saved_geometry = self.root.geometry()
            self._sync_compact_state()       # also sets the status title
            # Shrink-to-fit: drop the full-view minsize, let Tk size the window
            # to exactly the mini content, then lock the minimum there so it
            # opens as small as the controls allow (not the old fixed 392x280).
            self.root.minsize(1, 1)
            self.root.update_idletasks()
            self.root.geometry("")
            self.root.update_idletasks()
            self.root.minsize(self.root.winfo_reqwidth(),
                              self.root.winfo_reqheight())
        else:
            self._compact_frame.pack_forget()
            self._topbar.pack(fill="x", padx=0, pady=0)
            self._topbar_sep.pack(fill="x")
            self._body.pack(fill="both", expand=True,
                            padx=10, pady=(6, 10))
            self.root.title(f"A2 Macro Controller v{config.VERSION}")
            self.root.minsize(1180, 620)
            if hasattr(self, "_saved_geometry"):
                self.root.geometry(self._saved_geometry)
            else:
                self.root.geometry("1280x720")

    def _sync_compact_state(self):
        """Reflect run state in the mini view (Start<->Stop) + the title bar."""
        running = self._running
        p = self.theme
        if hasattr(self, "_compact_start"):
            if running:
                self._compact_start.pack_forget()
                self._compact_stop.pack(side="left", fill="both", expand=True)
                self._compact_stop.config(state="normal")
            else:
                self._compact_stop.pack_forget()
                self._compact_start.pack(side="left", fill="both", expand=True)
                self._compact_start.config(state="normal")
            self._compact_start.configure(
                background=p["ok"], activebackground=p["ok"])
            self._compact_stop.configure(
                background=p["bad"], activebackground=p["bad"])
        if getattr(self, "_compact_expand", None) is not None:
            self._compact_expand.configure(
                background=p["surface_2"], foreground=p["fg_dim"],
                activebackground=p["surface_3"], activeforeground=p["fg"])
        self._update_compact_title()

    def _update_compact_title(self):
        """In mini mode the OS title bar carries the run status (no in-window
        status text), e.g. 'A2 Macro · Running' / '· Stopped'."""
        if self._compact:
            try:
                self.root.title(f"A2 Macro · {self.status_var.get()}")
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ status LED pulse
    def _start_status_pulse(self):
        """Begin pulsing the status dot glow while running."""
        if hasattr(self, "_status_pill"):
            self._status_pill.start_pulse()

    def _stop_status_pulse(self):
        if hasattr(self, "_status_pill"):
            self._status_pill.stop_pulse()
        self._update_status_color()

    # shutdown
    def _on_close(self):
        if self._autosave_after is not None:
            try:
                self.root.after_cancel(self._autosave_after)
            except tk.TclError:
                pass
        if self.autosave_var.get():
            self._apply_and_save(quiet=True)
        if self._stop_event is not None:
            self._stop_event.set()
        _set_keep_awake(False)
        self._stop_capture_stream()
        if self._hotkey_watcher is not None:
            self._hotkey_watcher.stop()
        main.set_log_sink(None)
        self.root.destroy()


class _Splash:
    """Borderless centred loading window shown while the main window is built
    and all skill thumbnails are warmed, so the user never sees a half-drawn
    GUI populate piece by piece."""

    def __init__(self, root, dark):
        theme = _THEMES["dark" if dark else "light"]
        self._theme = theme
        win = tk.Toplevel(root)
        self._win = win
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        w, h = 380, 170
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        win.configure(background=theme["border_strong"])

        frame = tk.Frame(win, background=theme["surface"], highlightthickness=0)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(frame, text="A2 Macro Controller",
                 font=(ui_font(), 15, "bold"),
                 fg=theme["fg"], bg=theme["surface"]).pack(pady=(38, 4))
        self._msg = tk.Label(frame, text="Loading…", font=(ui_font(), 9),
                             fg=theme["fg_muted"], bg=theme["surface"])
        self._msg.pack()
        self._barw = w - 80
        self._bar = tk.Canvas(frame, height=6, width=self._barw,
                              highlightthickness=0, bg=theme["surface_3"],
                              bd=0)
        self._bar.pack(pady=(16, 0))

    def set_progress(self, done, total, msg=None):
        try:
            if msg:
                self._msg.configure(text=msg)
            frac = (done / total) if total else 1.0
            self._bar.delete("all")
            draw_rounded_rect(self._bar, 0, 0,
                              max(3, int(self._barw * frac)), 6, 3,
                              fill=self._theme["accent"], outline="")
            self._win.update_idletasks()
        except tk.TclError:
            pass

    def close(self):
        try:
            self._win.destroy()
        except tk.TclError:
            pass


def main_gui():
    root = tk.Tk()
    root.title(f"A2 Macro Controller v{config.VERSION}")
    root.geometry("1280x720")
    root.minsize(1180, 620)
    root.withdraw()                       # stay hidden until fully built

    dark = bool(config.DARK_MODE)
    splash = _Splash(root, dark)
    root.update()                         # paint the splash before the heavy work

    app = App(root)
    try:
        app.preload_assets(progress=splash.set_progress)
    except Exception:
        pass
    splash.close()

    root.deiconify()
    set_dark_titlebar(root, app.dark)
    root.lift()
    root.mainloop()


if __name__ == "__main__":
    main_gui()
