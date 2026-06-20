# Internship Cold-Email Kit

A tiny, transparent toolkit for sending **personalized** internship (or job)
cold emails from Gmail, with a built-in "never email the same place twice" log.

You bring a CSV of contacts and an email template; it merges them, attaches your
resume, previews everything, sends (transactionally), and archives who you've
contacted. Pure standard-library Python — **requires Python 3.11+** (for the
built-in `tomllib`). The deprecated `send_batch.sh` path additionally needs
[mailmerge](https://github.com/awdeorio/mailmerge) (MIT).

<p align="center">
  <img src="assets/workflow.png" alt="How the kit works: CSV and template, personalize, attach resume, preview, send via Gmail, never email twice" width="640">
</p>

> Be a good citizen: keep volume low, make every message genuinely specific,
> and only email people/addresses that are meant to receive applications.
> This is for personalized outreach, not bulk spam.

## How it works
- `config.toml` — **the one file you fill in.** Machine settings only: your Gmail
  address, SMTP/IMAP hosts, the Keychain service name, file paths, pacing, and the
  outreach *area* you're targeting. Copy it from `config.toml.example`.
- `mailmerge_template.txt` — your email wording, with `{{placeholders}}`.
- `mailmerge_database.csv` — one row per recipient; column names map to the
  `{{placeholders}}`. This is the staging area for the next batch.
- `common.py` — shared library (Keychain, SMTP, IMAP, config, CSV I/O). Not run
  directly; the scripts below import it.
- `outreach_cli.py` — **one entrypoint** for everything *except* sending:
  `status`, `setup`, `find`, `stage`, `preview`, `replies`, `followup`. It never
  sends mail — that stays an explicit, separate step.
- `send.py` — **the sender.** Reads the Gmail app password from a credential
  backend at send time (no prompt), previews, sends transactionally (each success
  is archived to `sent_log.csv` immediately and the database shrinks as it goes,
  with timestamped backups), so nobody is contacted twice and a crash never
  double-sends.
- `read_replies.py` / `thread_reply.py` — offline IMAP reply checker and threaded
  replier (the headless fallback).
- `send_batch.sh` — **deprecated** raw-`mailmerge` rollback path; use `send.py`.

## Install
```bash
pip install .                 # exposes the `outreach` and `outreach-send` commands
pip install ".[keyring]"      # optional cross-platform credential storage
pip install -e ".[dev]"       # for development (editable + pytest/ruff)
```
Runs on any OS with **Python 3.11+** (no required third-party deps).

## Setup
```bash
# 1. Copy the examples and fill them in with your details
cp config.toml.example config.toml          # your address, paths, area, pacing
cp mailmerge_template.txt.example mailmerge_template.txt

# 2. Put your resume next to config.toml (match the [files].resume path)

# 3. Gmail app password (NOT your normal password):
#    - Enable 2-Step Verification: https://myaccount.google.com/security
#    - Create an app password:      https://myaccount.google.com/apppasswords

# 4. Store the app password once. Pick ONE backend (checked in this order):
keyring set mailmerge-gmail "$USER"                              # any OS (pip install keyring)
security add-generic-password -s mailmerge-gmail -a "$USER" -w   # macOS Keychain
export OUTREACH_GMAIL_APP_PASSWORD=...                           # CI/escape hatch only

# 5. Check everything is wired up (creates config.toml here if missing):
outreach setup     # checks config, resume, password backend, sender, area
```
Config is discovered as `--config PATH` > `$OUTREACH_CONFIG` > `./config.toml`, and
all file paths in `[files]` resolve relative to the config file's directory.

## Usage

### Drive the pipeline: `outreach` (everything except sending)
```bash
outreach status         # funnel: queue / staged / sent
outreach find           # print the scout brief (your area + exclude list)
outreach stage -n 14    # move 14 backlog rows -> database (deduped, backed up)
outreach preview        # render the staged batch
outreach replies        # offline IMAP reply check (approximate fallback)
outreach followup       # contacts silent past the cadence (by sent_at)
outreach migrate        # add new columns (e.g. sent_at) to an older sent_log
outreach backfill-dates # sync sent_at + last_reply from Gmail (--apply)
```
`backfill-dates` makes `followup` accurate from Gmail, no external CRM needed:
pass 1 fills `sent_at` from your Sent folder (real send dates), pass 2 records
`last_reply` by checking who emailed you back, so `followup` automatically
**excludes people who already replied**. Dry-run by default; `--apply` writes (it
backs up `sent_log.csv` first). Reply detection is approximate (it matches a reply
from the same address you emailed; a reply from a different address is missed).
By design this CLI **never sends mail**. It takes you right up to a staged,
previewed batch and then stops.

### Send: `outreach-send` (the only send path)
```bash
outreach-send                # dry-run: preview every email, send nothing
outreach-send --send         # preview, then ask "Send N emails? [y/N]", then send
outreach-send --send --yes   # skip the confirmation prompt (hands-off)
```
(Equivalent to `python3 send.py ...` from a source checkout.)
The app password is read from your credential backend at send time, so this works
in any shell. Sending is transactional: each success is archived to `sent_log.csv`
immediately and the database shrinks row by row (atomic writes, timestamped
backups in `.backups/`). Before sending, any staged address already in
`sent_log.csv` is skipped, so even a crash mid-batch can't double-send on retry.
Failed rows are kept in the database for retry.

### Deprecated: raw mailmerge / `send_batch.sh`
Kept only as a rollback. Prefer `send.py`. Needs `mailmerge` installed and the old
`mailmerge_server.conf`, run from a real terminal for its hidden password prompt.

## Sending real email — please read
This sends real email from **your own** Gmail account, and your account's
reputation is on the line. Use it responsibly:
- **Personalized outreach only, not bulk spam.** Every `note` should be genuinely
  specific to that company.
- **Keep volume low.** Default pacing is ~14/day (`[pacing].batch_size`); cold-email
  blasts trip spam filters and risk your account. Gmail also enforces daily send
  limits.
- **Only email addresses meant to receive applications** (careers@, hiring@, a
  person who invited contact). Honor any request to stop, and don't re-contact
  someone who has declined.
- You are responsible for complying with anti-spam law (e.g. CAN-SPAM/GDPR) in your
  jurisdiction.

## CSV columns
The header row defines the `{{variables}}`. The defaults are:

| column        | used for                                    |
|---------------|---------------------------------------------|
| contact_email | the `TO:` address (required)                |
| contact_name  | greeting                                    |
| company       | `{{company}}`                               |
| role          | `{{role}}`                                  |
| subject       | the subject line                            |
| note          | one personalized sentence about that company |

Add or rename columns freely; just keep the template placeholders in sync.

## Privacy & data
Your CSVs hold real people's contact details and live **only on your machine**. The
included `.gitignore` keeps private files out of git: resume, real contacts
(`mailmerge_database.csv`, `outreach_queue.csv`), `sent_log.csv`, your filled-in
`mailmerge_template.txt`, `config.toml`, and the `.backups/` directory. Only the
`.example` files are meant to be committed. Double-check before pushing, and never
commit your app password (it belongs in a credential backend, not in any file). See
[SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## License
MIT — see [LICENSE](LICENSE). The deprecated `send_batch.sh` path builds on
[mailmerge](https://github.com/awdeorio/mailmerge) by awdeorio (also MIT).
