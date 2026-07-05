"""
Multi-scale template matching helpers (OpenCV).

Skill icons render at different sizes depending on UI context, so templates
are matched across a range of scales; the best confidence over all scales wins.
"""

import hashlib
from pathlib import Path

import cv2
import numpy as np


def load_template(path):
    """Load an image as a 3-channel BGR array.

    Uses imdecode so paths with spaces / non-ASCII characters work on Windows.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Could not decode image: {path}")
    return img


def save_image(path, image):
    """Write a BGR array to `path` as PNG.

    Uses imencode + tofile (the mirror of load_template) for non-ASCII paths.
    """
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"Could not encode image for: {path}")
    buf.tofile(str(path))


def resize_to_width(image, width):
    """Resize `image` to `width` px wide, preserving aspect ratio.

    Returns (resized_image, scale) where scale = width / original_width.  A
    coordinate in the resized image maps back to the original via coord / scale.
    Used to normalise every device capture to one match width so a single set
    of templates works on any resolution.  A no-op (scale 1.0) when already that
    width.
    """
    h, w = image.shape[:2]
    if w == width or w == 0:
        return image, 1.0
    scale = width / w
    new_h = max(1, round(h * scale))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(image, (width, new_h), interpolation=interp), scale


def list_skill_files(folder):
    """Return the .png skill icons in `folder`, sorted best-to-worst.

    Numeric names (1.png best) sort ascending; non-numeric names follow
    alphabetically. A missing folder yields an empty list.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() == ".png"]

    def sort_key(p):
        return (0, int(p.stem)) if p.stem.isdigit() else (1, p.stem.lower())

    files.sort(key=sort_key)
    return files


def skill_hash(path):
    """Content hash of a skill image file, or None if unreadable.

    The same skill is often byte-identical across several category folders, so
    this hash lets avoidance match it wherever it appears, not just one copy.
    """
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def is_blank(image, std_threshold=8.0):
    """True if the image has almost no variation (a black/uniform frame)."""
    return float(np.asarray(image).std()) < std_threshold


def crop_band(image, band):
    """Crop `image` to a horizontal band.

    `band` is (y_top, y_bottom) or None. Returns (cropped_image, y_offset),
    where y_offset is added back to recover original coordinates.
    """
    if band is None:
        return image, 0
    h = image.shape[0]
    top = max(0, min(int(band[0]), h))
    bot = max(top, min(int(band[1]), h))
    return image[top:bot], top


def multi_scale_match(image, template, scales, downscale=1.0):
    """Match `template` against `image` across `scales`.

    `downscale` (0 < d <= 1) shrinks both image and template before matching
    for speed; returned coordinates always map back to the ORIGINAL `image`.

    Returns (best_confidence, (center_x, center_y), (width, height)) relative
    to `image`. If nothing matched, confidence is -1.0 and the rest is None.
    """
    # Defensive: an empty search image (e.g. a band crop that fell entirely
    # outside the frame because of a stale resolution) must degrade to "no
    # match", never reach cv2.resize -- that raises (-215:Assertion !ssize.empty()).
    if image is None or image.size == 0 or template is None or template.size == 0:
        return -1.0, None, None

    if downscale != 1.0:
        image = cv2.resize(image, None, fx=downscale, fy=downscale,
                           interpolation=cv2.INTER_AREA)
        if image.size == 0:
            return -1.0, None, None

    img_h, img_w = image.shape[:2]
    t_h, t_w = template.shape[:2]

    best_conf = -1.0
    best_center = None
    best_size = None

    for s in scales:
        w = max(1, int(round(t_w * s * downscale)))
        h = max(1, int(round(t_h * s * downscale)))
        if w > img_w or h > img_h:
            continue  # template larger than the search image at this scale

        interp = cv2.INTER_AREA if w < t_w else cv2.INTER_LINEAR
        resized = cv2.resize(template, (w, h), interpolation=interp)

        result = cv2.matchTemplate(image, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val > best_conf:
            best_conf = max_val
            best_center = (int(round((max_loc[0] + w / 2) / downscale)),
                           int(round((max_loc[1] + h / 2) / downscale)))
            best_size = (int(round(w / downscale)), int(round(h / downscale)))

    return best_conf, best_center, best_size
