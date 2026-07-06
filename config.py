import json
import sys
from pathlib import Path

# App version, baked into each build. The updater compares the GitHub release
# tag against version.txt (written by build.bat), NOT this constant; this one
# is for the main app to display. Bump both before tagging a release.
VERSION = "2.2.2"

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

ACTIVE_CATEGORIES      = ["Sprites", "Attack Speed", "Elemental"]
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

# After a skill-selection screen is first detected, wait this long and re-grab
# the frame before scanning the cards.  The cards and the Refresh button animate
# in, so a fast poll can scan too early and miss them -- defaulting to the first
# slot, or not realising Refresh is available.  0.1-0.2s covers the animation;
# set to 0 to disable.
SKILL_SETTLE_DELAY = 0.15

# ------------------------------------------------------------------ Run timeout
RUN_TIMEOUT_HOURS      = 2
STUCK_TIMEOUT_MINUTES  = 5
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

# adb SERVER port for this app. BlueStacks ships its own adb (HD-Adb.exe, an old
# 1.0.36) which, when BlueStacks has run, leaves a 1.0.36 server on the shared
# default port 5037. The modern SDK adb the macro uses then detects the version
# mismatch and kills+restarts that server on every command -- the "version war"
# -- and during the restart a USB phone briefly drops out of `adb devices`, so
# connect() fails with "Device ... not found". Giving this app its OWN server
# port isolates it from BlueStacks's 5037 server so the two never fight.
#
# This is safe for BlueStacks targets: emulators are reached with an explicit
# `adb connect 127.0.0.1:<port>` over TCP, which works from any local server.
# Set to 5037 (or 0/None) to share the system server instead (the old behaviour).
ADB_SERVER_PORT = 5038


def apply_adb_server_port():
    """Pin every adb subprocess this process spawns to ADB_SERVER_PORT via the
    ANDROID_ADB_SERVER_PORT env var (adb reads it; subprocesses inherit os.environ
    so capture.py's screenrecord uses the same server). Falsy = leave the
    environment alone (system default 5037)."""
    import os
    if ADB_SERVER_PORT:
        os.environ["ANDROID_ADB_SERVER_PORT"] = str(ADB_SERVER_PORT)

# ------------------------------------------------------------------ Humanisation
CLICK_JITTER      = 6
CLICK_DELAY_RANGE = (0.05, 0.25)
DELAY_JITTER      = 0.35

# ------------------------------------------------------------------ Reference images
# Refs the skill-selection flow needs by name (loaded into Macro.refs).  All
# other buttons are loaded by filename via Macro._load_group, so adding a new
# tap-on-sight button needs no constant here.
REF_PLAY     = REF_DIR / "play_button.png"
REF_DEVIL    = REF_DIR / "devil_reject.png"
REF_VALKYRIE = REF_DIR / "valkyrie.png"
REF_LEVEL    = REF_DIR / "level.png"
REF_GLORY    = REF_DIR / "glory.png"
REF_ANGEL    = REF_DIR / "angel.png"
REF_REFRESH  = REF_DIR / "refresh.png"

# ------------------------------------------------------------------ Capture
# Capture frames from a continuous Android screenrecord H.264 stream instead of
# a per-poll `screencap`. The stream keeps the display surface composited (fixes
# BlueStacks all-black frames during gameplay) and is much faster. Needs PyAV;
# falls back to screencap automatically if PyAV is missing or the stream fails.
USE_STREAM_CAPTURE = True

# ------------------------------------------------------------------ GUI
AUTOSAVE   = True
DARK_MODE  = True
KEEP_AWAKE = False
HOTKEY     = "ctrl+`"

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

# Plant Defense movement: the spawn position is decided by the USERNAME
# ALPHABETICAL ORDER of the two players, NOT by who hosted (an earlier version
# wrongly deduced it from the entry button). The macro cannot read usernames,
# so the user sets their side once with the Spawn Side toggle in the GUI's
# Plant Defense panel; with the same partner it stays correct all session
# (watch where you spawn in round 1 if unsure). main.py reads PLANT_SPAWN
# when the movement runs and picks the matching two-vector path.
#
# T is the only tuning parameter -- all durations are stored as T-multiples so
# every movement scales together when T is adjusted.
# angle convention: 0=right, 90=up, 180=left, 270=down.
PLANT_SPAWN           = 1      # 1 = spawn left, 2 = spawn right (set in GUI)
PLANT_ROUNDS          = 10     # stop after this many completed rounds (0 = unlimited)
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
EL_BOARD_W        = 484          # legacy: BlueStacks-scale board px (reference only)
EL_BOARD_H        = 656

