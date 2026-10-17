# codex-style

A Codex skill for maintaining colorful Markdown headings and warm-orange emphasis
in the Codex CLI terminal interface. It rebuilds the **exact installed stable
version** from official source, validates the renderer, and keeps recovery copies.

This is an independent community project, not an official OpenAI feature.
It changes terminal rendering only, not the contents of generated files or the
appearance of other Codex clients.

## Palette

| Element | Style |
| --- | --- |
| H1 | Light red, bold, underlined |
| H2 | Magenta, bold |
| H3 | Yellow, bold, italic |
| H4 | Cyan, bold, italic |
| H5 | Blue, bold, italic |
| H6 | Light cyan, bold, italic |
| Strong emphasis | Warm orange `#D19A66`, bold |

The existing green blockquote styling is preserved. Named colors depend on the
terminal palette; the emphasis color uses RGB. Links, syntax highlighting, list
markers, and other non-target styles keep their upstream behavior.

## Requirements and compatibility

- Linux or WSL, with a user-writable Codex CLI installation on `PATH`.
- Python 3.11 or newer. The helper scripts use only the Python standard library.
- Git, Rust/Cargo and the toolchain required by the matching Codex source tag,
  `just`, `cargo-nextest`, and `strip` for a repair/build.
- Network access to the official Codex source and its build dependencies, and
  enough disk space, memory, and time for a full Rust release build. This can be
  substantially more expensive than installing a prebuilt CLI package.

The locator supports native Linux executables and Linux x64/arm64 npm platform
packages in nested or ancestor `node_modules` directories, including sibling,
hoisted, and symlinked packages. It also supports the older package-local
`vendor` layout. Native Windows and macOS are not supported.
Unknown shell wrappers are rejected, so repair cannot replace a launcher script.
Stable versions must report exactly `codex-cli X.Y.Z`; prerelease/custom version strings
are rejected instead of being silently mapped to a different official tag.

The regression fixtures come from `rust-v0.159.2`; the bundled seed patch targets
`rust-v0.163.0`. Other versions are attempted only against their own matching
official source. Unknown renderer layouts or failing tests stop installation.
These fixtures and patch checks are not a promise that every version will build.

## Install as a skill

Clone into an unused skill directory. Do not overwrite an existing installation.

```bash
git clone https://github.com/omicsSyn/codex-style.git ~/.codex/skills/codex-style
cd ~/.codex/skills/codex-style
python3 scripts/maintain.py status
```

Then ask Codex, for example:

> Use $codex-style to check my CLI colors.

> Use $codex-style to restore the colors after my CLI update.

The bundled launcher also works directly:

```bash
python3 scripts/codex-style status
python3 scripts/codex-style repair
python3 scripts/codex-style rollback
python3 scripts/codex-style recover
```

`apply` is an alias for `repair`. No global shell command, configuration change,
dependency installation, or background updater is created automatically. The
tool does not edit your global `AGENTS.md` or ask the assistant to change its
writing style.

## Optional automatic maintenance (1.2.0)

Automation is **opt-in**. It requires a working user-level systemd manager and
Linux `inotify`/`pidfd_open` support, in addition to the repair requirements above.
Keep the checkout at its installed location: services run its Python scripts.

To restore colors automatically after changes to the current Codex installation:

```bash
python3 scripts/automation.py install
```

This enables `codex-style-auto.service`. It listens for filesystem events rather
than periodically polling. After an event, it waits for the installation to stay
stable for 10 seconds, then checks the binary and patch hashes. Active colors
skip the build; overwritten colors invoke the existing verified repair pipeline.
The watcher keeps receiving events during a build, including whole-package
replacement and same-version reinstallation. An explicit rollback is preserved
for that version. Failed or interrupted attempts are recorded without endlessly
rebuilding the same installation.

To also authorize a daily stable npm update at **08:00 Beijing time**:

```bash
python3 scripts/automation.py install --daily-update \
  --calendar '*-*-* 08:00:00 Asia/Shanghai'
```

Without `--calendar`, the daily check defaults to 08:00 in the system's local
timezone. Scheduled upgrades support a standard, writable global npm installation;
native executable installs can use watching, but not this npm updater. Prefix
discovery rejects an unfamiliar layout. The updater checks npm's official
`latest` tag, skips equal or older versions, backs up the current package (and
any hoisted native executable), installs the exact newer stable version, then
checks its version and startup. It never automatically downgrades or installs a
prerelease. Updating and color repair use the same maintenance lock.

The timer uses `Persistent=true`: if the machine or WSL was stopped at the
scheduled time, it catches up when the user services next start. It does not wake
a powered-off machine or keep WSL running. Existing Codex sessions are not
restarted; start a new session after repair to load the customized executable.

Service configuration records the current command paths and private runtime root.
Rerun setup after moving the checkout or installation, but preserve and inspect
existing configuration first: setup refuses to overwrite differing files.
It does not change your login/linger policy. If downloads need the current proxy,
add `--inherit-proxy`; only proxy/certificate variables are copied to a private
environment file. API keys and authentication files are not copied.

```bash
systemctl --user status codex-style-auto.service
systemctl --user list-timers codex-update.timer --all
journalctl --user -u codex-update.service -u codex-style-auto.service -n 40
python3 scripts/update.py --check-only
```

