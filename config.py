import json
import sys
from pathlib import Path

# Paths
# Frozen by PyInstaller: data (skills/, ref/, settings.json) sits next to the
# .exe, not inside the bundle. From source: this file's folder.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"
REF_DIR = BASE_DIR / "ref"
# User-captured "click on sight" buttons, normalised to the ref baseline.
REF_CUSTOM_DIR = REF_DIR / "custom"

# Capture window
# Screen rect (x, y, w, h) of whatever window shows the game: a BlueStacks
# window or a scrcpy phone mirror. Named BLUESTACKS_REGION for settings.json
# back-compat. The GUI edits it as two corners with a Pick button.
BLUESTACKS_REGION = (656, 0, 1264 - 656, 1078 - 0)
REGION_X, REGION_Y = BLUESTACKS_REGION[0], BLUESTACKS_REGION[1]

# Fixed click targets (absolute screen coords)
# Leftmost skill card. Fallback pick when no wanted skill is offered and
# rerolls are exhausted.
FIRST_SKILL_SLOT = (809, 561)

# Second skill card. Used instead of the first when the first holds an avoid
# skill (see AVOID_SKILLS).
SECOND_SKILL_SLOT = (1009, 561)

# Tap to dismiss the game-over screen. Defaults to the window centre; move it
# to an empty area if the centre lands on an unwanted button.
GAME_OVER_TAP = (REGION_X + BLUESTACKS_REGION[2] // 2,
                 REGION_Y + BLUESTACKS_REGION[3] // 2)

# Skill categories
# One subfolder of skills/ per name. Icons inside are numbered 1.png (best)
# .. N.png (worst); the macro picks the lowest-numbered icon it sees.
SKILL_CATEGORIES = [
    "Sprites",
    "Main Weapon",
    "Circles",
    "Attack Speed",
    "Attack Power",
    "Strikes",
    "Elemental",
    "Plants",
    "HP",
    "Meteor",
    "Potions",
    "Other",
]

# Active categories, highest priority first. A category not listed is ignored.
# Earlier categories outrank later ones. Managed by the GUI. Default: all on.
ACTIVE_CATEGORIES = list(SKILL_CATEGORIES)

# Specific skills picked above ALL categories, in pick order. Each entry is a
# "Category/file.png" path. Managed by the GUI's Custom priority section.
CUSTOM_PRIORITY_SKILLS = []

# Skills to never take. Each entry is a "Category/file.png" path. On a forced
# fallback, if the first slot holds one of these the macro takes the second
# slot. Managed by the GUI's Avoid skills section.
AVOID_SKILLS = []

# Template matching
MATCH_THRESHOLD = 0.78          # min confidence for a skill-icon match
REF_THRESHOLD = 0.80            # min confidence for a UI-element match

# Skill icons are a fixed size, matching at ~0.55, so this is just 0.55 with a
# small margin. (min, max, step). Widen only if real captures miss skills.
SCALE_RANGE = (0.50, 0.60, 0.05)

# UI elements are near-fixed size, so a tight range keeps each poll fast.
REF_SCALE_RANGE = (0.90, 1.10, 0.05)

# Calibration baselines: the scales the bundled templates matched at in
# BlueStacks fullscreen (skill icons ~0.55, UI refs ~1.0). The whole UI scales
# uniformly, so these keep a fixed ratio at any window size; the GUI's
# Calibrate button measures one element and sets both ranges from them. They
# describe the bundled files, not user prefs, so they are NOT persisted.
SKILL_SCALE_BASELINE = 0.55
REF_SCALE_BASELINE = 1.0

# Skill matching runs at this fraction of full res for speed (match cost grows
# with pixel area). Coords are scaled back automatically. 0.4-0.6 is sane;
# 1.0 disables.
SKILL_DOWNSCALE = 0.5

# Skill cards always sit in the same row, so the search is restricted to this
# vertical band, (y_top, y_bottom) relative to BLUESTACKS_REGION's top. None
# for the full window. Widen it if skills are missed.
SKILL_MATCH_BAND = (400, 720)

# Card-sized box (w, h) around a slot, used to check whether one slot holds an
# avoid skill. Enlarge if avoid detection misses.
SKILL_SLOT_BOX = (170, 210)

# Timing (seconds)
POLL_INTERVAL = 2             # between screen checks
ACTION_DELAY = 1.2              # pause after a click so the screen settles
STARTUP_DELAY = 5.0             # grace period to focus the game window first

# Run timeout
# Auto-stop after this many hours. 0 (or None) disables it.
RUN_TIMEOUT_HOURS = 2

# On timeout, also close the game window (terminate CLOSE_PROCESSES). If
# False, just stop and leave BlueStacks / scrcpy running.
CLOSE_ON_TIMEOUT = True

# On timeout during a scrcpy session, also put the phone to sleep via adb so
# the game pauses and the phone stops overheating. No-op for BlueStacks.
SLEEP_PHONE_ON_TIMEOUT = True

# Process names terminated on timeout (both backends; anything not running is
# skipped). Edit to match your BlueStacks version if these differ.
CLOSE_PROCESSES = [
    "scrcpy.exe",
    "HD-Player.exe",
    "BlueStacks.exe",
    "BlueStacksServices.exe",
]

# adb executable for the phone-sleep. The macro prefers the adb.exe bundled
# next to a running scrcpy.exe; this default ("adb" on PATH) is a fallback.
# Set a full path to adb.exe if neither is found.
ADB_PATH = "adb"

# Humanisation
# A little randomness so input is not pixel-perfect or perfectly periodic.
CLICK_JITTER = 6                    # max +/- px added to each click point
CLICK_DELAY_RANGE = (0.05, 0.25)    # random pause before each click
MOVE_DURATION_RANGE = (0.08, 0.22)  # random cursor-travel time
DELAY_JITTER = 0.35                 # POLL/ACTION delays vary +/- this fraction

# Reference UI templates
REF_PLAY = REF_DIR / "play_button.png"
REF_DEVIL = REF_DIR / "devil_reject.png"
REF_GAME_OVER = REF_DIR / "game_over.png"
# Skill-screen banners; any one detects the screen. Optional: a missing PNG is
# skipped.
REF_VALKYRIE = REF_DIR / "valkyrie.png"
REF_LEVEL = REF_DIR / "level.png"
REF_GLORY = REF_DIR / "glory.png"
# Reroll button; disappears once refreshes run out.
REF_REFRESH = REF_DIR / "refresh.png"
# Prompts that behave like Play: clicked whenever on screen.
REF_GET_READY = REF_DIR / "get-ready.png"
REF_CONTINUE = REF_DIR / "continue.png"
REF_START_CHALLENGE = REF_DIR / "start_challenge.png"


# GUI
# Save settings automatically a moment after every change. Toggled by the
# GUI's Autosave checkbox; persisted.
AUTOSAVE = False

# Colour scheme: True = dark, False = light. Persisted.
DARK_MODE = True

# When True, Start runs the Eternal Lode minigame macro
# (eternal_lode.run_eternal_lode) instead of main.run_macro. Persisted.
ETERNAL_LODE_MODE = False

# Eternal Lode minigame
# The board is a 6-col x 8-row grid; the macro acts only on the "primary row"
# (the row below the on-screen level marker).
ETERNAL_LODE_DIR = REF_DIR / "Eternal Lode"
# Board reference is 484x656 px = 6 cols x 8 rows, so cell pitch is
# 484/6 ~= 80.67 px wide and 656/8 = 82 px tall at the ref baseline.
EL_BOARD_COLS = 6
EL_BOARD_ROWS = 8
EL_BOARD_W = 484
EL_BOARD_H = 656

# Match thresholds. Cell content (dirt/rock/chest) uses the looser skill-style
# threshold; UI/indicators use the stricter ref threshold.
EL_CELL_THRESHOLD = 0.72
EL_UI_THRESHOLD = 0.78

# Eternal Lode loop pacing.
EL_ACTION_DELAY = 0.5     # wait after a click for the animation to settle
EL_CHEST_CLICK_DELAY = 0.3  # gap between repeated chest taps
EL_CHEST_MAX_CLICKS = 20    # safety cap on chest tapping


# Settings persistence
# settings.json (next to this file) overrides the defaults above, so main.py
# and diagnose.py run with whatever the GUI last saved. Only PERSISTED_KEYS
# are ever read or written.
SETTINGS_PATH = BASE_DIR / "settings.json"

PERSISTED_KEYS = (
    "POLL_INTERVAL", "ACTION_DELAY", "STARTUP_DELAY",
    "RUN_TIMEOUT_HOURS", "CLOSE_ON_TIMEOUT", "SLEEP_PHONE_ON_TIMEOUT",
    "MATCH_THRESHOLD", "REF_THRESHOLD", "SKILL_DOWNSCALE",
    "SCALE_RANGE", "REF_SCALE_RANGE",
    "CLICK_JITTER", "DELAY_JITTER",
    "FIRST_SKILL_SLOT", "SECOND_SKILL_SLOT", "GAME_OVER_TAP",
    "BLUESTACKS_REGION", "SKILL_MATCH_BAND",
    "ACTIVE_CATEGORIES", "CUSTOM_PRIORITY_SKILLS", "AVOID_SKILLS",
    "AUTOSAVE", "DARK_MODE", "ETERNAL_LODE_MODE",
)

# Keys whose value must be a tuple (JSON round-trips tuples as lists).
_TUPLE_KEYS = frozenset({
    "SCALE_RANGE", "REF_SCALE_RANGE", "FIRST_SKILL_SLOT",
    "SECOND_SKILL_SLOT", "GAME_OVER_TAP", "BLUESTACKS_REGION",
    "SKILL_MATCH_BAND",
})


def _recompute_derived():
    """Refresh values computed from other settings. Call after any change."""
    global REGION_X, REGION_Y
    REGION_X, REGION_Y = BLUESTACKS_REGION[0], BLUESTACKS_REGION[1]


def current_settings():
    """Persisted-key settings as a plain, JSON-ready dict."""
    g = globals()
    out = {}
    for key in PERSISTED_KEYS:
        val = g[key]
        out[key] = list(val) if isinstance(val, tuple) else val
    return out


def apply_settings(data):
    """Override config globals from `data` (persisted keys), then recompute
    derived values. Unknown keys are ignored."""
    g = globals()
    for key, val in data.items():
        if key not in PERSISTED_KEYS:
            continue
        if key in _TUPLE_KEYS and isinstance(val, list):
            val = tuple(val)
        g[key] = val
    _recompute_derived()


def load_settings(path=None):
    """Load and apply settings.json. Returns True if a file was read, False if
    there was none or it could not be parsed."""
    path = Path(path) if path else SETTINGS_PATH
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    apply_settings(data)
    return True


def save_settings(path=None):
    """Write the current persisted settings to settings.json."""
    path = Path(path) if path else SETTINGS_PATH
    path.write_text(json.dumps(current_settings(), indent=2),
                    encoding="utf-8")


# Apply persisted settings at import so the macro/diagnostics use what the GUI
# last saved.
load_settings()
