"""
Minimal, stdlib-only i18n helper for A2 Macro Controller and A2 Updater.

Flat key -> string tables live in lang/<code>.json next to this file (or next
to the exe when frozen). t(key, **kwargs) looks the key up in the current
language, falls back to English, then to the key itself, and never raises:
a broken translation should degrade to something readable, not crash the GUI.

Deliberately does not import config.py, so updater.py (a separate stdlib-only
PyInstaller exe) can use this module without pulling in the main app's
dependencies.
"""

import json
import sys
from pathlib import Path

# Resolve the lang/ directory the same way config.py resolves BASE_DIR: the
# folder of the exe when frozen by PyInstaller, otherwise the folder of this
# source file. Implemented locally (not imported from config) so updater.py
# never has to import config to use translations.
if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).resolve().parent
else:
    _BASE_DIR = Path(__file__).resolve().parent
LANG_DIR = _BASE_DIR / "lang"

# (code, display name) pairs, in menu order. Display names are what a user
# picks from, so they are written in their own language.
AVAILABLE_LANGUAGES = [
    ("en", "English"),
    ("fr", "Francais"),
    ("de", "Deutsch"),
]
_AVAILABLE_CODES = {code for code, _name in AVAILABLE_LANGUAGES}

DEFAULT_LANGUAGE = "en"

# Loaded-table cache, keyed by language code. A missing/invalid file caches an
# empty dict so a bad JSON does not re-attempt disk reads on every t() call.
_CACHE = {}
_current_lang = DEFAULT_LANGUAGE


def _load_table(code):
    """Return the key->string dict for `code`, or {} if the file is missing or
    invalid. Cached after the first attempt (successful or not)."""
    if code in _CACHE:
        return _CACHE[code]
    table = {}
    path = LANG_DIR / f"{code}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            table = data
    except (OSError, ValueError):
        table = {}
    _CACHE[code] = table
    return table


def set_language(code):
    """Select the active language for subsequent t() calls. Unknown codes
    fall back to English silently (the GUI is expected to only offer codes
    from AVAILABLE_LANGUAGES, but a stale settings.json value must not crash
    startup)."""
    global _current_lang
    _current_lang = code if code in _AVAILABLE_CODES else DEFAULT_LANGUAGE


def get_language():
    """Currently selected language code."""
    return _current_lang


def available_languages():
    """[(code, display_name), ...] in menu order."""
    return list(AVAILABLE_LANGUAGES)


def t(key, **kwargs):
    """Translate `key` for the current language.

    Fallback chain: current language table -> English table -> the key
    itself. Never raises: a str.format() failure (e.g. a missing/renamed
    placeholder) just returns the unformatted string instead of crashing
    whatever button or dialog called t()."""
    table = _load_table(_current_lang)
    text = table.get(key)
    if text is None and _current_lang != DEFAULT_LANGUAGE:
        text = _load_table(DEFAULT_LANGUAGE).get(key)
    if text is None:
        text = key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text
