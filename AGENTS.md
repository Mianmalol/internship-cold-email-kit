# AGENTS.md

## Cursor Cloud specific instructions

This repo is a small, **standard-library-only Python 3.11+ CLI tool** (the "Internship
Cold-Email Kit"). There is **no web server, UI, database, or long-running service** —
it runs as one-shot `outreach` / `outreach-send` commands whose state lives in local
CSV files. So there is nothing to "start"; you exercise it by running CLI commands.

### Environment
- Dev dependencies live in a virtualenv at `.venv/` (the startup update script creates
  it and runs `pip install -e ".[dev]"`). Activate it with `source .venv/bin/activate`,
  or call tools directly (`.venv/bin/pytest`, `.venv/bin/ruff`, `.venv/bin/outreach`).
- A system `python3 -m venv` requires the `python3-venv` apt package; it is already
  present in this environment, so the update script does not reinstall it.

### Lint / test / run
- Lint: `ruff check .`
- Tests: `pytest -q` (35 hermetic tests — no real Gmail account or network needed).
- The two console entry points are `outreach` (whole pipeline except sending) and
  `outreach-send` (the only command that emails). See `README.md` for the full command
  reference and `CONTRIBUTING.md` for the dev workflow.

### Running the pipeline (non-obvious caveats)
- The CLI looks for `config.toml` in: `--config PATH`, then `$OUTREACH_CONFIG`, then the
  **current working directory**. Real campaign files (`config.toml`,
  `mailmerge_template.txt`, the CSVs, `resume.pdf`) are gitignored, so they do **not**
  exist in a fresh checkout. To run the tool, create them in a scratch dir from the
  committed `*.example` files (e.g. `cp config.toml.example config.toml`).
- `outreach status`, `stage`, `preview`, `find` and `outreach-send` (without `--send`)
  are completely safe — they never send mail and do **not** need a Gmail password.
- Actually sending (`outreach-send --send`) and reply-sync (`outreach replies`,
  `outreach backfill-dates`) require a real **Gmail App Password** (via `keyring`,
  macOS Keychain, or `OUTREACH_GMAIL_APP_PASSWORD`) plus outbound SMTP/IMAP. These are
  external dependencies and are not needed for development, testing, or lint.
