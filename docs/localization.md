# Localization: GUI language and localized refs

Two related features: translating the GUI text, and capturing language-specific
template images for the game's text-bearing buttons.

## GUI i18n (`i18n.py` + `lang/`)

- Flat `key -> string` JSON tables in `lang/en.json`, `fr.json`, `de.json`
  (next to the source, or next to the exe when frozen). Keys are flat
  snake_case (e.g. `settings.field.poll_interval`).
- `t(key, **kwargs)` looks up the current language, falls back to English, then
  to the key itself, and never raises: a bad `str.format` just returns the
  unformatted string. Placeholders are `{name}` style and MUST be identical
  across all three languages.
- `set_language(code)` selects the active language; unknown codes fall back to
  English. `gui.py` calls `i18n.set_language(config.LANGUAGE)` once at module
  load, before any module-level `t()` (status colours, game-mode names,
  settings schema) runs.
- `updater.py` is wrapped too and reads `LANGUAGE` straight from
  `settings.json` without importing `config` (it is a separate stdlib exe).

### Language dropdown

Settings -> Language persists `LANGUAGE` (in `PERSISTED_KEYS`) and calls
`set_language` immediately, but the visible UI is only relabelled on the next
app start. There is deliberately NO walk-every-widget live relabel pass; a
restart hint sits under the dropdown.

### Adding / changing strings

Every new user-facing string goes through `t("key")` with the key added to ALL
THREE `lang/*.json` files (natural FR/DE with proper accents, identical
`{placeholders}`). Verify parity: same key set and same placeholder set in
en/fr/de. Macro-engine log lines (`main.py`, `all_star.py`, `eternal_lode.py`)
and low-level GUI diagnostic log lines without keys stay English by decision;
the `lang/` files ship with releases (user edits are not preserved by the
updater, so improved translations belong upstream).

## Localized refs (`ref/fr`, `ref/de`)

Skill and boss icons are extracted game assets: language-neutral, no variants
needed. Only user-captured `ref/` button templates can contain rendered text.
`config.LOCALIZED_REFS` is the audited list of those text-bearing refs.

- Default English templates live in `ref/`. Language variants override by
  filename in `ref/<lang>/`. `config.ref_path(path)` resolves the active
  language's variant first and falls back to the English ref. `ref_zones.json`
  keys by base filename regardless of language.
- The resolver is wired into `main.py` (named refs + `_load_group`),
  `all_star.py`, and `eternal_lode.py`.
- The GUI "Language refs (FR / DE)..." wizard (Settings -> Custom buttons)
  walks each `LOCALIZED_REFS` entry, shows the English example, and captures a
  crop via the same ADB-screenshot + rubber-band flow, saving to `ref/<lang>/`.
  The user switches the game to that language first.
- `updater.py` preserves `ref/fr` and `ref/de` (like `ref/custom`);
  `build.bat` ships them empty; `.gitignore` ignores them.
