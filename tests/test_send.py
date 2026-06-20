"""Send-path tests: golden MIME parity + idempotency / crash-safety behavior.

Hermetic: everything uses committed fixtures under tests/fixtures and a temp
workspace config, so this runs on a fresh clone with no real template/resume/config.
"""
import contextlib
import hashlib
import json
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import common  # noqa: E402
import send  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
FIXTURE_TEMPLATE = os.path.join(FIXTURES, "template.txt")
GOLDEN = os.path.join(ROOT, "tests", "golden", "build_message.json")
FIELDS = ["contact_email", "contact_name", "company", "role", "subject", "note"]

SAMPLE_ROW = {
    "contact_email": "careers@example.com",
    "contact_name": "Alex",
    "company": "Example Co",
    "role": "ML Engineering Intern",
    "subject": "ML internship, remote",
    "note": "I saw your Who's Hiring post, and your simulation work is exactly my space",
}


def _snapshot(msg, body_text):
    snap = {"To": msg["To"], "Subject": msg["Subject"], "From": msg["From"],
            "is_multipart": msg.is_multipart(), "body_text": body_text, "parts": []}
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True) or b""
        snap["parts"].append({
            "content_type": part.get_content_type(),
            "filename": part.get_filename(),
            "disposition": part.get_content_disposition(),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_len": len(payload),
        })
    return snap


def test_build_message_matches_golden():
    """build_message output must match the committed fixture-based golden snapshot."""
    headers, body = common.load_template(FIXTURE_TEMPLATE)
    msg = send.build_message(SAMPLE_ROW, headers, body, FIXTURES)
    got = _snapshot(msg, common.render(body, SAMPLE_ROW))
    with open(GOLDEN) as f:
        want = json.load(f)
    assert got == want


def test_build_message_missing_attachment_raises(tmp_path):
    headers = {"TO": "{{contact_email}}", "SUBJECT": "{{subject}}",
               "FROM": "Me <me@x.com>", "ATTACHMENT": "does_not_exist.pdf"}
    with pytest.raises(FileNotFoundError):
        send.build_message(SAMPLE_ROW, headers, "Hi {{contact_name}}", str(tmp_path))


def test_build_message_handles_unicode_note():
    headers, body = common.load_template(FIXTURE_TEMPLATE)
    row = dict(SAMPLE_ROW, note="Café résumé, 日本語 simulation work")
    msg = send.build_message(row, headers, body, FIXTURES)
    assert "日本語" in common.body_text(msg)


class _FakeSMTP:
    """Stand-in for an smtplib session; fails for addresses in `fail_for`."""
    def __init__(self, fail_for):
        self.fail_for = fail_for
        self.sent = []

    def send_message(self, msg):
        to = msg["To"]
        if to in self.fail_for:
            raise RuntimeError("simulated SMTP failure")
        self.sent.append(to)


