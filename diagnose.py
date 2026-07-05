"""Diagnostic tool for A2 Macro Controller.

Connects to the ADB device, takes a screenshot, and reports what the
macro sees: ADB connection status, template-match results, and which
skill or UI element would be acted on.

Usage:
    python diagnose.py            # connect, screenshot, run all checks
    python diagnose.py save       # also write an annotated debug image
"""

import sys
import time
from pathlib import Path

import cv2
import numpy as np

import config
from adb import ADBClient, ADBError, find_adb, list_devices
from matcher import (
    is_blank,
    list_skill_files,
    load_template,
    multi_scale_match,
    resize_to_width,
    save_image,
)

CAPTURE_PATH    = config.BASE_DIR / "debug_capture.png"
ANNOTATED_PATH  = config.BASE_DIR / "debug_annotated.png"


def banner(text):
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)


# ------------------------------------------------------------------
# ADB diagnostics
# ------------------------------------------------------------------

def check_adb():
    """Verify the ADB binary, list devices, connect, and return an
    ADBClient ready for screencap -- or None on failure."""
    banner("ADB CONNECTION CHECK")

    # Locate adb binary.
    try:
        adb_exe = find_adb(config.ADB_PATH)
        print(f"  adb binary : {adb_exe}")
    except RuntimeError as e:
        print(f"  ERROR: {e}")
        return None

    # List all devices.
    devices = list_devices(adb_exe)
    if not devices:
        print("  No devices found.  Connect a phone via USB or start "
              "BlueStacks with ADB enabled.")
        return None
    print(f"  Found {len(devices)} device(s):")
    for d in devices:
        print(f"    {d['label']}  state={d['state']}")

    # Connect to configured or auto-selected device.
    adb = ADBClient(adb_exe=adb_exe)
    try:
        if config.ADB_DEVICE:
            adb.connect(config.ADB_DEVICE)
            print(f"  Connected to configured device: {config.ADB_DEVICE}")
        else:
            serial = adb.auto_connect()
            print(f"  Auto-connected to: {serial}")
    except ADBError as e:
        print(f"  ERROR: {e}")
        return None

    # Resolution.
    try:
        w, h = adb.resolution()
        print(f"  Resolution : {w} x {h}")
    except ADBError as e:
        print(f"  WARNING: could not query resolution: {e}")

    # Calibration info.
    print(f"  CALIBRATED_SCALE : {config.CALIBRATED_SCALE}")
    print(f"  skill scales = {[round(s, 3) for s in config.skill_scales()]}")
    print(f"  ref scales   = {[round(s, 3) for s in config.ref_scales()]}")

    print("  OK, ADB connection is healthy.")
    return adb


# ------------------------------------------------------------------
# Screenshot
# ------------------------------------------------------------------

def capture_screen(adb):
    """Take an ADB screenshot and return the BGR numpy array."""
    banner("SCREENSHOT")
    print("  Capturing via ADB...")
    t0 = time.time()
    try:
        image = adb.screenshot(black_retries=2)
    except ADBError as e:
        print(f"  ERROR: {e}")
        return None

    elapsed = time.time() - t0
    h, w = image.shape[:2]
    mean = float(np.asarray(image).mean())
    std  = float(np.asarray(image).std())
    print(f"  size: {w} x {h}   mean: {mean:.1f}   std: {std:.1f}   "
          f"took {elapsed:.2f}s")

    cv2.imwrite(str(CAPTURE_PATH), image)
    print(f"  saved -> {CAPTURE_PATH}")

    if is_blank(image):
        print("  WARNING: capture is blank/uniform.  Make sure the device "
              "screen is on and the app is running.")
    else:
        print("  OK, capture has content.")
    return image


# ------------------------------------------------------------------
# Reference-image check
# ------------------------------------------------------------------

def check_refs(image, norm=1.0):
    banner("REFERENCE IMAGE MATCHES")
    scales = config.ref_scales()
    print(f"  ref scales = {[round(s, 3) for s in scales]}   "
          f"threshold = {config.REF_THRESHOLD}   (coords = device pixels)\n")

    # Test EVERY ref the macro can use, not a fixed subset: every PNG in ref/
    # plus the custom buttons in ref/custom/.  The old hardcoded list silently
    # skipped newer refs (challenge-has-ended*, the speed/spin/wheel refs), so a
    # missing or mismatched end-screen ref looked fine here when it was not.
    paths = sorted(config.REF_DIR.glob("*.png"))
    paths += sorted(config.REF_CUSTOM_DIR.glob("*.png"))
    if not paths:
        print(f"  no ref images found in {config.REF_DIR}")
        return
    for path in paths:
        rel = (path.name if path.parent == config.REF_DIR
               else f"custom/{path.name}")
        try:
            tpl = load_template(path)
        except (FileNotFoundError, ValueError) as e:
            print(f"  {rel:26s}: MISSING ({e})")
            continue
        if is_blank(tpl):
            print(f"  {rel:26s}: BLANK placeholder, re-capture this image")
            continue
        conf, center, _ = multi_scale_match(image, tpl, scales,
                                            config.REF_DOWNSCALE)
        verdict = "MATCH" if conf >= config.REF_THRESHOLD else "no match"
        loc = (f"({round(center[0] / norm)}, {round(center[1] / norm)})"
               if center else "n/a")
        print(f"  {rel:26s}: conf {conf:.3f}  [{verdict}]  @ {loc}")


