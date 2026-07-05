"""Build All-Star boss templates from the transparent icons.

Converts every PNG in ALL STAR/boss-icons-transparent/ into a runtime template
in game-macro/ref/all_star/bosses/: the icon canvas is flattened onto black and
resized by the 0.9 on-screen render scale (measured from the in-game example
screenshots at the 1080-wide calibration; the existing templates were produced
with exactly this recipe).

File names are slugified to match config.ALL_STAR_LEVELS: lowercased, periods
stripped, spaces replaced by hyphens.  Icons whose slug is not in any level's
pool are reported and SKIPPED (catches misspelled file names), and the pool
bosses still missing a template are listed at the end.  Safe to re-run any
time; existing templates are overwritten from their icon.  purple-whirl has no
icon (its template was hand-cropped from a screenshot) and is left alone.

Usage:  python make_boss_templates.py
"""

import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ICON_DIR = HERE / "boss-icons-transparent"
OUT_DIR = ROOT / "game-macro" / "ref" / "all_star" / "bosses"
RENDER_SCALE = 0.9

sys.path.insert(0, str(ROOT / "game-macro"))
import config  # noqa: E402


def slugify(stem):
    return stem.strip().lower().replace(".", "").replace(" ", "-")


def flatten_on_black(img):
    """RGBA -> BGR with transparency composited onto black."""
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        return (img[:, :, :3].astype(np.float32) * alpha).astype(np.uint8)
    return img


def main():
    pool_names = {n for pool in config.ALL_STAR_LEVELS.values() for n in pool}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    written, unknown = [], []
    for icon_path in sorted(ICON_DIR.glob("*.png")):
        slug = slugify(icon_path.stem)
        if slug not in pool_names:
            unknown.append((icon_path.name, slug))
            continue
        data = np.fromfile(str(icon_path), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if img is None:
            print(f"SKIP (cannot decode): {icon_path.name}")
            continue
        bgr = flatten_on_black(img)
        w = max(1, round(bgr.shape[1] * RENDER_SCALE))
        h = max(1, round(bgr.shape[0] * RENDER_SCALE))
        tpl = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
        out = OUT_DIR / f"{slug}.png"
        ok, buf = cv2.imencode(".png", tpl)
        if not ok:
            print(f"SKIP (encode failed): {icon_path.name}")
            continue
        buf.tofile(str(out))
        written.append(slug)
        print(f"wrote {out.name}  ({w}x{h})")

    if unknown:
        print(f"\n{len(unknown)} icon(s) do not match any pool boss "
              "(misspelled? rename and re-run):")
        for fname, slug in unknown:
            print(f"  {fname}  (slug '{slug}')")

    have = {p.stem.lower() for p in OUT_DIR.glob("*.png")}
    missing = sorted(pool_names - have)
    print(f"\n{len(written)} template(s) written; "
          f"{len(pool_names) - len(missing)}/{len(pool_names)} pool bosses "
          "have a template")
    if missing:
        print(f"still missing ({len(missing)}):")
        for lvl in sorted(config.ALL_STAR_LEVELS):
            gone = [n for n in config.ALL_STAR_LEVELS[lvl] if n in missing]
            if gone:
                print(f"  level {lvl}: {', '.join(gone)}")


if __name__ == "__main__":
    main()
