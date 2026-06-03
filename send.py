#!/usr/bin/env python3
"""Keychain-backed sender for the internship outreach pipeline.

Replaces the interactive `mailmerge` password prompt: the Gmail app password is
read from the macOS Keychain at send time, so no TTY / hidden prompt is needed.
This runs anywhere (including non-interactively).

Reuses the existing files so behavior matches the old flow:
  - mailmerge_database.csv   targets (header: contact_email,contact_name,company,role,subject,note)
  - mailmerge_template.txt   email, with TO/SUBJECT/FROM/ATTACHMENT headers + {{placeholders}}
  - mailmerge_server.conf    SMTP host/port/username (INI)
On a successful send it archives the sent rows into sent_log.csv and resets the
database to header-only, so no company is ever emailed twice.

Usage:
  python3 send.py            # dry-run: render and preview every email, send nothing
  python3 send.py --send     # preview, then ask 'Send N emails? [y/N]', then send
  python3 send.py --send --yes   # skip the confirmation prompt (hands-off)

One-time Keychain setup (stores the Gmail APP password, encrypted, not on disk):
  security add-generic-password -s mailmerge-gmail -a "$USER" -w
"""
import argparse
import configparser
import csv
import getpass
import mimetypes
import os
import re
import smtplib
import subprocess
import sys
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "mailmerge_database.csv")
LOG = os.path.join(HERE, "sent_log.csv")
TEMPLATE = os.path.join(HERE, "mailmerge_template.txt")
SERVER_CONF = os.path.join(HERE, "mailmerge_server.conf")
KEYCHAIN_SERVICE = "mailmerge-gmail"


def get_password():
    """Fetch the Gmail app password from the macOS Keychain."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", getpass.getuser(), "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except subprocess.CalledProcessError:
        sys.exit(
            f"\nERROR: no Keychain entry '{KEYCHAIN_SERVICE}' for user "
            f"'{getpass.getuser()}'.\nRun this once to store your Gmail app password:\n"
            f'  security add-generic-password -s {KEYCHAIN_SERVICE} -a "$USER" -w\n'
        )


def load_template():
    """Split the template into its header dict and body, preserving raw text for {{subst}}."""
    raw = open(TEMPLATE, encoding="utf-8").read()
    head, _, body = raw.partition("\n\n")
    headers = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().upper()] = v.strip()
    return headers, body


def render(text, row):
    """Replace every {{column}} with the row value."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: row.get(m.group(1), m.group(0)), text)


def build_message(row, headers, body):
    msg = EmailMessage()
    msg["To"] = render(headers["TO"], row)
    msg["Subject"] = render(headers["SUBJECT"], row)
    msg["From"] = render(headers.get("FROM", ""), row)
    msg.set_content(render(body, row))
    attach = headers.get("ATTACHMENT")
    if attach:
        path = os.path.join(HERE, render(attach, row))
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))
    return msg


def read_rows():
    with open(DB, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def archive(fieldnames, sent_rows):
    """Append sent rows to sent_log.csv (creating it with a header if needed)."""
    new_log = not os.path.exists(LOG) or os.path.getsize(LOG) == 0
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if new_log:
            w.writeheader()
        w.writerows(sent_rows)


def write_db(fieldnames, rows):
    """Rewrite the database with exactly `rows` (header always written)."""
    with open(DB, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually send (default is dry-run preview)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    fieldnames, rows = read_rows()
    if not rows:
        print(f"No targets in {os.path.basename(DB)} (header only). Nothing to send.")
        return
    headers, body = load_template()

    print(f"=== {len(rows)} email(s) staged ===\n")
    for r in rows:
        print(f"  TO:      {render(headers['TO'], r)}")
        print(f"  SUBJECT: {render(headers['SUBJECT'], r)}\n")

    if not args.send:
        print("Dry-run only. Re-run with --send to deliver.")
        return

    if not args.yes:
        ok = input(f"Send {len(rows)} email(s)? [y/N] ").strip().lower()
        if ok != "y":
            print("Aborted. Nothing sent.")
            return

    conf = configparser.ConfigParser()
    conf.read(SERVER_CONF)
    s = conf["smtp_server"]
    host, port, username = s["host"], int(s["port"]), s["username"]
    password = get_password()

    sent, failed = [], []
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        for r in rows:
            try:
                smtp.send_message(build_message(r, headers, body))
                print(f"  sent -> {r['contact_email']} ({r['company']})")
                sent.append(r)
            except Exception as e:
                print(f"  FAILED -> {r['contact_email']} ({r['company']}): {e}")
                failed.append(r)

    if sent:
        archive(fieldnames, sent)
        write_db(fieldnames, failed)  # keep only failed rows for retry (empty if all sent)
        print(f"\nDone. {len(sent)} sent and archived to {os.path.basename(LOG)}.")
        if failed:
            print(f"{len(failed)} failed and kept in {os.path.basename(DB)} for retry.")
        else:
            print(f"{os.path.basename(DB)} is now empty (header only), ready for the next batch.")
        print("Tip: watch for Mailer-Daemon bounces over the next few hours.")
    else:
        print("\nNothing sent successfully; database unchanged.")


if __name__ == "__main__":
    main()