# Board geometry is anchored to the bright-green "depth frontier" line each
# iteration. The line gives board left/right and cell width; cell_w yields a
# device_scale that pre-scales every Eternal Lode template once.
EL_TEMPLATE_CELL_PX = 75.0       # native cell template size the refs were cut at
EL_LINE_MIN_GREEN   = 220        # depth line is near-pure green (G); timer bar is ~180
EL_LINE_MIN_DELTA   = 90         # G must exceed R and B by this much
EL_LINE_SEARCH_TOP  = 0.30       # search band for the line (fraction of height)
EL_LINE_SEARCH_BOT  = 0.95
EL_LINE_MIN_PIXELS  = 0.25       # min green px on the line row, as a fraction of width
EL_LINE_MIN_SPAN    = 0.40       # min horizontal span of the line, fraction of width
EL_TRIANGLE_MAX_THICK = 12       # green px taller than this at a column => triangle, trim it

# Brightness-based cell classification. The game renders diggable cells at full
# brightness and hidden (non-diggable) cells dimmed. Validated separation:
#   diggable ~110-127, hidden ~75-81, empty ~72 (stddev ~1).
EL_BRIGHT_THRESHOLD = 95         # avg brightness above this = diggable
EL_EMPTY_BRIGHT_MAX = 75         # avg brightness below this + low stddev = empty
EL_EMPTY_STD_MAX    = 3.0        # stddev below this (with low brightness) = empty
EL_CELL_INSET       = 0.15       # fractional inset for brightness crop (avoids grid lines)

EL_CELL_THRESHOLD = 0.60         # template match confidence for cell classification
EL_UI_THRESHOLD   = 0.78
EL_ZERO_THRESHOLD = 0.93         # resource zero-detection (whole-button matching)
EL_TOOLBAR_BAND   = (0.82, 1.0)  # bottom strip where tool/resource buttons live

EL_BOARD_TOP_FRAC = 0.42         # upper y-guard (fraction of screen) so scans stay on the board

EL_FAST_MODE         = False       # scan bottom 2 rows first, dig lowest cell only
EL_ACTION_DELAY      = 0.5
EL_CHEST_SPAM_SECS   = 2.0        # spam-tap chests for this long (no scan between taps)
EL_CHEST_TAP_DELAY   = 0.3       # delay between spam taps

# ------------------------------------------------------------------ All-Star Cup
# A speed-optimised mode for the All-Star Cup event.  Each level presents three
# boss-selection sets (in place of the usual skill picks); within one event a
# boss appears in at most one of a level's sets.  The macro scans the three
# card slots, identifies which bosses are showing, and selects the visible one
# with the LOWEST death-animation duration (fastest to despawn = fastest run).
# The per-level pools below are the usual line-ups, but they DRIFT slightly
# between events (a color variant from another level's row can swap in), so by
# default every boss template is scanned, not just the level's 9.
#
# Templates live in ref/all_star/bosses/<name>.png: each boss icon flattened on
# black and pre-scaled to how it renders on screen at ALL_STAR_CALIB_WIDTH
# (built by ALL STAR/make_boss_templates.py).  The slot positions, the 0.9
# render scale, and the match threshold were all measured from in-game example
# screenshots (see ALL STAR/ in the repo).
ALL_STAR_DIR       = REF_DIR / "all_star"
ALL_STAR_BOSS_DIR  = ALL_STAR_DIR / "bosses"
ALL_STAR_CHALLENGE = ALL_STAR_DIR / "challenge.png"

# Which level to play: 1..7, or the string "elim" for the Elimination mode
# (see ALL_STAR_ELIM_BOSSES).  Set by the GUI dropdown.
ALL_STAR_LEVEL = 1

# One-time GUI warning that All-Star mode only works on rooted BlueStacks.
# Set to True after the popup has been shown once, so it never nags again.
ALL_STAR_ROOT_WARNED = False

