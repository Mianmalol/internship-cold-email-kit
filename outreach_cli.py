#!/usr/bin/env python3
"""One entrypoint for the internship-outreach pipeline.

Composable, mostly non-interactive commands the agent (or you) can drive end to
end. CRUCIALLY this CLI NEVER sends mail. The only send path is
`python3 send.py --send`, gated by your explicit go-ahead against a fresh preview.
Autonomy ends at "staged + previewed".

Commands:
  outreach_cli.py status          funnel snapshot (queue / staged / sent)
  outreach_cli.py setup           scaffold config.toml + check resume + Keychain
  outreach_cli.py find            print the internship-scout brief (area + exclude list)
  outreach_cli.py stage [-n N]    move N rows queue -> database (backed-up, deduped)
  outreach_cli.py preview         render the staged batch (delegates to send.py dry-run)
  outreach_cli.py replies         run the IMAP reply fallback (delegates to read_replies.py)
  outreach_cli.py followup        list silent contacts (cross-check via Gmail connector)
"""
import argparse
import os
import subprocess
import sys

import common

PY = sys.executable


def _safe_rows(path):
    if not os.path.exists(path):
        return None, []
    return common.read_rows(path)


def _sent_companies(sent_log):
    _, rows = _safe_rows(sent_log)
    return {(r.get("company") or "").strip().lower() for r in rows if r.get("company")}


def _paths(cfg):
    f = cfg["files"]  # already absolute (resolved against the workspace)
    return f["queue"], f["database"], f["sent_log"]


def cmd_status(args, cfg):
    queue, db, sent_log = _paths(cfg)
    _, q_rows = _safe_rows(queue)
    _, db_rows = _safe_rows(db)
    sent = _sent_companies(sent_log)
    print("=== Outreach funnel ===")
    print(f"  Sent (contacted):   {len(sent)}")
    print(f"  Staged (in DB):     {len(db_rows)}")
    print(f"  Queue (backlog):    {len(q_rows)}")
    if not q_rows:
        print("\n  Backlog is dry -> run `find` and the internship-scout subagent.")
    elif db_rows:
        print("\n  Batch staged -> `preview`, then `python3 send.py --send` once you approve.")


def cmd_setup(args, cfg):
    cfg_path = cfg["config_path"]
    if not os.path.exists(cfg_path):
        # Scaffold the config at the requested/discovered path (never in the package).
        os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(common.CONFIG_TEMPLATE)
        print(f"Created {cfg_path}.")
        print("Edit it: address, resume path, [outreach].area; then re-run setup.")
        cfg = common.load_config(cfg_path)  # reload so checks below see the new file
    else:
        print(f"config.toml: OK ({cfg_path})")

    resume = cfg["files"]["resume"]
    print(f"Resume: {'OK ' + resume if os.path.exists(resume) else 'MISSING -> ' + resume}")

    backend, ok, hint = common.secret_status(cfg["email"]["keychain_service"])
    print(f"Password: {'OK via ' + backend if ok else 'MISSING -> ' + hint}")

    addr = cfg["email"].get("address")
    print(f"Sender:  {addr or 'UNSET -> set [email].address in config.toml'}")
    area = cfg["outreach"].get("area")
    print(f"Area:    {area or 'UNSET -> set [outreach].area in config.toml'}")


def cmd_find(args, cfg):
    queue, _, sent_log = _paths(cfg)
    area = cfg["outreach"].get("area") or "(unset - fill [outreach].area in config.toml)"
    sent = sorted(_sent_companies(sent_log))
    print("=== internship-scout brief ===")
    print("AREA:", area)
    print(f"\nEXCLUDE these {len(sent)} already-contacted companies:")
    print("  " + (", ".join(sent) if sent else "(none yet)"))
    print("\nHand this brief to the internship-scout subagent. It returns email-VERIFIED")
    print(f"leads; append them to {os.path.basename(queue)}, then run `stage`.")


