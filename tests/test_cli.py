"""Tests for config loading, transactional CSV ops, and the stage command (B/D/E)."""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import common  # noqa: E402
import outreach_cli  # noqa: E402

FIELDS = ["contact_email", "contact_name", "company", "role", "subject", "note"]


def _row(company, email):
    return {"contact_email": email, "contact_name": "x", "company": company,
            "role": "r", "subject": "s", "note": "n"}


def test_write_db_is_atomic_roundtrip(tmp_path):
    p = tmp_path / "x.csv"
    common.write_db(str(p), FIELDS, [_row("A", "a@x.com")])
    fields, rows = common.read_rows(str(p))
    assert fields == FIELDS
    assert rows[0]["company"] == "A"
    # no leftover temp files in the dir
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".tmp_")]


def _cfg(tmp_path, batch_size=14):
    return {
        "files": {"queue": str(tmp_path / "q.csv"),
                  "database": str(tmp_path / "db.csv"),
                  "sent_log": str(tmp_path / "sent.csv")},
        "pacing": {"batch_size": batch_size},
    }


def test_stage_moves_dedups_and_respects_n(tmp_path):
    cfg = _cfg(tmp_path, batch_size=2)
    common.write_db(cfg["files"]["queue"], FIELDS,
                    [_row("A", "a@x.com"), _row("B", "b@x.com"), _row("C", "c@x.com")])
    common.write_db(cfg["files"]["database"], FIELDS, [])
    common.write_db(cfg["files"]["sent_log"], FIELDS, [_row("C", "c@x.com")])  # already sent

    outreach_cli.cmd_stage(argparse.Namespace(n=None), cfg)

    _, db_rows = common.read_rows(cfg["files"]["database"])
    _, q_rows = common.read_rows(cfg["files"]["queue"])
    assert {r["company"] for r in db_rows} == {"A", "B"}  # C skipped (already sent), capped at 2
    assert q_rows == []  # A/B moved out, C dropped (already sent) -> queue cleaned


class _FakeIMAP:
    """IMAP stand-in backed by an in-memory message store.

    Each message: {"box": mailbox, "uid": int, "h": {header: value}}. Supports the
    searches backfill uses: TO, FROM, and HEADER REFERENCES/In-Reply-To.
    """
    def __init__(self, messages):
        self.msgs = messages
        self.box = None

    def select(self, mailbox, readonly=False):
        self.box = mailbox
        return ("OK", [b""])

    def _box(self):
        return sorted((m for m in self.msgs if m["box"] == self.box), key=lambda m: m["uid"])

    @staticmethod
    def _match(m, crit):
        if crit[0] in ("TO", "FROM"):
            return crit[1].strip('"').lower() in (m["h"].get(crit[0].title(), "")).lower()
        if crit[0] == "HEADER":
            field, val = crit[1], crit[2].strip('"')
            return val in (m["h"].get(field.title() if field != "In-Reply-To" else field, "") or "")
        return False

    def uid(self, op, *args):
        if op == "search":
            crit = tuple(a for a in args[1:])  # drop charset (None)
            hits = [str(m["uid"]).encode() for m in self._box() if self._match(m, crit)]
            return ("OK", [b" ".join(hits) if hits else b""])
        if op == "fetch":
            uid, spec = int(args[0]), args[1]
            m = next((x for x in self._box() if x["uid"] == uid), None)
            if not m:
                return ("OK", [None])
            fields = re.search(r"FIELDS \(([^)]+)\)", spec).group(1).split()
            raw = "".join(f"{f}: {m['h'].get(f, '')}\r\n" for f in fields) + "\r\n"
            return ("OK", [(b"meta", raw.encode())])
        return ("NO", [b""])

    def logout(self):
        pass


SENT = '"[Gmail]/Sent Mail"'
ALL = '"[Gmail]/All Mail"'


def _login_selecting(fake):
    def _login(*a, **k):
        fake.select(k.get("mailbox", ALL))
        return fake
    return _login


def test_backfill_thread_match_catches_different_address_reply(tmp_path, monkeypatch):
    sent_log = tmp_path / "sent.csv"
    common.write_db(str(sent_log), ["contact_email", "company"],
                    [{"contact_email": "jobs@resim.ai", "company": "ReSim"},
                     {"contact_email": "hi@b.com", "company": "B"}])
    msgs = [
        # what we sent (Sent Mail), with the Gmail-assigned Message-ID
        {"box": SENT, "uid": 1, "h": {"To": "jobs@resim.ai", "From": "me@x.com",
            "Date": "Wed, 03 Jun 2026 10:00:00 -0700", "Message-ID": "<orig@gmail.com>"}},
        {"box": SENT, "uid": 2, "h": {"To": "hi@b.com", "From": "me@x.com",
            "Date": "Wed, 03 Jun 2026 10:00:00 -0700", "Message-ID": "<b@gmail.com>"}},
        # the reply comes from a DIFFERENT address, but references our message-id
        {"box": ALL, "uid": 10, "h": {"From": "matthew@resim.ai", "To": "me@x.com",
            "Date": "Fri, 05 Jun 2026 09:00:00 -0700", "In-Reply-To": "<orig@gmail.com>"}},
    ]
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login_selecting(_FakeIMAP(msgs)))
    cfg = {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                     "sent_log": str(sent_log)},
           "email": {"address": "me@x.com", "keychain_service": "svc",
                     "imap_host": "imap.example.com"}}
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True), cfg)

    _, rows = common.read_rows(str(sent_log))
    by = {r["contact_email"]: r for r in rows}
    assert by["jobs@resim.ai"]["sent_at"] == "2026-06-03T17:00:00Z"
    assert by["jobs@resim.ai"]["last_reply"] == "2026-06-05T16:00:00Z"  # caught via thread
    assert by["hi@b.com"]["last_reply"] == ""                            # no reply


