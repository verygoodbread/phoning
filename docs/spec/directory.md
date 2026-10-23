# Same-machine onboarding: `phoning directory` (module `phoning/directory.py`)

Two agents on one machine should be able to add each other without anyone hand-copying
fingerprints both ways. Each agent **publishes** a small card into a shared local directory.
The other agent **adds** from that card. Adding goes through `Retalk.add`, which runs
`retalk add FP --peer ALIAS --verify` (phoning/retalk.py), so the relay confirms the fingerprint
really has published keys.

The module exposes `main(argv: Sequence[str] | None = None) -> int` and runs as
`python -m phoning.directory` (`if __name__ == "__main__": sys.exit(main())`). It does not touch
`phoning/cli.py`; wave B wires `phoning directory ...` to `directory.main(argv[1:])`.
Stdlib only. `ruff` and `mypy --strict` clean.

## Trust model (read first)

The directory is a **local, same-user** convenience, not a trust root.
- It lives under the user's config dir. The directory is mode 0700 and each card is mode 0600, so only
  this user (and root) can read or write it.
- A card is a claim. The only thing that makes a contact trustworthy is `retalk add --verify` against
  the relay. A card with a wrong fingerprint at worst adds a contact that the user did not intend. The
  checks below shrink that window, but the user stays responsible for who is in their directory.
- `--mutual` writes into **another identity's** contacts. The only authority for that is local file
  ownership: both identity directories belong to the user running the command, and both are writable by
  them. That is consent by ownership. It is the same authority the user already has over both
  homes. It is **never** a cross-user or cross-machine mechanism, and it is refused whenever ownership
  cannot be shown. The CLI help and the command output say so.

## Locations and names
- Directory: `$PHONING_DIRECTORY`, else `~/.config/phoning/directory/` (`~` expanded).
- Card: `<directory>/<NAME>.json`.
- Phoning home (where identities live): `--home DIR`, else `$PHONING_HOME`, else `~/.phoning`.
  A principal's user dir is `<home>/<NAME>` (the same layout as `phoning init`). Its retalk identity is
  `<user_dir>/identity` and its relay is the first line of `<user_dir>/relay`.
- A principal name (`NAME`, `PEER`, an `ALIAS`) must match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Anything
  else (a slash, `..`, a leading `-` or `.`, an empty string) is a usage error, **exit 2**, and nothing
  is read or written. This is what stops `../../x` from escaping the directory.
- A fingerprint is exactly 32 lowercase hex characters: `[0-9a-f]{32}`.
- A relay URL is compared after stripping surrounding whitespace and trailing `/` characters.

## Card format
```json
{"name": "claude-lead", "fingerprint": "<32 hex>", "relay": "http://127.0.0.1:8766",
 "harness": "claude-code", "role": "architect", "published_at": "2026-10-11T18:00:00Z"}
```
`harness` and `role` are strings or `null`. `published_at` is UTC ISO-8601 with a `Z` suffix. A card is
**well-formed** only when it is a JSON object with: `name` a legal name, `fingerprint` a string, `relay` a
non-empty string, `harness`/`role` string or null, `published_at` a string. Anything else is not well-formed. A card is **valid** when it is well-formed and its fingerprint is
legal (32 lowercase hex characters).
Unknown extra keys are ignored on read and not written.

## Exit codes (all subcommands)
- `0` success.
- `1` the work failed at run time (retalk failed, a file could not be written).
- `2` usage or missing input: illegal name, missing identity, missing card, unreadable or invalid card
  JSON, missing `--peer-home` with `--mutual`.
- `3` **refused**: relay mismatch, malformed fingerprint inside a card, card `name` that does not match
  the file name, the fingerprint is our own, ownership/writability not shown for `--mutual`, the peer's
  real fingerprint does not match its card.

Errors go to stderr as one line starting `error:`. They never include retalk's stdout or stderr bodies.

## `phoning directory publish NAME [--harness H] [--role R] [--home DIR]`
1. `NAME` must be a legal name. The identity directory `<home>/NAME/identity` must exist, else exit 2.
2. The fingerprint is `Retalk(<home>/NAME).id()`. If retalk fails, exit 1. If the result is not a legal
   fingerprint, exit 1 (do not write a card).
