"""Unit tests for the shared helpers extracted into common.py (Slice A)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common


def test_render_substitutes_known_keys():
    row = {"company": "Acme", "note": "Hi"}
    assert common.render("build {{company}}. {{note}}.", row) == "build Acme. Hi."


def test_render_leaves_unknown_placeholder_intact():
    # An unmatched {{key}} must pass through unchanged, never blow up.
    assert common.render("hi {{missing}}", {"company": "X"}) == "hi {{missing}}"


def test_decode_header_str_empty_is_empty():
    assert common.decode_header_str("") == ""
    assert common.decode_header_str(None) == ""


def test_decode_header_str_plain_passthrough():
    assert common.decode_header_str("Plain Subject") == "Plain Subject"


def test_load_template_splits_headers_and_body(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("TO: {{contact_email}}\nSUBJECT: hi\n\nBody {{company}} here\n")
    headers, body = common.load_template(str(p))
    assert headers["TO"] == "{{contact_email}}"
    assert headers["SUBJECT"] == "hi"
    assert body == "Body {{company}} here\n"


def test_csv_roundtrip_preserves_commas_in_notes(tmp_path):
    db = tmp_path / "db.csv"
    fields = ["contact_email", "company", "note"]
    rows = [{"contact_email": "a@b.com", "company": "Co",
             "note": "I saw your post, and I'd bring rigor"}]
    common.write_db(str(db), fields, rows)
    got_fields, got_rows = common.read_rows(str(db))
    assert got_fields == fields
    assert got_rows == rows  # comma inside the quoted note survives the roundtrip


def test_archive_appends_with_header_once(tmp_path):
    log = tmp_path / "sent.csv"
    fields = ["contact_email", "company"]
    common.archive(str(log), fields, [{"contact_email": "a@b.com", "company": "A"}])
    common.archive(str(log), fields, [{"contact_email": "c@d.com", "company": "C"}])
    text = log.read_text()
    assert text.count("contact_email,company") == 1  # header written only once
    assert "a@b.com" in text and "c@d.com" in text


def test_load_config_resolves_paths_against_config_dir(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[email]\naddress="me@x.com"\n[files]\ndatabase="db.csv"\n')
    cfg = common.load_config(str(cfg_path))
    assert cfg["workspace"] == str(tmp_path)
    assert cfg["files"]["database"] == str(tmp_path / "db.csv")  # relative -> workspace
    assert cfg["files"]["sent_log"] == str(tmp_path / "sent_log.csv")  # default, resolved


def test_load_config_defaults_when_missing(tmp_path):
    cfg = common.load_config(str(tmp_path / "nope.toml"))
    assert cfg["files"]["database"].endswith("mailmerge_database.csv")
    assert cfg["pacing"]["batch_size"] == 14
    assert cfg["email"]["smtp_host"] == "smtp.gmail.com"


def test_find_config_prefers_explicit_then_env(tmp_path, monkeypatch):
    explicit = tmp_path / "a.toml"
    monkeypatch.setenv("OUTREACH_CONFIG", str(tmp_path / "b.toml"))
    assert common.find_config(str(explicit)) == str(explicit)          # --config wins
    assert common.find_config() == str(tmp_path / "b.toml")            # then env


def test_backup_creates_copy_and_skips_missing(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text("hello")
    b = common.backup(str(p))
    assert b and os.path.exists(b)
    assert os.path.dirname(b) == str(tmp_path / ".backups")  # next to the file
    assert common.backup(str(tmp_path / "does_not_exist")) is None


def test_get_password_env_backend(monkeypatch):
    monkeypatch.setenv("OUTREACH_SECRET_BACKEND", "env")
    monkeypatch.setenv("OUTREACH_GMAIL_APP_PASSWORD", "s3cret")
    assert common.get_password("svc") == "s3cret"  # no macOS `security` call
    backend, ok, _ = common.secret_status("svc")
    assert backend == "env" and ok


def test_invalid_secret_backend_exits(monkeypatch):
    monkeypatch.setenv("OUTREACH_SECRET_BACKEND", "evn")  # typo
    import pytest
    with pytest.raises(SystemExit):
        common.get_password("svc")
