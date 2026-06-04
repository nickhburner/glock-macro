import json
import sys
from pathlib import Path

# Paths
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"
REF_DIR = BASE_DIR / "ref"
REF_CUSTOM_DIR = REF_DIR / "custom"

# ------------------------------------------------------------------ ADB
# Serial of the connected Android device.
# "localhost:5555" for BlueStacks; a USB serial for a real phone.
# Leave empty to auto-select when exactly one device is present.
ADB_DEVICE = ""

# Cached phone resolution [width, height].  [0, 0] = not yet measured.
PHONE_RESOLUTION = [0, 0]

# ------------------------------------------------------------------ Scale
# Single zoom factor applied to all template scales.
# 1.0 = bundled templates already match the phone's native resolution.
# Set by the one-time calibration scan; never changes for a given device.
#   skill_scale = SKILL_SCALE_BASELINE * CALIBRATED_SCALE
#   ref_scale   = REF_SCALE_BASELINE   * CALIBRATED_SCALE
CALIBRATED_SCALE = 1.0

# Every capture is downscaled to this width before template matching, then tap
# coordinates are scaled back up to the device's real pixels.  The templates
# were captured/calibrated at 540-wide (BlueStacks native), so matching at a
# fixed 540 width makes ONE set of templates work on every device regardless of
# its resolution -- a 1080-wide phone, a 540 emulator, anything.  It also keeps
# matching fast (always ~540 px wide) instead of scaling with device pixels.
# This replaces per-device CALIBRATED_SCALE tuning.
MATCH_WIDTH = 540

# Baseline scales the bundled templates were captured at in BlueStacks
# fullscreen.  NOT persisted: they describe the template files, not prefs.
SKILL_SCALE_BASELINE = 0.55
REF_SCALE_BASELINE   = 1.0

# Matching is done across a small RANGE of scales around the calibrated centre,
# not a single value.  A device rarely renders the UI at exactly the baseline,
# so the spread gives the matcher tolerance (the single-scale rework lost this
# and skills stopped matching).  Tolerance is a +/- fraction; steps is how many
# scales to test across that span (odd step counts keep the centre exact).
SKILL_SCALE_TOLERANCE = 0.18
SKILL_SCALE_STEPS     = 5

# Refs are expected to be (re)captured at the MATCH_WIDTH (540) baseline via the
# GUI "Recapture ref" tool, so they all peak at scale 1.0.  This tight 5-step
# 0.90..1.10 range is centred there.  NOTE: any ref still at the OLD BlueStacks
# baseline peaks at ~0.86 and will only match weakly until recaptured -- if some
# old refs remain, widen back to TOLERANCE 0.14 / STEPS 7 (range 0.86..1.14).
REF_SCALE_TOLERANCE   = 0.10
REF_SCALE_STEPS       = 5


def _scale_list(center, tol, steps):
    """Return `steps` scales spanning center*(1-tol) .. center*(1+tol)."""
    center = max(1e-4, float(center))
    if steps <= 1 or tol <= 0:
        return [round(center, 4)]
    lo, hi = center * (1.0 - tol), center * (1.0 + tol)
    span = (hi - lo) / (steps - 1)
    return [round(lo + span * i, 4) for i in range(steps)]


def skill_scales():
    """Scale range for skill icons, centred on the calibrated skill scale."""
    return _scale_list(SKILL_SCALE_BASELINE * CALIBRATED_SCALE,
                       SKILL_SCALE_TOLERANCE, SKILL_SCALE_STEPS)


def ref_scales():
    """Scale range for UI refs, centred on the calibrated ref scale.

    Spans both the recaptured-native (1.0) and old-baseline (~0.86) ref scales;
    see REF_SCALE_* above.
    """
    return _scale_list(REF_SCALE_BASELINE * CALIBRATED_SCALE,
                       REF_SCALE_TOLERANCE, REF_SCALE_STEPS)

