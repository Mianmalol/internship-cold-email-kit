#!/usr/bin/env python3
"""Reusable Gmail thread reader + threaded replier for the outreach pipeline.

Reuses the SAME Gmail account + macOS Keychain app password as send.py /
read_replies.py. Two subcommands:

  read  "<query>"                 READ-ONLY IMAP search across All Mail; prints the
                                  latest matching messages (headers + plain text).

  send  --to ADDR --subject SUBJ --body-file PATH [--thread-from ADDR] [--send]
                                  Build a reply. Dry-run by default (prints preview,
                                  sends nothing). Add --send to deliver. If
                                  --thread-from is given, the newest message FROM
                                  that address is looked up over IMAP and its
                                  Message-ID is used for In-Reply-To/References so
                                  the reply threads correctly.

Strictly: read is read-only; send only sends when --send is passed.

Shared plumbing (Keychain, IMAP login, SMTP, header decoding, body extraction)
lives in common.py. The sender address is read from config.toml ([email].address).
"""
import argparse
import email
import ssl
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import common


def _email_cfg(config_path=None):
    em = common.load_config(common.find_config(config_path))["email"]
    if not em.get("address"):
        sys.exit("ERROR: set [email].address in config.toml.")
    return em


def username(config_path=None):
    return _email_cfg(config_path)["address"]


def cmd_read(args):
    em = _email_cfg(args.config)
    imap = common.imap_login(em["address"], common.get_password(em["keychain_service"]),
                             readonly=True, host=em["imap_host"])
    uids, qval = set(), f'"{args.query}"'
    for field in ("FROM", "TEXT"):
        typ, data = imap.uid("search", None, field, qval)
        if typ == "OK" and data and data[0]:
            uids.update(data[0].split())
    if not uids:
        print(f"NO MESSAGES matched '{args.query}'.")
        imap.logout()
        return
    ordered = sorted(uids, key=lambda u: int(u))
    print(f"Matched {len(ordered)} message(s) for '{args.query}'. "
          f"Showing latest {min(args.limit, len(ordered))}:\n")
    for uid in ordered[-args.limit:]:
        typ, md = imap.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        print("=" * 72)
        print("From:   ", common.decode_header_str(msg.get("From")))
        print("To:     ", common.decode_header_str(msg.get("To")))
        print("Date:   ", common.decode_header_str(msg.get("Date")))
        print("Subject:", common.decode_header_str(msg.get("Subject")))
        print("-" * 72)
        txt = common.body_text(msg).strip()
        print(txt[:2500] if txt else "(no plain-text body)")
        print()
    imap.logout()


def thread_headers(em, password, from_addr):
    imap = common.imap_login(em["address"], password, readonly=True, host=em["imap_host"])
    mid = refs = ""
    typ, data = imap.uid("search", None, "FROM", f'"{from_addr}"')
    if typ == "OK" and data and data[0]:
        newest = data[0].split()[-1]
        typ, md = imap.uid(
            "fetch", newest,
            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)])")
        if typ == "OK" and md and md[0]:
            m = email.message_from_bytes(md[0][1])
            # Collapse any header folding (CRLF + whitespace) to single spaces;
            # email policy rejects header values containing linefeeds/CRs.
            mid = " ".join((m.get("Message-ID") or "").split())
            refs = " ".join((m.get("References") or "").split())
    imap.logout()
    return mid, refs


def cmd_send(args):
    em = _email_cfg(args.config)
    user = em["address"]
    password = common.get_password(em["keychain_service"])
    with open(args.body_file, encoding="utf-8") as f:
        body = f.read()

    in_reply_to = refs = ""
    if args.thread_from:
        in_reply_to, refs = thread_headers(em, password, args.thread_from)

    msg = EmailMessage()
    msg["From"] = f"Marco Li <{user}>"
    msg["To"] = args.to
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="gmail.com")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (refs + " " + in_reply_to).strip()
    msg.set_content(body)

    print("=== REPLY PREVIEW ===")
    print("To:        ", msg["To"])
    print("Subject:   ", msg["Subject"])
    print("In-Reply-To:", in_reply_to or "(none - sends as new message)")
    print("-" * 60)
    print(body)

    if not args.send:
        print("DRY-RUN: nothing sent. Re-run with --send to deliver.")
        return

    ctx = ssl.create_default_context()
    with common.smtp_session(em["smtp_host"], int(em["smtp_port"]), user,
                             password, ssl_context=ctx) as s:
        s.send_message(msg)
    print("\nSENT to", args.to)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="config path (default ./config.toml or $OUTREACH_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("read", help="read-only IMAP search of a thread")
    r.add_argument("query")
    r.add_argument("--limit", type=int, default=6)
    r.set_defaults(func=cmd_read)

    s = sub.add_parser("send", help="send (or dry-run) a threaded reply")
    s.add_argument("--to", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body-file", required=True)
    s.add_argument("--thread-from", help="address whose msg to thread under")
    s.add_argument("--send", action="store_true", help="actually deliver")
    s.set_defaults(func=cmd_send)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