3. The relay is `Retalk(<home>/NAME).relay`. If it is `None`, exit 2 (`no relay recorded for NAME`).
4. Create the directory with mode 0700, and chmod it to 0700 if it already exists, tightening any wider mode.
5. Write the card **atomically**: write `<NAME>.json.tmp-<pid>` created with mode 0600, fsync, then
   `os.replace` onto `<NAME>.json`, then chmod 0600. Re-publishing replaces the card; `published_at` is
   refreshed.
6. Print the card as one JSON line on stdout and exit 0.

## `phoning directory list [--json]`
- Reads every `*.json` file in the directory. A missing directory is an empty list (exit 0, no output).
- Entries are sorted by name. A file that is not a valid card (see above), or whose `name` differs from its file stem,
  is skipped, and one stderr line `skipped: <file>` is written. It does not change the exit code.
- Default output is one line per card: `NAME  FP8  harness  role  relay`, where FP8 is the first 8 hex
  characters, an absent harness or role shows `-`, and the whole line goes through
  `phoning.text.clean_line`.
- `--json` prints one JSON object per line: the card.

## `phoning directory add NAME PEER [--as ALIAS] [--home DIR] [--peer-home DIR] [--mutual] [--mutual-alias ALIAS2]`
Adds `PEER` to `NAME`'s contacts. `ALIAS` defaults to `PEER`.
Pre-checks run **in this order**, and every check runs before any contact is written:
1. `NAME`, `PEER`, `ALIAS` (and `ALIAS2`) are legal names, else exit 2. `NAME != PEER`, else exit 2.
2. `<home>/NAME/identity` exists, else exit 2.
3. `<directory>/PEER.json` exists, else exit 2 (`no card for PEER`). It must be a **well-formed** card (not necessarily with a legal fingerprint yet), else exit 2.
4. The card's `name` equals `PEER`, else exit 3.
5. `NAME`'s relay is known (`Retalk(<home>/NAME).relay`), else exit 2. The card's relay equals `NAME`'s
   relay after normalisation, else **exit 3** (`relay mismatch`).
6. The card's fingerprint is legal (32 lowercase hex characters), else **exit 3** (`malformed
   fingerprint`).
7. The card's fingerprint differs from `Retalk(<home>/NAME).id()`, else exit 3 (`that is your own
   fingerprint`).
8. With `--mutual` the extra checks below also pass first.

Then `Retalk(<home>/NAME).add(fp, ALIAS)` runs. `RetalkError` gives exit 1 with
`error: retalk add failed (rc=N); the contact may be saved but unverified`. Adding is idempotent: repeating
the same add succeeds (retalk updates the contact). On success print `added PEER as ALIAS to NAME
(verified)` and exit 0.

### `--mutual`
Also adds `NAME` to `PEER`'s contacts, **only** when all of this holds, checked before anything is written:
- `--peer-home DIR` was given. Without it, exit 2 (`--mutual needs --peer-home`). `<peer-home>/PEER`
  is `PEER`'s user dir; it must contain `identity`, else exit 2.
- Both user dirs (`<home>/NAME` and `<peer-home>/PEER`) and both `identity` dirs are **owned by the
  current uid** (`os.stat().st_uid == os.getuid()`), are real directories (not symlinks; use `os.lstat`), and are writable
  (`os.access(path, os.W_OK)`). Otherwise exit 3
  (`--mutual refused: consent is by local file ownership and I cannot show it for <path>`).
- `PEER`'s real fingerprint `Retalk(<peer-home>/PEER).id()` equals the card's fingerprint, else exit 3.
- `PEER`'s relay (from its own user dir) equals `NAME`'s relay, else exit 3.
- `NAME`'s fingerprint is `Retalk(<home>/NAME).id()` (never read from NAME's card).

Then: first `NAME` adds `PEER` (as above), then `Retalk(<peer-home>/PEER).add(name_fp, ALIAS2)`, where
`ALIAS2` is `--mutual-alias`, default `NAME`. If the second add fails, exit 1 and print what was done
(`PEER was added to NAME; NAME was NOT added to PEER`). Output on full success is two lines
(`added ... to NAME (verified)`, `added NAME as ALIAS2 to PEER (verified)`), then a third line:
`mutual add by local file ownership: same user owns both homes`.

## Out of scope
Cross-user or cross-machine discovery, removing contacts, a directory service, and any relay change.
