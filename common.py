#!/usr/bin/env python3
"""Shared plumbing for the internship-outreach pipeline.

This module holds the plumbing shared by send.py, read_replies.py, thread_reply.py,
and outreach_cli.py: the Keychain fetch, IMAP login, SMTP session, header decoding,
template rendering, machine config, and (atomic, backed-up) CSV helpers.

Config: machine settings come from `config.toml` (see config.toml.example). The
Gmail app password comes from a credential backend (keyring / macOS Keychain /
env var), never from a file.
"""
import contextlib
import csv
import datetime
import getpass
import imaplib
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import tomllib
from email.header import decode_header, make_header

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_NAME = "config.toml"

KEYCHAIN_SERVICE = "mailmerge-gmail"
IMAP_HOST = "imap.gmail.com"
ALL_MAIL = '"[Gmail]/All Mail"'  # catches replies even after they've been archived

# Conventional defaults so a missing config.toml (e.g. a fresh clone) never hard-
# crashes an import. Only the email address has no safe default.
DEFAULT_EMAIL = {
    "smtp_host": "smtp.gmail.com", "smtp_port": 587,
    "imap_host": IMAP_HOST, "keychain_service": KEYCHAIN_SERVICE,
}
DEFAULT_FILES = {
    "template": "mailmerge_template.txt",
    "database": "mailmerge_database.csv",
    "sent_log": "sent_log.csv",
    "queue": "outreach_queue.csv",
    "resume": "resume.pdf",
}
DEFAULT_PACING = {"batch_size": 14, "followup_after_days": 6}

# Canonical sent-log schema. Existing logs may have only the first columns; that's
# tolerated on read and extended by `outreach migrate`.
SENT_LOG_FIELDS = ["contact_email", "contact_name", "company", "role",
                   "subject", "note", "sent_at", "last_reply"]

# Embedded so `outreach setup` can scaffold a config without a shipped example file
# (works for any install mode, editable or wheel).
CONFIG_TEMPLATE = '''\
[email]
address = "you@gmail.com"
smtp_host = "smtp.gmail.com"
smtp_port = 587
imap_host = "imap.gmail.com"
# Store your Gmail APP password (not your login password) in a backend, then point
# this name at it. See SECURITY.md.
keychain_service = "mailmerge-gmail"

[files]
template = "mailmerge_template.txt"
database = "mailmerge_database.csv"
sent_log = "sent_log.csv"
queue = "outreach_queue.csv"
resume = "resume.pdf"

[pacing]
batch_size = 14            # cold emails per day; do not blast
followup_after_days = 6

[outreach]
# The one thing you specify: what kind of internships/roles to target.
area = "<e.g. Machine Learning, scientific computing; remote, unpaid>"
'''


def find_config(explicit=None):
    """Locate config.toml: --config > $OUTREACH_CONFIG > ./config.toml (in CWD).

    The directory containing the chosen path is the WORKSPACE; all relative paths in
    [files] resolve against it, so a pip-installed CLI reads/writes the user's working
    directory, never the install location. The returned path may not exist yet (e.g.
    before `outreach setup`); load_config tolerates that. There is deliberately NO
    fallback to the module/install dir, so a stray source-tree config is never used.
    """
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("OUTREACH_CONFIG")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(DEFAULT_CONFIG_NAME)  # ./config.toml in the current dir


def resolve(path, base):
    """Resolve a (possibly relative) path against `base` (the workspace dir)."""
    return path if os.path.isabs(path) else os.path.join(base, path)


def load_config(path=None):
    """Return machine config with ABSOLUTE file paths and the workspace dir.

    Shape: {email, files (absolute paths), pacing, outreach, workspace, config_path}.
    Never raises on a missing file — defaults fill in everything except the email
    address (set [email].address in config.toml before sending).
    """
    path = path or find_config()
    workspace = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    email_cfg = {**DEFAULT_EMAIL, **data.get("email", {})}
    files_cfg = {k: resolve(v, workspace)
                 for k, v in {**DEFAULT_FILES, **data.get("files", {})}.items()}
    return {
        "email": email_cfg,
        "files": files_cfg,
        "pacing": {**DEFAULT_PACING, **data.get("pacing", {})},
        "outreach": data.get("outreach", {}),
        "workspace": workspace,
        "config_path": path,
    }