def test_backfill_does_not_overwrite_newer_existing_last_reply(tmp_path, monkeypatch):
    sent_log = tmp_path / "sent.csv"
    common.write_db(str(sent_log), ["contact_email", "company", "last_reply"],
                    [{"contact_email": "a@x.com", "company": "A",
                      "last_reply": "2026-07-01T00:00:00Z"}])  # already a newer reply
    msgs = [
        {"box": SENT, "uid": 1, "h": {"To": "a@x.com", "From": "me@x.com",
            "Date": "Wed, 03 Jun 2026 10:00:00 -0700", "Message-ID": "<o@gmail.com>"}},
        {"box": ALL, "uid": 9, "h": {"From": "a@x.com", "To": "me@x.com",
            "Date": "Thu, 04 Jun 2026 09:00:00 -0700"}},  # OLDER than the existing value
    ]
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login_selecting(_FakeIMAP(msgs)))
    cfg = {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                     "sent_log": str(sent_log)},
           "email": {"address": "me@x.com", "keychain_service": "svc",
                     "imap_host": "imap.example.com"}}
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True), cfg)

    _, rows = common.read_rows(str(sent_log))
    assert rows[0]["last_reply"] == "2026-07-01T00:00:00Z"  # kept the newer one


def test_backfill_from_match_still_catches_same_address_reply(tmp_path, monkeypatch):
    sent_log = tmp_path / "sent.csv"
    common.write_db(str(sent_log), ["contact_email", "company"],
                    [{"contact_email": "a@x.com", "company": "A"}])
    msgs = [
        {"box": SENT, "uid": 1, "h": {"To": "a@x.com", "From": "me@x.com",
            "Date": "Wed, 03 Jun 2026 10:00:00 -0700", "Message-ID": "<o@gmail.com>"}},
        {"box": ALL, "uid": 9, "h": {"From": "a@x.com", "To": "me@x.com",
            "Date": "Thu, 04 Jun 2026 09:00:00 -0700"}},  # same-address reply, no headers
    ]
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login_selecting(_FakeIMAP(msgs)))
    cfg = {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                     "sent_log": str(sent_log)},
           "email": {"address": "me@x.com", "keychain_service": "svc",
                     "imap_host": "imap.example.com"}}
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True), cfg)

    _, rows = common.read_rows(str(sent_log))
    assert rows[0]["last_reply"] == "2026-06-04T16:00:00Z"


def test_setup_scaffolds_config_at_requested_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OUTREACH_SECRET_BACKEND", "env")  # avoid macOS `security` call
    cfg_path = tmp_path / "sub" / "config.toml"
    cfg = common.load_config(str(cfg_path))  # file doesn't exist yet
    outreach_cli.cmd_setup(argparse.Namespace(), cfg)
    assert cfg_path.exists()  # created at the requested path, not CWD
    assert "address" in cfg_path.read_text()


def test_followup_uses_sent_at_cadence(tmp_path, capsys):
    import datetime
    sent_log = tmp_path / "sent.csv"
    fields = common.SENT_LOG_FIELDS
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    common.write_db(str(sent_log), fields, [
        {"contact_email": "old@x.com", "company": "OldCo", "sent_at": old},
        {"contact_email": "new@x.com", "company": "NewCo", "sent_at": recent},
        {"contact_email": "blank@x.com", "company": "BlankCo", "sent_at": ""},
        {"contact_email": "replied@x.com", "company": "RepCo", "sent_at": old,
         "last_reply": recent},  # old AND replied -> excluded, not a candidate
    ])
    cfg = {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                     "sent_log": str(sent_log)}, "pacing": {"followup_after_days": 6}}
    outreach_cli.cmd_followup(argparse.Namespace(), cfg)
    out = capsys.readouterr().out
    assert "old@x.com" in out               # 10 days, no reply -> due
    assert "new@x.com" not in out           # 1 day -> not due
    assert "replied@x.com" not in out       # old but replied -> excluded
    assert "1 contact(s) excluded" in out   # the replied one
    assert "no sent_at" in out              # blank -> reported as unknown


def test_stage_skips_already_staged(tmp_path):
    cfg = _cfg(tmp_path)
    common.write_db(cfg["files"]["queue"], FIELDS, [_row("A", "a@x.com")])
    common.write_db(cfg["files"]["database"], FIELDS, [_row("A", "a@x.com")])  # already staged
    common.write_db(cfg["files"]["sent_log"], FIELDS, [])

    outreach_cli.cmd_stage(argparse.Namespace(n=None), cfg)

    _, db_rows = common.read_rows(cfg["files"]["database"])
    assert len(db_rows) == 1  # not duplicated
