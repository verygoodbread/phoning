# Contributor rules for Phoning (humans and agents)

Read `README.md` first, then the doc your change touches (`docs/OPERATIONS.md`, `docs/SPEC-v0.2.md`,
`docs/spec/*.md`).

- **Stdlib only** at runtime. `ruff check phoning tests`, `mypy phoning` (strict) and the full test suite stay
  green on every change.
- **Add tests; never delete them.** A change that shrinks a test file needs a reason in the PR.
- **Never `retalk receive`** on an identity that has a running operator (`phoning own`): reads are
  destructive, so a second reader steals mail. Use `phoning read`/`wait`.
- **Keep the security invariants:**
  - The sender shown anywhere is the transport-verified one. A different claim is flagged, never trusted.
  - Peer text never goes into a wake-up nudge, a notification or an approval record.
  - Subprocesses get argument lists (never a shell), stdin `/dev/null` and a timeout.
  - Nothing creates a mailbox or a harness session as a side effect of a typo.
  - Untrusted text is stripped of control characters before display.
- **Wire format:** the `@hd1` envelope is versioned; a change to it needs a new version tag.
- Tests that run a relay use a throwaway `retalk-server` on a random port and throwaway homes. Never point a
  test at `~/.phoning`.
