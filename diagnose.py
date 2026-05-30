"""Calibration / diagnostic tool.
Captures the configured region and reports what the macro
would see.
Usage:
    python diagnose.py            # capture region + run all checks
    python diagnose.py full       # also save a full-screen capture
"""

import sys
import time

import cv2
import numpy as np
import pyautogui

import config
from matcher import (
    grab_screen_bgr,
    is_blank,
    list_skill_files,
    load_template,
    make_scales,
    multi_scale_match,
)

COUNTDOWN = 4
CAPTURE_PATH = config.BASE_DIR / "debug_capture.png"
FULLSCREEN_PATH = config.BASE_DIR / "debug_fullscreen.png"


def banner(text):
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)


def check_dpi():
    """Warn if captured pixel size differs from the logical screen size, which
    means Windows display scaling will throw off click coordinates."""
    banner("DISPLAY / DPI CHECK")
    logical = pyautogui.size()
    shot = pyautogui.screenshot()
    pixels = shot.size
    print(f"  logical screen size (pyautogui.size): {logical[0]} x {logical[1]}")
    print(f"  captured screenshot size:             {pixels[0]} x {pixels[1]}")
    if (logical[0], logical[1]) != pixels:
        print("  WARNING: sizes differ -> Windows display scaling is active.")
        print("  Clicks may land in the wrong place. Set Windows display")
        print("  scaling to 100%, OR re-measure all coordinates with the")
        print("  same scaling that is active when the macro runs.")
    else:
        print("  OK, no scaling mismatch.")
    return shot


def capture_region():
    banner("REGION CAPTURE")
    print(f"  region = {config.BLUESTACKS_REGION}")
    print(f"  focus the game window (BlueStacks or scrcpy) now, "
          f"capturing in {COUNTDOWN}s...")
    for i in range(COUNTDOWN, 0, -1):
        print(f"    {i}...", flush=True)
        time.sleep(1)

    image = grab_screen_bgr(config.BLUESTACKS_REGION)
    cv2.imwrite(str(CAPTURE_PATH), image)
    print(f"  saved capture -> {CAPTURE_PATH}")

    arr = np.asarray(image)
    print(f"  size: {arr.shape[1]} x {arr.shape[0]}   "
          f"mean: {arr.mean():.1f}   std: {arr.std():.1f}")
    if is_blank(image):
        print("  WARNING: capture is blank/uniform (likely all black).")
    else:
        print("  OK, capture has real content.")
    return image


def check_refs(image):
    banner("REFERENCE IMAGE MATCHES")
    print(f"  ref threshold = {config.REF_THRESHOLD}\n")
    ref_scales = make_scales(config.REF_SCALE_RANGE)
    refs = {
        "play_button": config.REF_PLAY,
        "devil_reject": config.REF_DEVIL,
        "game_over": config.REF_GAME_OVER,
        "valkyrie": config.REF_VALKYRIE,
        "level": config.REF_LEVEL,
        "glory": config.REF_GLORY,
        "refresh": config.REF_REFRESH,
        "get-ready": config.REF_GET_READY,
        "continue": config.REF_CONTINUE,
        "start_challenge": config.REF_START_CHALLENGE,
    }
    for name, path in refs.items():
        try:
            tpl = load_template(path)
        except (FileNotFoundError, ValueError) as e:
            print(f"  {name:14s}: MISSING ({e})")
            continue
        if is_blank(tpl):
            print(f"  {name:14s}: BLANK placeholder, re-capture this image")
            continue
        conf, center, size = multi_scale_match(image, tpl, ref_scales)
        verdict = "MATCH" if conf >= config.REF_THRESHOLD else "no match"
        loc = f"region {center}" if center else "n/a"
        print(f"  {name:14s}: conf {conf:.3f}  [{verdict}]  {loc}")


def check_skills(image):
    banner("SKILL MATCHES (active categories)")
    print(f"  skill threshold = {config.MATCH_THRESHOLD}   "
          f"downscale = {config.SKILL_DOWNSCALE}")
    print("  active categories: "
          + (", ".join(config.ACTIVE_CATEGORIES) or "(none)"))
    print("  (only meaningful when a skill-selection screen is captured)\n")
    skill_scales = make_scales(config.SCALE_RANGE)

    # Active categories in priority order as the macro scans them.
    results = []  # (category, name, conf, center, scale)
    t0 = time.time()
    for category in config.ACTIVE_CATEGORIES:
        files = list_skill_files(config.SKILLS_DIR / category)
        if not files:
            print(f"  [{category}]: no icons in this category")
            continue
        for path in files:
            try:
                tpl = load_template(path)
            except (FileNotFoundError, ValueError) as e:
                print(f"  {category}/{path.name}: MISSING ({e})")
                continue
            conf, center, size = multi_scale_match(
                image, tpl, skill_scales, config.SKILL_DOWNSCALE)
            scale = size[0] / tpl.shape[1] if size else 0.0
            results.append((category, path.name, conf, center, scale))
    print(f"\n  scanned {len(results)} icon(s) in {time.time() - t0:.1f}s "
          f"(the macro stops at the first match, so it is usually faster)\n")

    if not results:
        print("  no skill icons to scan, add PNGs to the skills/ category "
              "subfolders.")
        return

    by_conf = sorted(results, key=lambda r: r[2], reverse=True)
    print("  top 5 template matches (by confidence):")
    for category, name, conf, center, scale in by_conf[:5]:
        verdict = "MATCH" if conf >= config.MATCH_THRESHOLD else "no match"
        print(f"    {category}/{name:8s}  conf {conf:.3f}  scale {scale:.2f}  "
              f"[{verdict}]  region {center}")

    matched_scales = [r[4] for r in results if r[2] >= config.MATCH_THRESHOLD]
    if matched_scales:
        print(f"\n  matches landed at scales {min(matched_scales):.2f}-"
              f"{max(matched_scales):.2f}  (SCALE_RANGE is {config.SCALE_RANGE};"
              f" narrowing it toward this range is the biggest extra speed-up)")

    # Mirror the macro's pick
    band = config.SKILL_MATCH_BAND
    pick = None
    outside = []
    for category, name, conf, center, scale in results:   # priority order
        if conf < config.MATCH_THRESHOLD or center is None:
            continue
        if band is None or band[0] <= center[1] <= band[1]:
            if pick is None:
                pick = (category, name, conf)
        else:
            outside.append((f"{category}/{name}", center))

    if pick:
        print(f"\n  -> macro would pick: {pick[0]} / {pick[1]}  "
              f"(conf {pick[2]:.3f}, first skill above threshold)")
    else:
        print(f"\n  -> no skill above threshold; macro would click "
              f"the first slot {config.FIRST_SKILL_SLOT}")

    if outside:
        print(f"  NOTE: {len(outside)} match(es) fell outside SKILL_MATCH_BAND "
              f"{band}: " + ", ".join(f"{n}@y{c[1]}" for n, c in outside))
        print("  If a real skill icon is among them, widen SKILL_MATCH_BAND.")


def main():
    want_fullscreen = len(sys.argv) > 1 and sys.argv[1].lower() == "full"

    shot = check_dpi()
    if want_fullscreen:
        shot.save(FULLSCREEN_PATH)
        print(f"  saved full screen -> {FULLSCREEN_PATH}")

    image = capture_region()
    check_refs(image)
    check_skills(image)

if __name__ == "__main__":
    main()
