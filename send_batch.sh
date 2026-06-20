#!/usr/bin/env bash
# DEPRECATED. Use `python3 send.py` instead (Keychain-backed, no password prompt,
# atomic/backed-up archiving, runs anywhere). This script remains only as a
# rollback path that drives raw `mailmerge` and will be removed once the new path
# has shipped a couple of clean batches.
#
# Sends the current batch in mailmerge_database.csv, then ARCHIVES the sent
# rows into sent_log.csv and empties the database. This way a company is never
# emailed twice: once sent, it lives in sent_log.csv and is gone from the
# active database.
#
# Run from a REAL terminal (Terminal.app / iTerm) so the password prompt works:
#   cd ~/internship-outreach && ./send_batch.sh
set -euo pipefail
cd "$(dirname "$0")"

DB="mailmerge_database.csv"
LOG="sent_log.csv"

rows=$(tail -n +2 "$DB" | grep -c . || true)
if [ "$rows" -eq 0 ]; then
  echo "No targets in $DB (header only). Nothing to send."
  exit 0
fi

echo "=== Recipients in this batch ($rows) ==="
tail -n +2 "$DB" | cut -d',' -f1
echo
read -r -p "Send to these $rows recipients? [y/N] " ok
[ "$ok" = "y" ] || [ "$ok" = "Y" ] || { echo "Aborted. Nothing sent."; exit 0; }

# Send. If mailmerge fails (auth error, etc.), set -e aborts BEFORE archiving,
# so a failed batch is never marked as sent.
mailmerge --no-dry-run --no-limit

# --- reached only on a successful send ---
[ -f "$LOG" ] || head -n 1 "$DB" > "$LOG"   # create log with header on first run
tail -n +2 "$DB" >> "$LOG"                   # append the rows we just sent
head -n 1 "$DB" > "$DB.tmp" && mv "$DB.tmp" "$DB"  # reset database to header only

echo
echo "Done. $rows row(s) sent and archived to $LOG."
echo "$DB is now empty (header only), ready for your next batch."
echo "Tip: watch your inbox for Mailer-Daemon bounces over the next few hours."
