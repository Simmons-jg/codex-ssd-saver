---
name: codex-ssd-saver
description: Save your SSD from Codex log churn. Inspect, mitigate, verify, and remove local Codex SQLite log write controls for ~/.codex/logs_2.sqlite. Use when users report logs_2.sqlite or logs_2.sqlite-wal growth, high TRACE/DEBUG/INFO write rates, SSD wear or disk-write concerns, requests to install a WARN/ERROR-only SQLite trigger, or requests to verify that low-level Codex logs stopped growing.
---

# Codex SSD Saver

## Overview

Use this skill to diagnose and reduce excessive local Codex logging in `~/.codex/logs_2.sqlite`. Prefer the bundled Python script because it is cross-platform, avoids printing log bodies, backs up before changes, and verifies by inspecting new row levels rather than relying only on WAL timestamps.

This is a local mitigation, not an official Codex fix. Prefer official log-level settings or product updates when they exist.

## Check the Root Cause First

TRACE-level logging usually comes from a log-level setting rather than a Codex default. Before (or alongside) installing the trigger, check:

1. `RUST_LOG` environment variable (PowerShell):

   ```powershell
   Get-ChildItem Env:RUST_LOG            # current session
   [Environment]::GetEnvironmentVariable("RUST_LOG", "User")
   [Environment]::GetEnvironmentVariable("RUST_LOG", "Machine")
   ```

   On macOS/Linux: `env | grep RUST_LOG` and check shell profiles.

2. `~/.codex/config.toml` for any log/trace/verbosity keys.
3. Shortcuts or wrapper scripts that launch Codex with extra environment variables or flags.

If a `RUST_LOG=trace`-style setting exists, removing it (then restarting Codex) fixes the problem at the source - the trigger then serves as a safety net. If no such setting exists, the trigger is the practical mitigation.

## Quick Start

Run the script from this skill directory:

```bash
python scripts/codex_ssd_saver.py status
python scripts/codex_ssd_saver.py install
python scripts/codex_ssd_saver.py verify --seconds 30
python scripts/codex_ssd_saver.py ensure    # idempotent re-install; for scheduled runs
python scripts/codex_ssd_saver.py vacuum    # reclaim free pages; Codex must be closed
python scripts/codex_ssd_saver.py remove
```

Use `--db <path>` to inspect a non-default database. The default is `$CODEX_LOG_DB` when set, otherwise `~/.codex/logs_2.sqlite`.

## Workflow

1. Run `status` first. Check database/WAL sizes, trigger state, level counts, recent counts, and reclaimable free pages.
2. If mitigation is needed, run `install`. It backs up the database with SQLite's backup API, removes known older guard triggers, and installs a WARN/ERROR-only allowlist trigger.
3. Run `verify --seconds 30`. Exit codes: `0` PASS (no new `TRACE`/`DEBUG`/`INFO` rows), `1` FAIL, `3` INCONCLUSIVE (no write activity at all - ask the user to interact with Codex during the window, then rerun). `MAX(id)` may still increase if preserved `WARN` or `ERROR` rows arrive.
4. Recommend scheduling `ensure` so the guard survives Codex updates (see below).
5. If Codex behaves unexpectedly or the user wants default logging back, run `remove`.
6. If disk size remains large, run `vacuum` after the user quits Codex. Read `references/troubleshooting.md` before recommending cleanup.

## Trigger Policy

Install this allowlist trigger:

```sql
CREATE TRIGGER codex_keep_warn_error_logs
BEFORE INSERT ON logs
WHEN upper(coalesce(NEW.level, '')) NOT IN ('WARN', 'WARNING', 'ERROR')
BEGIN
    SELECT RAISE(IGNORE);
END;
```

Allowlist behavior is preferred over a narrow TRACE/DEBUG/INFO blacklist because unknown future low-value levels will not silently resume high-volume writes. Preserving `WARN`, `WARNING`, and `ERROR` keeps operationally useful diagnostics.

## Keeping the Guard Installed

Codex updates or schema migrations can silently drop the trigger, and low-level logging resumes without warning. `ensure` is idempotent: it exits quietly when the trigger is present and up to date, and reinstalls it otherwise. `ensure --check-only` reports without changing anything (exit 1 when missing/outdated).

Schedule a daily check on Windows (adjust the path):

```powershell
schtasks /Create /TN CodexSsdSaver /SC DAILY /ST 09:00 /TR "python <skill-dir>\scripts\codex_ssd_saver.py ensure"
```

On macOS/Linux use cron: `0 9 * * * python <skill-dir>/scripts/codex_ssd_saver.py ensure`.

## Reclaiming Disk Space

The trigger only stops future writes; SQLite keeps freed pages, so the file stays large. The `vacuum` subcommand refuses to run if the WAL changed within the last 60 seconds (Codex is likely running), backs up first, then runs `VACUUM` plus `wal_checkpoint(TRUNCATE)` and reports reclaimed bytes.

`vacuum` is a MANUAL operation only. Never schedule it or run it automatically: a quiet WAL does not prove Codex is closed. Always ask the user to explicitly confirm they quit Codex before running it. (Scheduling `ensure` is fine; scheduling `vacuum` is not.)

## Backups

`install`, `remove`, `ensure`, and `vacuum` back up the database first by default. A backup is a full copy of the database, which can be large - sizes are printed. Backups are rotated automatically, keeping the newest 3 by default (`--keep-backups N`; `0` keeps all).

## Safety Rules

- Do not inspect or print `feedback_log_body`; it can contain private task content.
- Do not delete, truncate, or rebuild the database ad hoc. To reclaim space, use the `vacuum` subcommand (it checks for activity and backs up first), and only after the user explicitly asks and quits Codex.
- Do not make the database read-only or redirect it with symlinks as a default mitigation.
- Do not block all logs by default. Preserve warning and error diagnostics.
- Treat trigger installation as reversible. Keep backups before install/remove.

## Script

Primary script:

`scripts/codex_ssd_saver.py`

Useful options:

- `--db PATH`: override the database path.
- `--backup-dir PATH`: choose where backups are written.
- `--no-backup`: skip backup only when the user explicitly accepts that risk.
- `--keep-backups N`: backups to retain after each new backup (default 3; 0 keeps all).
- `verify --seconds N`: watch for new rows for `N` seconds (exit 0 PASS / 1 FAIL / 3 INCONCLUSIVE).
- `ensure [--check-only]`: idempotent install; suitable for scheduled tasks.
- `vacuum [--min-idle-seconds N] [--force]`: reclaim free pages when Codex is closed.

## Troubleshooting

Read `references/troubleshooting.md` when:

- validation fails or the database is locked,
- the WAL still changes after install,
- historical totals still show many low-level rows,
- the user wants to reclaim disk space.