def validate_for_send(cfg):
    """Fail early (ValueError) on config that would make a send incoherent."""
    em = cfg["email"]
    if not em.get("address"):
        raise ValueError("no sender: set [email].address in config.toml")
    try:
        int(em["smtp_port"])
    except (KeyError, ValueError, TypeError):
        raise ValueError(f"[email].smtp_port must be an integer, got {em.get('smtp_port')!r}")
    template = cfg["files"]["template"]
    if not os.path.exists(template):
        raise ValueError(f"template not found: {template}")
    headers, _ = load_template(template)
    missing = [h for h in ("TO", "SUBJECT") if h not in headers]
    if missing:
        raise ValueError(f"template {template} missing header(s): {', '.join(missing)}")


def backup(path, backup_dir=None):
    """Copy `path` into a `.backups/` dir (next to it) with a UTC timestamp.

    Returns the backup path, or None if the source does not exist yet.
    """
    if not os.path.exists(path):
        return None
    backup_dir = backup_dir or os.path.join(
        os.path.dirname(os.path.abspath(path)), ".backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(backup_dir, f"{os.path.basename(path)}.{stamp}.bak")
    shutil.copy2(path, dest)
    return dest


def _atomic_write(path, write_fn):
    """Write via a temp file in the same dir, fsync, then os.replace().

    fsync of both the file and its directory makes the replacement durable across
    an OS/power crash, not just a clean process exit.
    """
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Directory fsync makes the rename durable, but O_DIRECTORY/dir-fsync isn't
        # supported everywhere (e.g. Windows); the file fsync + replace above are the
        # core guarantee, so treat the dir fsync as best-effort.
        try:
            dirfd = os.open(d, getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except (OSError, AttributeError):
            pass
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def normalize_email(addr):
    """Canonical contact identity used for dedup everywhere (stage, send, replies)."""
    return (addr or "").strip().lower()


def sent_emails(log_path):
    """Set of normalized recipient addresses already in the sent log (idempotency)."""
    if not os.path.exists(log_path):
        return set()
    _, rows = read_rows(log_path)
    return {normalize_email(r.get("contact_email")) for r in rows
            if r.get("contact_email")}


ENV_PASSWORD = "OUTREACH_GMAIL_APP_PASSWORD"
ENV_BACKEND = "OUTREACH_SECRET_BACKEND"  # force one of: keyring|security|env


def _from_keyring(service):
    """Look up the password via the optional, cross-platform `keyring` library."""
    try:
        import keyring  # lazy: absence must not affect imports/tests
    except ImportError:
        return None
    try:
        return keyring.get_password(service, getpass.getuser())
    except Exception:
        return None


def _from_security(service):
    """Look up the password via the macOS `security` CLI (Keychain)."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", service, "-a", getpass.getuser(), "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _from_env(_service):
    return os.environ.get(ENV_PASSWORD) or None


# Recommended local backends first; env var is an explicit CI/escape hatch only
# (env vars can leak via shell history, process listings, and CI logs).
_BACKENDS = {"keyring": _from_keyring, "security": _from_security, "env": _from_env}
_AUTO_ORDER = ["keyring", "security", "env"]


def _setup_hint():
    return (f"Store your Gmail APP password once. Recommended:\n"
            f"  - keyring (any OS): pip install keyring; "
            f"python -m keyring set {KEYCHAIN_SERVICE} \"$USER\"\n"
            f"  - macOS Keychain:   security add-generic-password -s "
            f"{KEYCHAIN_SERVICE} -a \"$USER\" -w\n"
            f"  - CI/escape hatch:  export {ENV_PASSWORD}=...")


def _backend_order():
    """Resolve the backend search order, exiting on an invalid forced override."""
    forced = os.environ.get(ENV_BACKEND)
    if forced and forced not in _BACKENDS:
        sys.exit(f"ERROR: invalid {ENV_BACKEND}={forced!r}; choose one of "
                 f"{', '.join(_BACKENDS)}.")
    return [forced] if forced else _AUTO_ORDER


def secret_status(service=KEYCHAIN_SERVICE):
    """Report (backend_name, found_bool, setup_hint) without raising. For `setup`."""
    for name in _backend_order():
        if _BACKENDS[name](service):
            return name, True, ""
    return "none", False, _setup_hint()


def get_password(service=KEYCHAIN_SERVICE):
    """Fetch the Gmail app password from the first backend that has it.

    Order: an explicit OUTREACH_SECRET_BACKEND override, else keyring (if installed)
    > macOS `security` > the OUTREACH_GMAIL_APP_PASSWORD env var. Cross-platform; the
    macOS-only path is just one option. Exits with a setup hint if none has it.
    """
    for name in _backend_order():
        pw = _BACKENDS[name](service)
        if pw:
            return pw
    sys.exit(f"\nERROR: no Gmail app password found (service '{service}').\n{_setup_hint()}\n")


@contextlib.contextmanager
def smtp_session(host, port, username, password, ssl_context=None):
    """Logged-in STARTTLS SMTP session as a context manager.

    Preserves both original call sites: send.py used a plain `starttls()`, while
    thread_reply.py passed an explicit ssl context. Pass `ssl_context` to get the
    latter; omit it for the former. Behavior is otherwise identical.
    """
    with smtplib.SMTP(host, port) as smtp:
        if ssl_context is not None:
            smtp.starttls(context=ssl_context)
        else:
            smtp.starttls()
        smtp.login(username, password)
        yield smtp


def imap_login(username, password, readonly=True, host=IMAP_HOST, mailbox=ALL_MAIL):
    """Log into Gmail over IMAP and select a mailbox (All Mail, INBOX fallback)."""
    imap = imaplib.IMAP4_SSL(host)
    imap.login(username, password)
    typ, _ = imap.select(mailbox, readonly=readonly)
    if typ != "OK":  # All Mail label may be localized/renamed
        imap.select("INBOX", readonly=readonly)
    return imap


def decode_header_str(s):
    """Best-effort decode of a MIME-encoded header to a plain string."""
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def body_text(msg):
    """Extract the plain-text body from a parsed email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return str(msg.get_payload())


def render(text, row):
    """Replace every {{column}} with the row value."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: row.get(m.group(1), m.group(0)), text)


def load_template(template_path):
    """Split the template into its header dict and body, preserving raw text for {{subst}}."""
    raw = open(template_path, encoding="utf-8").read()
    head, _, body = raw.partition("\n\n")
    headers = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().upper()] = v.strip()
    return headers, body


def read_rows(db_path):
    """Return (fieldnames, rows) from a mailmerge CSV.

    Uses utf-8-sig so a stray BOM on the header (e.g. contact_email) is stripped
    rather than silently corrupting the column name and defeating dedup.
    """
    with open(db_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def archive(log_path, fieldnames, sent_rows):
    """Record sent rows in the log via an ATOMIC full rewrite (not a bare append).

    - Durable: read existing rows, add the new ones, write atomically (temp+fsync+
      os.replace), so a crash can't leave a half-written line that later breaks
      dedup.
    - Header-stable: reuse the existing log's column order so appended rows never
      land under the wrong columns.
    - Fail-closed: refuse to write to a non-empty log whose header lacks
      contact_email, since that would silently defeat the idempotency guard.
    """
    has_log = os.path.exists(log_path) and os.path.getsize(log_path) > 0
    existing_fields, existing_rows = read_rows(log_path) if has_log else (None, [])
    if existing_fields is not None and "contact_email" not in existing_fields:
        raise ValueError(
            f"{log_path} header lacks 'contact_email' (got {existing_fields}); "
            f"refusing to write, as it would defeat dedup.")
    out_fields = existing_fields or fieldnames

    def _w(f):
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(existing_rows)
        w.writerows(sent_rows)
    _atomic_write(log_path, _w)


def utcnow_iso():
    """Current UTC time as an ISO-8601 'Z' timestamp (for sent_at stamps)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def migrate_sent_log(log_path):
    """Extend an existing sent log's header to the full SENT_LOG_FIELDS (blank-fill).

    Backward-compatible: old rows keep their values and gain empty new columns
    (e.g. sent_at). No-op if the log is absent or already complete. Returns True if
    it rewrote the file. Run this as a preflight BEFORE sending, never after.
    """
    if not (os.path.exists(log_path) and os.path.getsize(log_path) > 0):
        return False
    fields, rows = read_rows(log_path)
    if "contact_email" not in (fields or []):
        raise ValueError(f"{log_path} header lacks 'contact_email'; cannot migrate.")
    missing = [f for f in SENT_LOG_FIELDS if f not in fields]
    if not missing:
        return False
    new_fields = fields + missing

    def _w(f):
        w = csv.DictWriter(f, fieldnames=new_fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**{k: "" for k in new_fields}, **r})
    _atomic_write(log_path, _w)
    return True


def write_db(db_path, fieldnames, rows):
    """Atomically rewrite the database with exactly `rows` (header always written).

    Uses a temp file + os.replace so a crash mid-write can never leave a truncated
    or half-written CSV that would lose staged leads.
    """
    def _w(f):
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    _atomic_write(db_path, _w)
