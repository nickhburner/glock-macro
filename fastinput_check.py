"""
Standalone check for the low-latency tapper (fastinput.FastTapper).

Run this to confirm the fast tap path works on the currently selected device
before relying on it in a mode.  It is especially useful on a real phone, where
the type-B protocol path has not been live-verified (BlueStacks was).

What it does:
  1. Connects to config.ADB_DEVICE (or a serial passed as the first argument).
  2. Captures a frame to learn the screen size.
  3. Sets up FastTapper and prints the detected device profile + transform.
  4. Taps a few reference points, saving a before/after screenshot for each into
     ./_fastcheck/ with the intended point marked, so you can confirm taps land
     where intended.
  5. Reports tap throughput.

Tip: for an exact read-out of where each tap lands, first enable
Settings > System > Developer options > Pointer location on the device; the
crosshair it draws will appear in the "after" screenshots.

Usage:
    python fastinput_check.py [serial]
"""

import os
import sys
import time

import config
config.apply_adb_server_port()

import cv2

import adb
import fastinput


def _mark(img, x, y, label):
    out = img.copy()
    cv2.drawMarker(out, (int(x), int(y)), (0, 0, 255),
                   markerType=cv2.MARKER_CROSS, markerSize=40, thickness=2)
    cv2.circle(out, (int(x), int(y)), 22, (0, 0, 255), 2)
    cv2.putText(out, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (0, 0, 255), 2)
    return out


def main():
    serial = sys.argv[1] if len(sys.argv) > 1 else config.ADB_DEVICE
    if not serial:
        print("No device serial. Pass one as an argument or set ADB_DEVICE in "
              "settings.json.")
        return 1

    client = adb.ADBClient(device=serial)
    try:
        client.connect(serial)
    except adb.ADBError as exc:
        print(f"Could not connect to {serial}: {exc}")
        return 1

    frame = client.screenshot()
    h, w = frame.shape[:2]
    print(f"device {serial}  screen {w}x{h}")
    print(f"shell input ok: {client.shell_works()}")

    tapper = fastinput.FastTapper(client)
    if not tapper.setup(w, h, on_log=lambda *a: print("  ", *a)):
        print("\nFAST TAP NOT AVAILABLE on this device. The macro will use the "
              "ordinary `adb input tap` path (slower but always works).")
        return 0

    print("\nProfile:")
    print(f"  node          {tapper.node}")
    print(f"  name          {tapper.name!r}")
    print(f"  protocol      type-{'B' if tapper.type_b else 'A'}")
    print(f"  btn_touch     {tapper.has_btn_touch}")
    print(f"  range         x 0..{tapper.x_max}  y 0..{tapper.y_max}")
    print(f"  transform     {tapper.transform}")
    print(f"  hold          {tapper._hold * 1000:.0f}ms")

    out_dir = config.BASE_DIR / "_fastcheck"
    out_dir.mkdir(exist_ok=True)
    points = [
        ("center", w // 2, h // 2),
        ("upper_left", round(w * 0.25), round(h * 0.20)),
        ("upper_right", round(w * 0.75), round(h * 0.20)),
        ("lower_center", round(w * 0.50), round(h * 0.80)),
    ]
    print(f"\nTapping {len(points)} reference points (screenshots -> {out_dir}):")
    for label, x, y in points:
        before = client.screenshot()
        tapper.tap(x, y)
        time.sleep(0.6)
        after = client.screenshot()
        cv2.imwrite(str(out_dir / f"{label}_before.png"), _mark(before, x, y, label))
        cv2.imwrite(str(out_dir / f"{label}_after.png"), _mark(after, x, y, label))
        print(f"  tapped {label} at screen ({x},{y}) -> "
              f"event {tapper._to_event(x, y)}")

    n = 100
    t0 = time.time()
    for _ in range(n):
        tapper.tap(w // 2, h // 2)
    dt = time.time() - t0
    print(f"\nthroughput: {n} taps in {dt:.2f}s = {n/dt:.0f} taps/s, "
          f"{dt/n*1000:.1f} ms/tap (includes {tapper._hold*1000:.0f}ms hold)")

    tapper.close()
    print("done. Inspect the before/after pairs in _fastcheck/ to confirm taps "
          "landed on the marked points.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
