# Contributing

Thanks for your interest! This is a small, dependency-light tool; contributions
that keep it simple and safe are very welcome.

## Dev setup
```bash
pip install -e ".[dev]"   # pytest + ruff
```

## Before opening a PR
```bash
ruff check .
pytest -q
```
Both must pass. CI runs them on Python 3.11 and 3.13 (Ubuntu + macOS).

## Ground rules
- **Never commit real data or secrets.** No real contacts, addresses, resumes, app
  passwords, `config.toml`, or `sent_log.csv`. Only `*.example` files belong in git.
  Tests must stay hermetic (use the fixtures in `tests/fixtures/`, not real files).
- **Keep the runtime standard-library only.** New third-party requirements should be
  optional extras in `pyproject.toml`, lazily imported.
- **Don't weaken the safety model.** No autonomous/unattended send path; a human gate
  stays before every send. Preserve the idempotency + transactional guarantees in
  `send.py`/`common.py` (and their tests).
- Run `ruff format`-style consistent code; match the surrounding style.

## Tests
- Pure logic and CSV/state transitions go in `tests/test_common.py` /
  `tests/test_cli.py`; send-path behavior in `tests/test_send.py`.
- The golden snapshot (`tests/golden/build_message.json`) is generated from the
  fixture template; if you intentionally change message construction, regenerate it
  and make sure it contains no personal data.