# Per-level boss pools: name -> death-animation duration in seconds (lower is
# better; only the relative order matters).  From ALL STAR/allstarbossdata.csv.
# Each name maps to ref/all_star/bosses/<name>.png; pool entries whose template
# is missing are logged at start and simply cannot be identified or picked.
ALL_STAR_LEVELS = {
    1: {
        "purple-whirl": 0.05,   "red-whirl": 0.067,       "green-whirl": 0.083,
        "red-flower": 0.55,     "purple-flower": 0.633,   "orange-flower": 0.683,
        "purple-witch": 1.55,   "blue-witch": 1.617,      "orange-pot-witch": 1.717,
    },
    2: {
        "blue-book": 0.067,     "red-dragon": 0.517,      "purple-dragon": 0.55,
        "blue-dragon": 0.65,    "orange-pumpkin": 0.683,  "red-slime": 0.933,
        "yellow-cyc-mage": 1.05, "red-ghost-lantern": 1.65, "purple-ghost-lantern": 1.717,
    },
    3: {
        "green-snake": 0.633,   "red-golem": 0.683,       "grey-golem": 0.7,
        "green-golem": 0.8,     "green-cyc-mage": 0.883,  "red-cyc-mage": 0.983,
        "purple-q-bee": 1.283,  "yellow-q-bee": 1.283,    "green-q-medusa": 1.683,
    },
    4: {
        "reg-tomb-keeper": 0.567, "blue-archer": 0.8,     "tall-flame-mage": 1.083,
        "red-q-bee": 1.35,      "purple-demon": 1.583,    "red-hog": 1.65,
        "blue-hog": 1.683,      "yellow-demon": 1.717,    "red-demon": 1.717,
    },
    5: {
        "purple-wingseed": 0.05, "orange-wingseed": 0.05, "sunflower": 0.05,
        "purp-scythe": 0.65,    "green-yeti": 1.55,       "brown-hog": 1.567,
        "purp-yeti": 1.667,     "blue-yeti": 1.683,       "blue-frost": 2.383,
    },
    6: {
        "zomblet": 0.45,        "helix": 0.617,           "red-conch": 0.717,
        "nyanja": 0.733,        "nian-beast": 0.733,      "alex": 0.733,
        "seraph": 0.758,        "blue-conch": 0.883,      "phynx": 1.017,
    },
    7: {
        "demon-bat": 0.567,     "ghost-bat": 0.658,       "otta": 0.75,
        "dracoola": 0.833,      "lavashell-turt": 1.017,  "rolla": 1.167,
        "ghost-mask": 1.55,     "ember-mage": 1.583,      "violet-cat": 1.65,
    },
}

# Geometry was calibrated against screenshots this many px wide; the templates
# and the slot ratios below are scaled to the live frame by (live_width / this).
ALL_STAR_CALIB_WIDTH = 1080

# The three card slots as a fraction of width, and their shared vertical centre
# as a fraction of height.  Each slot is scanned in a box of +/- the half-size
# ratios around its centre (the slot boxes do not overlap).
#
# The cards' VERTICAL position is not a fixed fraction of height across aspect
# ratios: a taller screen (Pixel 1080x2410, ~0.448) centres them lower than a
# shorter one (BlueStacks 540x960, ~0.5625), where the boss sits at ~0.456H not
# 0.50H.  So the y-centre is set to 0.47 (between the two) and the box is made
# tall enough (half-h 0.075) to fully contain the boss on BOTH: verified 21/21
# bosses on the Pixel shots at 0.96-0.99 (unchanged) AND purple-whirl on a real
# BlueStacks frame at 0.978 (was a total miss at the old 0.50/0.052).  The X
# ratios are stable across aspect ratios (horizontal layout is width-symmetric).
ALL_STAR_SLOT_X_RATIOS     = (0.210, 0.499, 0.790)
ALL_STAR_SLOT_Y_RATIO      = 0.47
ALL_STAR_SLOT_HALF_W_RATIO = 0.111      # 120/1080
ALL_STAR_SLOT_HALF_H_RATIO = 0.075      # tall enough to cover the card-Y shift across aspect ratios

# Confidence to accept a boss in a slot.  Measured self-match is ~0.96-0.99 and
# the worst look-alike (purple vs red whirl, same shape) is ~0.81, so 0.85
# separates cleanly while leaving margin for stream compression / animation.
ALL_STAR_THRESHOLD = 0.85
# Slot crops are matched at this fraction of resolution for speed: ~3x faster
# (3ms vs 9ms per 3-slot frame) with colour preserved, so same-shape /
# different-colour bosses (the whirls) still separate.
ALL_STAR_SCAN_DOWNSCALE = 0.5

# Challenge (start-the-level) button.
ALL_STAR_CHALLENGE_THRESHOLD = 0.80
ALL_STAR_START_TIMEOUT       = 5   # s to look for Challenge before assuming the level already started

