# SPEC: Muse Code waker adapter (`phoning/muse_ring.py`)

Track "muse", build wave A. Evidence and measurements: `docs/research/muse.md` (read it first).
Context: `docs/SPEC-v0.2.md` §7 (doorbell).
This module is **self-contained** and does not import `phoning.doorbell`, `phoning.cli`, `phoning.up`, or
`phoning.mailbox` (wave B imports it from the doorbell, so a back-import would be circular). Stdlib only, Python >= 3.11, `mypy --strict` and `ruff` clean.

## 0. What Muse can and cannot do (summary of the research)
- An **ended** Muse session can be revived with its context by `muse exec --session-id <UUID>`: Muse replays the stored transcript into the model request.
  Needs: same workspace (`--workspace`), same provider (`--provider`), session not live, a UUID (not a name).
- A **live** (TUI or `serve`) session holds an exclusive `flock` on `<session dir>/.session.lock`. Resuming it must never be attempted.
- `muse session-message send` can address a live TUI session, but Muse 1.4.6 answers `sender_unverified` to every caller that is not a model tool call
  inside a Muse session, and requires `MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS=1`. So from a doorbell, `wake()` is expected to return `skip: sender_unverified` today.
- `exec --session-id <missing id>` **silently creates a new empty session**. Existence must be checked first.

## 1. Conventions
- Every public ring function returns ONE line, the same vocabulary as `doorbell.ring_*`:
  `rang: <word>`, `resumed: rc=N`, `skip: <reason>`, `ring failed: <reason>`. Callers treat `ring failed:` and `resumed: rc=<nonzero>` as failures (retry later) and `skip:` as a deliberate no-op.
  Reasons are fixed words or short sanitized tokens. **Subprocess stdout/stderr are never copied into a return value, log line or prompt.**
- Never raise out of `wake`, `resume_headless`, `ring_muse`: any unexpected exception becomes `ring failed: <ExceptionClassName>`.
- Fail closed: unknown liveness, unparseable store, unresolved or ambiguous target, unsupported provider, missing workspace => `skip:`; no subprocess is started.
- Subprocesses: write `import subprocess` / `import shutil` / `import os` as plain module imports and call `subprocess.run(...)`, `shutil.which(...)` (tests monkeypatch `muse_ring.subprocess.run` and `muse_ring.shutil.which`). argv list, never `shell=True`, `stdin=subprocess.DEVNULL`, `capture_output=True`, always a `timeout=`. The single exception: `session-message send` reads its body from stdin, so `wake` passes the fixed nudge with `input=<bytes>` and NOT `stdin=`.
- Child environment = `os.environ` plus `MUSE_NO_AUTO_UPDATE=1` (the launcher must not swap binaries behind a daemon) plus `MUSE_LOGIN=0` (never start a device login).
- Nothing in this module writes to the Muse store, `settings.json`, `auth.json`, or any lock file. Lock probing opens with `os.O_RDONLY` (never `O_CREAT`) and releases immediately.
- Text that is sent to a model (`fixed_nudge`) is built by the CALLER from fixed templates. This module only validates its shape (§2.1); it never adds peer text.

## 2. API

```python
RESUMABLE_PROVIDERS: tuple[str, ...] = ("echo", "meta")   # what `muse exec --provider` accepts
SEND_TIMEOUT: float = 30.0
RESUME_TIMEOUT: float = 900.0
NUDGE_MAX: int = 4000

@dataclass(frozen=True)
class Reach:
    state: str                      # "live" | "dead" | "unknown" | "missing"
    session_id: str | None = None   # canonical lowercase UUID when resolved
    name: str | None = None         # current Muse session name when known
    workspace: str | None = None    # recorded workspace_root
    provider: str | None = None     # recorded provider_id
    session_dir: str | None = None
    reason: str = ""                # one short line, fixed vocabulary, no peer text
    @property
    def resumable(self) -> bool: ...  # state == "dead" and provider in RESUMABLE_PROVIDERS and workspace is an existing directory
    def as_dict(self) -> dict[str, object]: ...  # all fields plus "resumable"

def muse_home(muse_home: str | None = None) -> str
def muse_exe(muse_bin: str | None = None) -> str | None
def find_session_dir(home: str, session_id: str) -> str | None
def resolve_target(target: str, muse_home: str | None = None) -> tuple[str | None, str]
def reachable(target: str, muse_home: str | None = None) -> Reach
def wake(target: str, fixed_nudge: str, *, muse_bin: str | None = None, muse_home: str | None = None, timeout: float = SEND_TIMEOUT) -> str
def resume_headless(target: str, fixed_nudge: str, cwd: str | None = None, *, muse_bin: str | None = None,
                    muse_home: str | None = None, home: str | None = None,
                    extra_args: Sequence[str] = (), timeout: float = RESUME_TIMEOUT) -> str
def ring_muse(target: str, fixed_nudge: str, *, muse_bin: str | None = None, muse_home: str | None = None,
              resume_dead: bool = True, cwd: str | None = None, home: str | None = None,
              extra_args: Sequence[str] = ()) -> str
def main(argv: Sequence[str] | None = None) -> int
```

