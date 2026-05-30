"""
Prints the cursor position after a countdown.

Usage:
    python getcoords.py            # 5-second countdown
    python getcoords.py 8          # custom countdown
"""

import sys
import time

import pyautogui


def capture_position(delay=5.0, on_tick=None):
    """Wait `delay` seconds, then return the current mouse (x, y).

    `on_tick`, if given, is called each second with the seconds remaining.
    """
    remaining = int(round(delay))
    while remaining > 0:
        if on_tick is not None:
            on_tick(remaining)
        time.sleep(1)
        remaining -= 1
    pos = pyautogui.position()
    return int(pos[0]), int(pos[1])


def main():
    delay = 5.0
    if len(sys.argv) > 1:
        try:
            delay = float(sys.argv[1])
        except ValueError:
            print(f"ignoring bad delay '{sys.argv[1]}', using {delay:.0f}s")

    print(f"move the mouse onto the target, capturing in {delay:.0f}s")
    x, y = capture_position(
        delay, on_tick=lambda s: print(f"  {s}...", flush=True))
    print(f"mouse position: ({x}, {y})")


if __name__ == "__main__":
    main()
