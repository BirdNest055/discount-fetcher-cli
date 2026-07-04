"""
error_handler — Comprehensive error handling for aldi-cli.

GOALS
-----
1. Pinpoint WHERE errors happen (stage + category + traceback)
2. Prevent infinite loops (bounded retries, circuit breaker, daemon stop)
3. Prevent email spam (state file tracks consecutive errors, dedup emails)
4. Stable exit codes for shell/CI integration

ERROR TYPES
-----------
- NetworkError     HTTP failures, timeouts, DNS, connection refused
- ParseError       JSON parse, missing config blob, malformed HTML
- StorageError     SQLite write failures, disk full, locked DB
- ConfigurationError  bad CLI args, missing env vars
- UnknownError     catch-all for unexpected exceptions

EXIT CODES
----------
  0  success (fetched, skipped, not-due, dry-run)
 10  network error
 20  parse error
 30  storage error
 40  configuration error
 50  unknown error
 60  circuit breaker tripped (too many consecutive failures)

CIRCUIT BREAKER
---------------
State is persisted to a JSON file (default: ~/.aldi-cli-state.json or
ALDI_STATE_FILE env var). Tracks:
- last_run_at          ISO timestamp of last run
- last_status          "success" | "error" | "not-due" | "skipped"
- last_error_signature  hash of (category, stage, message_head)
- consecutive_errors   how many times the same error has occurred in a row
- total_runs           cumulative run count
- total_errors         cumulative error count

RULES
-----
- After MAX_CONSECUTIVE_ERRORS (default 3) identical errors, mark as
  "circuit-open" — caller should skip notifications.
- On status transition (error → success OR success → error OR error type
  change), always emit a notification.
- On "not-due" status, never emit a notification (no spam).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Exit codes (kept stable so CI / shell scripts can branch on them)
# --------------------------------------------------------------------------- #

EXIT_SUCCESS = 0
EXIT_NETWORK = 10
EXIT_PARSE = 20
EXIT_STORAGE = 30
EXIT_CONFIG = 40
EXIT_UNKNOWN = 50
EXIT_CIRCUIT_OPEN = 60

# --------------------------------------------------------------------------- #
# Typed exceptions
# --------------------------------------------------------------------------- #


class AldiError(Exception):
    """Base class for all aldi-cli errors."""

    exit_code: int = EXIT_UNKNOWN
    category: str = "unknown"
    stage: str = "unknown"
    retryable: bool = False

    def __init__(self, message: str, *, stage: str = "unknown",
                 retryable: bool | None = None, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        if retryable is not None:
            self.retryable = retryable
        self.cause = cause

    def signature(self) -> str:
        """Stable signature for dedup. Same signature = same email suppressed."""
        msg_head = (self.message or "")[:120].lower().strip()
        return hashlib.sha1(
            f"{self.category}|{self.stage}|{msg_head}".encode()
        ).hexdigest()[:16]


class NetworkError(AldiError):
    exit_code = EXIT_NETWORK
    category = "network"
    retryable = True


class ParseError(AldiError):
    exit_code = EXIT_PARSE
    category = "parse"
    retryable = False


class StorageError(AldiError):
    exit_code = EXIT_STORAGE
    category = "storage"
    retryable = False


class ConfigurationError(AldiError):
    exit_code = EXIT_CONFIG
    category = "config"
    retryable = False


# --------------------------------------------------------------------------- #
# Circuit breaker state
# --------------------------------------------------------------------------- #

MAX_CONSECUTIVE_ERRORS = 3  # after this many identical errors, suppress emails


@dataclass
class RunState:
    last_run_at: Optional[str] = None
    last_status: str = "init"  # success | error | not-due | skipped | init
    last_error_signature: Optional[str] = None
    last_error_category: Optional[str] = None
    last_error_stage: Optional[str] = None
    last_error_message: Optional[str] = None
    consecutive_errors: int = 0
    total_runs: int = 0
    total_errors: int = 0
    last_notification_at: Optional[str] = None
    last_notification_signature: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RunState":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


def _default_state_path() -> Path:
    env = os.environ.get("ALDI_STATE_FILE")
    if env:
        return Path(env)
    # Default: sit next to the DB if possible, else home dir
    db_env = os.environ.get("ALDI_DB")
    if db_env:
        return Path(db_env).with_suffix(".state.json")
    return Path.home() / ".aldi-cli-state.json"


def load_state(path: Path | str | None = None) -> RunState:
    p = Path(path) if path else _default_state_path()
    if not p.exists():
        return RunState()
    try:
        return RunState.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        # Corrupt state file — start fresh
        return RunState()


def save_state(state: RunState, path: Path | str | None = None) -> None:
    p = Path(path) if path else _default_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    except Exception as e:
        # State persistence is best-effort — never fail the run because of it
        print(f"[warn] could not persist state file: {e}", file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def update_state_for_success(state: RunState) -> bool:
    """Update state after a successful run.
    Returns True if a notification should be sent (recovery from error)."""
    state.last_run_at = _now_iso()
    state.last_status = "success"
    recovered = state.consecutive_errors > 0 or state.last_error_signature is not None
    state.consecutive_errors = 0
    state.last_error_signature = None
    state.last_error_category = None
    state.last_error_stage = None
    state.last_error_message = None
    state.total_runs += 1
    return recovered


def update_state_for_skip(state: RunState, status: str = "skipped") -> bool:
    """Update state after a not-due / skipped run. Never notifies."""
    state.last_run_at = _now_iso()
    state.last_status = status
    state.total_runs += 1
    return False  # never notify on skips


def update_state_for_error(state: RunState, err: AldiError) -> tuple[bool, bool]:
    """Update state after an error.
    Returns (should_notify, circuit_open).
    - should_notify: True if email should be sent (first error, type change, or recovery context)
    - circuit_open: True if MAX_CONSECUTIVE_ERRORS reached (suppress further dup emails)"""
    state.last_run_at = _now_iso()
    state.last_status = "error"
    state.total_runs += 1
    state.total_errors += 1

    sig = err.signature()
    is_same = (state.last_error_signature == sig)
    if is_same:
        state.consecutive_errors += 1
    else:
        state.consecutive_errors = 1  # new error type — reset counter

    state.last_error_signature = sig
    state.last_error_category = err.category
    state.last_error_stage = err.stage
    state.last_error_message = err.message

    circuit_open = state.consecutive_errors >= MAX_CONSECUTIVE_ERRORS

    # Notify on first error, type change, OR if not yet circuit-open
    # Once circuit is open, suppress further notifications for SAME error
    if circuit_open and is_same:
        should_notify = False
    else:
        should_notify = True

    return should_notify, circuit_open


# --------------------------------------------------------------------------- #
# Retry helper (network only)
# --------------------------------------------------------------------------- #

def retry_network(fn, *, max_attempts: int = 3,
                  backoff_seconds: list[float] | None = None,
                  stage: str = "network",
                  log_fn=print) -> Any:
    """Retry a callable on NetworkError with exponential backoff.
    Other AldiError types are raised immediately (no retry).
    Default backoff: [5, 15, 45] seconds."""
    if backoff_seconds is None:
        backoff_seconds = [5, 15, 45]

    import time as _time
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except NetworkError as e:
            last_err = e
            if attempt >= max_attempts:
                break
            wait = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
            log_fn(f"[retry] {stage}: network error attempt {attempt}/{max_attempts}, "
                   f"waiting {wait}s — {e.message}")
            _time.sleep(wait)
        except AldiError:
            raise  # non-retryable
        except Exception as e:
            # Wrap unknown exceptions as NetworkError if they look network-y
            if isinstance(e, (ConnectionError, TimeoutError, OSError)):
                last_err = NetworkError(str(e), stage=stage, cause=e)
                if attempt >= max_attempts:
                    break
                wait = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                log_fn(f"[retry] {stage}: network error attempt {attempt}/{max_attempts}, "
                       f"waiting {wait}s — {e}")
                _time.sleep(wait)
            else:
                raise
    # All retries exhausted
    assert last_err is not None
    raise last_err


# --------------------------------------------------------------------------- #
# Error wrapping helpers
# --------------------------------------------------------------------------- #

def wrap_network(fn, *, stage: str = "network"):
    """Run fn(); wrap requests/urllib errors as NetworkError."""
    try:
        return fn()
    except AldiError:
        raise
    except Exception as e:
        # requests exceptions
        if e.__class__.__module__.startswith("requests"):
            raise NetworkError(f"{type(e).__name__}: {e}", stage=stage, cause=e) from e
        if isinstance(e, (ConnectionError, TimeoutError, OSError)):
            raise NetworkError(f"{type(e).__name__}: {e}", stage=stage, cause=e) from e
        raise


def wrap_parse(fn, *, stage: str = "parse"):
    """Run fn(); wrap JSON/regex/KeyError as ParseError."""
    try:
        return fn()
    except AldiError:
        raise
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, IndexError) as e:
        raise ParseError(f"{type(e).__name__}: {e}", stage=stage, cause=e) from e
    except Exception as e:
        # Anything else during parsing — also a parse error
        raise ParseError(f"{type(e).__name__}: {e}", stage=stage, cause=e) from e


def wrap_storage(fn, *, stage: str = "storage"):
    """Run fn(); wrap sqlite3 errors as StorageError."""
    try:
        return fn()
    except AldiError:
        raise
    except Exception as e:
        if e.__class__.__module__.startswith("sqlite3"):
            raise StorageError(f"{type(e).__name__}: {e}", stage=stage, cause=e) from e
        if isinstance(e, (OSError, PermissionError)):
            raise StorageError(f"{type(e).__name__}: {e}", stage=stage, cause=e) from e
        raise


# --------------------------------------------------------------------------- #
# Error reporting (for logs and emails)
# --------------------------------------------------------------------------- #

def format_error_report(err: AldiError, *, include_traceback: bool = True) -> str:
    """Human-readable error report for logs and emails."""
    lines = [
        f"ERROR CATEGORY: {err.category}",
        f"ERROR STAGE:    {err.stage}",
        f"EXIT CODE:      {err.exit_code}",
        f"RETRYABLE:      {err.retryable}",
        f"MESSAGE:        {err.message}",
    ]
    if err.cause:
        lines.append(f"CAUSE:          {type(err.cause).__name__}: {err.cause}")
    if include_traceback:
        tb = traceback.format_exception(type(err), err, err.__traceback__)
        if tb:
            lines.append("")
            lines.append("TRACEBACK:")
            lines.extend("  " + line.rstrip() for line in "".join(tb).splitlines())
    return "\n".join(lines)


def error_signature_for_workflow(err: AldiError | None, status: str) -> str:
    """Stable signature for the GHA workflow's email-dedup logic.
    Returns a short string like 'success' or 'network:discover:abc12345'."""
    if status == "success" or err is None:
        return "success"
    return f"{err.category}:{err.stage}:{err.signature()}"
