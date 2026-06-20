# Security

## Credentials
- Use a **Gmail app password**, never your account password. Create one at
  https://myaccount.google.com/apppasswords (requires 2-Step Verification).
- The app password is read at send time from a credential backend, in this order:
  1. `OUTREACH_SECRET_BACKEND` (force `keyring`, `security`, or `env`)
  2. [`keyring`](https://pypi.org/project/keyring/) — cross-platform OS keystore
  3. macOS `security` (Keychain)
  4. `OUTREACH_GMAIL_APP_PASSWORD` environment variable
- **Prefer `keyring` or the macOS Keychain.** The env var is a CI/escape hatch: env
  vars can leak via shell history, process listings, and CI logs. Don't put the
  password in any committed file.

## Local data
- `config.toml`, `sent_log.csv`, `mailmerge_database.csv`, `outreach_queue.csv`,
  your resume, and `.backups/` contain personal data and are gitignored. They are
  meant to stay on your machine. Only the `*.example` files are committed.
- Before pushing, confirm no real contacts, addresses, or secrets are staged
  (`git status`, and consider a scanner like
  [gitleaks](https://github.com/gitleaks/gitleaks) as a pre-commit hook).

## Reporting a vulnerability
Open a private report via the repository's GitHub **Security** tab
("Report a vulnerability"). Please don't file public issues for security-sensitive
reports.
