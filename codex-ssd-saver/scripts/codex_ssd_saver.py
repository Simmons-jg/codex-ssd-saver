#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


TRIGGER_NAME = "codex_keep_warn_error_logs"
ALLOWED_LEVELS = ("WARN", "WARNING", "ERROR")
BLOCKED_LEVELS = ("TRACE", "DEBUG", "INFO")
KNOWN_GUARD_TRIGGERS = (
    TRIGGER_NAME,
    "block_noisy_logs",
    "codex_drop_trace_logs",
    "codex_drop_all_logs",
)


def trigger_sql() -> str:
    allowed = ", ".join(f"'{level}'" for level in ALLOWED_LEVELS)
    return (
        f"CREATE TRIGGER {TRIGGER_NAME}\n"
        f"BEFORE INSERT ON logs\n"
        f"WHEN upper(coalesce(NEW.level, '')) NOT IN ({allowed})\n"
        f"BEGIN\n"
        f"    SELECT RAISE(IGNORE);\n"
        f"END"
    )


def normalize_sql(sql: str) -> str:
    """Normalize for equivalence: lowercase, drop ALL whitespace and trailing semicolons.

    Whitespace-insensitive so that a same-policy trigger installed by an older
    version (different indentation/line breaks) is treated as up to date rather
    than 'outdated'.
    """
    return re.sub(r"\s+", "", sql).rstrip(";").lower()


def guard_state(con: sqlite3.Connection) -> str:
    """Return 'ok', 'outdated', or 'missing' for the guard trigger."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (TRIGGER_NAME,),
    ).fetchone()
    if not row:
        return "missing"
    if normalize_sql(row["sql"]) != normalize_sql(trigger_sql()):
        return "outdated"
    return "ok"


def default_db() -> Path:
    override = os.environ.get("CODEX_LOG_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex" / "logs_2.sqlite"


def fail(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def connect(db: Path, readonly: bool) -> sqlite3.Connection:
    if not db.exists():
        fail(f"database not found: {db}")
    con = sqlite3.connect(str(db), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    if readonly:
        con.execute("PRAGMA query_only=ON")
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.OperationalError:
            con.close()
            uri = db.resolve().as_uri() + "?immutable=1"
            con = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
            con.row_factory = sqlite3.Row
            print(
                "note: normal read failed; using immutable snapshot "
                "(recent WAL contents may be missing)",
                file=sys.stderr,
            )
    return con


def has_column(con: sqlite3.Connection, name: str) -> bool:
    return any(row["name"] == name for row in con.execute("PRAGMA table_info(logs)"))


def assert_logs_table(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='logs'"
    ).fetchone()
    if not row:
        fail("logs table not found")
    columns = {row["name"] for row in con.execute("PRAGMA table_info(logs)")}
    missing = {"id", "ts", "level"} - columns
    if missing:
        fail(f"logs table is missing required columns: {', '.join(sorted(missing))}")


def sidecars(db: Path) -> list[Path]:
    return [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def file_info(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"path": str(path), "exists": False, "size": 0, "mtime": None}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def print_files(db: Path) -> None:
    print("Files:")
    for path in sidecars(db):
        info = file_info(path)
        if info["exists"]:
            print(f"  {path}  {format_bytes(int(info['size']))}  mtime={info['mtime']}")
        else:
            print(f"  {path}  missing")


def summary(con: sqlite3.Connection) -> sqlite3.Row:
    return con.execute(
        """
        SELECT
          coalesce(max(id), 0) AS max_id,
          count(*) AS total,
          sum(CASE WHEN upper(level) = 'TRACE' THEN 1 ELSE 0 END) AS trace_rows,
          sum(CASE WHEN upper(level) = 'DEBUG' THEN 1 ELSE 0 END) AS debug_rows,
          sum(CASE WHEN upper(level) = 'INFO' THEN 1 ELSE 0 END) AS info_rows,
          sum(CASE WHEN upper(level) IN ('WARN','WARNING') THEN 1 ELSE 0 END) AS warn_rows,
          sum(CASE WHEN upper(level) = 'ERROR' THEN 1 ELSE 0 END) AS error_rows,
          coalesce(max(CASE WHEN upper(level) = 'TRACE' THEN id END), 0) AS max_trace_id,
          coalesce(max(CASE WHEN upper(level) = 'DEBUG' THEN id END), 0) AS max_debug_id,
          coalesce(max(CASE WHEN upper(level) = 'INFO' THEN id END), 0) AS max_info_id
        FROM logs
        """
    ).fetchone()


def print_summary(row: sqlite3.Row) -> None:
    print("Summary:")
    for key in row.keys():
        print(f"  {key}={row[key]}")


def print_level_counts(con: sqlite3.Connection) -> None:
    print("All-time level counts:")
    has_bytes = has_column(con, "estimated_bytes")
    bytes_expr = "coalesce(sum(estimated_bytes), 0)" if has_bytes else "NULL"
    rows = con.execute(
        f"""
        SELECT upper(level) AS level, count(*) AS rows, coalesce(max(id), 0) AS max_id,
               {bytes_expr} AS bytes
        FROM logs
        GROUP BY upper(level)
        ORDER BY rows DESC
        """
    ).fetchall()
    if not rows:
        print("  no rows")
    for row in rows:
        extra = f" bytes={format_bytes(int(row['bytes']))}" if has_bytes else ""
        print(f"  {row['level']}: rows={row['rows']} max_id={row['max_id']}{extra}")


def print_recent_counts(con: sqlite3.Connection, seconds: int) -> None:
    cutoff = int(time.time()) - seconds
    print(f"Recent {seconds}s level counts:")
    has_bytes = has_column(con, "estimated_bytes")
    bytes_expr = "coalesce(sum(estimated_bytes), 0)" if has_bytes else "NULL"
    rows = con.execute(
        f"""
        SELECT upper(level) AS level, count(*) AS rows, coalesce(max(id), 0) AS max_id,
               {bytes_expr} AS bytes
        FROM logs
        WHERE ts >= ?
        GROUP BY upper(level)
        ORDER BY rows DESC
        """,
        (cutoff,),
    ).fetchall()
    if not rows:
        print("  no rows")
        return
    for row in rows:
        extra = f" bytes={format_bytes(int(row['bytes']))}" if has_bytes else ""
        print(f"  {row['level']}: rows={row['rows']} max_id={row['max_id']}{extra}")
    minutes = max(seconds / 60.0, 1e-9)
    total_rows = sum(int(row["rows"]) for row in rows)
    rate = f"  write rate: {total_rows / minutes:.1f} rows/min"
    if has_bytes:
        total_bytes = sum(int(row["bytes"]) for row in rows)
        rate += f", {format_bytes(int(total_bytes / minutes))}/min"
    print(rate)


def print_top_targets(con: sqlite3.Connection, limit: int = 8) -> None:
    if not has_column(con, "target"):
        return
    has_bytes = has_column(con, "estimated_bytes")
    bytes_expr = "coalesce(sum(estimated_bytes), 0)" if has_bytes else "0"
    order = "bytes" if has_bytes else "rows"
    print(f"Top targets by {order} (target names only; log bodies are never read):")
    rows = con.execute(
        f"""
        SELECT target, count(*) AS rows, {bytes_expr} AS bytes
        FROM logs
        GROUP BY target
        ORDER BY {order} DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("  no rows")
    for row in rows:
        extra = f" bytes={format_bytes(int(row['bytes']))}" if has_bytes else ""
        print(f"  {row['target']}: rows={row['rows']}{extra}")


