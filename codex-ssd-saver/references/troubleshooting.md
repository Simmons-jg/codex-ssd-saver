# Troubleshooting

## Database locked or busy

Ask the user to quit Codex Desktop if possible, then retry the operation. If Codex cannot be closed, `status` and `verify` are still useful; `install` and `remove` use a busy timeout but may fail while the app is actively writing.

## Historical totals still show TRACE, DEBUG, or INFO

The trigger blocks future inserts only. Old rows remain until the user deliberately deletes, rebuilds, or vacuums the database. Prefer verifying with rows created after the starting `MAX(id)`.

## WAL still changes after install

This can be expected when preserved `WARN` or `ERROR` rows are written, or when SQLite checkpoints existing WAL content. Treat it as a failure only if `verify` reports new blocked levels after the starting `MAX(id)`.

## Main database is still large

`logs_2.sqlite` can remain large because SQLite keeps freed pages. Check `freelist_count` and `page_size` in `status` output. If the user explicitly asks to reclaim disk, ask them to quit Codex first, then run:

```bash
python scripts/codex_ssd_saver.py vacuum
```

It refuses to run if the WAL changed within the last 60 seconds, backs up first, and reports reclaimed bytes.

## verify reports INCONCLUSIVE (exit 3)

No rows and no WAL activity were observed during the window, so the result proves nothing - Codex was likely idle. Ask the user to interact with Codex (send a message, open a session) during the verification window and rerun.

## Trigger disappeared after a Codex update

Schema migrations or table rebuilds can drop the trigger silently. Run `ensure` to reinstall it, and recommend scheduling `ensure` daily (see SKILL.md, "Keeping the Guard Installed").

## User wants default logging back

Run:

```bash
python scripts/codex_ssd_saver.py remove
```

Then restart Codex Desktop.

## Privacy

Never print `feedback_log_body` or full log message bodies during diagnosis. Counts, levels, targets, table schema, ids, and file sizes are enough for this skill.
