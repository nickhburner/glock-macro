"""
Graphical control panel for A2 Macro Controller.

Settings are saved to settings.json (next to config.py), which main.py and
diagnose.py also read. The macro runs in a background thread so the window
stays responsive.

Usage:
    python gui.py
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from PIL import Image, ImageTk

import config
import eternal_lode
import getcoords
import main
import region_picker
from matcher import (
    grab_screen_bgr,
    list_skill_files,
    rescale_image,
    save_image,
    skill_hash,
)

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
        "bg": "#f0f0f0", "panel": "#e1e1e1", "fg": "#1a1a1a",
        "border": "#c0c0c0", "button": "#e1e1e1", "button_active": "#d0d0d0",
        "entry_bg": "#ffffff", "entry_fg": "#1a1a1a",
        "cell_border": "#cfcfcf", "log_bg": "#ffffff", "log_fg": "#1a1a1a",
        "ok": "#2e9e44", "bad": "#c0392b", "warn": "#b9770e",
    },
    "dark": {
        "bg": "#2d2d30", "panel": "#3a3a3f", "fg": "#e4e4e4",
        "border": "#4a4a50", "button": "#3a3a3f", "button_active": "#4a4a52",
        "entry_bg": "#3c3c41", "entry_fg": "#f0f0f0",
        "cell_border": "#55555c", "log_bg": "#26262a", "log_fg": "#d6d6d6",
        "ok": "#4ec96a", "bad": "#e06c75", "warn": "#e0a458",
    },
}

# Status text -> palette key for its colour.
_STATUS_COLORS = {"Running": "ok", "Stopped": "bad", "Stopping…": "warn"}


# Settings form schema. Each entry is one of:
#   ("section", title)
#   ("button", label, App-method-name[, tooltip])
#   (config_key, label, kind, unit, hint[, capture])
# kind:
#   "float" / "int" / "bool"  single scalar
#   "floats3"                 three floats (min / max / step)
#   "ints2" / "ints4"         N integers
#   "region4"                 region edited as two corners (x1,y1,x2,y2) in the
#                             form, stored in config as (x,y,w,h)
# `capture` (optional) gives the field a "Pick" button: "point", "region" or
# "band".
SETTINGS_SCHEMA = [
    ("section", "Timing"),
    ("POLL_INTERVAL", "Poll interval", "float", "seconds",
     "How long the macro waits between screen checks."),
    ("ACTION_DELAY", "Action delay", "float", "seconds",
     "Pause after a click so the screen can settle before the next check."),
    ("STARTUP_DELAY", "Startup delay", "float", "seconds",
     "Grace period after pressing Start to focus the game window "
     "(BlueStacks or the scrcpy mirror)."),

    ("section", "Run timeout"),
    ("RUN_TIMEOUT_HOURS", "Run timeout", "float", "hours",
     "Stop the macro automatically after this long. Set to 0 to disable the "
     "timeout and run until stopped manually."),
    ("CLOSE_ON_TIMEOUT", "Close game window on timeout", "bool", "",
     "When the timeout fires, also close the game window, the BlueStacks "
     "emulator (and its background services) or the scrcpy mirror window."),
    ("SLEEP_PHONE_ON_TIMEOUT", "Sleep phone on timeout (scrcpy)", "bool", "",
     "When the timeout fires during a scrcpy session, turn the phone screen "
     "off via adb so the game pauses and the phone stops overheating. No "
     "effect with BlueStacks."),

    ("section", "Screen region & click targets"),
    ("button", "Get mouse coords", "_get_mouse_coords",
     "Wait 5 seconds, then print the current mouse position to the log, "
     "use it to read off coordinates."),
    ("BLUESTACKS_REGION", "Capture region", "region4",
     "px  (x1 / y1 / x2 / y2)",
     "Top-left and bottom-right screen corners of the game window the macro "
     "captures (BlueStacks or the scrcpy mirror). Click Pick, then click on "
     "the game window, a draggable rectangle appears over it. Drag its "
     "edges inward to crop to just the Android screen, then press ✓.",
     "region"),
    ("FIRST_SKILL_SLOT", "First skill slot", "ints2", "px  (x / y)",
     "Screen coords of the leftmost skill card. Taken as the fallback pick "
     "when no wanted skill is offered and rerolls run out. Click Pick to "
     "drag a bullseye onto the target.", "point"),
    ("SECOND_SKILL_SLOT", "Second skill slot", "ints2", "px  (x / y)",
     "Screen coords of the second skill card. Clicked instead of the first "
     "slot when the first slot is showing an avoid skill. Click Pick to drag "
     "a bullseye onto the target.", "point"),
    ("GAME_OVER_TAP", "Game-over tap", "ints2", "px  (x / y)",
     "Screen point tapped to dismiss the game-over / results screen. Click "
     "Pick to drag a bullseye onto the target.", "point"),
    ("SKILL_MATCH_BAND", "Skill match band", "ints2", "px  (top / bottom)",
     "Vertical band (relative to the region's top edge) the skill search is "
     "restricted to. Click Pick, then click on the game window, a "
     "horizontal band the width of the capture region appears. Drag its top "
     "and bottom to fit the skill row, then press ✓.",
     "band"),

    ("section", "Template matching"),
    ("MATCH_THRESHOLD", "Skill match threshold", "float", "0-1",
     "Minimum confidence to accept a skill-icon match. Higher is stricter, "
     "fewer false matches, but more misses."),
    ("REF_THRESHOLD", "UI match threshold", "float", "0-1",
     "Minimum confidence to accept a UI-element match (Play button, devil "
     "offer, game-over screen, refresh button)."),
    ("SKILL_DOWNSCALE", "Skill downscale", "float", "factor",
     "Skill matching runs at this fraction of full resolution for speed. "
     "1.0 = full res; 0.5 is roughly 4x faster. Lower is faster but less "
     "precise."),
    ("SCALE_RANGE", "Skill scale range", "floats3", "min / max / step",
     "Skill templates are matched resized across this range of scales. "
     "In-game skill icons are a fixed size, so this stays narrow, use "
     "Calibrate scale from screen to set it automatically."),
    ("REF_SCALE_RANGE", "UI scale range", "floats3", "min / max / step",
     "The scale range used when matching UI elements. Use Calibrate scale "
     "from screen to set it automatically."),
    ("button", "Calibrate scale from screen", "_calibrate_scale",
     "Capture the game window, ideally with an active skill-selection "
     "screen up, brute-force the on-screen scale, and fill Skill scale "
     "range and UI scale range to match. Run it whenever the game window "
     "size changes (e.g. switching between BlueStacks and scrcpy)."),

    ("section", "Humanisation"),
    ("CLICK_JITTER", "Click jitter", "int", "px",
     "Each click is nudged by up to this many random pixels, so input is not "
     "pixel-perfect."),
    ("DELAY_JITTER", "Delay jitter", "float", "fraction",
     "Poll and action delays vary randomly by +/- this fraction "
     "(0.35 means +/-35%)."),
]

# Entry boxes per non-bool field kind.
_KIND_WIDTHS = {"float": 1, "int": 1, "floats3": 3, "ints2": 2, "ints4": 4,
                "region4": 4}

# Tooltip text for the per-field "Pick" buttons, keyed by capture mode.
_CAPTURE_HINTS = {
    "point": "Drag a bullseye onto the target on screen, then press ✓ to "
             "save its position.",
    "region": "Click the game window, then drag the edges of the rectangle "
              "to crop the capture area. Press ✓ to save.",
    "band": "Click the game window, then drag the top and bottom of the "
            "rectangle to fit the skill row. Press ✓ to save.",
}


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


class App:
    def __init__(self, root):
        self.root = root
        self._thread = None
        self._stop_event = None
        self._running = False
        self._drag_cat = None
        self._coords_busy = False
        self._coord_buttons = []  # every coord-capture button (Pick + Get coords)
        self._autosave_after = None   # pending debounced-autosave `after` id
        self._picker = None
        self._picker_body = None
        self._picker_head = None
        self._picker_mode = "avoid"
        self._locked = False
        self._locked_geometry = None
        self._log_queue = queue.Queue()
        self.active_rows = {}     # category -> active-column row Frame
        self.vars = {}            # config key -> Tk var(s) for the form
        self._thumb_cache = {}    # (identifier, size) -> PhotoImage (or None)
        self._hash_cache = {}     # identifier -> content hash (or None)
        self._custom_thumb_refs = []   # live custom-button thumbnail refs

        # Category / custom-priority / avoid state from the saved config.
        self.active = [c for c in config.ACTIVE_CATEGORIES
                       if c in config.SKILL_CATEGORIES]
        self.custom = list(config.CUSTOM_PRIORITY_SKILLS)
        self.avoid = list(config.AVOID_SKILLS)

        # Colour scheme. clam (the most configurable built-in ttk theme) drives
        # both modes via the palette.
        self.dark = bool(config.DARK_MODE)
        self.theme = _THEMES["dark" if self.dark else "light"]
        self.style = ttk.Style()

        self._build_controls(root)

        # Two-column body: skill selection left, custom buttons / settings /
        # log right.
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="cols")
        body.columnconfigure(1, weight=1, uniform="cols")
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_skills(left)
        self._build_custom(left)
        self._build_avoid(left)
        self._build_custom_buttons(right)
        self._build_settings(right)
        self._build_log(right)

        self._load_settings_into_form()
        self._wire_autosave_traces()
        # Applies the colour scheme and does the initial render of the
        # colour-baked strips (skills / custom / avoid / custom buttons).
        self._apply_theme()

        main.set_log_sink(self._log_queue.put)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Escape>", lambda e: self._stop())
        root.bind("<Configure>", self._on_configure)
        self.root.after(120, self._drain_log)

    # controls
    def _build_controls(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=(8, 2))

        self.start_btn = ttk.Button(bar, text="▶  Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■  Stop", command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Save settings",
                   command=self._save).pack(side="left", padx=(6, 0))

        self.autosave_var = tk.BooleanVar(value=config.AUTOSAVE)
        autosave = ttk.Checkbutton(bar, text="Autosave",
                                   variable=self.autosave_var,
                                   command=self._on_autosave_toggle)
        autosave.pack(side="left", padx=(6, 0))
        Tooltip(autosave, "When on, settings are saved automatically a moment "
                          "after every change. When off, settings are saved "
                          "only when you press Save settings.")

        self.status_var = tk.StringVar(value="Stopped")
        self.status_label = ttk.Label(bar, textvariable=self.status_var,
                                      font=("", 10, "bold"))
        self.status_label.pack(side="right")
        # Recolour the status text whenever it changes.
        self.status_var.trace_add("write",
                                  lambda *_: self._update_status_color())

        self.lock_var = tk.BooleanVar(value=False)
        lock = ttk.Checkbutton(bar, text="Lock window", variable=self.lock_var,
                               command=self._toggle_lock)
        lock.pack(side="right", padx=(0, 14))
        Tooltip(lock, "Pin the window always-on-top and stop it being moved "
                      "or resized. Controls stay editable.")

        self.dark_var = tk.BooleanVar(value=self.dark)
        darkcb = ttk.Checkbutton(bar, text="Dark mode", variable=self.dark_var,
                                 command=self._toggle_theme)
        darkcb.pack(side="right", padx=(0, 14))
        Tooltip(darkcb, "Switch between the dark and light colour scheme.")

        # Eternal Lode mode toggle: routes Start to the minigame macro.
        # Persisted as config.ETERNAL_LODE_MODE.
        self.eternal_var = tk.BooleanVar(value=bool(config.ETERNAL_LODE_MODE))
        elcb = ttk.Checkbutton(bar, text="Eternal Lode",
                               variable=self.eternal_var,
                               command=self._toggle_eternal_lode)
        elcb.pack(side="right", padx=(0, 14))
        Tooltip(elcb, "Switch the Start button to the Eternal Lode minigame "
                      "macro. When unchecked, Start runs the regular game "
                      "macro.\n\nWARNING: this mode is currently untested "
                      "and may not work properly.")

        ttk.Label(parent, foreground="#888",
                  text="Settings, category, custom-priority and avoid changes "
                       "apply when you press Start or Save settings, or "
                       "automatically, when Autosave is on."
                  ).pack(fill="x", padx=10)

    # theme
    def _apply_theme(self):
        """Apply the current palette to every widget, then re-render the
        colour-baked strips (skills / custom / avoid / custom buttons). The
        single place colours are set; safe to call repeatedly."""
        p = self.theme
        s = self.style
        s.theme_use("clam")
        s.configure(".", background=p["bg"], foreground=p["fg"],
                    fieldbackground=p["entry_bg"], bordercolor=p["border"],
                    lightcolor=p["panel"], darkcolor=p["panel"],
                    arrowcolor=p["fg"], troughcolor=p["bg"])
        s.configure("TFrame", background=p["bg"])
        s.configure("TLabel", background=p["bg"], foreground=p["fg"])
        s.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
        s.configure("TLabelframe.Label", background=p["bg"],
                    foreground=p["fg"])
        s.configure("TButton", background=p["button"], foreground=p["fg"],
                    bordercolor=p["border"], focuscolor=p["bg"])
        s.map("TButton",
              background=[("active", p["button_active"]),
                          ("disabled", p["bg"])],
              foreground=[("disabled", p["border"])])
        s.configure("TCheckbutton", background=p["bg"], foreground=p["fg"])
        s.map("TCheckbutton", background=[("active", p["bg"])],
              foreground=[("disabled", p["border"])])
        s.configure("TEntry", fieldbackground=p["entry_bg"],
                    foreground=p["entry_fg"], insertcolor=p["fg"],
                    bordercolor=p["border"])
        s.configure("TScrollbar", background=p["button"],
                    troughcolor=p["bg"], bordercolor=p["border"],
                    arrowcolor=p["fg"])
        s.map("TScrollbar", background=[("active", p["button_active"])])

        self.root.configure(background=p["bg"])
        if getattr(self, "settings_canvas", None) is not None:
            self.settings_canvas.configure(background=p["bg"])
        if getattr(self, "log_text", None) is not None:
            self.log_text.configure(background=p["log_bg"],
                                    foreground=p["log_fg"],
                                    insertbackground=p["log_fg"])
        self._update_status_color()

        self._render_skills()
        self._render_custom()
        self._render_avoid()
        self._render_custom_buttons()

    def _update_status_color(self):
        if getattr(self, "status_label", None) is None:
            return
        key = _STATUS_COLORS.get(self.status_var.get())
        self.status_label.configure(
            foreground=self.theme[key] if key else self.theme["fg"])

    def _toggle_theme(self):
        self.dark = bool(self.dark_var.get())
        self.theme = _THEMES["dark" if self.dark else "light"]
        self._apply_theme()
        main.log("dark mode on" if self.dark else "dark mode off")
        self._apply_and_save(quiet=True)   # persist the choice

    def _toggle_eternal_lode(self):
        """Persist the Eternal Lode toggle and log the new state."""
        on = bool(self.eternal_var.get())
        main.log("Eternal Lode mode ON, Start now runs the Eternal Lode "
                 "minigame macro" if on
                 else "Eternal Lode mode OFF, Start runs the regular macro")
        self._apply_and_save(quiet=True)

    # skill categories
    def _build_skills(self, parent):
        skills = ttk.LabelFrame(parent, text="Skill categories")
        skills.pack(fill="x", padx=8, pady=6)

        cols = ttk.Frame(skills)
        cols.pack(fill="both", expand=True, padx=6, pady=6)
        cols.columnconfigure(0, weight=1, uniform="cols")
        cols.columnconfigure(1, weight=1, uniform="cols")
        cols.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(cols, text="Inactive  (tick to activate)")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        right = ttk.LabelFrame(
            cols, text="Active  (drag to reorder, top = first pick)")
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        self.inactive_frame = ttk.Frame(left)
        self.inactive_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self.active_frame = ttk.Frame(right)
        self.active_frame.pack(fill="both", expand=True, padx=4, pady=4)

    def _skill_count(self, category):
        return len(list_skill_files(config.SKILLS_DIR / category))

    def _render_skills(self):
        """Rebuild both category columns from self.active."""
        for frame in (self.inactive_frame, self.active_frame):
            for child in frame.winfo_children():
                child.destroy()
        self.active_rows = {}

        inactive = [c for c in config.SKILL_CATEGORIES if c not in self.active]

        if not inactive:
            ttk.Label(self.inactive_frame, text="(none)",
                      foreground="#999").pack(anchor="w")
        for cat in inactive:
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                self.inactive_frame, variable=var,
                text=f"{cat}   ({self._skill_count(cat)})",
                command=lambda c=cat: self._activate(c)).pack(anchor="w",
                                                              pady=1)

        if not self.active:
            ttk.Label(self.active_frame, text="(none)",
                      foreground="#999").pack(anchor="w")
        for cat in self.active:
            self._make_active_row(cat)

    def _make_active_row(self, cat):
        row = ttk.Frame(self.active_frame)
        handle = ttk.Label(row, text="≡", width=2, cursor="fleur")
        handle.pack(side="left")
        var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, variable=var,
                        command=lambda c=cat: self._deactivate(c)).pack(
                            side="left")
        name = ttk.Label(row, text=f"{cat}   ({self._skill_count(cat)})",
                         cursor="fleur")
        name.pack(side="left", fill="x", expand=True)

        for widget in (row, handle, name):
            widget.bind("<ButtonPress-1>", lambda e, c=cat: self._drag_start(c))
            widget.bind("<B1-Motion>", self._drag_motion)
            widget.bind("<ButtonRelease-1>", self._drag_end)

        row.pack(fill="x", pady=1)
        self.active_rows[cat] = row

    def _activate(self, cat):
        if cat not in self.active:
            self.active.append(cat)      # lowest priority
        self._render_skills()
        self._schedule_autosave()

    def _deactivate(self, cat):
        if cat in self.active:
            self.active.remove(cat)
        self._render_skills()
        self._schedule_autosave()

    # drag-to-reorder (active column)
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
        row_h += 2   # account for the 1px pady above and below

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
        for cat in self.active:
            self.active_rows[cat].pack(fill="x", pady=1)

    def _drag_end(self, _event):
        self._drag_cat = None
        self._schedule_autosave()   # priority order may have changed

    # custom priority / avoid skills
    def _build_custom(self, parent):
        custom = ttk.LabelFrame(parent, text="Custom priority skills")
        custom.pack(fill="x", padx=8, pady=6)

        bar = ttk.Frame(custom)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(bar, text="+  Add skills",
                   command=lambda: self._open_skill_picker("custom")).pack(
                       side="left")
        ttk.Label(bar, foreground="#888", wraplength=340, justify="left",
                  text="Skills taken above all categories (in the order "
                       "added). Click one below to remove it.").pack(
                           side="left", padx=8)

        self.custom_frame = ttk.Frame(custom)
        self.custom_frame.pack(fill="x", padx=6, pady=(2, 6))

    def _build_avoid(self, parent):
        avoid = ttk.LabelFrame(parent, text="Avoid skills")
        avoid.pack(fill="x", padx=8, pady=6)

        bar = ttk.Frame(avoid)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(bar, text="+  Add skills",
                   command=lambda: self._open_skill_picker("avoid")).pack(
                       side="left")
        ttk.Label(bar, foreground="#888", wraplength=340, justify="left",
                  text="Skills the macro never takes. Click one below to "
                       "remove it.").pack(side="left", padx=8)

        self.avoid_frame = ttk.Frame(avoid)
        self.avoid_frame.pack(fill="x", padx=6, pady=(2, 6))

    def _all_skills(self):
        """Return [(category, [identifier, ...]), ...] for every skill image."""
        out = []
        for cat in config.SKILL_CATEGORIES:
            files = list_skill_files(config.SKILLS_DIR / cat)
            out.append((cat, [f"{cat}/{p.name}" for p in files]))
        return out

    def _skill_hash(self, identifier):
        """Cached content hash of a skill image, so a click affects every
        duplicate copy across categories."""
        if identifier not in self._hash_cache:
            self._hash_cache[identifier] = skill_hash(
                config.SKILLS_DIR / identifier)
        return self._hash_cache[identifier]

    def _hashes_of(self, items):
        """Set of content hashes for a list of skill identifiers."""
        hashes = set()
        for identifier in items:
            h = self._skill_hash(identifier)
            if h is not None:
                hashes.add(h)
        return hashes

    def _thumb(self, identifier, size):
        """Return a cached PhotoImage thumbnail for a skill, or None."""
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
            except Exception:                       # noqa: BLE001
                photo = None
        self._thumb_cache[key] = photo
        return photo

    def _skill_cell(self, parent, identifier, size, command, highlight=None):
        """Clickable thumbnail cell for one skill. `highlight` is a border
        colour, or None for the default. Returns the Frame."""
        cell = tk.Frame(parent, background=highlight or self.theme["cell_border"])
        inner = tk.Frame(cell, background="white")
        inner.pack(padx=2, pady=2)

        photo = self._thumb(identifier, size)
        if photo is not None:
            thumb = tk.Label(inner, image=photo, background="white")
        else:
            thumb = tk.Label(inner, text="(missing)", width=9, height=4,
                             background="white", foreground="#999")
        thumb.pack()
        caption = tk.Label(inner, text=identifier.split("/")[-1],
                           background="white", font=("", 7))
        caption.pack()

        for widget in (cell, inner, thumb, caption):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", lambda e: command())
        return cell

    def _render_skill_strip(self, frame, items, color, toggle, empty_text):
        """Render a wrapping row of clickable skill thumbnails into `frame`."""
        for child in frame.winfo_children():
            child.destroy()
        if not items:
            ttk.Label(frame, text=empty_text, foreground="#999").pack(
                anchor="w")
            return
        grid = ttk.Frame(frame)
        grid.pack(anchor="w")
        cols = 9
        for i, identifier in enumerate(items):
            cell = self._skill_cell(
                grid, identifier, 56,
                command=lambda d=identifier: toggle(d), highlight=color)
            Tooltip(cell, identifier)
            cell.grid(row=i // cols, column=i % cols, padx=3, pady=3)

    def _render_custom(self):
        self._render_skill_strip(self.custom_frame, self.custom, _CUSTOM_COLOR,
                                 self._toggle_custom,
                                 "(no custom priority skills)")

    def _render_avoid(self):
        self._render_skill_strip(self.avoid_frame, self.avoid, _AVOID_COLOR,
                                 self._toggle_avoid, "(no skills avoided)")

    def _toggle_in(self, items, identifier):
        """Hash-based toggle: add the skill, or remove every copy of it if
        already present. Returns a new list."""
        h = self._skill_hash(identifier)
        if h is not None and any(self._skill_hash(d) == h for d in items):
            return [d for d in items if self._skill_hash(d) != h]
        if identifier in items:
            return items
        return items + [identifier]

    def _toggle_custom(self, identifier):
        self.custom = self._toggle_in(self.custom, identifier)
        self._refresh_skill_views()
        self._schedule_autosave()

    def _toggle_avoid(self, identifier):
        self.avoid = self._toggle_in(self.avoid, identifier)
        self._refresh_skill_views()
        self._schedule_autosave()

    def _refresh_skill_views(self):
        self._render_custom()
        self._render_avoid()
        if self._picker is not None and self._picker.winfo_exists():
            self._render_picker()

    # custom buttons
    def _build_custom_buttons(self, parent):
        frame = ttk.LabelFrame(parent, text="Custom buttons (clicked on sight)")
        frame.pack(fill="x", padx=8, pady=6)

        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        btn = ttk.Button(bar, text="+  Add custom button",
                         command=self._add_custom_button)
        btn.pack(side="left")
        self._coord_buttons.append(btn)
        Tooltip(btn, "Capture a button from the game (e.g. an event 'Enter' "
                     "button) by dragging a box over it. The macro then "
                     "clicks it whenever it appears. Run Calibrate scale from "
                     "screen FIRST, the capture is stored at the current UI "
                     "scale.")
        ttk.Label(bar, foreground="#888", wraplength=340, justify="left",
                  text="Extra 'press it when you see it' buttons. Click one "
                       "below to remove it; changes take effect on Start."
                  ).pack(side="left", padx=8)

        self.custom_buttons_frame = ttk.Frame(frame)
        self.custom_buttons_frame.pack(fill="x", padx=6, pady=(2, 6))

    def _custom_button_thumb(self, path, box):
        """Aspect-preserving thumbnail PhotoImage for a custom-button PNG."""
        try:
            image = Image.open(path).convert("RGB")
            image.thumbnail((box, box), _RESAMPLE)
            return ImageTk.PhotoImage(image)
        except Exception:                                # noqa: BLE001
            return None

    def _custom_button_cell(self, parent, path):
        cell = tk.Frame(parent, background=self.theme["cell_border"])
        inner = tk.Frame(cell, background="white")
        inner.pack(padx=2, pady=2)
        photo = self._custom_button_thumb(path, 100)
        if photo is not None:
            self._custom_thumb_refs.append(photo)   # keep alive
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

    def _render_custom_buttons(self):
        """Rebuild the custom-button thumbnail strip from ref/custom/."""
        frame = self.custom_buttons_frame
        for child in frame.winfo_children():
            child.destroy()
        self._custom_thumb_refs = []

        paths = sorted(config.REF_CUSTOM_DIR.glob("*.png"))
        if not paths:
            ttk.Label(frame, text="(no custom buttons)",
                      foreground="#999").pack(anchor="w")
            return
        grid = ttk.Frame(frame)
        grid.pack(anchor="w")
        cols = 5
        for i, path in enumerate(paths):
            cell = self._custom_button_cell(grid, path)
            cell.grid(row=i // cols, column=i % cols, padx=3, pady=3)

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

    def _add_custom_button(self):
        """Capture a button from the screen and save it as a custom ref.

        The crop is normalised to the ref baseline (resized by 1 / current
        REF_SCALE_RANGE) so it matches like the built-in buttons. Calibrate
        scale first.
        """
        if self._coords_busy:
            return
        if self._running:
            main.log("stop the macro before adding a custom button")
            return
        name = simpledialog.askstring(
            "Custom button",
            "Name this button (clicked whenever the macro sees it):",
            parent=self.root)
        if name is None:
            return
        safe = "".join(c for c in name if c.isalnum() or c in " -_").strip()
        if not safe:
            messagebox.showerror("Custom button",
                                 "Please enter a valid name.")
            return
        path = config.REF_CUSTOM_DIR / f"{safe}.png"
        if path.exists() and not messagebox.askyesno(
                "Custom button",
                f"A custom button named '{safe}' already exists. Replace it?"):
            return
        # Apply the form so the capture uses the current UI scale.
        data = self._collect_settings()
        if data is None:
            return
        config.apply_settings(data)
        lo, hi, _ = config.REF_SCALE_RANGE
        scale = (lo + hi) / 2.0 or 1.0

        # Start a small box over the capture-region centre, falling back to the
        # primary screen centre.
        corners = self._read_region_corners()
        if corners is not None:
            x1, y1, x2, y2 = corners
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        else:
            cx = self.root.winfo_screenwidth() // 2
            cy = self.root.winfo_screenheight() // 2
        bw, bh = 160, 80
        initial = (cx - bw // 2, cy - bh // 2, bw, bh)

        self._start_picker()

        def after_rect(rect):
            if rect is None:
                main.log("custom button capture cancelled")
                self._finish_picker()
                return
            x, y, w, h = (int(v) for v in rect)
            if w < 5 or h < 5:
                main.log("custom button: capture area too small, aborted")
                self._finish_picker()
                return

            def capture():
                ok = False
                try:
                    crop = grab_screen_bgr((x, y, w, h))
                    crop = rescale_image(crop, 1.0 / scale)   # -> ref baseline
                    config.REF_CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
                    save_image(path, crop)
                    main.log(f"saved custom button '{safe}'  ({w}x{h}px, "
                             f"normalised by 1/{scale:.2f}) -> ref/custom/"
                             f"{path.name}")
                    ok = True
                except Exception as e:                       # noqa: BLE001
                    main.log(f"custom button capture failed, {e!r}")
                finally:
                    def done():
                        self._finish_picker()
                        if ok:
                            self._render_custom_buttons()
                    try:
                        self.root.after(0, done)
                    except tk.TclError:
                        pass

            # Let the overlay tear down and the screen repaint (so the crop
            # doesn't catch the rectangle border/handles), then grab + save off
            # the Tk thread.
            self.root.after(
                200, lambda: threading.Thread(target=capture,
                                              daemon=True).start())

        region_picker.RectanglePicker(
            self.root, initial, after_rect,
            message="Drag the box over the button to capture it. "
                    "✓ to save, ✗ to cancel.")

    # skill picker (shared by custom priority + avoid)
    def _open_skill_picker(self, mode):
        self._picker_mode = mode
        if self._picker is not None and self._picker.winfo_exists():
            self._render_picker()
            self._picker.lift()
            return

        win = tk.Toplevel(self.root)
        self._picker = win
        win.geometry("640x560")
        win.transient(self.root)
        win.configure(background=self.theme["bg"])

        self._picker_head = ttk.Label(win, wraplength=600, foreground="#888")
        self._picker_head.pack(fill="x", padx=8, pady=6)

        footer = ttk.Frame(win)
        footer.pack(side="bottom", fill="x", padx=8, pady=6)
        ttk.Button(footer, text="Close",
                   command=win.destroy).pack(side="right")

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

        self._picker_body = body
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._render_picker()

    def _render_picker(self):
        body = self._picker_body
        if body is None:
            return

        mode = self._picker_mode
        if mode == "avoid":
            items, color, toggle = self.avoid, _AVOID_COLOR, self._toggle_avoid
            self._picker.title("Add skills to avoid")
            self._picker_head.config(
                text="Click a skill to toggle it on/off the avoid list. "
                     "Avoided skills are outlined in red. Close when done.")
        else:
            items, color, toggle = (self.custom, _CUSTOM_COLOR,
                                    self._toggle_custom)
            self._picker.title("Add custom priority skills")
            self._picker_head.config(
                text="Click a skill to toggle it on/off the custom priority "
                     "list. Custom priority skills are outlined in green. "
                     "Close when done.")

        for child in body.winfo_children():
            child.destroy()

        all_skills = self._all_skills()
        if not any(idents for _, idents in all_skills):
            ttk.Label(body, foreground="#999", wraplength=580,
                      text="No skill images found. Add PNG icons to the "
                           "skills/ category subfolders, then reopen this "
                           "window.").pack(padx=12, pady=24)
            return

        selected = self._hashes_of(items)
        cols = 6
        for category, idents in all_skills:
            if not idents:
                continue
            ttk.Label(body, text=category, font=("", 9, "bold")).pack(
                anchor="w", padx=6, pady=(8, 2))
            grid = ttk.Frame(body)
            grid.pack(anchor="w", padx=6)
            for i, identifier in enumerate(idents):
                hit = self._skill_hash(identifier) in selected
                cell = self._skill_cell(
                    grid, identifier, 72,
                    command=lambda d=identifier, t=toggle: t(d),
                    highlight=color if hit else None)
                cell.grid(row=i // cols, column=i % cols, padx=3, pady=3)

    # settings form
    def _build_settings(self, parent):
        outer = ttk.LabelFrame(parent, text="Settings")
        outer.pack(fill="x", padx=8, pady=6)

        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0,
                           height=240, background=self.theme["bg"])
        self.settings_canvas = canvas
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        form = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<Enter>",
                    lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        row = 0
        for entry in SETTINGS_SCHEMA:
            tag = entry[0]

            if tag == "section":
                ttk.Label(form, text=entry[1], font=("", 9, "bold")).grid(
                    row=row, column=0, columnspan=2, sticky="w",
                    pady=(8, 2), padx=4)
                row += 1
                continue

            if tag == "button":
                btn_label, method = entry[1], entry[2]
                btn_tip = entry[3] if len(entry) > 3 else ""
                holder = ttk.Frame(form)
                holder.grid(row=row, column=0, columnspan=2, sticky="w",
                            padx=14, pady=2)
                btn = ttk.Button(holder, text=btn_label,
                                 command=getattr(self, method))
                btn.pack(side="left")
                self._coord_buttons.append(btn)
                if btn_tip:
                    Tooltip(btn, btn_tip)
                row += 1
                continue

            key, label, kind, unit, hint = entry[:5]
            capture = entry[5] if len(entry) > 5 else None
            lbl = ttk.Label(form, text=label)
            lbl.grid(row=row, column=0, sticky="w", padx=(14, 8), pady=2)
            if hint:
                Tooltip(lbl, hint)

            cell = ttk.Frame(form)
            cell.grid(row=row, column=1, sticky="w", pady=2)
            if kind == "bool":
                var = tk.BooleanVar()
                ttk.Checkbutton(cell, variable=var).pack(side="left")
                self.vars[key] = var
            else:
                boxes = []
                for _ in range(_KIND_WIDTHS[kind]):
                    sv = tk.StringVar()
                    ttk.Entry(cell, textvariable=sv, width=6).pack(
                        side="left", padx=(0, 4))
                    boxes.append(sv)
                self.vars[key] = boxes
            if unit:
                ttk.Label(cell, text=unit, foreground="#888").pack(
                    side="left", padx=(2, 0))
            if capture:
                btn = ttk.Button(
                    cell, text="Pick", width=6,
                    command=lambda k=key, m=capture: self._capture_into(k, m))
                btn.pack(side="left", padx=(8, 0))
                self._coord_buttons.append(btn)
                Tooltip(btn, _CAPTURE_HINTS[capture])
            row += 1

    def _load_settings_into_form(self):
        for entry in SETTINGS_SCHEMA:
            if entry[0] in ("section", "button"):
                continue
            key, label, kind, unit, hint = entry[:5]
            value = getattr(config, key)
            var = self.vars[key]
            if kind == "bool":
                var.set(bool(value))
            elif kind in ("float", "int"):
                var[0].set(_fmt(value))
            elif kind == "region4":
                # config stores (x, y, w, h); form edits two corners.
                x, y, w, h = value
                for box, v in zip(var, (x, y, x + w, y + h)):
                    box.set(_fmt(v))
            else:
                seq = list(value) if value is not None else []
                for i, box in enumerate(var):
                    box.set(_fmt(seq[i]) if i < len(seq) else "")

    def _collect_settings(self, quiet=False):
        """Build a settings dict from the form, or None if a field is bad.

        A bad field pops an error dialog unless `quiet` is set (autosave passes
        quiet=True so a half-typed value never interrupts the user).
        """
        out = {}
        label = ""
        try:
            for entry in SETTINGS_SCHEMA:
                if entry[0] in ("section", "button"):
                    continue
                key, label, kind, unit, hint = entry[:5]
                var = self.vars[key]
                if kind == "bool":
                    out[key] = bool(var.get())
                elif kind == "float":
                    out[key] = float(var[0].get())
                elif kind == "int":
                    out[key] = int(float(var[0].get()))
                elif kind == "floats3":
                    out[key] = [float(b.get()) for b in var]
                elif kind == "region4":
                    # form edits two corners; config wants (x, y, w, h).
                    x1, y1, x2, y2 = [int(float(b.get())) for b in var]
                    out[key] = [min(x1, x2), min(y1, y2),
                                abs(x2 - x1), abs(y2 - y1)]
                else:  # ints2
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
        out["ETERNAL_LODE_MODE"] = bool(self.eternal_var.get())
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
            self._render_skills()   # refresh icon counts

    # autosave
    def _on_autosave_toggle(self):
        """Persist the autosave preference (and, if just enabled, save now)."""
        on = self.autosave_var.get()
        main.log("autosave on, settings save after every change"
                 if on else "autosave off")
        self._apply_and_save(quiet=True)

    def _wire_autosave_traces(self):
        """Trigger an autosave when any settings-form field is edited.

        Called once after the form is populated, so loading saved values does
        not count as a change.
        """
        for var in self.vars.values():
            for v in (var if isinstance(var, list) else [var]):
                v.trace_add("write", lambda *_: self._schedule_autosave())

    def _schedule_autosave(self):
        """If autosave is on, save shortly after the latest change.

        Debounced: rapid edits coalesce into one save instead of writing
        settings.json each time.
        """
        if not self.autosave_var.get():
            return
        if self._autosave_after is not None:
            try:
                self.root.after_cancel(self._autosave_after)
            except tk.TclError:
                pass
        self._autosave_after = self.root.after(500, self._do_autosave)

    def _do_autosave(self):
        """Run a debounced autosave, quietly, so a bad field just skips it."""
        self._autosave_after = None
        if self.autosave_var.get():
            self._apply_and_save(quiet=True)

    # coordinate capture
    def _set_coord_buttons(self, state):
        """Enable or disable every coord-capture button at once."""
        for btn in self._coord_buttons:
            try:
                btn.config(state=state)
            except tk.TclError:
                pass

    def _read_region_corners(self):
        """Live capture region (x1, y1, x2, y2) from the form, or None."""
        try:
            return tuple(int(float(b.get()))
                         for b in self.vars["BLUESTACKS_REGION"])
        except (KeyError, IndexError, ValueError, tk.TclError):
            return None

    def _fill_field(self, key, values):
        """Write captured numbers into a coord field's entry boxes."""
        for box, val in zip(self.vars[key], values):
            box.set(str(int(val)))

    def _end_capture(self, key=None, values=None):
        """Re-enable the capture buttons; fill `key`'s boxes if values given.
        Safe to call from a worker thread (marshals onto the Tk loop)."""
        def done():
            if key is not None and values is not None:
                self._fill_field(key, values)
            self._coords_busy = False
            self._set_coord_buttons("normal")
        try:
            self.root.after(0, done)
        except tk.TclError:
            pass

    def _get_mouse_coords(self):
        """Log the cursor position without filling any field."""
        if self._coords_busy:
            return
        self._coords_busy = True
        self._set_coord_buttons("disabled")

        def worker():
            main.log("Get mouse coords: move the mouse onto the target...")
            x, y = getcoords.capture_position(
                5, on_tick=lambda s: main.log(f"  capturing in {s}..."))
            main.log(f"mouse position: ({x}, {y})")
            self._end_capture()

        threading.Thread(target=worker, daemon=True).start()

    def _capture_into(self, key, mode):
        """Dispatch a Pick button to the right picker: "point" (bullseye),
        "region" (window-pick + rectangle) or "band" (window-pick +
        width-locked rectangle)."""
        if self._coords_busy:
            return
        if mode == "point":
            self._pick_point(key)
        elif mode == "region":
            self._pick_region(key)
        elif mode == "band":
            self._pick_band(key)

    def _start_picker(self):
        """Mark the GUI as picking and disable the coord buttons."""
        self._coords_busy = True
        self._set_coord_buttons("disabled")

    def _finish_picker(self):
        self._coords_busy = False
        self._set_coord_buttons("normal")

    def _pick_region(self, key):
        """Two-step capture-region pick: click the game window, then drag the
        rectangle's edges to crop to just the Android screen."""
        self._start_picker()

        def after_window(rect):
            if rect is None:
                main.log("capture region pick cancelled")
                self._finish_picker()
                return
            wx, wy, ww, wh = rect
            # Inset so the starting rectangle clears the window edge, easier to
            # grab the handles.
            inset = max(6, min(ww, wh) // 40)
            initial = (wx + inset, wy + inset,
                       max(20, ww - 2 * inset), max(20, wh - 2 * inset))

            def after_refine(result):
                if result is None:
                    main.log("capture region pick cancelled")
                else:
                    x, y, w, h = (int(v) for v in result)
                    self._fill_field(key, [x, y, x + w, y + h])
                    main.log(f"capture region = ({x}, {y}) -> "
                             f"({x + w}, {y + h})  [{w} x {h} px]")
                self._finish_picker()

            region_picker.RectanglePicker(
                self.root, initial, after_refine,
                message="Drag the edges to crop to just the Android screen. "
                        "✓ to save, ✗ to cancel.")

        region_picker.pick_window(
            self.root, after_window,
            message="Click anywhere on the game window. "
                    "Press Esc or ✗ to cancel.")

    def _pick_point(self, key):
        """Bullseye picker for a single (x, y) field."""
        self._start_picker()

        # Start from the field's current value, else the capture-region
        # centre, else the screen centre.
        initial = None
        try:
            cur = [int(float(b.get())) for b in self.vars[key]]
            initial = (cur[0], cur[1])
        except (ValueError, IndexError, KeyError, tk.TclError):
            corners = self._read_region_corners()
            if corners is not None:
                x1, y1, x2, y2 = corners
                initial = ((x1 + x2) // 2, (y1 + y2) // 2)

        label = key.replace("_", " ").lower()

        def after(result):
            if result is None:
                main.log(f"{label} pick cancelled")
            else:
                x, y = (int(v) for v in result)
                self._fill_field(key, [x, y])
                main.log(f"{label} = ({x}, {y})")
            self._finish_picker()

        region_picker.PointPicker(
            self.root, initial, after,
            message=f"Drag the bullseye onto the {label} target. "
                    f"✓ to save, ✗ to cancel.")

    def _pick_band(self, key):
        """Adjust a horizontal band the width of the current capture region
        to match the on-screen skill row."""
        corners = self._read_region_corners()
        if corners is None:
            messagebox.showerror(
                "Pick skill band",
                "Set the capture region first, the skill band uses its "
                "width as a starting size.")
            return
        rx, ry, rx2, ry2 = corners
        rw, rh = rx2 - rx, ry2 - ry
        if rw < 20 or rh < 20:
            messagebox.showerror(
                "Pick skill band",
                "Capture region is empty, set it first.")
            return

        self._start_picker()

        def after_click(_rect):
            if _rect is None:
                main.log("skill band pick cancelled")
                self._finish_picker()
                return
            # Pre-fill: full region width, half its height, vertically centred.
            band_h = max(20, rh // 2)
            band_y = ry + (rh - band_h) // 2
            initial = (rx, band_y, rw, band_h)

            def after_refine(result):
                if result is None:
                    main.log("skill band pick cancelled")
                else:
                    _, by, _, bh = (int(v) for v in result)
                    top = max(0, by - ry)
                    bottom = max(top + 1, by + bh - ry)
                    self._fill_field(key, [top, bottom])
                    main.log(f"skill band = (top={top}, bottom={bottom})  "
                             f"(relative to region top y={ry})")
                self._finish_picker()

            region_picker.RectanglePicker(
                self.root, initial, after_refine,
                message="Drag the top and bottom to fit the skill row. "
                        "✓ to save, ✗ to cancel.",
                lock_edges={"left", "right"}, move_axis="y")

        region_picker.pick_window(
            self.root, after_click,
            message="Click anywhere on the game window. "
                    "Press Esc or ✗ to cancel.")

    # scale calibration
    def _calibrate_scale(self):
        """Measure the game's on-screen scale and fill the scale-range fields,
        retuning SCALE_RANGE and REF_SCALE_RANGE for the current window size.
        See main.measure_game_scale."""
        if self._coords_busy:
            return
        if self._running:
            main.log("stop the macro before calibrating scale")
            return
        # Apply the form first so the capture uses the current region.
        data = self._collect_settings()
        if data is None:
            return
        config.apply_settings(data)
        self._coords_busy = True
        self._set_coord_buttons("disabled")
        region = config.BLUESTACKS_REGION

        def worker():
            result = None
            try:
                main.log("Scale calibration: bring up an active skill-"
                         "selection screen in the game window...")
                for s in range(5, 0, -1):
                    main.log(f"  capturing in {s}...")
                    time.sleep(1)
                result = main.measure_game_scale(grab_screen_bgr(region))
            except Exception as e:                       # noqa: BLE001
                main.log(f"  scale calibration error: {e!r}")
            finally:
                self._finish_calibration(result)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_calibration(self, result):
        """Handle a calibration result on the Tk thread."""
        def done():
            if result is None:
                pass   # an error was already logged by the worker
            elif result.get("ok"):
                self._apply_scale_result(result)
            else:
                main.log(f"  scale calibration failed: "
                         f"{result.get('reason', 'unknown')}")
            self._coords_busy = False
            self._set_coord_buttons("normal")
        try:
            self.root.after(0, done)
        except tk.TclError:
            pass

    def _apply_scale_result(self, result):
        """Fill the Skill / UI scale-range fields from a result.

        Calibration refines the scale, so the runtime locks to a single scale
        per match (min == max) instead of sweeping a margin each frame. If a
        later resize makes matching flaky, widen the max box by ~0.05 by hand.
        """
        step = 0.05
        skill = round(result["skill_scale"], 3)
        ref = round(result["ref_scale"], 3)
        ranges = {
            "SCALE_RANGE": (skill, skill, step),
            "REF_SCALE_RANGE": (ref, ref, step),
        }
        for key, triple in ranges.items():
            for box, value in zip(self.vars[key], triple):
                box.set(_fmt(value))
        main.log(f"  matched {result['kind']} '{result['name']}' at scale "
                 f"{result['matched_scale']:.3f} (conf {result['conf']:.2f}), "
                 f"zoom {result['zoom']:.2f}x")
        main.log(f"  -> locked skill scale {skill:.3f}, UI scale {ref:.3f} "
                 f"(one scale per match); press Save settings to keep them")

    # run / stop
    def _start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        # Skill-priority config is irrelevant in Eternal Lode mode, so skip the
        # "nothing to pick" warning there.
        if (not self.eternal_var.get()
                and not self.active and not self.custom):
            if not messagebox.askyesno(
                    "Nothing to pick",
                    "No skill categories are active and no custom priority "
                    "skills are set, so the macro will always take a skill "
                    "slot.\n\nStart anyway?"):
                return
        if not self._apply_and_save():
            return

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._set_running(True)
        self._thread.start()

    def _run_thread(self):
        # Eternal Lode checkbox routes Start to the minigame macro.
        runner = (eternal_lode.run_eternal_lode if self.eternal_var.get()
                  else main.run_macro)
        try:
            runner(self._stop_event)
        except Exception as e:                       # noqa: BLE001
            main.log(f"ERROR: macro crashed; {e!r}")
        finally:
            try:
                self.root.after(0, lambda: self._set_running(False))
            except tk.TclError:
                pass   # window already closed

    def _stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._running:
            self.status_var.set("Stopping…")

    def _set_running(self, running):
        self._running = running
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        self.status_var.set("Running" if running else "Stopped")

    # window lock
    def _toggle_lock(self):
        self._set_locked(self.lock_var.get())

    def _set_locked(self, locked):
        """Locked: window pinned always-on-top and not movable/resizable."""
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
        # While locked, snap the window back if moved or resized.
        if not self._locked or event.widget is not self.root:
            return
        if (self._locked_geometry is not None
                and self.root.geometry() != self._locked_geometry):
            self.root.geometry(self._locked_geometry)

    # log
    def _build_log(self, parent):
        frame = ttk.LabelFrame(parent, text="Log")
        frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Button(bar, text="Clear", width=8,
                   command=self._clear_log).pack(side="right")

        self.log_text = scrolledtext.ScrolledText(
            frame, height=8, width=44, state="disabled", wrap="word",
            font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _drain_log(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    # shutdown
    def _on_close(self):
        if self._autosave_after is not None:
            try:
                self.root.after_cancel(self._autosave_after)
            except tk.TclError:
                pass
        if self.autosave_var.get():
            self._apply_and_save(quiet=True)   # flush any pending autosave
        if self._stop_event is not None:
            self._stop_event.set()
        main.set_log_sink(None)
        self.root.destroy()


def main_gui():
    root = tk.Tk()
    root.title("A2 Macro Controller")
    root.geometry("1280x720")
    root.minsize(1180, 620)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main_gui()