# ------------------------------------------------------------------ Click targets
# All coordinates are in the phone's native pixel space (phone-ADB space),
# which matches ADB screenshot pixels exactly.  These absolute values are only
# defaults/cache: resolve_geometry() recomputes them from the live screenshot
# resolution at macro start, so a wrong/stale PHONE_RESOLUTION can never push a
# tap or the skill band off-screen (which used to crash cv2.resize).
FIRST_SKILL_SLOT  = (97, 432)     # leftmost skill card  (fallback only)
SECOND_SKILL_SLOT = (270, 432)    # second skill card    (fallback only)
GAME_OVER_TAP     = (270, 403)    # dismiss the results screen

# ------------------------------------------------------------------ Skill categories
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

ACTIVE_CATEGORIES      = list(SKILL_CATEGORIES)
CUSTOM_PRIORITY_SKILLS = []
AVOID_SKILLS           = []

# ------------------------------------------------------------------ Template matching
MATCH_THRESHOLD = 0.78
REF_THRESHOLD   = 0.80
SKILL_DOWNSCALE = 0.5
# UI refs/banners are matched over the whole frame, so they dominate the poll
# time. Matching at this fraction of resolution cuts that ~2-3x with negligible
# accuracy loss (TM_CCOEFF_NORMED is scale-robust). Lower = faster but riskier
# for the short refs (game_over, level). 1.0 disables.
REF_DOWNSCALE   = 0.6

# Vertical band (phone-pixel y_top / y_bottom) the skill-icon search is
# restricted to.  Recomputed from the live resolution by resolve_geometry().
SKILL_MATCH_BAND = (307, 576)

# Card-sized box (w, h) around a slot for the avoid-skill check.
SKILL_SLOT_BOX = (170, 210)

# ---- Layout ratios (fractions of width/height) used to derive the absolute
# pixel geometry above for whatever resolution the device actually reports.
# Measured against the Archero-style skill screen; tweak if a device differs.
SKILL_BAND_TOP_RATIO    = 0.32    # skill-icon row sits ~0.45-0.50*H; give margin
SKILL_BAND_BOTTOM_RATIO = 0.60
SKILL_ROW_Y_RATIO       = 0.47    # y of the skill cards (fallback-tap height)
FIRST_SLOT_X_RATIO      = 0.18    # leftmost card centre
SECOND_SLOT_X_RATIO     = 0.50    # second card centre
GAME_OVER_X_RATIO       = 0.50
GAME_OVER_Y_RATIO       = 0.42

# Fallback skill-slot icon centres, by how many cards the screen shows.  The
# game centres the cards as a group and spaces them by count, so a 2-card screen
# (valkyrie / angel) puts its second card where a 3-card screen (level / glory)
# has empty space -- the old fixed [0.18, 0.50, 0.82] mis-tapped 2-card screens.
# x = fraction of width; the cards are always centred vertically (SKILL_SLOT_Y).
# When no wanted skill matches, the macro taps the leftmost of these NOT holding
# an avoid skill (slot1 -> slot2 -> slot3), so these must be accurate.
SKILL_SLOT_X_BY_COUNT = {
    3: (0.18, 0.50, 0.82),
    2: (0.29, 0.71),
}
# Tap height for the slots above: the icon row (SKILL_ROW_Y_RATIO), which is the
# same for 2- and 3-card screens -- only the horizontal spacing changes.
# Banners whose skill screen shows only TWO cards; all others show three.
SKILL_BANNERS_2_CARD  = ("valkyrie", "angel")

# Generic "dismiss a popup" tap: horizontal centre, ~10% up from the bottom.
# Used for challenge-ended screens and to clear wheel-spin result popups, where
# the real close button is a tiny/inconsistent text strip.
BOTTOM_TAP_X_RATIO      = 0.50
BOTTOM_TAP_Y_RATIO      = 0.90


