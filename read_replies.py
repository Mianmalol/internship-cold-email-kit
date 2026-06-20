#!/usr/bin/env python3
"""Read-only IMAP reader: who from sent_log.csv has replied (and who bounced).

Outbound sending is handled by send.py over SMTP. This script is the inbound
counterpart: it logs into the SAME Gmail account over IMAP using the SAME app
password already stored in the macOS Keychain (Gmail app passwords work for both
SMTP and IMAP), and reports which contacted companies have written back. Use it to
EXCLUDE responders before sending a follow-up batch.

It is strictly READ-ONLY: it never deletes, moves, marks-as-read, or sends
anything. It only runs IMAP SEARCH/FETCH and prints a report. Nothing leaves the
machine.

Shared plumbing (Keychain, IMAP login, header decoding) lives in common.py.

Reuses the existing pipeline files/credential so behavior matches send.py:
  - sent_log.csv            the contact list to check (contact_email, company, ...)
  - mailmerge_server.conf   SMTP/IMAP username (INI [smtp_server] -> username)
  - Keychain service 'mailmerge-gmail'   the Gmail app password

Usage:
  python3 read_replies.py            # human report: replied / bounced / silent
  python3 read_replies.py --emails   # print ONLY the email addresses that replied
                                     # (one per line, for piping into a filter)

Requires IMAP enabled in Gmail (Settings -> Forwarding and POP/IMAP -> Enable IMAP;
on by default for most accounts).
"""
import argparse
import csv
import datetime
import email
import os
import sys
from email.utils import getaddresses, parsedate_to_datetime

import common

SENT_MAIL = '"[Gmail]/Sent Mail"'  # Gmail's Sent folder; holds the real send dates

APPROX_NOTE = (
    "NOTE: this is the FAST APPROXIMATE fallback. It only finds replies sent FROM\n"
    "the exact address you emailed, so it MISSES replies from a different address\n"
    "(a person replying from a personal account) and cannot reliably tell an\n"
    "auto-acknowledgement from a real human reply. For authoritative reply\n"
    "detection use the Gmail connector; cross-check anything important by hand."
)