Use the same `CODEX_STYLE_STATE_DIR` for manual checks as for setup. Automation
records `state/auto.json`, `state/auto.log`, `state/update.json`, and
`state/update.log` beneath that private runtime root. Package backups are kept
in `npm-backups/`; none are automatically deleted. A successful npm upgrade is
recorded separately from successful color repair. An unknown renderer layout
still requires diagnosis and adaptation; automation does not launch an AI agent.
Pending recovery journals block both automatic repair and scheduled upgrades.
After diagnosing a failed automatic attempt, resume its job or explicitly retry:

```bash
python3 scripts/auto.py --retry
```

To disable scheduled upgrades, run `systemctl --user disable --now
codex-update.timer`. To disable watching too, run `systemctl --user disable --now
codex-style-auto.service`. Stop automation when it is idle: stopping the watcher
also terminates its running repair subprocess. Disabling a timer does not cancel
an update service that has already started. Runtime state, configuration, and
backups are preserved.

## What repair changes

Repair snapshots the skill, helper, and tracked state, then fetches the official
`rust-vX.Y.Z` tag matching the installed executable. It applies or narrowly adapts
the renderer patch, runs formatting and the full Markdown rendering test module,
builds a release binary, checks its version and startup, and replaces the installed
executable only after verification. Before replacement, it saves a recovery
journal containing the backup location, checksums, and original metadata.
Caught exceptions trigger recovery of the prior executable, adapted patch,
state, and job record. Forced process termination requires the `recover` command
described below.

Restart Codex CLI after a successful repair. A package-manager update can replace
the custom executable; run `status`, then `repair` for the new version.

`rollback` restores the same-version binary saved before customization. It checks
the current executable and backup hashes, preserves a copy of the custom binary,
and refuses to overwrite an untracked update. If the CLI was already customized
before its first recorded repair, its first backup is that existing image, not a
verified official binary.

## Recover after an interruption

Starting with 1.1.1, installation and rollback persist a recovery journal before
changing the executable. If the process receives `SIGTERM`, `SIGKILL`, or another
abrupt termination, use the same runtime directory and run:

```bash
python3 scripts/maintain.py status
python3 scripts/maintain.py recover
```

Pending recovery blocks a new install, repair, or rollback. `recover` checks the
original backup and verifies that the installed executable is either the recorded
original or target image. It restores the pre-operation executable and metadata,
then removes the completed journal. It can itself be interrupted and retried.
Temporary source files are not required; if the original job still exists, its
previous record is restored too. After recovery, retry `install --work-dir
/path/to/job` for a retained verified job, or start a new repair if the job expired.
If a rollback was interrupted, recovery restores the customized executable that
was present before that rollback; rerun `rollback` to finish the intended change.

Recovery refuses a changed/corrupt backup or an unrelated executable update.
Keep the journal and backups for diagnosis when it refuses. Do not edit recorded
hashes to force recovery. This mechanism does not guarantee recovery from missing
backups or filesystem/storage corruption, and it cannot reconstruct a journal
for an interruption that happened under 1.1.0. Such older interruptions require
inspected, checksum-verified manual restoration.

Do not run maintenance against the same executable from different runtime
directories. The opt-in scheduled updater coordinates through the same lock;
other package-manager updates must not run concurrently with maintenance.

## Private runtime data

Runtime state defaults to `~/.local/state/codex-style` (or
`$XDG_STATE_HOME/codex-style`). Set `CODEX_STYLE_STATE_DIR` to use a different
absolute directory outside this checkout. Use the same setting for every stage,
status check, and rollback; separate installations need separate state directories.
Each new job binds to its resolved runtime directory. Resuming with a different
setting is rejected before mutation. Unfinished 1.1.0 jobs lack that binding and
must be preserved rather than resumed by 1.1.1; create a fresh job. Existing
completed 1.1.0 installation state and backups remain usable.

This directory stores state records, adapted patches, source snapshots, recovery
binaries, and any pending `transaction.json` journal. Build jobs use a temporary directory that is retained on
failure and printed for diagnosis. Jobs and snapshots contain local paths and
may contain environment-specific build diagnostics. They are local operational
data, not material to commit or attach wholesale to a public issue.

This repository contains source, documentation, patches, and test fixtures only.
It has no telemetry or upload feature. Git/Cargo perform the source/dependency
downloads needed for repair. Review and redact diagnostic excerpts before sharing
them; do not share authentication files, runtime directories, or full backups.

## Development checks

```bash
python3 -B -m unittest discover -s scripts/tests -v
python3 scripts/maintain.py --help
```

The offline regression suite checks adaptation, failure recovery, installation
guards, rollback, state-directory binding, and npm layouts using temporary Git
repositories and fake executables. Automation checks cover version selection,
private runtime state, optional service generation, and update/repair exclusion.
It also kills isolated child processes during
installation, rollback, and recovery to test the persistent journal. It does
**not** compile or replace the installed Codex CLI. Actual
`repair` requires the full Rust test/build checks before installation.

On a Linux/WSL host with a working user systemd manager, also run
`python3 -B scripts/tests/check_watch.py` for isolated filesystem-event tests.
It temporarily creates a uniquely named test service, uses a fake CLI and fake
repair, removes the test service afterward, and preserves temporary diagnostic
logs. It never changes the installed CLI or production automation units.

For staged diagnosis and manual compatibility repairs, use [SKILL.md](SKILL.md).
Do not remove failing tests or relax verification to force an installation.

## License and upstream code

Apache-2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE). The Rust regression
fixtures are unchanged files from OpenAI Codex, and patches include upstream code
context. Their provenance is recorded in [THIRD_PARTY.md](THIRD_PARTY.md).
