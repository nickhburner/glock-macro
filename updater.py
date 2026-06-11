"""
Standalone updater for A2 Macro Controller.

Checks the GitHub Releases feed for a newer version, downloads the release
zip, and applies it over the install folder while preserving the user's
settings.json, ref/custom/ captures, and ref/ref_zones.json.

Deliberately stdlib-only (plus the local widgets.py, which is pure tkinter)
so the built "A2 Updater.exe" stays small and needs no third-party packages.
The main app never touches the network; only this updater does.

Usage:
    python updater.py        (or run "A2 Updater.exe" next to the main exe)
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from widgets import (
    ui_font, set_dark_titlebar, draw_rounded_rect,
    RoundedSection, RoundedButton,
)

APP_NAME     = "A2 Macro Controller"
MAIN_EXE     = APP_NAME + ".exe"
RELEASES_API = ("https://api.github.com/repos/"
                "nickhburner/glock-macro/releases/latest")

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

VERSION_FILE  = BASE_DIR / "version.txt"
SETTINGS_PATH = BASE_DIR / "settings.json"

# User files the updater must NEVER overwrite (paths relative to the install
# folder, forward slashes, lowercase).
PRESERVED_FILES = ("settings.json", "ref/ref_zones.json")
PRESERVED_DIRS  = ("ref/custom",)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Same palettes as gui.py so the two windows look like one app.
_THEMES = {
    "light": {
        "bg": "#eef0f3", "surface": "#ffffff", "surface_2": "#f1f3f6",
        "surface_3": "#e6e9ee", "inset": "#f6f7f9", "border": "#dde0e6",
        "border_strong": "#c8cdd5", "fg": "#1c1e24", "fg_dim": "#565c66",
        "fg_muted": "#888e98", "ok": "#46b66e", "bad": "#e2575c",
        "warn": "#e0a534", "accent": "#5b86e8",
    },
    "dark": {
        "bg": "#1a1b1f", "surface": "#212329", "surface_2": "#282b32",
        "surface_3": "#31343c", "inset": "#16171b", "border": "#33363e",
        "border_strong": "#41454e", "fg": "#f0f1f5", "fg_dim": "#a0a5b0",
        "fg_muted": "#717680", "ok": "#46b66e", "bad": "#e2575c",
        "warn": "#e0a534", "accent": "#5b86e8",
    },
}


# ------------------------------------------------------------------ helpers

def read_local_version():
    """Current version from version.txt next to the exe, or 'unknown'."""
    try:
        v = VERSION_FILE.read_text(encoding="utf-8").strip()
        return v or "unknown"
    except OSError:
        return "unknown"


def dark_mode_enabled():
    """Read DARK_MODE from settings.json without importing config (the
    updater must keep working even if config.py changes)."""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return bool(data.get("DARK_MODE", True))
    except (OSError, ValueError):
        return True


def version_tuple(v):
    """'v1.2.10' -> (1, 2, 10).  Empty tuple if no digits found."""
    return tuple(int(n) for n in re.findall(r"\d+", v or ""))


def is_newer(remote, local):
    r, l = version_tuple(remote), version_tuple(local)
    if not r:
        return False
    if not l:           # local version unknown: offer the update
        return True
    return r > l


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def check_for_update():
    """Hit the GitHub API. Returns a release-info dict, or raises."""
    req = urllib.request.Request(RELEASES_API, headers={
        "User-Agent": "A2-Updater",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    asset = next((a for a in data.get("assets", [])
                  if a.get("name", "").lower().endswith(".zip")), None)
    if asset is None:
        raise RuntimeError("Latest release has no .zip asset attached.")
    return {
        "tag":   data.get("tag_name", ""),
        "notes": (data.get("body") or "").strip(),
        "url":   asset["browser_download_url"],
        "size":  int(asset.get("size", 0)),
        "name":  asset["name"],
    }


def download_release(url, dest, expected_size, progress_cb):
    """Stream `url` to `dest` in chunks, calling progress_cb(done, total)."""
    req = urllib.request.Request(url, headers={"User-Agent": "A2-Updater"})
    done = 0
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or expected_size or 0)
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            progress_cb(done, total)
    if expected_size and dest.stat().st_size != expected_size:
        raise RuntimeError("Download incomplete (size mismatch); try again.")


def is_main_app_running():
    """True if the main exe shows up in tasklist."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {MAIN_EXE}", "/FO", "CSV",
             "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW)
        return MAIN_EXE.lower() in out.stdout.lower()
    except Exception:
        return False    # can't tell; the copy step will surface a lock error