def geometry_for(w, h):
    """Pure: return (band, first_slot, second_slot, game_over) for a (w, h),
    without mutating globals.  The macro calls this each frame in the
    normalised (MATCH_WIDTH) match space; the wizard uses resolve_geometry()."""
    w, h = int(w), int(h)
    band   = (round(h * SKILL_BAND_TOP_RATIO), round(h * SKILL_BAND_BOTTOM_RATIO))
    first  = (round(w * FIRST_SLOT_X_RATIO),  round(h * SKILL_ROW_Y_RATIO))
    second = (round(w * SECOND_SLOT_X_RATIO), round(h * SKILL_ROW_Y_RATIO))
    go     = (round(w * GAME_OVER_X_RATIO),   round(h * GAME_OVER_Y_RATIO))
    return band, first, second, go


def resolve_geometry(w, h):
    """Recompute and STORE the device-space skill band / slot / game-over coords
    from an actual (width, height).  Used by the setup wizard and at macro start
    so settings.json/the GUI show coords for the real device.  (Matching itself
    uses geometry_for() in normalised space; see MATCH_WIDTH.)"""
    global SKILL_MATCH_BAND, FIRST_SKILL_SLOT, SECOND_SKILL_SLOT, GAME_OVER_TAP
    SKILL_MATCH_BAND, FIRST_SKILL_SLOT, SECOND_SKILL_SLOT, GAME_OVER_TAP = \
        geometry_for(w, h)
    return SKILL_MATCH_BAND, FIRST_SKILL_SLOT, SECOND_SKILL_SLOT, GAME_OVER_TAP


# ------------------------------------------------------------------ Match zones
# Each ref / custom button can be pinned to a coarse, generous vertical HALF of
# the screen, so it is matched over ~50% of the frame instead of the whole thing
# (~2x faster). The zones OVERLAP, so a UI element near a boundary is still
# covered despite the vertical layout shifting between aspect ratios. "full"
# matches the whole frame. Per-ref choices live in ref/ref_zones.json, keyed by
# the path relative to ref/ ("level.png", "custom/a.png").
ZONE_BANDS = {
    "top":    (0.00, 0.50),
    "middle": (0.25, 0.75),
    "bottom": (0.50, 1.00),
}
ZONE_CHOICES = ("full", "top", "middle", "bottom")
REF_ZONES_PATH = REF_DIR / "ref_zones.json"


def zone_band(zone, h):
    """Pixel (y_top, y_bottom) for a zone at frame height `h`, or None for
    'full'/unknown (match the whole frame)."""
    r = ZONE_BANDS.get(zone)
    return None if r is None else (round(h * r[0]), round(h * r[1]))