def trigger_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type='trigger' AND tbl_name='logs'
        ORDER BY name
        """
    ).fetchall()


def print_triggers(con: sqlite3.Connection) -> None:
    rows = trigger_rows(con)
    print("Triggers on logs:")
    if not rows:
        print("  none")
        return
    for row in rows:
        marker = " (active guard)" if row["name"] == TRIGGER_NAME else ""
        print(f"  {row['name']}{marker}")


def print_pragmas(con: sqlite3.Connection) -> None:
    page_size = int(con.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(con.execute("PRAGMA page_count").fetchone()[0])
    freelist_count = int(con.execute("PRAGMA freelist_count").fetchone()[0])
    journal_mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    free_bytes = page_size * freelist_count
    total_bytes = page_size * page_count
    print("SQLite:")
    print(f"  journal_mode={journal_mode}")
    print(f"  page_size={page_size}")
    print(f"  page_count={page_count} ({format_bytes(total_bytes)})")
    print(f"  freelist_count={freelist_count} ({format_bytes(free_bytes)} reclaimable after VACUUM)")


def backup_database(db: Path, backup_dir: Path, keep: int | None = None) -> list[Path]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    created: list[Path] = []

    backup_db = backup_dir / f"{db.name}.backup-{stamp}.sqlite"
    with sqlite3.connect(str(db), timeout=30) as src:
        with sqlite3.connect(str(backup_db)) as dst:
            src.backup(dst)
    created.append(backup_db)

    for path in sidecars(db)[1:]:
        if path.exists():
            dest = backup_dir / f"{path.name}.raw-{stamp}"
            shutil.copy2(path, dest)
            created.append(dest)

    print("Backups:")
    for path in created:
        print(f"  {path}  {format_bytes(path.stat().st_size)}")
    rotate_backups(db, backup_dir, keep)
    return created


def rotate_backups(db: Path, backup_dir: Path, keep: int | None) -> None:
    """Keep only the newest `keep` timestamped backups (0/None keeps all)."""
    if not keep or keep < 1:
        return
    pattern = re.compile(re.escape(db.name) + r"\.backup-(\d{8}-\d{6})\.sqlite$")
    stamps = sorted(
        {m.group(1) for p in backup_dir.iterdir() if (m := pattern.match(p.name))},
        reverse=True,
    )
    for stamp in stamps[keep:]:
        for path in backup_dir.iterdir():
            if path.is_file() and path.name.startswith(db.name) and stamp in path.name:
                path.unlink()
                print(f"  rotated out: {path}")


def install_trigger(con: sqlite3.Connection) -> None:
    for name in KNOWN_GUARD_TRIGGERS:
        con.execute(f"DROP TRIGGER IF EXISTS {name}")
    con.execute(trigger_sql())


def command_status(args: argparse.Namespace) -> int:
    db = args.db
    with connect(db, readonly=True) as con:
        assert_logs_table(con)
        print_files(db)
        print()
        print_pragmas(con)
        print()
        print_triggers(con)
        print()
        print_summary(summary(con))
        print()
        print_level_counts(con)
        print()
        print_top_targets(con)
        print()
        print_recent_counts(con, args.recent_seconds)
    return 0


def command_install(args: argparse.Namespace) -> int:
    db = args.db
    if not args.no_backup:
        backup_database(db, args.backup_dir or db.parent, args.keep_backups)
        print()
    with connect(db, readonly=False) as con:
        assert_logs_table(con)
        install_trigger(con)
        print(f"Installed trigger: {TRIGGER_NAME}")
        print(f"Allowed levels: {', '.join(ALLOWED_LEVELS)}")
        print(f"Blocked by default: all other levels, including {', '.join(BLOCKED_LEVELS)}")
        print()
        print_triggers(con)
    return 0


def command_remove(args: argparse.Namespace) -> int:
    db = args.db
    if not args.no_backup:
        backup_database(db, args.backup_dir or db.parent, args.keep_backups)
        print()
    with connect(db, readonly=False) as con:
        assert_logs_table(con)
        con.execute(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME}")
        print(f"Removed trigger: {TRIGGER_NAME}")
        print()
        print_triggers(con)
    return 0


def command_ensure(args: argparse.Namespace) -> int:
    db = args.db
    with connect(db, readonly=True) as con:
        assert_logs_table(con)
        state = guard_state(con)
    if state == "ok":
        print(f"OK: trigger {TRIGGER_NAME} is installed and up to date")
        return 0
    if args.check_only:
        print(f"MISSING: guard state is '{state}' (check-only, no changes made)")
        if state == "outdated":
            print(
                "note: a trigger with the expected name exists but its policy differs; "
                "'ensure' would back up and replace it once, after which it reports OK"
            )
        return 1
    if not args.no_backup:
        backup_database(db, args.backup_dir or db.parent, args.keep_backups)
        print()
    with connect(db, readonly=False) as con:
        assert_logs_table(con)
        install_trigger(con)
        print(f"REINSTALLED: guard state was '{state}', trigger {TRIGGER_NAME} installed")
        print_triggers(con)
    return 0


def command_vacuum(args: argparse.Namespace) -> int:
    db = args.db
    wal = Path(str(db) + "-wal")
    if wal.exists() and not args.force:
        idle = time.time() - wal.stat().st_mtime
        if idle < args.min_idle_seconds:
            fail(
                f"WAL was modified {idle:.0f}s ago (< {args.min_idle_seconds}s); "
                "Codex may still be running. Quit Codex and retry, or pass --force"
            )
    if not args.no_backup:
        backup_database(db, args.backup_dir or db.parent, args.keep_backups)
        print()
    before = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    con = sqlite3.connect(str(db), timeout=5, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        try:
            con.execute("BEGIN EXCLUSIVE")
            con.execute("COMMIT")
        except sqlite3.OperationalError:
            fail("database is locked; quit Codex and retry")
        print("Running VACUUM (this can take a while)...")
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    after = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    print(
        f"before={format_bytes(before)} after={format_bytes(after)} "
        f"reclaimed={format_bytes(max(0, before - after))}"
    )
    return 0


def new_rows_by_level(con: sqlite3.Connection, after_id: int) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT upper(level) AS level, count(*) AS rows, min(id) AS min_id, max(id) AS max_id
        FROM logs
        WHERE id > ?
        GROUP BY upper(level)
        ORDER BY rows DESC
        """,
        (after_id,),
    ).fetchall()


