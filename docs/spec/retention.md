# Retention: `phoning prune` (module `phoning/retention.py`)

Status: wave A track "retention". Wiring into `cli.py` / `up.py` is wave B.

## Problem

Each mailbox `log.jsonl` is append-only and holds decrypted message bodies. It grows forever.
`phoning prune NAME` trims old records, archives what it removes, and never disturbs readers or a live
owner daemon.

## Command

```
phoning prune NAME [--keep-days 30] [--keep-last 500] [--archive | --no-archive]
                  [--dry-run] [--json] [--home DIR]
```

Module entry point: `phoning.retention.main(argv: Sequence[str] | None = None) -> int`
(also runnable as `python -m phoning.retention`). Stdlib only.

- Home: `--home`, else `$PHONING_HOME`, else `~/.phoning`. Mailbox dir: `<home>/NAME/mailbox`.
- `NAME` must match `[A-Za-z0-9_.-]{1,64}` and not be `.` or `..` (no path traversal). Else exit 2.
- `--keep-days` is a float >= 0, `--keep-last` an int >= 0. Negative or non-numeric: exit 2.
- `--archive` is the default; `--no-archive` discards pruned records. (`argparse.BooleanOptionalAction`.)
- Missing mailbox (`log.jsonl` absent): print `error: no mailbox for NAME at DIR` to stderr, exit 2.
  Prune never creates a mailbox, and `--dry-run` never creates or modifies any file or directory.
- Exit codes: 0 success (including "nothing to prune"), 2 usage / no mailbox, 1 I/O failure.

## What is kept

A record is kept if ANY of these holds:

1. **recent**: its timestamp is within `keep-days` of now. The timestamp is `recv_ts`, else `env.ts`.
   A record with no parseable timestamp is kept (fail closed). Naive timestamps are read as UTC.
2. **tail**: it is among the last `keep-last` records by `seq`. The record with the highest `seq` is
   always kept, even with `--keep-last 0`, so `append` can never reuse a `seq`.
3. **unread**: `seq` is greater than the LOWEST committed reader cursor in `mailbox/cursors/*.json`.
   Unread records are never dropped. A cursor file that is unreadable or invalid counts as 0
   (protects everything), the same as `Mailbox.cursor`. `*.tmp` files are not cursors. No cursor
   files means no unread protection. The report names the reader holding the lowest cursor.
4. **thread**: it is referenced by a kept record. Starting from the records kept by rules 1-3, add
   (a) the record whose `env.eid` equals `env.thread` of a kept record (the thread root),
   (b) the record whose `env.eid` equals `env.reply_to` of a kept record,
   repeating until nothing is added (a reply chain is kept up to its root).
5. **decisions**: so board folds stay correct, for every kept record whose `env.type` is `ask` or
   `propose`, every record whose `env.reply_to` equals that record's `env.eid` is also kept
   (the rule also applies to an `ask`/`propose` that was itself kept by this rule). The replies' ancestors are already kept. Without this an old ask kept as a thread
   root would fold as an unanswered `open_ask` after its reply was pruned.
6. **opaque**: a line that is not a JSON object, or has no integer `seq`, is kept verbatim.
   Prune never interprets what it does not understand.

Kept lines are written back byte-for-byte (no re-serialisation), in their original order.
A trailing partial line (no final newline) is preserved as is.

`tid` de-duplication in `Mailbox.append` only sees records still in the log. After a prune, a relay
re-delivery of a pruned `tid` older than the retention window would be appended again as a new record.
This is accepted; the relay does not redeliver delivered mail beyond that window.

## Cost

Prune holds the append lock for the whole pass, so every appender stalls meanwhile. The keep-set computation must
be linear (index `eid` and `reply_to` once, then a worklist), not quadratic. 20000 records must take seconds.

## Seq numbers are preserved

Records keep their `seq`; nothing is renumbered, because reader cursors store `seq`. After a prune the log
has gaps. `mailbox.py` handles that (verified by reading it, and by the acceptance tests):

- `peek` / `read` / `wait` filter with `seq > cursor` and never assume contiguity.
- `append` assigns `max(seq in log) + 1`. This is why rule 2 always keeps the highest `seq`.
- `commit` stores whatever `seq` it is given.

## Locking: the flock protocol, and why prune cannot `os.replace`

`Mailbox.append` opens `log.jsonl` (`O_RDWR|O_CREAT|O_APPEND`), takes `flock(LOCK_EX)` on that fd, scans the
whole file for the highest `seq`, writes one line at the end, fsyncs, unlocks. `_read_all` (used by `peek`, `read`,
`wait`) takes `flock(LOCK_SH)` on its own fd. Nothing re-checks, after locking, that the path still names the
inode it opened.

Prune therefore takes `flock(LOCK_EX)` on `log.jsonl` the same way, and holds it for the whole read-decide-write.

**Deviation from the brief (`os.replace` of `log.jsonl`).** Replacing the file by rename gives it a new inode.
An appender that already opened the old inode and is blocked on its flock then wakes up holding a lock on an
unlinked file and appends there; the record is lost. Measured against the unmodified `Mailbox.append`: a tight
appender plus a prune loop that atomically replaced the file lost about 91% of the appended records (264 of
3000 survived). Since `mailbox.py` may not change in this wave, prune rewrites the log **in place**, inside the
same inode, while holding the lock:

