# Bootstrap validation — 2026-10-02

Scope: documentation and validation tooling only. Progress:
[Issue 1](https://github.com/FYuki/digital-souls-core/issues/1).

Repository identity: `FYuki/digital-souls-core`, stable ID `1401730345`, public.
Initial main: `9806cf4e60d09256b26c5cc00b49a65eee251276` (README only).
The repository had no refs or implementation before initialization.

Official action tags were verified with `git ls-remote` on 2026-10-02:

- `https://github.com/actions/checkout.git`, `refs/tags/v4.2.2`:
  `11bd71901bbe5b1630ceea73d27597364c9af683`.
- `https://github.com/actions/setup-node.git`, `refs/tags/v4.4.0`:
  `49933ea5288caeca8642d1e84afbd3f7d6820020`.

Local environment: Windows PowerShell, Node 24.19.0, Git. Commands and final
revision/results are recorded in the PR after execution. CI executes on
Ubuntu 24.04 with the same pinned Node version; hosted OS images can change.

Product lint, formatting, types, UT, IT1 and packaging: NOT IMPLEMENTED.
Real-environment IT2/ST: NOT RUN. No providers or GPU were used. The documentation
checker's regression tests are tooling tests only, not Core product tests.
Branch protections, rulesets and GitHub Apps were not changed.

## Executed checks and sample asset review

- `node --test tools/check-docs.test.mjs`: PASS, 11 tests, 0 failures/skips.
- `node tools/check-docs.mjs`: PASS, text/JSON/local links and asset hashes.
- `git diff --cached --check`: PASS before commit.
- Staged text reviewed; one-off scan of tracked text for PEM private-key headers,
  GitHub/OpenAI/AWS access-key patterns and quoted key/password/secret assignments:
  zero hits. This is a limited pattern check, not an exhaustive security audit.
- Card is public fictional persona/lore and sample dialogue, not a private chat
  export. Included card and PNG match their source SHA-256 and size.
- PNG visually inspected; PNG chunks are IHDR, caBX, IDAT and IEND. C2PA metadata
  describes OpenAI generated media; provenance metadata was preserved unchanged.
- [Miori manifest](../../core/characters/miori/manifest.json) inventories eight source
  files. Two included; voice-related files held pending distribution terms;
  three companion documents omitted from the minimal sample.

The public PoC revision is pinned in the asset manifest. No private-repository
content was transferred. Audio distribution terms remain unresolved; no WAV or
voice metadata is included. See [asset terms](../../core/characters/miori/TERMS.md).

## Required-test guard follow-up — 2026-10-02

Review reproduced that plain `node --test` returns zero for all-skipped tests
and counts an empty file as a synthetic passing test. The previous 11 tests
did actually pass; this correction closes a future false-success path.

CI and CONTRIBUTING now use `tools/required-tests-reporter.mjs` via Node's
official `--test-reporter` interface. The reporter consumes
[`test:summary` events](https://nodejs.org/api/test.html#event-testsummary),
requires successful nonempty per-file summaries, rejects failed/skipped/TODO/
cancelled outcomes and reconciles registered counts against the final total.
An empty file's synthetic pass therefore cannot be hidden by another passing file.

On Node 24.19.0 / Windows, the guarded command in CONTRIBUTING passes 23 actual
tests: 11 checker tests and 12 guard fixture tests. Fixtures exercise single/multi
file success, a passing suite, empty file/suite, mixed empty+passing files, all
and partial skips, TODO, explicit cancellation, assertion failure and load failure.
Rejected fixtures must exit 1 with the guard diagnostic; they are expected-failure
fixtures, not skipped or failing tests in the outer suite.

Text/JSON/link/manifest validation and `git diff --cached --check` pass.
Latest pushed revision and CI run are recorded in PR #2 and Issue #1.
