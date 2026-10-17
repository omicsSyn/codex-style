# Third-party sources

The project uses the Apache License 2.0. The upstream license is also preserved
beside the regression fixtures. The OpenAI Codex NOTICE is reproduced in `NOTICE`.

## Unmodified Rust regression fixtures

The following files are byte-for-byte copies from
[OpenAI Codex rust-v0.159.2](https://github.com/openai/codex/tree/rust-v0.159.2),
commit `ff6aec96948b70d94983af2641a6b67c94faeff5`:

- `scripts/tests/fixtures/markdown_render.rs` from `codex-rs/tui/src/markdown_render.rs`
- `scripts/tests/fixtures/markdown_render_tests.rs` from `codex-rs/tui/src/markdown_render_tests.rs`
- `scripts/tests/fixtures/LICENSE` from `LICENSE`

Paths and example identities in these files are upstream test data, not local
user information. These fixtures are used for offline Python behavioral tests;
they are not a vendored replacement for a complete Codex checkout.

## Patches

`scripts/tool/heading-colors.patch` targets
[OpenAI Codex rust-v0.163.0](https://github.com/openai/codex/tree/rust-v0.163.0),
commit `83086188ae0614bd882818ff1aa23f3194962012`.
`scripts/tests/fixtures/expected.patch` is the adapted fixture patch for
`rust-v0.159.2`; `old.patch` is retained to exercise a prior context mismatch.

The patches change heading colors and strong-emphasis color, with corresponding
test expectations. They include unmodified upstream context. Their diff format
identifies each modified line. Original upstream code remains subject to its
license and notices.