def load_contacts(log_path):
    """Return [(email_lower, company)] from sent_log.csv, de-duplicated by address."""
    if not os.path.exists(log_path):
        sys.exit(f"ERROR: {log_path} not found. Nothing to check.")
    seen, contacts = set(), []
    with open(log_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            addr = (row.get("contact_email") or "").strip().lower()
            if addr and addr not in seen:
                seen.add(addr)
                contacts.append((addr, (row.get("company") or "").strip()))
    return contacts


def headers_of(imap, uid, fields):
    """Fetch several header fields for a message UID in ONE read-only round-trip."""
    field_str = " ".join(fields)
    typ, data = imap.uid("fetch", uid, f"(BODY.PEEK[HEADER.FIELDS ({field_str})])")
    if typ != "OK" or not data or not data[0]:
        return {}
    raw = data[0][1].decode("utf-8", "replace")
    msg = email.message_from_string(raw)
    return {f: common.decode_header_str(msg.get(f, "")) for f in fields}


def _parse_date(raw):
    """Parse an RFC-2822 Date header into a UTC 'YYYY-MM-DDTHH:MM:SSZ' string."""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_of(imap, uid):
    return _parse_date(headers_of(imap, uid, ["Date"]).get("Date", ""))


def sent_meta_for(imap, addr):
    """For mail we sent TO `addr` (select Gmail Sent Mail first): return
    (earliest_send_date, [message_ids]).

    earliest_send_date backfills sent_at; the message_ids let us find replies by
    thread (In-Reply-To/References) regardless of which address replied.
    """
    typ, data = imap.uid("search", None, "TO", f'"{addr}"')
    if typ != "OK" or not data or not data[0]:
        return None, []
    uids = data[0].split()
    dates, msgids = [], []
    for u in uids:
        h = headers_of(imap, u, ["Date", "Message-ID"])  # both in one fetch
        d = _parse_date(h.get("Date", ""))
        if d:
            dates.append(d)
        mid = " ".join((h.get("Message-ID") or "").split())  # collapse folding
        if mid:
            msgids.append(mid)
    return (min(dates) if dates else None), msgids  # earliest BY DATE, not UID order


def last_reply_for(imap, addr):
    """LATEST date `addr` emailed us, FROM-matched (select All Mail first).

    Catches a reply only if it was sent from the same address we emailed. Returns the
    max parsed Date across all matches (not whatever UID happens to be last).
    """
    typ, data = imap.uid("search", None, "FROM", f'"{addr}"')
    if typ != "OK" or not data or not data[0]:
        return None
    dates = [d for u in data[0].split() if (d := _date_of(imap, u))]
    return max(dates) if dates else None


def thread_reply_for(imap, msgids, our_addr):
    """LATEST reply date in any thread that references one of `msgids`, from someone
    OTHER than us (select All Mail first).

    This is the address-independent path: it matches on In-Reply-To/References, so a
    reply sent from a different address than the one we emailed is still caught. Our
    own follow-ups in the thread are excluded by sender.
    """
    our = (our_addr or "").lower()
    latest, seen = None, set()
    for mid in msgids:
        uids = set()
        for field in ("REFERENCES", "In-Reply-To"):
            typ, data = imap.uid("search", None, "HEADER", field, f'"{mid}"')
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        for uid in uids:
            if uid in seen:
                continue
            seen.add(uid)
            h = headers_of(imap, uid, ["From", "Date"])
            from_addrs = [a.lower() for _, a in getaddresses([h.get("From", "")]) if a]
            if our and our in from_addrs:
                continue  # our own message in the thread, not a reply
            d = _parse_date(h.get("Date", ""))
            if d and (latest is None or d > latest):
                latest = d
    return latest


def find_replies(imap, contacts):
    """For each contact, IMAP-search All Mail for mail FROM them. Returns dict
    email -> (company, date, subject, count) for those who replied.

    One SEARCH per contact (inherent to FROM-matching) plus ONE combined header
    fetch for the newest hit (Subject + Date together, not two round-trips)."""
    replied = {}
    for addr, company in contacts:
        typ, data = imap.uid("search", None, "FROM", f'"{addr}"')
        if typ != "OK" or not data or not data[0]:
            continue
        uids = data[0].split()
        if not uids:
            continue
        h = headers_of(imap, uids[-1], ["Subject", "Date"])
        replied[addr] = (company, h.get("Date", ""), h.get("Subject", ""), len(uids))
    return replied


def find_bounces(imap, contacts):
    """Find Mailer-Daemon bounce notices and match our recipient addresses in them.
    Returns a set of bounced addresses (best-effort)."""
    bounced = set()
    by_addr = {a for a, _ in contacts}
    typ, data = imap.uid("search", None, "FROM", '"mailer-daemon"')
    if typ != "OK" or not data or not data[0]:
        return bounced
    for uid in data[0].split():
        # Pull the whole bounce so we can scan for which address failed.
        typ, msgdata = imap.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not msgdata or not msgdata[0]:
            continue
        text = msgdata[0][1].decode("utf-8", "replace").lower()
        for addr in by_addr:
            if addr in text:
                bounced.add(addr)
    return bounced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="config path (default ./config.toml or $OUTREACH_CONFIG)")
    ap.add_argument("--emails", action="store_true",
                    help="print only the addresses that replied, one per line")
    args = ap.parse_args()

    cfg = common.load_config(common.find_config(args.config))
    contacts = load_contacts(cfg["files"]["sent_log"])
    em = cfg["email"]
    username = em.get("address")
    if not username:
        sys.exit("ERROR: no email address in config.toml ([email] address) or "
                 "mailmerge_server.conf.")
    password = common.get_password(em["keychain_service"])

    imap = common.imap_login(username, password, readonly=True, host=em["imap_host"])
    try:
        replied = find_replies(imap, contacts)
        bounced = find_bounces(imap, contacts) if not args.emails else set()
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    if args.emails:
        for addr in replied:
            print(addr)
        return

    total = len(contacts)
    print(f"Checked {total} contacted companies against {username} (All Mail).")
    print(APPROX_NOTE + "\n")

    if replied:
        print(f"=== REPLIED ({len(replied)}) -- EXCLUDE these from follow-ups ===")
        for addr, (company, date, subject, n) in sorted(
                replied.items(), key=lambda kv: kv[1][0].lower()):
            more = f"  (+{n - 1} more)" if n > 1 else ""
            print(f"  {company} <{addr}>{more}")
            print(f"      last: {date}")
            print(f"      subj: {subject}\n")
    else:
        print("=== REPLIED (0) ===\n  No replies found yet.\n")

    if bounced:
        print(f"=== BOUNCED / undeliverable ({len(bounced)}) -- bad address, drop ===")
        for addr in sorted(bounced):
            company = next((c for a, c in contacts if a == addr), "")
            print(f"  {company} <{addr}>")
        print()

    silent = total - len(replied) - len(bounced)
    print(f"=== SILENT (~{silent}) -- candidates for a follow-up ===")
    print("Note: 'silent' = no reply and no bounce detected. Bounce matching is")
    print("best-effort (scans Mailer-Daemon notices). Verify a sample by hand.")


if __name__ == "__main__":
    main()