def load_ref_zones():
    """ref-relative path -> zone, e.g. {'level.png': 'top'}. Missing file = {}."""
    try:
        data = json.loads(REF_ZONES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ref_zones(zones):
    REF_ZONES_PATH.write_text(json.dumps(zones, indent=2), encoding="utf-8")

# ------------------------------------------------------------------ Timing
POLL_INTERVAL = 1       # seconds between screen checks
ACTION_DELAY  = 1.2     # pause after a tap so the screen can settle
STARTUP_DELAY = 2.0     # grace period before the first poll

# ------------------------------------------------------------------ Run timeout
RUN_TIMEOUT_HOURS      = 2
CLOSE_ON_TIMEOUT       = True
SLEEP_PHONE_ON_TIMEOUT = True

CLOSE_PROCESSES = [
    "scrcpy.exe",
    "HD-Player.exe",
    "BlueStacks.exe",
    "BlueStacksServices.exe",
]

# adb binary; overridden by auto-detection in adb.find_adb().
ADB_PATH = "adb"

# ------------------------------------------------------------------ Humanisation
CLICK_JITTER      = 6
CLICK_DELAY_RANGE = (0.05, 0.25)
DELAY_JITTER      = 0.35

# ------------------------------------------------------------------ Reference images
REF_PLAY            = REF_DIR / "play_button.png"
REF_DEVIL           = REF_DIR / "devil_reject.png"
REF_GAME_OVER       = REF_DIR / "game_over.png"
REF_VALKYRIE        = REF_DIR / "valkyrie.png"
REF_LEVEL           = REF_DIR / "level.png"
REF_GLORY           = REF_DIR / "glory.png"
REF_ANGEL           = REF_DIR / "angel.png"
REF_REFRESH         = REF_DIR / "refresh.png"
REF_GET_READY       = REF_DIR / "get-ready.png"
REF_CONTINUE        = REF_DIR / "continue.png"
REF_START_CHALLENGE = REF_DIR / "start_challenge.png"

# ------------------------------------------------------------------ Capture
# Capture frames from a continuous Android screenrecord H.264 stream instead of
# a per-poll `screencap`. The stream keeps the display surface composited (fixes
# BlueStacks all-black frames during gameplay) and is much faster. Needs PyAV;
# falls back to screencap automatically if PyAV is missing or the stream fails.
USE_STREAM_CAPTURE = True

# ------------------------------------------------------------------ GUI
AUTOSAVE   = False
DARK_MODE  = True
KEEP_AWAKE = False

ETERNAL_LODE_MODE = False

# High-level game mode chosen in the GUI dropdown.
# "chapter" / "eternal" / "plant" / "jungle"
GAME_MODE = "chapter"

# ------------------------------------------------------------------ Eternal Lode
# Movement: a swipe-and-hold on the virtual joystick, run once after the first
# skill selection of each gamemode entry (in 1x speed for consistency). The
# joystick spawns wherever the swipe starts, so we start at the bottom centre.
# Each swipe goes MOVEMENT_SWIPE_LEN_RATIO of the frame HEIGHT in the chosen
# angle, held for the chosen duration. Each mode stores two vectors
# (angle1, dur1, angle2, dur2); set a duration to 0 to skip that vector.
# MODE: 0 = Don't move, 1 = Chapter Position, 2 = Plant Defense, 3 = Custom.
MOVEMENT_MODE             = 0
MOVEMENT_JOYSTICK_X_RATIO = 0.50    # joystick start x (fraction of width)
MOVEMENT_JOYSTICK_Y_RATIO = 0.90    # joystick start y (~10% up from the bottom)
MOVEMENT_SWIPE_LEN_RATIO  = 0.10    # swipe length as a fraction of frame height
MOVEMENT_CHAPTER = (90.0, 0.5, 0.0, 0.0)
MOVEMENT_CUSTOM  = (0.0, 1.0, 180.0, 1.0)

# Plant Defense movement: the spawn position is decided by HOW the match was
# entered, not by image detection -- the host (who started the match via
# start_challenge) always spawns in position 1 (left side), while a guest (who
# joined and entered via get-ready) spawns in position 2 (right side). main.py
# records the spawn when the start button is tapped and feeds it here, so the
# script just picks the appropriate two-vector path for the chosen direction.
#
# T is the only tuning parameter -- all durations are stored as T-multiples so
# every movement scales together when T is adjusted.
# angle convention: 0=right, 90=up, 180=left, 270=down.
MOVEMENT_PLANT_PRESET = "top"   # direction: top / bottom / left / right
MOVEMENT_PLANT_T      = 0.3    # seconds per unit; tune once, all durations scale

# (angle_deg, T_multiple, angle_deg, T_multiple)
# Suffix 1 = spawned left / suffix 2 = spawned right.
# Bottom: single diagonal to the base of the plant.
# Top / Left / Right: two-vector arc around the plant's rectangular hitbox.
# Near-side destinations (1→Left, 2→Right): 2.5T + 1.5T total.
# Cross destinations (1→Right, 2→Left): 4T + 2T total.
_PLANT_PRESETS = {
    "bottom1": ( 45.0, 1.0,   0.0, 0.0),
    "bottom2": (135.0, 1.0,   0.0, 0.0),
    "top1":    (130.0, 4.5,   0.0, 4.0),
    "top2":    ( 50.0, 4.5, 180.0, 4.0),
    "right1":  ( 15.0, 4.0, 150.0, 2.0),
    "right2":  ( 50.0, 2.5, 180.0, 1.5),
    "left1":   (130.0, 2.5,   0.0, 1.5),
    "left2":   (165.0, 4.0,  30.0, 2.0),
}


def plant_movement(spawn: int):
    """Return (angle1, dur1, angle2, dur2) for the current MOVEMENT_PLANT_PRESET
    and detected spawn position. Durations are in seconds (T_multiple * T)."""
    key = f"{MOVEMENT_PLANT_PRESET}{spawn}"
    a1, m1, a2, m2 = _PLANT_PRESETS.get(key, _PLANT_PRESETS.get(
        f"{MOVEMENT_PLANT_PRESET}1", (90.0, 1.0, 0.0, 0.0)))
    return (a1, m1 * MOVEMENT_PLANT_T, a2, m2 * MOVEMENT_PLANT_T)

ETERNAL_LODE_DIR  = REF_DIR / "Eternal Lode"
EL_BOARD_COLS     = 6
EL_BOARD_ROWS     = 8
EL_BOARD_W        = 484
EL_BOARD_H        = 656
EL_CELL_THRESHOLD = 0.72
EL_UI_THRESHOLD   = 0.78
EL_ACTION_DELAY      = 0.5
EL_CHEST_CLICK_DELAY = 0.3
EL_CHEST_MAX_CLICKS  = 20

# ------------------------------------------------------------------ Settings persistence
SETTINGS_PATH = BASE_DIR / "settings.json"

PERSISTED_KEYS = (
    "POLL_INTERVAL", "ACTION_DELAY", "STARTUP_DELAY",
    "RUN_TIMEOUT_HOURS", "CLOSE_ON_TIMEOUT", "SLEEP_PHONE_ON_TIMEOUT",
    "MATCH_THRESHOLD", "REF_THRESHOLD", "SKILL_DOWNSCALE", "REF_DOWNSCALE",
    "CALIBRATED_SCALE", "ADB_DEVICE", "PHONE_RESOLUTION",
    "CLICK_JITTER", "DELAY_JITTER",
    "FIRST_SKILL_SLOT", "SECOND_SKILL_SLOT", "GAME_OVER_TAP",
    "SKILL_MATCH_BAND",
    "ACTIVE_CATEGORIES", "CUSTOM_PRIORITY_SKILLS", "AVOID_SKILLS",
    "USE_STREAM_CAPTURE",
    "AUTOSAVE", "DARK_MODE", "KEEP_AWAKE", "ETERNAL_LODE_MODE", "GAME_MODE",
    "MOVEMENT_MODE", "MOVEMENT_CHAPTER", "MOVEMENT_PLANT_PRESET", "MOVEMENT_PLANT_T",
    "MOVEMENT_CUSTOM",
    "MOVEMENT_JOYSTICK_X_RATIO", "MOVEMENT_JOYSTICK_Y_RATIO",
    "MOVEMENT_SWIPE_LEN_RATIO",
)

_TUPLE_KEYS = frozenset({
    "FIRST_SKILL_SLOT", "SECOND_SKILL_SLOT", "GAME_OVER_TAP",
    "SKILL_MATCH_BAND",
    "MOVEMENT_CHAPTER", "MOVEMENT_CUSTOM",
})


def current_settings():
    """Persisted-key settings as a plain, JSON-ready dict."""
    g = globals()
    out = {}
    for key in PERSISTED_KEYS:
        val = g[key]
        out[key] = list(val) if isinstance(val, tuple) else val
    return out


def apply_settings(data):
    """Override config globals from `data` (persisted keys only), then
    recompute derived values.  Unknown keys are silently ignored."""
    g = globals()
    for key, val in data.items():
        if key not in PERSISTED_KEYS:
            continue
        if key in _TUPLE_KEYS and isinstance(val, list):
            val = tuple(val)
        g[key] = val


def load_settings(path=None):
    """Load and apply settings.json.  Returns True if a file was read."""
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
    """Write current persisted settings to settings.json."""
    path = Path(path) if path else SETTINGS_PATH
    path.write_text(json.dumps(current_settings(), indent=2), encoding="utf-8")


load_settings()