# ------------------------------------------------------------------
# Skill-icon check
# ------------------------------------------------------------------

def check_skills(image, norm=1.0, band=None):
    banner("SKILL MATCHES (active categories)")
    if band is None:
        band = config.SKILL_MATCH_BAND
    scales = config.skill_scales()
    print(f"  skill scales = {[round(s, 3) for s in scales]}   "
          f"threshold = {config.MATCH_THRESHOLD}   "
          f"downscale = {config.SKILL_DOWNSCALE}")
    print("  active categories: "
          + (", ".join(config.ACTIVE_CATEGORIES) or "(none)"))
    print("  (only meaningful when a skill-selection screen is captured)\n")

    results = []
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
                image, tpl, scales, config.SKILL_DOWNSCALE)
            results.append((category, path.name, conf, center))
    print(f"  scanned {len(results)} icon(s) in {time.time() - t0:.1f}s\n")

    if not results:
        print("  no skill icons to scan -- add PNGs to the skills/ folders")
        return

    def dev(center):
        return (round(center[0] / norm), round(center[1] / norm)) if center else None

    by_conf = sorted(results, key=lambda r: r[2], reverse=True)
    print("  top 5 template matches (by confidence):")
    for category, name, conf, center in by_conf[:5]:
        verdict = "MATCH" if conf >= config.MATCH_THRESHOLD else "no match"
        loc = f"{dev(center)}" if center else "n/a"
        print(f"    {category}/{name:8s}  conf {conf:.3f}  [{verdict}]  @ {loc}")

    # Mirror the macro's pick logic (band is in match space).
    pick = None
    outside = []
    for category, name, conf, center in results:
        if conf < config.MATCH_THRESHOLD or center is None:
            continue
        if band is None or band[0] <= center[1] <= band[1]:
            if pick is None:
                pick = (category, name, conf, center)
        else:
            outside.append((f"{category}/{name}", center))

    if pick:
        print(f"\n  -> macro would pick: {pick[0]} / {pick[1]}  "
              f"(conf {pick[2]:.3f})  tap device {dev(pick[3])}")
    else:
        print(f"\n  -> no skill above threshold; macro would tap "
              f"first slot (match space {config.FIRST_SKILL_SLOT})")

    if outside:
        print(f"  NOTE: {len(outside)} match(es) outside the skill band "
              f"{band}: " + ", ".join(f"{n}@y{c[1]}" for n, c in outside))
        print("  If real skills are among them, adjust the band ratios.")


# ------------------------------------------------------------------
# Annotated image
# ------------------------------------------------------------------

def save_annotated(image):
    """Draw match boxes for all refs over threshold and save to disk."""
    if image is None:
        return
    annotated = image.copy()
    ref_scales = config.ref_scales()

    refs = {
        "play": config.REF_PLAY, "devil": config.REF_DEVIL,
        "valkyrie": config.REF_VALKYRIE, "level": config.REF_LEVEL,
        "glory": config.REF_GLORY, "angel": config.REF_ANGEL,
        "refresh": config.REF_REFRESH,
    }
    for name, path in refs.items():
        try:
            tpl = load_template(path)
        except (FileNotFoundError, ValueError):
            continue
        conf, center, size = multi_scale_match(annotated, tpl, ref_scales)
        if conf >= config.REF_THRESHOLD and center and size:
            x0 = center[0] - size[0] // 2
            y0 = center[1] - size[1] // 2
            x1 = x0 + size[0]
            y1 = y0 + size[1]
            cv2.rectangle(annotated, (x0, y0), (x1, y1), (0, 255, 0), 2)
            cv2.putText(annotated, f"{name} {conf:.2f}", (x0, max(0, y0 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

    cv2.imwrite(str(ANNOTATED_PATH), annotated)
    print(f"\n  Annotated image saved -> {ANNOTATED_PATH}")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main():
    want_save = len(sys.argv) > 1 and sys.argv[1].lower() == "save"

    adb = check_adb()
    if adb is None:
        print("\nADB check failed -- fix the issues above and re-run.")
        return

    image = capture_screen(adb)
    if image is None:
        print("\nCould not capture screen -- check device connection.")
        return

    # Mirror the macro: normalise the capture to MATCH_WIDTH and match there,
    # converting reported coords back to device pixels.
    dev_h, dev_w = image.shape[:2]
    config.PHONE_RESOLUTION = [dev_w, dev_h]
    match_img, norm = resize_to_width(image, config.MATCH_WIDTH)
    mh, mw = match_img.shape[:2]
    band, first, second, go = config.geometry_for(mw, mh)
    config.resolve_geometry(dev_w, dev_h)   # device-space coords for settings parity
    print(f"  device {dev_w}x{dev_h} -> matching at {mw}x{mh} (scale {norm:.3f})")
    print(f"  band {band}  slots {first}/{second}  game-over {go}  (match space)")

    check_refs(match_img, norm)
    check_skills(match_img, norm, band)

    if want_save:
        save_annotated(match_img)


if __name__ == "__main__":
    main()