def cmd_stage(args, cfg):
    queue, db, sent_log = _paths(cfg)
    n = args.n if args.n is not None else cfg["pacing"]["batch_size"]
    if n <= 0:
        print(f"Nothing to do: stage count is {n}.")
        return

    q_fields, q_rows = _safe_rows(queue)
    if not q_rows:
        print("Queue is empty (header only). Run `find` + internship-scout to refill.")
        return
    db_fields, db_rows = _safe_rows(db)
    fields = db_fields or q_fields

    # Dedup by normalized email (the identity send.py enforces) AND by company.
    blk_emails = common.sent_emails(sent_log)
    blk_emails |= {common.normalize_email(r.get("contact_email")) for r in db_rows}
    blk_companies = _sent_companies(sent_log)
    blk_companies |= {(r.get("company") or "").strip().lower() for r in db_rows}

    def handled(r):
        return (common.normalize_email(r.get("contact_email")) in blk_emails
                or (r.get("company") or "").strip().lower() in blk_companies)

    to_move, leftover, dropped = [], [], []
    for r in q_rows:
        if handled(r):
            dropped.append(r)  # already sent or staged -> remove from queue
        elif len(to_move) < n:
            to_move.append(r)
            blk_emails.add(common.normalize_email(r.get("contact_email")))
            blk_companies.add((r.get("company") or "").strip().lower())
        else:
            leftover.append(r)  # pending beyond this batch -> keep in queue

    if not to_move and not dropped:
        print("Nothing new to stage (queue holds only already-staged/sent rows).")
        return

    common.backup(queue)
    common.backup(db)
    # DB grows first (additive), queue is rewritten without moved-or-handled rows.
    # If interrupted between the two writes, the email/company dedup above plus the
    # idempotency check in send.py mean nothing double-sends; backups aid recovery.
    common.write_db(db, fields, db_rows + to_move)
    common.write_db(queue, q_fields, leftover)

    print(f"Staged {len(to_move)} -> {os.path.basename(db)}; "
          f"dropped {len(dropped)} already-handled; {len(leftover)} left in queue.")
    for r in to_move:
        print(f"  + {r.get('company')} <{r.get('contact_email')}>")
    print("\nNext: `outreach_cli.py preview`, then `python3 send.py --send` once you approve.")


def cmd_preview(args, cfg):
    # `-m` works whether installed as a wheel or run from a source checkout.
    rc = subprocess.run([PY, "-m", "send", "--config", cfg["config_path"]]).returncode
    if rc:
        raise SystemExit(rc)


def cmd_replies(args, cfg):
    rc = subprocess.run([PY, "-m", "read_replies",
                         "--config", cfg["config_path"]]).returncode
    if rc:
        raise SystemExit(rc)


def cmd_migrate(args, cfg):
    _, _, sent_log = _paths(cfg)
    if not os.path.exists(sent_log):
        print(f"No sent log at {sent_log}; nothing to migrate.")
        return
    common.backup(sent_log)
    changed = common.migrate_sent_log(sent_log)
    print("Migrated sent_log to current schema (added missing columns)." if changed
          else "sent_log already has the current schema; no change.")


