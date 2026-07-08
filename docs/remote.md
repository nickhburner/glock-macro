# Remote status and control (optional, off by default)

Watch the macro's status and log from a phone and press Start / Stop / Sleep
phone / Close BlueStacks from anywhere. The main app never touches the network:
a separate opt-in companion does all outbound HTTPS. Feature is fully OFF until
the user enables it in the Settings -> Remote panel.

## Pieces

- `remote/worker/worker.js` - a Cloudflare Worker the user deploys on their own
  free account. A dumb mailbox keyed by `install_id` (see below). Stores only a
  latest status blob (TTL 180 s) and at most one pending command (TTL 60 s).
  Per-IP and per-install rate limiting. See `remote/worker/DEPLOY.md`.
- `remote/pages/index.html` - a static status page the user hosts (GitHub
  Pages). Verifies signatures with WebCrypto before trusting anything.
- `remote_agent.py` / `A2 Remote.exe` - the companion. The ONLY networked part.
  Reads `settings.json` directly (no `config` import), pushes status, polls for
  commands, verifies them, and writes them for the GUI.
- `gui.py` - the Remote panel plus the local file IPC and companion lifecycle.

## Protocol (all three ends must stay byte-for-byte in sync)

- Per-install secret `token` = `secrets.token_urlsafe(32)`, stored in
  `remote_token.txt` (gitignored, never shipped, never logged in full).
- `install_id = SHA-256(token)` hex. Every relay key derives from it; the relay
  never sees the token.
- Canonical JSON both ends sign: `json.dumps(obj, sort_keys=True,
  separators=(",",":"))` with `ensure_ascii=True`. `index.html` mirrors this in
  `canonicalJson()`. All numbers in signed payloads MUST be integers (float
  formatting differs between Python and JS).
- Signature = `HMAC-SHA256(token, canonical_json(blob))` lowercase hex.
- Status push: companion POSTs `/push {install_id, blob, sig}`,
  `blob = {nonce, status, ts}`. Web page GETs `/status?id=<iid>` and verifies.
- Command: web page POSTs `/cmd {install_id, blob, sig}`,
  `blob = {command, nonce, ts}`. Companion GETs `/cmd?id=<iid>` (single-consume
  delete on the worker) and accepts only if signature matches, `|now-ts|<=60 s`,
  the nonce is unseen, and the command is allow-listed.
- Pairing link (fragment only, never sent to a server):
  `<pages-url>#i=<install_id>&k=<token>&r=<worker-url>`.

Allowed commands: `start`, `stop`, `sleep_phone`, `close_emulator`. This list
is duplicated in `worker.js`, `remote_agent.py`, and `gui.py`
(`_REMOTE_ALLOWED_COMMANDS`) as defence in depth.

## Local IPC (GUI <-> companion, files in the install folder)

- `remote_status.json` - GUI writes every ~3 s (atomic temp + `os.replace`):
  `{ts, running, status, mode, log (last 40 lines), version}`.
- `remote_cmd.json` - companion writes a verified command; GUI polls, executes
  on the main thread, deletes the file.
- `remote_heartbeat.json` - companion writes each cycle so the GUI status line
  can show "running, last push N s ago".
- `remote_token.txt` - the secret.

The GUI drives all of this from `root.after` (`_remote_tick`, ~1.5 s). The
companion is launched with `CREATE_NO_WINDOW` when the feature is enabled (and
at app start if it was left on) and terminated on disable / app exit. The
companion also self-exits if `REMOTE_ENABLED` goes false, the token vanishes, or
the status file goes stale for 10 minutes.

## Config keys

`REMOTE_ENABLED` (False), `REMOTE_RELAY_URL` (""), `REMOTE_PAGES_URL` (""),
`REMOTE_PUSH_INTERVAL` (5). All persisted. Raising the push interval stretches
the Cloudflare free-plan KV write budget (see `remote/worker/DEPLOY.md`).

## Preservation

`updater.py` keeps `remote_token.txt` in `PRESERVED_FILES` so an update never
overwrites the secret. `remote/` sources and all `remote_*` runtime files are
gitignored / never copied into `dist/`.
