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

The locator supports native Linux executables and the Linux x64/arm64 npm layouts
recognized by the helper. Native Windows and macOS are not supported.
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
```

`apply` is an alias for `repair`. No global shell command, configuration change,
dependency installation, or background updater is created automatically. The
tool does not edit your global `AGENTS.md` or ask the assistant to change its
writing style.

## What repair changes

Repair snapshots the skill, helper, and tracked state, then fetches the official
`rust-vX.Y.Z` tag matching the installed executable. It applies or narrowly adapts
the renderer patch, runs formatting and the full Markdown rendering test module,
builds a release binary, checks its version and startup, and replaces the installed
executable only after verification. Installation failures trigger recovery of
the prior executable, adapted patch, and state.

Restart Codex CLI after a successful repair. A package-manager update can replace
the custom executable; run `status`, then `repair` for the new version.

`rollback` restores the same-version binary saved before customization. It checks
the current executable and backup hashes, preserves a copy of the custom binary,
and refuses to overwrite an untracked update. If the CLI was already customized
before its first recorded repair, its first backup is that existing image, not a
verified official binary.

## Private runtime data

Runtime state defaults to `~/.local/state/codex-style` (or
`$XDG_STATE_HOME/codex-style`). Set `CODEX_STYLE_STATE_DIR` to use a different
absolute directory outside this checkout. Use the same setting for every stage,
status check, and rollback; separate installations need separate state directories.

This directory stores state records, adapted patches, source snapshots, and
recovery binaries. Build jobs use a temporary directory that is retained on
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
guards, rollback, and state isolation using temporary Git repositories and fake
executables. It does **not** compile or replace the installed Codex CLI. Actual
`repair` requires the full Rust test/build checks before installation.

For staged diagnosis and manual compatibility repairs, use [SKILL.md](SKILL.md).
Do not remove failing tests or relax verification to force an installation.

## License and upstream code

Apache-2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE). The Rust regression
fixtures are unchanged files from OpenAI Codex, and patches include upstream code
context. Their provenance is recorded in [THIRD_PARTY.md](THIRD_PARTY.md).
