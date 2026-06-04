"""
region_picker -- Phase 2-8 transition stub.

The on-screen drag pickers (RectanglePicker, PointPicker, pick_window) have
been removed as part of the ADB refactor.  All region/coordinate configuration
will move to the ADB-based setup wizard added in Phase 8 (gui.py rework).

These stubs let gui.py import this module without error.  Each picker call
immediately invokes the callback with None (equivalent to user-cancellation)
so the GUI re-enables its buttons cleanly.
"""


def pick_window(parent, callback, message=""):
    """Stub: immediately cancel (feature removed, pending Phase 8)."""
    parent.after(1, lambda: callback(None))


class RectanglePicker:
    """Stub: immediately cancel (feature removed, pending Phase 8)."""
    def __init__(self, parent, initial, callback, message=""):
        parent.after(1, lambda: callback(None))


class PointPicker:
    """Stub: immediately cancel (feature removed, pending Phase 8)."""
    def __init__(self, parent, initial, callback, message=""):
        parent.after(1, lambda: callback(None))
