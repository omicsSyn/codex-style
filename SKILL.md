---
name: codex-style
description: Maintain custom heading and emphasis colors in Codex CLI on Linux or WSL. Use for checking color status, restoring colors after an update, or adapting a version-matched renderer patch with backups, tests, and rollback.
license: Apache-2.0
metadata:
  version: "1.1.1"
---

# Codex CLI color maintenance

Use the bundled `scripts/maintain.py` and `scripts/tool/` resources. This skill
changes six heading styles and strong emphasis in the terminal renderer. It
preserves the existing quote color and other rendering behavior.

## Scope and authorization

Run only `status` when the user asks to inspect status. A request to restore or
maintain colors authorizes the necessary backups, candidate adaptation, tests,
build, and same-version installation. Do not infer that authorization from a
request for an explanation or audit. Explain that repair compiles Codex and
replaces its installed executable; a restart is needed to load the new program.

Do not downgrade Codex, substitute a binary from another version, install missing
system dependencies automatically, change proxy settings, or edit the user's
global instructions. Do not delete old snapshots or recovery binaries. This
tool supports Linux/WSL and one installed Codex binary per state directory.

The runtime directory defaults to `~/.local/state/codex-style`, honoring
`XDG_STATE_HOME`; `CODEX_STYLE_STATE_DIR` overrides it. Keep runtime data outside
the skill checkout. It contains local paths, logs, and recovery files; do not
publish it. See [README.md](README.md) for dependencies and installation.

## Standard workflow

From this skill's directory:

```bash
python3 scripts/maintain.py status
python3 scripts/maintain.py repair
python3 scripts/maintain.py rollback
python3 scripts/maintain.py recover
```

`repair` returns without rebuilding if the tracked binary and patch are active.
Otherwise it snapshots the skill, bundled helper and current state; fetches the
official tag matching the installed version; applies or narrowly adapts the
patch; runs formatting checks and the complete Markdown rendering test module;
builds the release executable; verifies its version and startup; and installs
with a persistent recovery journal. The adapted patch stays in the private
runtime directory. Before replacing an executable, installation and rollback
record the original binary, checksums, and metadata needed for recovery.

If `status` reports an incomplete operation, a status-only request remains
read-only. When maintenance is authorized, run `recover` with the original
runtime directory before attempting another repair or rollback. Recovery restores
the pre-operation image and metadata, not the unfinished target image. It checks
the backup and current executable and refuses an unrelated external update.
Keep the journal and backups if recovery cannot complete. Do not run maintenance
concurrently with a package-manager update or from another runtime directory
against the same executable.

Unknown renderer semantics must stop automatic adaptation. Inspect the actual
version's source and test failures before changing a candidate. Limit changes
to the renderer and its tests. Preserve non-color behavior, existing tests, and
external dependency versions. Back up this skill before changing its helpers,
rules, or tests:

```bash
python3 scripts/maintain.py backup --purpose "Describe the compatibility change"
python3 -B -m unittest discover -s scripts/tests -v
```

If a skill validator is available, run it too. Helper regression tests use
simulated build outputs and do not replace real Rust validation.

## Resume a failed job

Read the printed work directory's `job.json` and relevant stage log. Diagnose
network failures, missing official tags, patch context changes, renderer changes,
missing dependencies, test failures, and installation changes separately. Use a
bounded retry only when the observed cause supports it. Never bypass login or
network access controls. Preserve failed jobs for diagnosis.

```bash
python3 scripts/maintain.py prepare --work-dir /path/to/job
python3 scripts/maintain.py adapt --work-dir /path/to/job
python3 scripts/maintain.py capture --work-dir /path/to/job
python3 scripts/maintain.py verify --work-dir /path/to/job
python3 scripts/maintain.py install --work-dir /path/to/job
```

These are stages, not a sequence to run indiscriminately. `prepare` resumes only
an interrupted preparation against the same installed version and image. Use
`adapt` for recognized upstream structures, or `capture` after a reviewed manual
repair. Any subsequent source, staging, patch, or binary change invalidates
verification. If the installed Codex changes, start a job for that version.
Always use the same runtime directory when resuming or rolling back.
Jobs now record that directory and reject a changed setting before mutation.
Jobs created by 1.1.0 have no binding: preserve them and prepare a new job rather
than guessing their original state location. Completed 1.1.0 installations can
still use their existing state and same-version backups.

An interrupted 1.1.1 install or rollback can be recovered using the runtime
journal even if the temporary source job has expired. An available job record
is restored along with the executable and metadata. After recovery, resume a
retained verified job with `install`, or start a new repair if the source job is
gone. An interrupted rollback is undone by `recover`; rerun `rollback` afterward
if that remains the intended action. Do not claim recovery from deleted backups,
an altered executable, or arbitrary storage corruption. Pre-1.1.1 interruptions
have no journal and still require inspected, checksum-verified manual recovery.

## Completion criteria

- Source tag, built executable, and installed version match.
- Source differences are limited to the reviewed renderer files. External
  dependencies have not changed.
- The full Markdown rendering test module passes with a nonzero count, including
  headings, quoted headings, strong emphasis, and combined strong/italic text.
- Release build, version, startup and installation checksum checks pass.
- `status` reports `custom transcript colors active`; verified snapshots and
  a same-version recovery binary remain readable.

Report actual checks and remaining limitations. Do not describe offline fixture
tests as a real Codex build. When blocked, report the stage and smallest needed
action. After a successful installation, ask the user to restart Codex CLI.
