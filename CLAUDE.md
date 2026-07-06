# CLAUDE.md — discount-fetcher-cli

## What this app does
Python CLI that fetches ALDI SÜD prospectus data daily via GitHub Actions.
Stores in SQLite for timeline analysis. Sends email notifications via Resend.

## Key files
- `discount-fetcher-cli.py` — the CLI (fetch, auto-fetch, daemon, list, products, etc.)
- `error_handler.py` — typed exceptions, circuit breaker, retry logic, state file
- `.github/workflows/daily-fetch.yml` — production: daily cron + email
- `.github/workflows/dev-test.yml` — dev: push trigger + smoke tests

## Error handling
- Typed: NetworkError (exit 10), ParseError (20), StorageError (30), ConfigError (40), Unknown (50)
- Circuit breaker: 3 consecutive identical errors → suppress email (state file)
- Retry: network only (3x with 5s/15s/45s backoff)

## Commands
```bash
python discount-fetcher-cli.py auto-fetch --mode expiry    # daily check
python discount-fetcher-cli.py fetch <url>                  # manual fetch
python discount-fetcher-cli.py list                         # list publications
python discount-fetcher-cli.py products <pub-id>            # list products
```

## Current version: 2.0
## Tech: Python 3.12, requests, SQLite, Resend API, GitHub Actions