### 2.1 Helpers
- `muse_home(arg)`: `arg` (user-expanded), else `$XDG_DATA_HOME/muse` when `XDG_DATA_HOME` is set and non-empty, else `~/.local/share/muse` (expanded).
- `muse_exe(arg)`: when `arg` is given (non-empty) it is expanded and accepted only if it is an executable regular file, otherwise `None` (an explicit but unusable path is NEVER replaced by the PATH `muse`); when `arg` is `None` or empty, `shutil.which("muse")`, else `None`.
- Nudge validation (private `_valid_nudge`): must be a `str`, 1..`NUDGE_MAX` chars, contain no ASCII control character (`\x00-\x1f`, `\x7f`) and not start with `-`. Invalid => `ring failed: invalid nudge` (nothing runs).
- `find_session_dir(home, sid)`: matches `<home>/sessions/*/*/*/<sid>/session.jsonl` (the date layout `YYYY/MM/DD`). Exactly one match returns the session directory; none or several returns `None`. `sid` must be a UUID (`[0-9a-f]{8}-...`, case-insensitive; compare lowercase) else `None`.
- Session facts come from `<session dir>/session.jsonl` (JSON lines, unparseable lines skipped):
  - `workspace` and `provider`: the first line with `payload_type == "runtime.session.metadata"` among the first 400 lines; values `payload.record.workspace_root` and `payload.record.provider_id` (each a non-empty string, else `None`).
  - `name`: the `payload.new_name` (string or `None`) of the LAST line with `payload_type == "session.name.changed"`. Do a cheap substring test on the raw line before `json.loads`.

### 2.2 `resolve_target(target, muse_home=None) -> (session_id | None, reason)`
- `target` is stripped; empty, longer than 128 chars, or containing a control character => `(None, "bad target")`.
- A UUID => its lowercase form if `find_session_dir` finds it, else `(None, "no such session")`.
- Anything else is a session NAME: scan every `<home>/sessions/*/*/*/*/session.jsonl`; sessions whose CURRENT name equals the target exactly (case-sensitive) are the matches. One match => `(uuid, "")`; none => `(None, "no such session")`; several => `(None, "ambiguous name")`. Subagent logs under `<session dir>/subagent/` are not sessions and are never scanned. A missing store => `(None, "no such session")`.

### 2.3 `reachable(target, muse_home=None) -> Reach` (read-only, no muse binary needed)
1. `resolve_target`; failure => `Reach("missing", reason=<reason>)`.
2. Read name, workspace, provider.
3. Liveness probe on `<session dir>/.session.lock`:
   - the file does not exist => `state="unknown"`, reason `no lock file`;
   - `os.open(O_RDONLY)` fails => `unknown`, reason `lock unreadable`;
   - `fcntl.flock(fd, LOCK_SH | LOCK_NB)` raises `BlockingIOError`/`EWOULDBLOCK`/`EAGAIN` => `live`, reason `session lock held`;
   - succeeds => immediately `LOCK_UN`, close, `dead`, reason `session lock free`;
   - any other error => `unknown`, reason `lock probe failed`.
   The fd is always closed. Never pass `O_CREAT`; never take `LOCK_EX`.
4. `Reach.session_id/name/workspace/provider/session_dir` are filled whenever the session was resolved.

### 2.4 `wake(target, fixed_nudge, ...)`: live sessions, via `muse session-message send`
1. Invalid nudge => `ring failed: invalid nudge`. No executable => `ring failed: no muse executable`.
2. `reachable`: `missing` => `skip: unreachable: <reason>`; `unknown` => `skip: liveness unknown`; `dead` => `skip: not running (resume_headless wakes ended sessions)`. Only `live` proceeds.
3. Run `[exe, "session-message", "send", "--target", <canonical uuid>, "--json"]` with the nudge (UTF-8, no trailing newline) on stdin, the child environment plus `MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS=1`, `timeout=timeout`. The target is always the resolved UUID, never the raw name.
4. Interpret the reply: stdout is decoded as UTF-8 (errors replaced) and parsed as JSON; only a JSON **object** counts as parsed, anything else is unparseable and gives `ring failed: rc=<N>` whatever the return code:
   - parsed object, return code 0 and no `error_code` key => `rang: <status>` where `<status>` is the JSON `status` if it matches `^[a-z_]{1,32}$`, else `sent`;
   - an `error_code` that matches `^[a-z0-9_]{1,48}$` (with any return code) => `skip: <error_code>` (e.g. `skip: sender_unverified`, `skip: external_agent_ingress_closed`, `skip: target_not_found`);
   - otherwise => `ring failed: rc=<N>`.
   - `subprocess.TimeoutExpired` => `ring failed: timed out`; `OSError`/other => `ring failed: <ExceptionClassName>`.

