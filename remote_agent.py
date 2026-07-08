"""
A2 Remote companion agent.

The ONLY network-touching process of the remote-status feature. The main
app never talks to the network; it only exchanges small JSON files with
this agent inside the install folder:

    remote_status.json   written by the GUI every ~3 s while remote is on
    remote_cmd.json      written by THIS agent when a verified command
                         arrives; the GUI polls it, executes, deletes it
    remote_token.txt     the per-install secret (created by the GUI)
    remote_heartbeat.json  written by this agent each cycle so the GUI can
                         show "companion running / last push X s ago"

Outbound HTTPS only, to the user's own Cloudflare Worker relay (see
remote/worker/DEPLOY.md). No inbound ports, no UPnP, nothing listens.

Cadence is deliberately write-thrifty: it polls for commands at a steady
~10 s (cheap KV reads) but pushes status (the scarce KV writes) only about
once a minute while idle, bursting to ~5 s for a short window after a command
so the effect shows up quickly. See the IDLE_PUSH_* / ACTIVE_* constants.

Protocol (must match remote/worker/worker.js and remote/pages/index.html):
    install_id = SHA-256 hex of the token; every relay key derives from it.
    Push:  POST <relay>/push  {"install_id", "blob", "sig"} where
           blob = {"nonce", "status", "ts"} and
           sig  = HMAC-SHA256(token, canonical_json(blob)) hex.
    Poll:  GET <relay>/cmd?id=<install_id>; a returned {"blob","sig"} is
           verified (signature, |now-ts| <= 60 s, unseen nonce, allow-listed
           command) before remote_cmd.json is written.

Canonical JSON is json.dumps(obj, sort_keys=True, separators=(",", ":"))
with the default ensure_ascii=True. The JS side mirrors this byte for byte
(see the long comment in remote/pages/index.html); to keep that true, all
numbers inside signed payloads MUST be integers (floats format differently
between Python and JavaScript).

Exit rules (so the agent can never outlive the feature):
    - REMOTE_ENABLED is false in settings.json        -> exit
    - remote_token.txt is missing                     -> exit
    - remote_status.json stale for over 10 minutes    -> exit (GUI closed)

Deliberately stdlib-only, like updater.py, so the built "A2 Remote.exe"
stays small. It reads settings.json directly WITHOUT importing config.py.

Usage:
    python remote_agent.py      (or run "A2 Remote.exe" next to the main exe)
"""

import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

SETTINGS_PATH = BASE_DIR / "settings.json"
TOKEN_PATH = BASE_DIR / "remote_token.txt"
STATUS_PATH = BASE_DIR / "remote_status.json"
CMD_PATH = BASE_DIR / "remote_cmd.json"
HEARTBEAT_PATH = BASE_DIR / "remote_heartbeat.json"
LOG_PATH = BASE_DIR / "remote_agent.log"

ALLOWED_COMMANDS = ("start", "stop", "sleep_phone", "close_emulator")

# Cadence. Status pushes are the only KV WRITES (the scarce Cloudflare
# free-plan resource), so they run slowly when idle and only burst fast right
# after a command. Command polls are KV READS (cheap and plentiful), so they
# run at a steady, responsive rate regardless.
IDLE_PUSH_DEFAULT = 60        # default idle seconds between status pushes
IDLE_PUSH_MIN = 30            # clamp floor
IDLE_PUSH_MAX = 270           # clamp ceiling (< worker STATUS_TTL so the blob
                             # never lapses to 404 between idle pushes)
ACTIVE_PUSH_INTERVAL = 5      # push cadence during the post-command burst
ACTIVE_WINDOW = 90            # stay in fast/burst mode this long after a command
POLL_INTERVAL = 10           # command-poll cadence while idle
ACTIVE_POLL_INTERVAL = 5     # command-poll cadence during the burst
BACKOFF_MAX = 60              # cap for the network-error backoff
HTTP_TIMEOUT = 10             # seconds per request
STATUS_STALE_EXIT = 600       # exit when remote_status.json is this stale
CMD_MAX_AGE = 60              # reject commands older/newer than this
NONCE_REMEMBER = 600          # remember seen nonces for this long
MAX_LOG_LINES = 40            # clamp for the pushed log tail
MAX_LOG_LINE_CHARS = 400      # clamp for each pushed log line

USER_AGENT = "A2-Remote-Agent"

log = logging.getLogger("remote_agent")


# ------------------------------------------------------------------ helpers