def _make_workspace(tmp_path, db_rows, sent_rows=None):
    """Write a temp workspace: config.toml + db.csv (+ sent.csv) + fixture resume."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[email]\n'
        'address = "u@x.com"\n'
        'smtp_host = "smtp.example.com"\n'
        'smtp_port = 587\n'
        'imap_host = "imap.example.com"\n'
        'keychain_service = "svc"\n'
        '[files]\n'
        f'template = "{FIXTURE_TEMPLATE}"\n'
        'database = "db.csv"\n'
        'sent_log = "sent.csv"\n'
        'resume = "resume.pdf"\n')
    shutil.copy(os.path.join(FIXTURES, "resume.pdf"), tmp_path / "resume.pdf")
    common.write_db(str(tmp_path / "db.csv"), FIELDS, db_rows)
    if sent_rows is not None:
        common.write_db(str(tmp_path / "sent.csv"), FIELDS, sent_rows)
    return cfg_path


def _run_send(monkeypatch, tmp_path, rows, fail_for, sent_rows=None):
    """Drive send.main(--send --yes) against a temp workspace with a fake SMTP."""
    cfg_path = _make_workspace(tmp_path, rows, sent_rows)
    fake = _FakeSMTP(fail_for)
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")

    @contextlib.contextmanager
    def fake_session(*a, **k):
        yield fake

    monkeypatch.setattr(common, "smtp_session", fake_session)
    monkeypatch.setattr(sys, "argv",
                        ["send.py", "--config", str(cfg_path), "--send", "--yes"])
    send.main()
    return tmp_path / "db.csv", tmp_path / "sent.csv", fake


def test_all_sent_archived_and_db_emptied(monkeypatch, tmp_path):
    rows = [dict(SAMPLE_ROW, contact_email="a@x.com"),
            dict(SAMPLE_ROW, contact_email="b@x.com")]
    db, log, _ = _run_send(monkeypatch, tmp_path, rows, fail_for=set())
    _, remaining = common.read_rows(str(db))
    _, archived = common.read_rows(str(log))
    assert remaining == []
    assert {r["contact_email"] for r in archived} == {"a@x.com", "b@x.com"}


def test_failed_row_kept_for_retry(monkeypatch, tmp_path):
    rows = [dict(SAMPLE_ROW, contact_email="ok@x.com"),
            dict(SAMPLE_ROW, contact_email="bad@x.com")]
    db, log, _ = _run_send(monkeypatch, tmp_path, rows, fail_for={"bad@x.com"})
    _, remaining = common.read_rows(str(db))
    _, archived = common.read_rows(str(log))
    assert [r["contact_email"] for r in remaining] == ["bad@x.com"]
    assert [r["contact_email"] for r in archived] == ["ok@x.com"]


def test_already_sent_address_is_not_resent(monkeypatch, tmp_path):
    """Idempotency: an address already in sent_log.csv is skipped, not resent."""
    rows = [dict(SAMPLE_ROW, contact_email="new@x.com"),
            dict(SAMPLE_ROW, contact_email="done@x.com")]
    db, log, fake = _run_send(monkeypatch, tmp_path, rows, fail_for=set(),
                              sent_rows=[dict(SAMPLE_ROW, contact_email="done@x.com")])
    assert fake.sent == ["new@x.com"]                 # done@ not re-sent
    _, archived = common.read_rows(str(log))
    emails = [r["contact_email"] for r in archived]
    assert emails.count("done@x.com") == 1            # not appended again
    _, remaining = common.read_rows(str(db))
    assert remaining == []


def test_sent_at_is_stamped_on_archive(monkeypatch, tmp_path):
    rows = [dict(SAMPLE_ROW, contact_email="a@x.com")]
    _, log, _ = _run_send(monkeypatch, tmp_path, rows, fail_for=set())
    fields, archived = common.read_rows(str(log))
    assert "sent_at" in fields
    assert archived[0]["sent_at"].endswith("Z") and archived[0]["sent_at"]


def test_migrate_sent_log_adds_columns_backward_compatibly(tmp_path):
    log = tmp_path / "sent.csv"
    common.write_db(str(log), ["contact_email", "company"],
                    [{"contact_email": "o@x.com", "company": "Old"}])
    assert common.migrate_sent_log(str(log)) is True
    fields, rows = common.read_rows(str(log))
    assert "sent_at" in fields
    assert rows[0]["contact_email"] == "o@x.com" and rows[0]["sent_at"] == ""
    assert common.migrate_sent_log(str(log)) is False  # idempotent


def test_within_batch_duplicate_sends_once(monkeypatch, tmp_path):
    """Two staged rows with the same normalized address -> exactly one send."""
    rows = [dict(SAMPLE_ROW, contact_email="A@x.com"),
            dict(SAMPLE_ROW, contact_email="  a@x.com  ")]
    _, _, fake = _run_send(monkeypatch, tmp_path, rows, fail_for=set())
    assert len(fake.sent) == 1


def test_blank_email_row_is_skipped(monkeypatch, tmp_path):
    rows = [dict(SAMPLE_ROW, contact_email=""),
            dict(SAMPLE_ROW, contact_email="real@x.com")]
    _, _, fake = _run_send(monkeypatch, tmp_path, rows, fail_for=set())
    assert fake.sent == ["real@x.com"]


def test_bookkeeping_failure_aborts_loudly(monkeypatch, tmp_path):
    """A send that succeeds but can't be recorded must abort the batch (not continue)."""
    rows = [dict(SAMPLE_ROW, contact_email="a@x.com"),
            dict(SAMPLE_ROW, contact_email="b@x.com")]
    monkeypatch.setattr(common, "archive",
                        lambda *a, **k: (_ for _ in ()).throw(IOError("disk full")))
    with pytest.raises(SystemExit):
        _run_send(monkeypatch, tmp_path, rows, fail_for=set())


def test_archive_reuses_existing_log_header_order(tmp_path):
    log = tmp_path / "sent.csv"
    common.write_db(str(log), ["company", "contact_email"],
                    [{"company": "Old", "contact_email": "o@x.com"}])
    common.archive(str(log), ["contact_email", "company"],
                   [{"contact_email": "n@x.com", "company": "New"}])
    assert log.read_text().splitlines()[0] == "company,contact_email"  # order preserved
    _, rows = common.read_rows(str(log))
    assert {r["contact_email"] for r in rows} == {"o@x.com", "n@x.com"}


def test_archive_fails_closed_on_missing_email_header(tmp_path):
    log = tmp_path / "sent.csv"
    common.write_db(str(log), ["company", "email"], [{"company": "X", "email": "x@x.com"}])
    with pytest.raises(ValueError):
        common.archive(str(log), ["contact_email", "company"],
                       [{"contact_email": "n@x.com", "company": "N"}])


def test_sent_emails_handles_bom_header(tmp_path):
    log = tmp_path / "sent.csv"
    log.write_text("﻿contact_email,company\nx@x.com,X\n", encoding="utf-8")
    assert "x@x.com" in common.sent_emails(str(log))