### 2.5 `resume_headless(target, fixed_nudge, cwd=None, ...)`: ended sessions, via `muse exec --session-id`
1. Invalid nudge => `ring failed: invalid nudge`. No executable => `ring failed: no muse executable`.
2. `reachable`: `missing` => `skip: unreachable: <reason>`; `unknown` => `skip: liveness unknown`; `live` => `skip: live (a running Muse session is never resumed)`.
   `dead` but provider not in `RESUMABLE_PROVIDERS` (or `None`) => `skip: unreachable: provider cannot be resumed headlessly`; recorded workspace missing or not a directory => `skip: unreachable: workspace gone`.
3. `cwd`: if given it must be an existing directory (else `skip: unreachable: cwd gone`); the process cwd is `cwd` when given, else the recorded workspace. `--workspace` is ALWAYS the recorded workspace (Muse refuses to resume elsewhere).
4. `extra_args` (wave B passes things such as `--permission-profile X` or `--approval-mode M`): every item must be a `str` without NUL, and none may be `--session-id`, `--workspace`, `--provider` or start with one of them followed by `=`; otherwise `ring failed: invalid extra args`. They are inserted before the `--` separator.
5. Run `[exe, "exec", "--json", "--provider", <recorded provider>, "--workspace", <recorded workspace>, "--session-id", <uuid>, *extra_args, "--", <nudge>]`, `cwd=` as above, `stdin=DEVNULL`, `timeout=timeout`, environment = child env, plus `PHONING_HOME=<abspath(expanduser(home))>` when `home` is given. Return `resumed: rc=<N>`. `TimeoutExpired` => `ring failed: timed out`; other exceptions => `ring failed: <ExceptionClassName>`.
6. Re-probe liveness inside this function (do not trust an earlier `reachable` result): the lock is checked immediately before the spawn.

### 2.6 `ring_muse(...)`: what the doorbell calls
Validate nudge and executable (same lines as above), `reachable`, then: `live` => `wake(...)`; `dead` => `resume_headless(...)` when `resume_dead` is true, else `skip: not running (resume not enabled)`; `missing`/`unknown` => the same `skip:` lines as above. It never resumes a session that is `live` or `unknown`.

### 2.7 `main(argv)` (read-only diagnostic: `python -m phoning.muse_ring reach TARGET [--muse-home DIR]`)
The module ends with `if __name__ == "__main__": sys.exit(main())` so `python -m phoning.muse_ring` works. Prints `Reach.as_dict()` as one JSON line. Exit 0 for `live`/`dead`, 1 for `unknown`/`missing`, 2 for usage errors. It never starts muse.

## 3. Tests (already written; the implementation must make them pass)
- `tests/unit/test_muse_ring.py`: helpers, name resolution, liveness states, every return line, argv/env/stdin/cwd/timeouts via a recording fake `subprocess.run`.
- `tests/acceptance/test_muse_ring_acceptance.py`: a fake `muse` executable on a temp store: revival with history, live sessions never resumed (a real child process holds the flock), typo ids never create a session, wake refusals, name resolution, no peer text, no secret/stdout leakage.
- `tests/acceptance/test_muse_real.py`: opt-in (`PHONING_TEST_REAL_MUSE=1`) against the real `muse` binary with `--provider echo` in an isolated XDG store.

## 4. Wave B wiring (for whoever edits `phoning/doorbell.py`, `phoning/cli.py`, `phoning/up.py`)
- `phoning doorbell NAME --ring muse --muse-session TARGET [--muse-bin PATH] [--muse-home DIR] [--muse-no-resume] [--muse-arg ARG ...] [--hook-reader R]`
  - `TARGET` is a Muse session UUID or name. Without `--muse-session`: exit 2.
  - The doorbell builds the nudge with its existing `build_nudge(name, hook_reader, home)` and calls `muse_ring.ring_muse(target, nudge, muse_bin=..., muse_home=..., resume_dead=not no_resume, home=<resolved phoning home>, extra_args=muse_args)`.
  - Result handling is the existing one: `ring failed:` and `resumed: rc!=0` keep `pending`; `rang:`/`skip:`/`resumed: rc=0` clear it.
- `up.json` doorbell entry keys: `ring: "muse"`, `muse_session` (str), `muse_bin` (str), `muse_home` (str), `muse_resume` (bool, default true), `muse_args` (list of str). Unknown keys stay rejected. `up` passes them as the flags above (`muse_resume: false` => `--muse-no-resume`; each `muse_args` item => `--muse-arg ITEM`).
- Liveness model for the doorbell: a live Muse session currently yields `skip: sender_unverified`. Wave B should keep the mail unread (the hook/waker path delivers it) and say so in `phoning up` status; do not retry in a tight loop.