def canonical_json(obj):
    """The canonical serialisation both ends sign. MUST stay in sync with
    canonicalJson() in remote/pages/index.html (see module docstring)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sign(token, obj):
    """Lowercase-hex HMAC-SHA256 of the canonical JSON of `obj`."""
    return hmac.new(token.encode("utf-8"),
                    canonical_json(obj).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def install_id_of(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_json_file(path):
    """Parse a JSON file, or return None on any problem (missing, malformed,
    or caught mid-write; the writers all use atomic os.replace, so a retry
    on the next cycle always resolves this)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def atomic_write_json(path, obj):
    """Write via a temp file + os.replace so readers never see a half file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def read_token():
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def normalize_relay_url(url):
    """Validate the relay URL. HTTPS is REQUIRED; plain http is allowed only
    for loopback (wrangler dev testing), where traffic never leaves the
    machine. Returns the URL without a trailing slash, or None if refused."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1"):
        return url
    return None


def http_json(url, payload=None):
    """One HTTP round trip. POSTs `payload` as JSON when given, else GETs.
    Returns (status_code, parsed_json_or_None). Raises OSError/URLError on
    network-level failures (the caller backs off)."""
    data = None
    headers = {"User-Agent": USER_AGENT}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        # 4xx/5xx: still a server answer, not a network failure.
        body = e.read()
        code = e.code
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return code, parsed


def clean_for_signing(value, depth=0):
    """Force a status payload into the signable subset: strings, bools,
    integers, None, and lists/dicts of those. Floats become integers when
    exact, else strings, because float formatting differs between Python
    and JavaScript and would break signature verification."""
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else repr(value)
    if isinstance(value, list):
        return [clean_for_signing(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): clean_for_signing(v, depth + 1)
                for k, v in value.items()}
    return str(value)


def build_status_payload(raw):
    """Clamp the GUI-written status file into the pushed payload."""
    if not isinstance(raw, dict):
        raw = {}
    lines = raw.get("log")
    if not isinstance(lines, list):
        lines = []
    lines = [str(x)[:MAX_LOG_LINE_CHARS] for x in lines[-MAX_LOG_LINES:]]
    acts = raw.get("activity")
    if not isinstance(acts, list):
        acts = []
    acts = [str(x)[:MAX_LOG_LINE_CHARS] for x in acts[-MAX_LOG_LINES:]]
    payload = {
        "running": bool(raw.get("running", False)),
        "status": str(raw.get("status", ""))[:200],
        "mode": str(raw.get("mode", ""))[:40],
        "version": str(raw.get("version", ""))[:40],
        "activity": acts,
        "log": lines,
    }
    return clean_for_signing(payload)


# ------------------------------------------------------------------ agent