# Spam-tap target used to skip loading and clear intermediate screens: a spot
# that is empty on both the loading screen and the boss-selection screen (well
# below the cards), so the taps never select a card or trigger a button.
ALL_STAR_TAP_X_RATIO  = 0.50
ALL_STAR_TAP_Y_RATIO  = 0.85    # dead centre, low (below cards + countdown, on empty background)
# Minimum extra delay between spam taps.  0 means tap as fast as the tapper
# allows: the synchronous fast tap already self-paces at ~25ms each (it blocks
# until the tap lands), so no artificial delay is needed and both the
# loading-skip spam and the boss-select spam stay genuinely rapid (~40 taps/s).
ALL_STAR_TAP_INTERVAL = 0.0

ALL_STAR_SCAN_TIMEOUT   = 60.0        # s to wait for a set's bosses before giving up
# Scan every boss template (all levels, 63) instead of only the current level's
# 9-boss pool.  The pools drift slightly between events (a color variant from
# another level's CSV row can swap in, which the pool-only scan cannot
# identify), and the full scan proved fast enough live, so it is the default.
# Turn off to scan only the level's pool (~13x cheaper while nothing matches).
ALL_STAR_SCAN_ALL_BOSSES = True

# --- Elim (Elimination) mode: ALL_STAR_LEVEL = "elim" --------------------------
# Ten levels, each starting with the same 3-boss selection popup.  The user
# pre-picks ONE boss per level in the GUI (thumbnails labelled with death-anim
# seconds); each level's scan looks ONLY for that boss and taps it on sight.
# A level with no pick falls back to scanning every template and taking the
# fastest-dying visible boss.
ALL_STAR_ELIM_SETS   = 10
# Ordered picks for levels 1..10: boss template names ("" = no pick).  Set by
# the GUI's Elim boss picker.
ALL_STAR_ELIM_BOSSES = []
# Seconds to keep spam-tapping the chosen slot after detection.  The boss is
# detected the instant it appears (while still small), but the card does not
# become selectable until it finishes growing/settling a moment later, so the
# spam must last long enough to still be tapping when it becomes selectable.
ALL_STAR_SELECT_BRIEF   = 0.5
# After a boss is selected the PLAYER fights it (~10-20s) and the screen then
# goes black to transition to the next set.  The macro waits out that fight for
# the black screen WITHOUT tapping (the player is moving the character), up to
# this long.  Must comfortably exceed the longest fight.
ALL_STAR_BLACK_TIMEOUT  = 360
ALL_STAR_BLACK_MEAN     = 12          # frame mean brightness below this = black transition between sets

# --- 3rd-boss death detection: pause the instant its health bar empties --------
# After the THIRD boss is selected the macro stops tapping and rapidly polls the
# boss health bar (the long red bar at the top of the screen). It taps the pause
# button ONCE the instant the bar empties on the KILL, then stops -- the user exits
# manually for the time bonus.
#
# The 3rd part flow (per the user): the bar is EMPTY during the minions phase
# (which can be long), then the boss spawns and the bar FILLS to full, stays full
# briefly, then depletes to empty as the boss dies. Both the minions phase and the
# death look "empty" by red-fraction, so the macro must ARM first (confirm the bar
# filled = boss spawned) and only then treat empty as the kill. That is the
# FULL_FRAC -> EMPTY_FRAC two-state guard below; it also covers the pre-fight
# transition (no bar = reads empty). Detection = saturated-red fill fraction of the
# bar crop: ~0.5-0.9 full, ~0.00-0.01 empty (measured on real BlueStacks + Pixel).
ALL_STAR_PAUSE_ON_3RD_DEATH = True
ALL_STAR_HP_FULL_FRAC     = 0.30   # red-frac confirming the bar filled = boss spawned (arms the watch)
ALL_STAR_HP_EMPTY_FRAC    = 0.04   # red-frac at/below this (AFTER arming) = boss dead -> pause. Raise to pause earlier, lower to wait for a more-empty bar.
ALL_STAR_HP_SPAWN_TIMEOUT = 300.0  # s to wait for the boss to spawn (bar to fill) through the minions phase; if it never fills, skip the auto-pause (do NOT risk a wrong pause)
ALL_STAR_HP_DEATH_TIMEOUT = 180.0  # s to watch the spawned boss deplete to empty before giving up