def cmd_followup(args, cfg):
    import datetime
    _, _, sent_log = _paths(cfg)
    _, rows = _safe_rows(sent_log)
    days = cfg["pacing"]["followup_after_days"]
    now = datetime.datetime.now(datetime.timezone.utc)

    seen, due, unknown, replied = set(), [], [], 0
    for r in rows:
        addr = (r.get("contact_email") or "").strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        if (r.get("last_reply") or "").strip():
            replied += 1          # they responded -> not a follow-up target
            continue
        company, sent_at = (r.get("company") or ""), (r.get("sent_at") or "").strip()
        try:
            dt = datetime.datetime.strptime(sent_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
            age = (now - dt).days
            if age >= days:
                due.append((company, addr, age))
        except ValueError:
            unknown.append((company, addr))

    print(f"=== Follow-up candidates (silent >= {days} days, no reply) ===")
    for company, addr, age in sorted(due, key=lambda t: -t[2]):
        print(f"  {company} <{addr}>  ({age}d ago)")
    if not due:
        print("  (none)")
    if replied:
        print(f"\n{replied} contact(s) excluded (already replied, per last_reply).")
    if unknown:
        print(f"{len(unknown)} contact(s) have no sent_at -> date unknown; "
              f"run `outreach backfill-dates --apply`.")
    print("\nReply detection is best-effort: `backfill-dates` catches same-address "
          "replies AND different-address replies that thread (In-Reply-To/References). "
          "Refresh it with `outreach backfill-dates`; the Gmail connector stays "
          "authoritative for edge cases.")


def cmd_backfill_dates(args, cfg):
    """Sync sent_at + last_reply from Gmail (Sent folder + replies). Universal.

    Pass 1 fills blank sent_at from Gmail's Sent folder (real send dates). Pass 2
    cross-checks who replied (latest message FROM each contact) and records
    last_reply, so `followup` can exclude people who already responded. Dry-run by
    default; --apply writes (backs up sent_log.csv first). Needs no external CRM.
    """
    import read_replies  # local import: only this command needs IMAP

    _, _, sent_log = _paths(cfg)
    if not os.path.exists(sent_log):
        print(f"No sent log at {sent_log}; nothing to backfill.")
        return
    fields, rows = common.read_rows(sent_log)
    em = cfg["email"]
    if not em.get("address"):
        raise SystemExit("ERROR: set [email].address in config.toml first.")

    our_addr = em["address"]
    sent_found, reply_found, thread_only = {}, {}, 0
    meta = {}  # addr -> [message_ids] of mail we sent them (for thread matching)
    imap = common.imap_login(our_addr, common.get_password(em["keychain_service"]),
                             readonly=True, host=em["imap_host"], mailbox=read_replies.SENT_MAIL)
    try:
        print(f"Pass 1: send dates + message-ids for {len(rows)} contact(s) (Gmail Sent)...")
        for r in rows:
            addr = common.normalize_email(r.get("contact_email"))
            if not addr:
                continue
            date, msgids = read_replies.sent_meta_for(imap, addr)
            meta[addr] = msgids
            if date and not (r.get("sent_at") or "").strip():
                sent_found[addr] = date
        typ, _ = imap.select(common.ALL_MAIL, readonly=True)
        if typ != "OK":
            imap.select("INBOX", readonly=True)
        print(f"Pass 2: replies for {len(rows)} contact(s) (FROM-match + thread-match)...")
        for r in rows:
            addr = common.normalize_email(r.get("contact_email"))
            if not addr:
                continue
            by_from = read_replies.last_reply_for(imap, addr)
            by_thread = read_replies.thread_reply_for(imap, meta.get(addr, []), our_addr)
            latest = max([d for d in (by_from, by_thread) if d], default=None)
            if latest:
                reply_found[addr] = latest
                if by_thread and not by_from:
                    thread_only += 1  # caught only because of thread matching
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    print(f"Found send dates for {len(sent_found)}; replies from {len(reply_found)} "
          f"contact(s) ({thread_only} via thread-match / different address).")
    if not args.apply:
        print("\nDry-run. Re-run with --apply to write into sent_log.csv.")
        return

    out_fields = list(fields)
    for f in common.SENT_LOG_FIELDS:
        if f not in out_fields:
            out_fields.append(f)
    new_rows = []
    for r in rows:
        addr = common.normalize_email(r.get("contact_email"))
        nr = {**{k: "" for k in out_fields}, **r}
        if not (nr.get("sent_at") or "").strip() and addr in sent_found:
            nr["sent_at"] = sent_found[addr]
        if addr in reply_found:
            # Never move last_reply backwards: keep the newer of existing vs found
            # (both are fixed-width UTC, so max() is chronological; "" loses to any).
            nr["last_reply"] = max(nr.get("last_reply") or "", reply_found[addr])
        new_rows.append(nr)
    common.backup(sent_log)
    common.write_db(sent_log, out_fields, new_rows)
    print(f"\nApplied -> {os.path.basename(sent_log)} "
          f"(sent_at for {len(sent_found)}, last_reply for {len(reply_found)}; "
          f"backup in .backups/).")


COMMANDS = {
    "status": cmd_status, "setup": cmd_setup, "find": cmd_find,
    "stage": cmd_stage, "preview": cmd_preview, "replies": cmd_replies,
    "followup": cmd_followup, "migrate": cmd_migrate,
    "backfill-dates": cmd_backfill_dates,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config path (default ./config.toml or $OUTREACH_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        if name == "stage":
            p.add_argument("-n", type=int, default=None,
                           help="how many to stage (default: [pacing].batch_size)")
        if name == "backfill-dates":
            p.add_argument("--apply", action="store_true",
                           help="write the dates (default: dry-run preview)")
    args = ap.parse_args()
    COMMANDS[args.cmd](args, common.load_config(common.find_config(args.config)))


if __name__ == "__main__":
    main()