1. Under `LOCK_EX`: read the file and decide.
2. If nothing would be pruned: return; do not touch the file, do not archive.
3. Write the archive (below).
4. **Stage**: write the kept bytes to `log.jsonl.prune.partial` (0600), fsync it, `os.replace` it to
   `log.jsonl.prune.ready`, fsync the directory. `os.replace` is atomic here, so a `.ready` file always holds
   a complete plan.
5. **Apply**: `ftruncate(fd, 0)`, write the kept bytes at offset 0 in a loop over short writes, `fsync(fd)`.
   The inode and its mode (0600) are unchanged.
6. Delete `.ready`, fsync the directory, unlock.

Appenders and readers blocked on the flock see either the old or the new content, never a half-written file.

**Crash recovery.** If the process dies between steps 4 and 6, `log.jsonl` may be truncated or partial and
`log.jsonl.prune.ready` holds the intended content. On its next (non-dry-run) start, prune, holding the lock,
finds `.ready` and completes it: new log = `.ready` content + every parseable record in the current log whose
`seq` is greater than the highest `seq` in `.ready` (records appended after the crash), de-duplicated by `seq`;
then deletes `.ready`. A stale `*.partial` is deleted. `--dry-run` only reports `recovery_pending: true`.
The residual risk is a hard kill (SIGKILL or power loss) inside step 5, a window of milliseconds: until the
next prune, an appender could see a truncated log and assign a low `seq`. Recovery would then drop such a
record, because only records with `seq` above the staged maximum are merged. The probability is tiny and the
failure is bounded to records appended in that gap; see Open risks.

## Rewrite and archive details

- Archive dir `mailbox/archive/` mode 0700 (created, and chmod-ed if it already exists).
- Archive file `log-YYYYMMDDTHHMMSS.jsonl.gz` (UTC time of the prune), mode 0600, gzip of the pruned lines,
  byte-for-byte, in original order. Written to a temp name, fsynced, `os.replace`d into place, before step 4.
  If that name exists, `log-YYYYMMDDTHHMMSS-N.jsonl.gz` (N = 1, 2, ...) is used. Never overwritten.
- With `--no-archive`, no archive dir or file is created.
- If the archive write fails, prune aborts before touching `log.jsonl` (exit 1).
- A crash after the archive and before the apply leaves the pruned records both archived and still in the log.
  That duplicates, never loses.

## Output

Human (default), one line, e.g.
`pruned alice: 412 of 1030 records (kept 618; archive mailbox/archive/log-20261011T101500.jsonl.gz)`
or `nothing to prune for alice (1030 records)`. `--dry-run` prefixes `[dry-run] would prune`.

`--json` prints one JSON object on stdout:

```
{"name": "alice", "dry_run": false, "total": 1030, "kept": 618, "pruned": 412,
 "kept_by": {"recent": 400, "tail": 100, "unread": 50, "thread": 60, "decisions": 8, "opaque": 0},
 "min_cursor": 812, "min_cursor_reader": "doorbell",
 "bytes_before": 482113, "bytes_after": 290004,
 "archive": "/…/mailbox/archive/log-20261011T101500.jsonl.gz", "recovered": false,
 "recovery_pending": false, "warnings": []}
```

- `kept_by` counts each kept record once, attributed to the first rule that kept it in the order
  `tail`, `recent`, `unread`, `thread`, `decisions`, `opaque`. The values sum to `kept`.
- `min_cursor` / `min_cursor_reader` are `null` when no cursor files exist.
- `archive` is `null` for `--no-archive`, `--dry-run`, or when nothing was pruned.
- `--dry-run` reports counts and `bytes_after` (what the log would be) and writes nothing.
  A dry run takes `LOCK_SH` only.

## Tests (acceptance, `tests/acceptance/test_retention_acceptance.py`)

dry-run writes nothing; age/tail pruning with seq preserved byte-for-byte; unread protection with two readers;
thread chain and ask/propose decisions keep the board fold identical; `peek`/`read`/`wait`/`append`/`commit`
across seq gaps; archive content, modes and `--no-archive`; `--json` shape; idempotence; fail-closed
(unparseable timestamp, junk lines, corrupt cursor); crash recovery from a staged `.ready`; usage errors;
and the concurrency test: a separate appender process in a tight loop while prune runs repeatedly, afterwards
every appended record exists exactly once in `log.jsonl` plus the archives, seqs are unique and contiguous, and
no record kept by the retention rules was lost.

## Open risks

- A stale cursor file (a dead reader at seq 0) pins the whole log. The report names the reader; deleting the
  cursor file is the operator's call. A `--ignore-reader` option is a possible follow-up.
- In-place rewrite has a millisecond crash window (above). A real fix needs `Mailbox.append` to re-check the
  inode after locking; then prune could use `os.replace`. That is a wave B change to `mailbox.py`.
- Prune reads the whole log into memory, like `append` already does.

## Wave B wiring

- `cli.py`: subcommand `prune` dispatching to `phoning.retention.main(argv)` (argv = everything after `prune`).
- `up.py`: optional periodic prune in `phoning up` (for example once a day per mailbox, defaults as above, `--archive`), and/or a LaunchAgent
  option `--prune` that runs `phoning prune NAME` on a schedule. Run only as the mailbox owner's machine user. Prune does not need the
  owner lock and is safe against a live owner daemon.