class Agent:
    def __init__(self):
        self.nonces = {}          # nonce -> forget-after timestamp
        self.failures = 0         # consecutive network failures
        self.last_push_ok = 0     # ts of the last successful push
        self.last_push = 0.0      # ts of the last push ATTEMPT (cadence gate)
        self.active_until = 0.0   # burst-mode (fast) until this ts
        self.last_error = ""
        self.started = time.time()
        self.warned_no_relay = False

    # ---- exit rules -------------------------------------------------

    def should_exit(self, settings):
        if not settings.get("REMOTE_ENABLED", False):
            log.info("REMOTE_ENABLED is off; exiting")
            return True
        if read_token() is None:
            log.info("remote_token.txt is gone; exiting")
            return True
        status = read_json_file(STATUS_PATH)
        ref = self.started
        if isinstance(status, dict) and isinstance(status.get("ts"), (int, float)):
            ref = max(ref, float(status["ts"]))
        elif STATUS_PATH.exists():
            try:
                ref = max(ref, STATUS_PATH.stat().st_mtime)
            except OSError:
                pass
        if time.time() - ref > STATUS_STALE_EXIT:
            log.info("remote_status.json stale for over %d s (GUI closed?); "
                     "exiting", STATUS_STALE_EXIT)
            return True
        return False

    # ---- one push/poll cycle ----------------------------------------

    def cycle(self, settings, token):
        relay = normalize_relay_url(settings.get("REMOTE_RELAY_URL", ""))
        if relay is None:
            if not self.warned_no_relay:
                log.warning("relay URL missing or not https; waiting for a "
                            "valid REMOTE_RELAY_URL in settings.json")
                self.warned_no_relay = True
            self.last_error = "relay URL missing or not https"
            return
        self.warned_no_relay = False
        iid = install_id_of(token)
        net_failed = False
        now = time.time()

        # Push the latest status, but only when the push interval has elapsed.
        # Idle interval is slow (KV-write friendly); burst interval is fast for
        # a short window after a command so its effect shows up quickly.
        push_interval = (ACTIVE_PUSH_INTERVAL if now < self.active_until
                         else self.idle_push_interval(settings))
        if now - self.last_push >= push_interval:
            self.last_push = now
            try:
                raw = read_json_file(STATUS_PATH)
                blob = {
                    "nonce": secrets.token_hex(16),
                    "status": build_status_payload(raw),
                    "ts": int(time.time()),
                }
                code, body = http_json(relay + "/push", {
                    "install_id": iid, "blob": blob, "sig": sign(token, blob)})
                if code == 200:
                    self.last_push_ok = int(time.time())
                    self.last_error = ""
                else:
                    err = (body or {}).get("error", "") if isinstance(body, dict) else ""
                    self.last_error = "push rejected: HTTP %d %s" % (code, err)
                    log.warning(self.last_error)
            except (urllib.error.URLError, OSError, ValueError) as e:
                net_failed = True
                self.last_error = "push failed: %s" % _short_err(e)
                log.warning(self.last_error)

        # Always poll for a pending command (a cheap KV read). A verified
        # command switches on burst mode so status pushes speed up too.
        try:
            code, body = http_json(relay + "/cmd?id=" + iid)
            if code == 200 and isinstance(body, dict):
                if self.handle_command(token, body):
                    self.active_until = time.time() + ACTIVE_WINDOW
            # 404 = no pending command; anything else is not actionable.
        except (urllib.error.URLError, OSError, ValueError) as e:
            net_failed = True
            self.last_error = "poll failed: %s" % _short_err(e)
            log.warning(self.last_error)

        self.failures = self.failures + 1 if net_failed else 0

    # ---- command verification ---------------------------------------

    def handle_command(self, token, body):
        """Verify and (if good) write a command for the GUI. Returns True when a
        command was accepted, so the caller can switch on burst mode."""
        blob = body.get("blob")
        sig = body.get("sig")
        if not isinstance(blob, dict) or not isinstance(sig, str):
            log.warning("command dropped: malformed relay response")
            return False
        if not hmac.compare_digest(sign(token, blob), sig.lower()):
            log.warning("command dropped: BAD SIGNATURE")
            return False
        command = blob.get("command")
        if command not in ALLOWED_COMMANDS:
            log.warning("command dropped: not allow-listed (%r)", command)
            return False
        ts = blob.get("ts")
        now = time.time()
        if not isinstance(ts, int) or abs(now - ts) > CMD_MAX_AGE:
            log.warning("command dropped: stale or bad timestamp (%s)", command)
            return False
        nonce = blob.get("nonce")
        self.nonces = {n: exp for n, exp in self.nonces.items() if exp > now}
        if not isinstance(nonce, str) or nonce in self.nonces:
            log.warning("command dropped: replayed nonce (%s)", command)
            return False
        self.nonces[nonce] = now + NONCE_REMEMBER
        atomic_write_json(CMD_PATH, {
            "command": command,
            "ts": ts,
            "nonce": nonce,
            "received_at": int(now),
        })
        log.info("verified command written for the GUI: %s", command)
        return True

    # ---- heartbeat ----------------------------------------------------

    def write_heartbeat(self):
        try:
            atomic_write_json(HEARTBEAT_PATH, {
                "ts": int(time.time()),
                "pid": os.getpid(),
                "last_push_ok": self.last_push_ok,
                "error": self.last_error[:200],
            })
        except OSError:
            pass

    # ---- main loop ----------------------------------------------------

    def idle_push_interval(self, settings):
        """Seconds between status pushes while idle (REMOTE_PUSH_INTERVAL,
        clamped so the blob never outlives the worker's STATUS_TTL)."""
        try:
            v = int(settings.get("REMOTE_PUSH_INTERVAL", IDLE_PUSH_DEFAULT))
        except (TypeError, ValueError):
            v = IDLE_PUSH_DEFAULT
        return max(IDLE_PUSH_MIN, min(IDLE_PUSH_MAX, v))

    def sleep_seconds(self, settings):
        # The loop wakes on the command-poll cadence (cheap reads); the push
        # cadence gate inside cycle() decides when to actually spend a KV write.
        if self.failures > 0:
            # Exponential backoff on consecutive network errors, capped.
            return min(BACKOFF_MAX, POLL_INTERVAL * (2 ** min(self.failures, 6)))
        return (ACTIVE_POLL_INTERVAL if time.time() < self.active_until
                else POLL_INTERVAL)

    def run(self):
        log.info("companion started (pid %d)", os.getpid())
        while True:
            settings = read_json_file(SETTINGS_PATH) or {}
            if self.should_exit(settings):
                break
            token = read_token()
            if token is None:      # vanished between checks
                break
            # The token is re-read every cycle, so a GUI "Regenerate token"
            # is picked up automatically (new install_id next push).
            self.cycle(settings, token)
            self.write_heartbeat()
            time.sleep(self.sleep_seconds(settings))
        self.write_heartbeat()
        log.info("companion exiting")


def _short_err(e):
    """One-line error text without any request/URL details (the token never
    appears in URLs anyway, but keep logs terse and boring)."""
    text = str(e) or e.__class__.__name__
    return text.replace("\n", " ")[:160]


def setup_logging():
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=65536, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main():
    setup_logging()
    try:
        Agent().run()
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
    except Exception:
        log.exception("companion crashed")
        raise


if __name__ == "__main__":
    main()
