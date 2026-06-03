# Internship Cold-Email Kit

A tiny, transparent toolkit for sending **personalized** internship (or job)
cold emails from Gmail, with a built-in "never email the same place twice" log.

It's a thin layer on top of [mailmerge](https://github.com/awdeorio/mailmerge)
(MIT). You bring a CSV of contacts and an email template; it merges them,
attaches your resume, previews everything, sends, and archives who you've
contacted.

> Be a good citizen: keep volume low, make every message genuinely specific,
> and only email people/addresses that are meant to receive applications.
> This is for personalized outreach, not bulk spam.

## How it works
- `mailmerge_template.txt` — your email, with `{{placeholders}}`.
- `mailmerge_database.csv` — one row per recipient; column names map to the
  `{{placeholders}}`.
- `mailmerge_server.conf` — Gmail SMTP settings (password entered at send time,
  never stored).
- `send.py` — **preferred sender (macOS).** Reads the Gmail app password from the
  macOS Keychain at send time, so there is no interactive password prompt and it
  can run anywhere (even non-interactively). Previews, sends, then moves sent rows
  into `sent_log.csv` and empties the database so nobody is contacted twice.
- `send_batch.sh` — legacy fallback: confirms recipients, sends via `mailmerge`'s
  hidden password prompt, then archives and empties the database the same way.

## Setup
```bash
# 1. Install mailmerge
pip install -r requirements.txt        # or: pipx install mailmerge

# 2. Copy the example files and fill them in with your details
cp mailmerge_template.txt.example mailmerge_template.txt
cp mailmerge_database.csv.example mailmerge_database.csv
cp mailmerge_server.conf.example  mailmerge_server.conf

# 3. Put your resume in this folder as resume.pdf
#    (or change the ATTACHMENT line in the template)

# 4. Gmail app password (not your normal password):
#    - Enable 2-Step Verification: https://myaccount.google.com/security
#    - Create an app password:      https://myaccount.google.com/apppasswords

# 5. (macOS, for send.py) Store the app password once in the Keychain,
#    encrypted, never written to disk. You'll be prompted to type it twice:
security add-generic-password -s mailmerge-gmail -a "$USER" -w
```

## Usage

### Preferred: `send.py` (macOS Keychain — no password prompt)
```bash
python3 send.py            # dry-run: preview every email, send nothing
python3 send.py --send     # preview, then ask "Send N emails? [y/N]", then send
python3 send.py --send --yes   # skip the confirmation prompt (hands-off)
```
The app password is read from the Keychain at send time, so this works in any
shell. On a successful send, sent rows are archived to `sent_log.csv`, the
database is reset to header-only, and any failed rows are kept for retry.

### Legacy: raw mailmerge / `send_batch.sh`
```bash
mailmerge --dry-run --no-limit     # preview every email, send nothing
mailmerge --no-dry-run --limit 1   # send only the first row (good as a self-test)
./send_batch.sh                    # send all, then archive to sent_log.csv
```
Run these from a real terminal so mailmerge's hidden password prompt works.

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

## Privacy
The included `.gitignore` keeps your private files (resume, real contacts,
`sent_log.csv`, your filled-in template/config) out of git. Only the `.example`
files are meant to be committed. Double-check before pushing.

## Credits
Built on [mailmerge](https://github.com/awdeorio/mailmerge) by awdeorio. MIT licensed.