def _is_preserved(rel_posix):
    rp = rel_posix.lower()
    if rp in PRESERVED_FILES:
        return True
    return any(rp == d or rp.startswith(d + "/") for d in PRESERVED_DIRS)


def apply_update(zip_path, tag, progress_cb):
    """Extract to a staging dir, then copy into the install folder, skipping
    preserved user files. The main exe is backed up to .bak and restored if
    the copy fails part-way."""
    staging = Path(tempfile.mkdtemp(prefix="a2update_"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staging)
        # A zip of the dist/ folder may wrap everything in one top-level dir.
        root = staging
        entries = list(root.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            root = entries[0]
        files = [p for p in root.rglob("*") if p.is_file()]
        if not files:
            raise RuntimeError("Downloaded zip is empty.")

        main_exe = BASE_DIR / MAIN_EXE
        backup = None
        if main_exe.exists():
            backup = main_exe.with_suffix(".exe.bak")
            shutil.copy2(main_exe, backup)
        self_exe = (Path(sys.executable).resolve()
                    if getattr(sys, "frozen", False) else None)
        try:
            for i, src in enumerate(files, start=1):
                rel = src.relative_to(root)
                # version.txt is written from the release tag only AFTER a
                # fully successful copy, so a failed update never claims the
                # new version while holding old files.
                if (_is_preserved(rel.as_posix())
                        or rel.as_posix().lower() == "version.txt"):
                    continue
                dest = BASE_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Windows won't let a running exe be overwritten, but it CAN
                # be renamed: shunt our own exe aside so the new one lands.
                if self_exe and dest.exists() and dest.resolve() == self_exe:
                    old = dest.with_suffix(".exe.old")
                    if old.exists():
                        old.unlink()
                    dest.rename(old)
                shutil.copy2(src, dest)
                progress_cb(i, len(files))
        except Exception:
            if backup and backup.exists():
                shutil.copy2(backup, main_exe)      # rollback
            raise
        VERSION_FILE.write_text(tag + "\n", encoding="utf-8")
        if backup and backup.exists():
            backup.unlink()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cleanup_old_self():
    """Remove the .exe.old left behind by a previous self-update."""
    for p in BASE_DIR.glob("*.exe.old"):
        try:
            p.unlink()
        except OSError:
            pass    # still locked; next run gets it


# ------------------------------------------------------------------ GUI

class ProgressBar(tk.Canvas):
    """Slim rounded progress bar matching the app theme."""

    def __init__(self, parent, theme, height=8, **kw):
        super().__init__(parent, height=height, highlightthickness=0,
                         bd=0, bg=parent.cget("background"), **kw)
        self._theme = theme
        self._frac = 0.0
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, frac):
        self._frac = max(0.0, min(1.0, frac))
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 4:
            return
        r = h // 2
        draw_rounded_rect(self, 0, 0, w, h, r,
                          fill=self._theme["inset"],
                          outline=self._theme["border"])
        fill_w = round(w * self._frac)
        if fill_w >= h:     # too short to round nicely below one bar-height
            draw_rounded_rect(self, 0, 0, fill_w, h, r,
                              fill=self._theme["accent"], outline="")


class UpdaterApp:
    def __init__(self, root):
        self.root = root
        self.theme = _THEMES["dark" if dark_mode_enabled() else "light"]
        p = self.theme
        f = ui_font()

        root.title("A2 Updater")
        root.geometry("480x470")
        root.minsize(440, 430)
        root.configure(background=p["bg"])
        set_dark_titlebar(root, dark_mode_enabled())

        self.release = None     # info dict once a newer release is found
        self.local_version = read_local_version()
        self.busy = False

        # Header: app name + current version
        head = tk.Frame(root, bg=p["bg"])
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(head, text=APP_NAME, bg=p["bg"], fg=p["fg"],
                 font=(f, 13, "bold")).pack(side="left")
        self.ver_lbl = tk.Label(head, text=f"v{self.local_version}",
                                bg=p["bg"], fg=p["fg_muted"], font=(f, 10))
        self.ver_lbl.pack(side="left", padx=(8, 0), pady=(3, 0))
        self.check_btn = RoundedButton(
            head, text="Check for Updates", command=self.on_check,
            bg=p["accent"], fg="#ffffff", font=(f, 9, "bold"), radius=6,
            padx=14, pady=5, canvas_bg=p["bg"])
        self.check_btn.pack(side="right")

        # Release info card
        section = RoundedSection(root, title="Latest release", bg=p["surface"],
                                 border_color=p["border"], title_fg=p["fg"],
                                 parent_bg=p["bg"])
        section.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        inner = section.inner

        info_row = tk.Frame(inner, bg=p["surface"])
        info_row.pack(fill="x", pady=(2, 6))
        self.rel_version_lbl = tk.Label(
            info_row, text="--", bg=p["surface"], fg=p["fg"],
            font=(f, 11, "bold"))
        self.rel_version_lbl.pack(side="left")
        self.rel_size_lbl = tk.Label(
            info_row, text="", bg=p["surface"], fg=p["fg_muted"],
            font=(f, 9))
        self.rel_size_lbl.pack(side="right")

        notes_wrap = tk.Frame(inner, bg=p["surface"])
        notes_wrap.pack(fill="both", expand=True)
        self.notes = tk.Text(
            notes_wrap, height=8, wrap="word", relief="flat", bd=0,
            bg=p["inset"], fg=p["fg_dim"], insertbackground=p["fg_dim"],
            font=(f, 9), padx=10, pady=8,
            highlightthickness=1, highlightbackground=p["border"],
            highlightcolor=p["border"])
        self.notes.pack(fill="both", expand=True)
        self._set_notes("Press \"Check for Updates\" to look for a new "
                        "version on GitHub.")

        # Footer: progress + status + apply button
        foot = tk.Frame(root, bg=p["bg"])
        foot.pack(fill="x", padx=16, pady=(0, 14))
        self.progress = ProgressBar(foot, p)
        self.progress.pack(fill="x", pady=(0, 8))
        status_row = tk.Frame(foot, bg=p["bg"])
        status_row.pack(fill="x")
        self.status_lbl = tk.Label(status_row, text="Ready.", bg=p["bg"],
                                   fg=p["fg_muted"], font=(f, 9),
                                   anchor="w", justify="left")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.apply_btn = RoundedButton(
            status_row, text="Download && Apply", command=self.on_apply,
            bg=p["ok"], fg="#ffffff", font=(f, 10, "bold"), radius=6,
            padx=18, pady=6, canvas_bg=p["bg"])
        self.apply_btn.pack(side="right")
        self.apply_btn.configure(state="disabled")

    # ---- UI helpers (main thread only)

    def _set_notes(self, text):
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text)
        self.notes.configure(state="disabled")

    def set_status(self, text, kind="dim"):
        color = {"dim": self.theme["fg_muted"], "ok": self.theme["ok"],
                 "bad": self.theme["bad"], "warn": self.theme["warn"]}[kind]
        self.status_lbl.configure(text=text, fg=color)

    def _set_busy(self, busy):
        self.busy = busy
        self.check_btn.configure(state="disabled" if busy else "normal")
        can_apply = (not busy) and self.release is not None
        self.apply_btn.configure(state="normal" if can_apply else "disabled")

    def _ui(self, fn, *args):
        """Marshal a call from a worker thread onto the Tk main thread."""
        self.root.after(0, lambda: fn(*args))

    # ---- Check for updates

    def on_check(self):
        if self.busy:
            return
        self._set_busy(True)
        self.progress.set(0)
        self.set_status("Checking GitHub for the latest release...")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        try:
            info = check_for_update()
        except (urllib.error.URLError, OSError) as e:
            self._ui(self._check_failed,
                     "Could not reach GitHub. Check your internet "
                     f"connection and try again. ({e})")
            return
        except Exception as e:
            self._ui(self._check_failed, f"Update check failed: {e}")
            return
        self._ui(self._check_done, info)

    def _check_failed(self, msg):
        self._set_busy(False)
        self.set_status(msg, "bad")

    def _check_done(self, info):
        self.release = None
        if is_newer(info["tag"], self.local_version):
            self.release = info
            self.rel_version_lbl.configure(text=info["tag"])
            self.rel_size_lbl.configure(text=fmt_size(info["size"]))
            self._set_notes(info["notes"] or "(no release notes)")
            self.set_status(f"Update available: {info['tag']}", "ok")
        else:
            self.rel_version_lbl.configure(text=info["tag"] or "--")
            self.rel_size_lbl.configure(text="")
            self._set_notes("You're up to date!")
            self.set_status("You're up to date!", "ok")
        self._set_busy(False)

    # ---- Download & apply

    def on_apply(self):
        if self.busy or self.release is None:
            return
        if is_main_app_running():
            self.set_status(f"Close {APP_NAME} first, then try again.",
                            "warn")
            return
        self._set_busy(True)
        self.progress.set(0)
        threading.Thread(target=self._apply_worker, args=(self.release,),
                         daemon=True).start()

    def _apply_worker(self, rel):
        tmp = Path(tempfile.mkdtemp(prefix="a2dl_"))
        zip_path = tmp / rel["name"]
        try:
            self._ui(self.set_status, f"Downloading {rel['name']}...")
            try:
                download_release(rel["url"], zip_path, rel["size"],
                                 self._dl_progress)
            except Exception:
                # One quiet retry, then give up.
                self._ui(self.set_status, "Download hiccup, retrying...",
                         "warn")
                download_release(rel["url"], zip_path, rel["size"],
                                 self._dl_progress)

            self._ui(self.set_status, "Applying update...")
            apply_update(zip_path, rel["tag"], self._copy_progress)
        except (urllib.error.URLError, OSError) as e:
            self._ui(self._apply_failed,
                     f"Update failed: {e}. Nothing was changed that wasn't "
                     "rolled back; try again later.")
            return
        except Exception as e:
            self._ui(self._apply_failed, f"Update failed: {e}")
            return
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._ui(self._apply_done, rel["tag"])

    def _dl_progress(self, done, total):
        if total:
            self._ui(self.progress.set, 0.8 * done / total)

    def _copy_progress(self, done, total):
        if total:
            self._ui(self.progress.set, 0.8 + 0.2 * done / total)

    def _apply_failed(self, msg):
        self._set_busy(False)
        self.progress.set(0)
        self.set_status(msg, "bad")

    def _apply_done(self, tag):
        self.local_version = tag.lstrip("vV") or tag
        self.ver_lbl.configure(text=f"v{self.local_version}")
        self.release = None
        self.progress.set(1.0)
        self._set_busy(False)
        self.apply_btn.configure(state="disabled")
        self._set_notes("Update complete! Your settings, custom buttons, "
                        "and match zones were preserved.\n\nYou can close "
                        "this window and launch the app as usual.")
        self.set_status(f"Updated to {tag}. You're good to go!", "ok")


def main():
    cleanup_old_self()
    root = tk.Tk()
    UpdaterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