# The health bar and pause button sit at a different HEIGHT fraction on a tall
# phone (Pixel, w/h ~0.448) than a short emulator (BlueStacks, ~0.5625) -- the same
# aspect-ratio shift as the boss cards (see ALL_STAR_SLOT_Y_RATIO). So both are
# keyed by aspect: w/h >= split uses the WIDE (BlueStacks) preset, else TALL (Pixel).
# Bar crop = (x0, x1, y0, y1) ratios (cropped to EXACTLY the bar, excluding the
# boss icon and the dark segment bar above it). Pause button = (x, y) ratios.
# Measured from ALL STAR/Boss death detection/ screenshots.
ALL_STAR_HP_ASPECT_SPLIT = 0.50
ALL_STAR_HP_BAR_WIDE  = (0.27, 0.83, 0.106, 0.120)   # BlueStacks 540x960
ALL_STAR_HP_BAR_TALL  = (0.28, 0.86, 0.153, 0.174)   # Pixel 1080x2410
ALL_STAR_PAUSE_WIDE   = (0.069, 0.045)
ALL_STAR_PAUSE_TALL   = (0.069, 0.108)


def all_star_hp_geometry(w, h):
    """Return (bar_region, pause_xy) ratio tuples for this frame's aspect ratio."""
    wide = (w / max(1, h)) >= ALL_STAR_HP_ASPECT_SPLIT
    bar = ALL_STAR_HP_BAR_WIDE if wide else ALL_STAR_HP_BAR_TALL
    pause = ALL_STAR_PAUSE_WIDE if wide else ALL_STAR_PAUSE_TALL
    return bar, pause

# ------------------------------------------------------------------ Fast input
# Low-latency tap injection (fastinput.FastTapper).  Instead of `adb shell input
# tap` (~35ms each: boots app_process + opens a connection), feed `sendevent`
# commands to one persistent `adb shell` (~20ms per tap, no connection or JVM
# startup).  Used by the All-Star mode where tap latency is the dominant delay.
# Needs a writable /dev/input node: true on rooted BlueStacks (SELinux Disabled +
# shell in the `input` group) and rooted phones.  Falls back to `adb input tap`
# automatically whenever the fast path cannot be set up.
FAST_TAP_ENABLED   = True
# Extra seconds to hold each touch down before releasing.  sendevent's own
# spacing between the down and up calls already registers a tap on BlueStacks, so
# 0 is fine; a positive value inserts an on-device `sleep` between down and up for
# devices that need a longer press.
FAST_TAP_HOLD      = 0.0
# Screen-pixel to event-coordinate mapping.  "auto" picks "rotated" for
# BlueStacks (portrait app on a landscape panel; a square normalised touch grid)
# and "direct" otherwise.  Force "rotated" or "direct" to override.
FAST_TAP_TRANSFORM = "auto"

# ------------------------------------------------------------------ Settings persistence
SETTINGS_PATH = BASE_DIR / "settings.json"

PERSISTED_KEYS = (
    "POLL_INTERVAL", "ACTION_DELAY", "STARTUP_DELAY", "SKILL_SETTLE_DELAY",
    "RUN_TIMEOUT_HOURS", "STUCK_TIMEOUT_MINUTES",
    "CLOSE_ON_TIMEOUT", "SLEEP_PHONE_ON_TIMEOUT",
    "MATCH_THRESHOLD", "REF_THRESHOLD", "SKILL_DOWNSCALE", "REF_DOWNSCALE",
    "CALIBRATED_SCALE", "ADB_DEVICE", "PHONE_RESOLUTION", "ADB_SERVER_PORT",
    "ADB_PATH",
    "CLICK_JITTER", "DELAY_JITTER",
    "FIRST_SKILL_SLOT", "SECOND_SKILL_SLOT", "GAME_OVER_TAP",
    "SKILL_MATCH_BAND",
    "ACTIVE_CATEGORIES", "CUSTOM_PRIORITY_SKILLS", "AVOID_SKILLS",
    "USE_STREAM_CAPTURE",
    "AUTOSAVE", "DARK_MODE", "KEEP_AWAKE", "HOTKEY",
    "ETERNAL_LODE_MODE", "GAME_MODE", "EL_FAST_MODE", "ALL_STAR_LEVEL",
    "ALL_STAR_SCAN_ALL_BOSSES", "ALL_STAR_ELIM_BOSSES", "ALL_STAR_ROOT_WARNED",
    "FAST_TAP_ENABLED", "FAST_TAP_HOLD", "FAST_TAP_TRANSFORM",
    "ALL_STAR_PAUSE_ON_3RD_DEATH", "ALL_STAR_HP_FULL_FRAC", "ALL_STAR_HP_EMPTY_FRAC",
    "MOVEMENT_MODE", "MOVEMENT_CHAPTER", "MOVEMENT_PLANT_PRESET", "MOVEMENT_PLANT_T",
    "PLANT_SPAWN", "PLANT_ROUNDS",
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
apply_adb_server_port()