def command_verify(args: argparse.Namespace) -> int:
    db = args.db
    with connect(db, readonly=True) as con:
        assert_logs_table(con)
        before = summary(con)
        before_wal = file_info(Path(str(db) + "-wal"))
        print(f"Verifying for {args.seconds}s...")
        print(f"start_max_id={before['max_id']}")
        print(
            "start_low_max_ids="
            f"TRACE:{before['max_trace_id']} DEBUG:{before['max_debug_id']} INFO:{before['max_info_id']}"
        )

        time.sleep(args.seconds)

        after = summary(con)
        after_wal = file_info(Path(str(db) + "-wal"))
        rows = new_rows_by_level(con, int(before["max_id"]))
        new_row_count = sum(int(row["rows"]) for row in rows)
        bad_rows = [row for row in rows if row["level"] not in ALLOWED_LEVELS]
        low_max_unchanged = (
            before["max_trace_id"] == after["max_trace_id"]
            and before["max_debug_id"] == after["max_debug_id"]
            and before["max_info_id"] == after["max_info_id"]
        )

        print(f"end_max_id={after['max_id']}")
        print(
            "end_low_max_ids="
            f"TRACE:{after['max_trace_id']} DEBUG:{after['max_debug_id']} INFO:{after['max_info_id']}"
        )
        print(f"total_rows_delta={after['total'] - before['total']}")
        print(f"max_id_delta={after['max_id'] - before['max_id']}")
        print(f"new_rows_after_start_max_id={new_row_count}")
        print(f"wal_size_delta={int(after_wal['size']) - int(before_wal['size'])}")
        print(f"wal_mtime_start={before_wal['mtime']}")
        print(f"wal_mtime_end={after_wal['mtime']}")
        print("New rows by level after start_max_id:")
        if rows:
            for row in rows:
                print(
                    f"  {row['level']}: rows={row['rows']} "
                    f"id_range={row['min_id']}..{row['max_id']}"
                )
        else:
            print("  no rows")

        if bad_rows:
            print("FAIL: blocked log levels were inserted after start_max_id")
            return 1
        if not low_max_unchanged:
            print("FAIL: TRACE/DEBUG/INFO max ids changed")
            return 1
        if (
            new_row_count == 0
            and after["max_id"] == before["max_id"]
            and int(after_wal["size"]) == int(before_wal["size"])
            and after_wal["mtime"] == before_wal["mtime"]
        ):
            print(
                "INCONCLUSIVE: no write activity was observed during the window; "
                "interact with Codex while verifying, then rerun"
            )
            return 3
        print("PASS: no TRACE/DEBUG/INFO rows were inserted during verification")
        if after["max_id"] != before["max_id"]:
            print("note: MAX(id) increased only for preserved WARN/WARNING/ERROR rows")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Save your SSD: inspect and guard Codex SQLite logs.")
    parser.add_argument("--db", type=Path, default=default_db(), help="Path to logs_2.sqlite")
    parser.add_argument("--backup-dir", type=Path, default=None, help="Directory for backups")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup before install/remove")
    parser.add_argument(
        "--keep-backups",
        type=int,
        default=3,
        help="Timestamped backups to keep after each new backup (default 3; 0 keeps all)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show file sizes, triggers, and level counts")
    status.add_argument("--recent-seconds", type=int, default=300)
    status.set_defaults(func=command_status)

    install = subparsers.add_parser("install", help="Install WARN/ERROR-only trigger")
    install.set_defaults(func=command_install)

    verify = subparsers.add_parser("verify", help="Verify low-level rows stopped growing")
    verify.add_argument("--seconds", type=int, default=15)
    verify.set_defaults(func=command_verify)

    remove = subparsers.add_parser("remove", help="Remove the guard trigger")
    remove.set_defaults(func=command_remove)

    ensure = subparsers.add_parser(
        "ensure",
        help="Install the trigger only if missing or outdated (idempotent; for scheduled runs)",
    )
    ensure.add_argument(
        "--check-only", action="store_true", help="Report state without changing anything (exit 1 if not ok)"
    )
    ensure.set_defaults(func=command_ensure)

    vacuum = subparsers.add_parser(
        "vacuum", help="Reclaim free pages with VACUUM (requires Codex to be closed)"
    )
    vacuum.add_argument(
        "--min-idle-seconds",
        type=int,
        default=60,
        help="Refuse if the WAL changed within this many seconds (default 60)",
    )
    vacuum.add_argument("--force", action="store_true", help="Skip the WAL idle check")
    vacuum.set_defaults(func=command_vacuum)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.db = args.db.expanduser()
    if hasattr(args, "seconds") and args.seconds < 0:
        fail("--seconds must be non-negative", code=2)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
