# Codex SSD Saver

Codex SSD Saver is a Codex skill that reduces local SQLite log churn from
`~/.codex/logs_2.sqlite`.

It installs a reversible SQLite trigger that keeps `WARN`, `WARNING`, and
`ERROR` rows while ignoring lower-value log rows such as `TRACE`, `DEBUG`, and
`INFO`. This is useful when Codex is writing high-volume local logs and you want
to reduce SSD write pressure without losing important diagnostics.

This is a local mitigation, not an official Codex fix. Prefer official
log-level settings or product updates when they exist.

## Install the Skill

Copy the `codex-ssd-saver` folder into your Codex skills directory:

```powershell
Copy-Item -Recurse .\codex-ssd-saver "$HOME\.codex\skills\codex-ssd-saver"
```

On macOS/Linux:

```bash
cp -R ./codex-ssd-saver ~/.codex/skills/codex-ssd-saver
```

## Quick Start

From the repository root:

```bash
python codex-ssd-saver/scripts/codex_ssd_saver.py status
python codex-ssd-saver/scripts/codex_ssd_saver.py install
python codex-ssd-saver/scripts/codex_ssd_saver.py verify --seconds 30
```

The default database is `$CODEX_LOG_DB` when set, otherwise
`~/.codex/logs_2.sqlite`. Use `--db <path>` to inspect another database.

## Commands

### `status`

Prints database and WAL sizes, trigger state, SQLite free pages, all-time level
counts, recent write rate, and top targets by byte volume. It does not print log
bodies.

```bash
python codex-ssd-saver/scripts/codex_ssd_saver.py status --recent-seconds 300
```

### `install`

Backs up the database, removes older guard triggers, and installs the
WARN/ERROR-only trigger.

```bash
python codex-ssd-saver/scripts/codex_ssd_saver.py install
```

### `verify`

Watches for new rows and passes only if no `TRACE`, `DEBUG`, or `INFO` rows are
inserted during the window. `MAX(id)` may still increase for preserved
`WARN`/`ERROR` rows.

```bash
python codex-ssd-saver/scripts/codex_ssd_saver.py verify --seconds 30
```

Exit codes:

- `0`: pass
- `1`: failed, blocked levels were inserted
- `3`: inconclusive, no write activity was observed

### `ensure`

Idempotently checks that the guard trigger is installed and up to date. This is
safe to schedule because it only touches the trigger and creates a backup before
repairing a missing or outdated trigger.

```bash
python codex-ssd-saver/scripts/codex_ssd_saver.py ensure
python codex-ssd-saver/scripts/codex_ssd_saver.py ensure --check-only
```

Windows scheduled task example:

```powershell
schtasks /Create /TN CodexSsdSaver /SC DAILY /ST 09:00 /TR "python C:\path\to\codex-ssd-saver\scripts\codex_ssd_saver.py ensure"
```

macOS/Linux cron example:

```cron
0 9 * * * python /path/to/codex-ssd-saver/scripts/codex_ssd_saver.py ensure
```

### `vacuum`

Reclaims historical free pages after Codex is closed. This is a manual operation
only. Do not schedule it. A quiet WAL does not prove Codex is closed, so confirm
Codex has exited before running it.

```bash
python codex-ssd-saver/scripts/codex_ssd_saver.py vacuum
```

### `remove`

Removes the guard trigger and restores default logging behavior.

```bash
python codex-ssd-saver/scripts/codex_ssd_saver.py remove
```

## Trigger Policy

The installed trigger is:

```sql
CREATE TRIGGER codex_keep_warn_error_logs
BEFORE INSERT ON logs
WHEN upper(coalesce(NEW.level, '')) NOT IN ('WARN', 'WARNING', 'ERROR')
BEGIN
    SELECT RAISE(IGNORE);
END;
```

Allowlisting `WARN`, `WARNING`, and `ERROR` is intentional. It prevents unknown
future low-value levels from silently resuming high-volume writes.

## Safety

- Never print `feedback_log_body`; it can contain private task content.
- `install`, `remove`, `ensure`, and `vacuum` back up the database by default.
- Do not run `vacuum` while Codex is active.
- Do not make the database read-only or redirect it with symlinks as the default
  mitigation.

## Repository Layout

```text
codex-ssd-saver/
  SKILL.md
  agents/openai.yaml
  scripts/codex_ssd_saver.py
  scripts/codex_log_guard.py
  references/troubleshooting.md
```
