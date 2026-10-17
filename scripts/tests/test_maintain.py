"""Offline behavioral regression tests; never replace the live Codex program."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('codex_style_maintain', Path(__file__).parents[1] / 'maintain.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
FIXTURES = Path(__file__).parent / 'fixtures'
BUNDLED_TOOL = M.DEFAULT_TOOL / 'codex-style'


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='codex-style-regression-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / 'work'
        self.source = self.work / 'source'
        self.source.mkdir(parents=True)
        for name in M.FILES:
            p = self.source / name
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(FIXTURES / p.name, p)
        (self.source / '.gitignore').write_text('/codex-rs/target/\n')
        (self.source / 'codex-rs/Cargo.toml').write_text('[workspace.package]\nversion = "0.159.2"\n')
        (self.source / 'codex-rs/Cargo.lock').write_text('version = 4\n\n[[package]]\nname = "codex-tui"\nversion = "0.0.0"\n\n[[package]]\nname = "external"\nversion = "1.0.0"\nsource = "registry+https://example.invalid/index"\nchecksum = "fixed"\n')
        M.command(['git', 'init', '--quiet'], self.source)
        M.command(['git', 'add', '.'], self.source)
        self.commit()
        M.command(['git', 'apply', str(FIXTURES / 'expected.patch')], self.source)
        self.expected = {name: (self.source / name).read_bytes() for name in M.FILES}
        for name in M.FILES:
            shutil.copy2(FIXTURES / Path(name).name, self.source / name)
        self.tool = self.root / 'tool'
        self.tool.mkdir()
        shutil.copy2(BUNDLED_TOOL, self.tool / 'codex-style')
        shutil.copy2(FIXTURES / 'old.patch', self.tool / 'heading-colors.patch')
        self.fake_skill = self.root / 'skill'
        self.fake_skill.mkdir()
        (self.fake_skill / 'SKILL.md').write_text('---\nname: test-style\ndescription: Test fixture\nmetadata:\n  version: "1.0.0"\n---\n')
        (self.fake_skill / 'scripts').mkdir()
        (self.fake_skill / 'scripts/helper.py').write_text('# Complete simulated old skill\n')
        self.binary = self.root / 'codex'
        self.write_binary(self.binary, 'official')
        environment = patch.dict(os.environ, {'CODEX_STYLE_STATE_DIR': str(self.root / 'runtime')})
        environment.start()
        self.addCleanup(environment.stop)
        self.h = M.helpers(self.tool)
        self.ensure_tools = self.h['ensure_tools']
        self.h['ensure_tools'] = lambda: None  # Build commands are simulated in this fixture.
        self.locate_binary = self.h['locate_codex_binary']
        self.h['PATCH_FILE'] = self.tool / 'heading-colors.patch'
        self.h['AGENTS_FILE'] = self.root / 'AGENTS.md'
        self.h['AGENTS_FILE'].write_text('Existing unrelated rule\n' + 'Unrelated global instructions must remain unchanged' + '\n')
        self.h['locate_codex_binary'] = lambda: self.binary
        self.helper_patch = patch.object(M, 'helpers', return_value=self.h)
        self.helper_patch.start()
        self.addCleanup(self.helper_patch.stop)
        self.real_snapshot = M.snapshot
        self.snapshot_patch = patch.object(M, 'snapshot', side_effect=self.backup)
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)
        snapshot = self.backup(self.tool, 'Simulated prepare')
        M.save_job(self.work, {'version': '0.159.2', 'tool': str(self.tool),
                              'binary': str(self.binary), 'starting_sha256': M.digest(self.binary),
                              'source_commit': M.command(['git', 'rev-parse', 'HEAD'], self.source).strip(),
                              'snapshot': str(snapshot), 'phase': 'prepared'})
        self.original_binary = self.binary.read_bytes()
        self.original_patch = self.h['PATCH_FILE'].read_bytes()
        self.original_agents = self.h['AGENTS_FILE'].read_bytes()

    def commit(self):
        M.command(['git', '-c', 'user.name=Skill Test', '-c', 'user.email=skill-test@example.invalid',
                   'commit', '--quiet', '-m', 'Regression fixture'], self.source)

    def backup(self, tool, purpose):
        return self.real_snapshot(Path(tool), purpose, root=self.root / 'snapshots', skill=self.fake_skill)

    @staticmethod
    def write_binary(path, marker, version='0.159.2'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n# ' + marker + '\ncase "$1" in\n--version) printf "codex-cli ' + version + '\\n";;\n--help) exit 0;;\n*) exit 2;;\nesac\n')
        path.chmod(0o755)

    def fake_verification(self, summary=None, fail=False, build_version='0.159.2'):
        real_command = M.command
        text = ''.join('PASS [0.01s] codex-tui markdown_render::markdown_render_tests::' + name + '\n'
                       for name in M.REQUIRED_TESTS)
        text += summary or 'Summary [0.04s] 4 tests run: 4 passed, 0 skipped\n'

        def run(args, cwd=None, log=None, env=None):
            if args[:2] == ['cargo', 'fmt']:
                Path(log).write_text('')
                return ''
            if args[:2] == ['just', 'test']:
                Path(log).write_text(text)
                if fail:
                    raise RuntimeError('Injected renderer test failure')
                return ''
            if args[:2] == ['cargo', 'build']:
                self.write_binary(self.source / 'codex-rs/target/release/codex', 'custom', build_version)
                Path(log).write_text('Finished release profile\n')
                return ''
            if args[0] == 'strip':
                # The fixture is an executable shell script, not an ELF binary.
                return ''
            return real_command(args, cwd, log, env)
        return patch.object(M, 'command', side_effect=run)

    def verified(self):
        M.adapt(self.work)
        with self.fake_verification():
            M.verify(self.work)

    def assert_original(self):
        self.assertEqual(self.binary.read_bytes(), self.original_binary)
        self.assertEqual(self.h['PATCH_FILE'].read_bytes(), self.original_patch)
        self.assertEqual(self.h['AGENTS_FILE'].read_bytes(), self.original_agents)
        self.assertIsNone(self.h['load_state']('0.159.2'))
        self.assertFalse(self.h['ACTIVE_PATCH_FILE'].exists())

    def test_observed_0159_context_failure_rebuilds_validated_patch(self):
        with self.assertRaises(RuntimeError):
            M.command(['git', 'apply', '--check', str(self.tool / 'heading-colors.patch')], self.source)
        M.adapt(self.work)
        for name in M.FILES:
            self.assertEqual((self.source / name).read_bytes(), self.expected[name])
        self.assertIn('Style::new().light_blue()', (self.source / M.FILES[0]).read_text())
        self.assertTrue((self.work / 'patch-failure.log').is_file())
        self.assert_original()

    def test_valid_patch_can_use_normal_path(self):
        shutil.copy2(FIXTURES / 'expected.patch', self.tool / 'heading-colors.patch')
        M.adapt(self.work)
        self.assertEqual(M.load_job(self.work)['adaptation'], 'Existing patch applied')
        for name in M.FILES:
            self.assertEqual((self.source / name).read_bytes(), self.expected[name])

    def test_unknown_style_semantics_stop_without_partial_edits(self):
        renderer = self.source / M.FILES[0]
        renderer.write_text(renderer.read_text().replace('h1: Style::new().bold().underlined()',
                                                        'h1: Style::new().reversed()'))
        M.command(['git', 'add', '.'], self.source)
        self.commit()
        job = M.load_job(self.work)
        job['source_commit'] = M.command(['git', 'rev-parse', 'HEAD'], self.source).strip()
        M.save_job(self.work, job)
        before = renderer.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Unfamiliar h1 semantics'):
            M.adapt(self.work)
        self.assertEqual(renderer.read_bytes(), before)
        self.assertEqual((self.source / M.FILES[1]).read_bytes(), (FIXTURES / 'markdown_render_tests.rs').read_bytes())
        self.assert_original()

    def test_install_without_validation_is_rejected(self):
        M.adapt(self.work)
        with self.assertRaisesRegex(RuntimeError, 'successful verification'):
            M.install(self.work)
        self.assert_original()

    def test_failed_tests_do_not_leave_success_evidence(self):
        M.adapt(self.work)
        with self.fake_verification(fail=True):
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                M.verify(self.work)
        self.assertNotIn('validation', M.load_job(self.work))
        self.assert_original()

    def test_zero_test_success_is_rejected(self):
        M.adapt(self.work)
        with self.fake_verification(summary='Summary [0.0s] 0 tests run: 0 passed\n'):
            with self.assertRaisesRegex(RuntimeError, 'nonempty pass'):
                M.verify(self.work)
        self.assert_original()

    def test_wrong_build_version_is_rejected(self):
        M.adapt(self.work)
        with self.fake_verification(build_version='0.158.0'):
            with self.assertRaisesRegex(RuntimeError, 'Build version'):
                M.verify(self.work)
        self.assertNotIn('validation', M.load_job(self.work))
        self.assert_original()

    def test_binary_changed_after_validation_is_rejected(self):
        self.verified()
        candidate = self.source / 'codex-rs/target/release/codex'
        candidate.write_text(candidate.read_text() + '# post-validation change\n')
        with self.assertRaisesRegex(RuntimeError, 'Candidate changed'):
            M.install(self.work)
        self.assert_original()

    def test_source_changed_after_validation_is_rejected(self):
        self.verified()
        renderer = self.source / M.FILES[0]
        renderer.write_text(renderer.read_text() + '\n// post-validation change\n')
        with self.assertRaisesRegex(RuntimeError, 'Candidate changed|Source changed'):
            M.install(self.work)
        self.assert_original()

    def test_new_external_codex_update_is_not_overwritten(self):
        self.verified()
        self.write_binary(self.binary, 'external update', '0.160.0')
        updated = self.binary.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Installed Codex changed'):
            M.install(self.work)
        self.assertEqual(self.binary.read_bytes(), updated)
        self.assertEqual(self.h['PATCH_FILE'].read_bytes(), self.original_patch)

    def test_transactional_install_succeeds_in_fixture(self):
        self.verified()
        result = M.install(self.work)
        self.assertEqual(result['phase'], 'installed')
        self.assertTrue(M.active(self.h))
        self.assertEqual(self.h['AGENTS_FILE'].read_bytes(), self.original_agents)
        state = self.h['load_state']('0.159.2')
        self.assertEqual(Path(state['backup_path']).read_bytes(), self.original_binary)
        M.check_snapshot(state['preinstall_tool_backup'])
        self.assertEqual(self.h['PATCH_FILE'].read_bytes(), self.original_patch)
        self.assertEqual(self.h['ACTIVE_PATCH_FILE'].read_bytes(), (self.work / 'candidate.patch').read_bytes())

    def test_prerelease_and_custom_versions_are_not_treated_as_official_stable(self):
        for version in ('0.159.2-alpha.3', '0.159.2-custom', '0.159.2 extra'):
            with self.subTest(version=version):
                self.write_binary(self.binary, 'unsupported', version)
                with self.assertRaisesRegex(RuntimeError, 'Cannot parse Codex version'):
                    self.h['codex_version'](self.binary)

    def test_public_cli_rejects_unknown_wrapper_without_creating_state(self):
        environment = os.environ.copy()
        environment.pop('CODEX_STYLE_STATE_DIR', None)
        environment['XDG_STATE_HOME'] = str(self.root / 'xdg-state')
        environment['PATH'] = str(self.root) + os.pathsep + environment.get('PATH', '')
        result = subprocess.run([sys.executable, '-B', str(M.SKILL_ROOT / 'scripts/codex-style'), 'status'],
                                env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported Codex launcher', result.stderr)
        self.assertFalse((self.root / 'xdg-state').exists())

    def test_missing_build_tools_are_reported(self):
        with patch.object(shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'Missing build tools: git, cargo, just, cargo-nextest, strip'):
                self.ensure_tools()

    def test_locator_accepts_native_elf_and_rejects_shell_wrapper(self):
        with patch.object(shutil, 'which', return_value=str(self.binary)):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported Codex launcher'):
                self.locate_binary()
        self.binary.write_bytes(b'\x7fELF' + b'fixture-only')
        with patch.object(shutil, 'which', return_value=str(self.binary)):
            self.assertEqual(self.locate_binary(), self.binary)

    def test_npm_locator_checks_candidate_is_native(self):
        package = self.root / 'npm-package'
        launcher = package / 'bin/codex.js'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('// npm launcher fixture\n')
        candidate = package / 'node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex'
        self.write_binary(candidate, 'unknown wrapper')
        with patch.object(shutil, 'which', return_value=str(launcher)), patch('platform.machine', return_value='x86_64'):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported Codex launcher'):
                self.locate_binary()
            candidate.write_bytes(b'\x7fELF' + b'fixture-only')
            self.assertEqual(self.locate_binary(), candidate)

    def test_public_cli_rejects_state_inside_checkout_without_writing(self):
        environment = os.environ.copy()
        environment['CODEX_STYLE_STATE_DIR'] = str(M.SKILL_ROOT / 'runtime-forbidden')
        result = subprocess.run([sys.executable, '-B', str(M.SKILL_ROOT / 'scripts/maintain.py'), 'backup'],
                                env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('outside the skill checkout', result.stderr)
        self.assertFalse((M.SKILL_ROOT / 'runtime-forbidden').exists())

    def test_install_succeeds_without_global_instructions_file(self):
        self.h['AGENTS_FILE'].unlink()
        self.verified()
        M.install(self.work)
        self.assertFalse(self.h['AGENTS_FILE'].exists())
        self.assertTrue(M.active(self.h))

    def test_rollback_restores_same_version_and_keeps_recovery_copy(self):
        self.verified()
        M.install(self.work)
        customized = self.binary.read_bytes()
        result = M.rollback(self.tool)
        self.assertEqual(result['phase'], 'restored')
        self.assertEqual(self.binary.read_bytes(), self.original_binary)
        self.assertEqual(Path(result['recovery']).read_bytes(), customized)
        self.assertFalse(self.h['load_state']('0.159.2')['active'])
        self.assertEqual(self.h['AGENTS_FILE'].read_bytes(), self.original_agents)
        self.assertEqual(M.rollback(self.tool)['phase'], 'already_restored')

    def test_rollback_rejects_untracked_binary_even_at_same_version(self):
        self.verified()
        M.install(self.work)
        self.write_binary(self.binary, 'same-version external update')
        changed = self.binary.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'untracked update'):
            M.rollback(self.tool)
        self.assertEqual(self.binary.read_bytes(), changed)

    def test_rollback_rejects_corrupted_backup(self):
        self.verified()
        M.install(self.work)
        customized = self.binary.read_bytes()
        backup = Path(self.h['load_state']('0.159.2')['backup_path'])
        backup.write_bytes(backup.read_bytes() + b'# corruption\n')
        with self.assertRaisesRegex(RuntimeError, 'missing or changed'):
            M.rollback(self.tool)
        self.assertEqual(self.binary.read_bytes(), customized)

    def test_rollback_state_failure_restores_custom_binary_and_state(self):
        self.verified()
        M.install(self.work)
        customized = self.binary.read_bytes()
        state_path = self.h['state_path']('0.159.2')
        state_before = state_path.read_bytes()
        self.h['write_state'] = lambda *args: (_ for _ in ()).throw(RuntimeError('Injected state failure'))
        with self.assertRaisesRegex(RuntimeError, 'state failure'):
            M.rollback(self.tool)
        self.assertEqual(self.binary.read_bytes(), customized)
        self.assertEqual(state_path.read_bytes(), state_before)

    def test_snapshot_cannot_be_nested_in_source(self):
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            self.real_snapshot(self.tool, 'Invalid location', root=self.fake_skill / 'snapshots', skill=self.fake_skill)

    def test_snapshot_preserves_runtime_state_and_adapted_patch(self):
        self.verified()
        M.install(self.work)
        snap = self.backup(self.tool, 'After install')
        self.assertEqual((snap / 'active.patch').read_bytes(), self.h['ACTIVE_PATCH_FILE'].read_bytes())
        self.assertEqual((snap / 'runtime-state/0.159.2.json').read_bytes(), self.h['state_path']('0.159.2').read_bytes())
        M.check_snapshot(snap)

    def test_state_write_failure_restores_program_patch_and_rules(self):
        self.verified()
        self.h['write_state'] = lambda *args: (_ for _ in ()).throw(RuntimeError('Injected state write failure'))
        with self.assertRaisesRegex(RuntimeError, 'state write failure'):
            M.install(self.work)
        self.assert_original()

    def test_complete_snapshot_detects_missing_old_script(self):
        snap = Path(M.load_job(self.work)['snapshot'])
        self.assertEqual((snap / 'skill/scripts/helper.py').read_bytes(),
                         (self.fake_skill / 'scripts/helper.py').read_bytes())
        (snap / 'skill/scripts/helper.py').unlink()
        with self.assertRaises(FileNotFoundError):
            M.check_snapshot(snap)

    def test_external_dependency_update_is_rejected(self):
        lock = self.source / 'codex-rs/Cargo.lock'
        lock.write_text(lock.read_text().replace('version = "1.0.0"', 'version = "1.1.0"'))
        with self.assertRaisesRegex(RuntimeError, 'external dependencies'):
            M.check_lockfile(self.source, '0.159.2')

    def test_non_renderer_edit_after_capture_is_rejected_before_tests(self):
        M.adapt(self.work)
        core = self.source / 'codex-rs/core/behavior.rs'
        core.parent.mkdir()
        core.write_text('// Undeclared behavior change\n')
        M.command(['git', 'add', str(core)], self.source)
        with self.fake_verification():
            with self.assertRaisesRegex(RuntimeError, 'outside reviewed renderer'):
                M.verify(self.work)
        self.assertNotIn('validation', M.load_job(self.work))
        self.assert_original()

    def test_staged_non_renderer_edit_after_verification_is_rejected(self):
        self.verified()
        core = self.source / 'codex-rs/core/behavior.rs'
        core.parent.mkdir()
        core.write_text('// Undeclared staged behavior change\n')
        M.command(['git', 'add', str(core)], self.source)
        with self.assertRaisesRegex(RuntimeError, 'outside reviewed renderer'):
            M.install(self.work)
        self.assert_original()

    def test_staging_renderer_after_verification_changes_fingerprint(self):
        self.verified()
        M.command(['git', 'add', M.FILES[0]], self.source)
        with self.assertRaisesRegex(RuntimeError, 'Source changed after verification'):
            M.install(self.work)
        self.assert_original()

    def test_capture_includes_staged_renderer_changes(self):
        M.adapt(self.work)
        before = (self.work / 'candidate.patch').read_bytes()
        M.command(['git', 'add', *M.FILES], self.source)
        M.capture(self.work)
        self.assertEqual((self.work / 'candidate.patch').read_bytes(), before)

    def test_new_source_commit_after_capture_is_rejected(self):
        M.adapt(self.work)
        M.command(['git', 'add', *M.FILES], self.source)
        self.commit()
        with self.fake_verification():
            with self.assertRaisesRegex(RuntimeError, 'Source commit changed'):
                M.verify(self.work)
        self.assert_original()

    def test_clone_failure_resumes_same_directory_and_preserves_error_log(self):
        work = self.root / 'fetch-work'
        real_command = M.command
        attempts = 0

        def clone(args, cwd=None, log=None, env=None):
            nonlocal attempts
            if args[0] == 'git' and 'clone' in args:
                attempts += 1
                if attempts == 1:
                    Path(log).write_text('Injected TLS handshake failure\n')
                    raise RuntimeError('Injected TLS handshake failure')
                return real_command(['git', 'clone', '--quiet', str(self.source), args[-1]], log=log)
            return real_command(args, cwd, log, env)

        with patch.object(M, 'command', side_effect=clone):
            with self.assertRaisesRegex(RuntimeError, 'TLS handshake'):
                M.prepare(self.tool, work)
            first = M.load_job(work)
            self.assertEqual(first['phase'], 'preparing')
            self.assertTrue(Path(first['snapshot']).is_dir())
            self.assertEqual(M.prepare(self.tool, work), work)
        job = M.load_job(work)
        self.assertEqual(job['phase'], 'prepared')
        self.assertEqual(job['snapshot'], first['snapshot'])
        self.assertNotIn('failure', job)
        logs = list(work.glob('source_failed_*.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('TLS handshake', logs[0].read_text())
        self.assert_original()

    def test_partial_checkout_is_preserved_for_diagnosis(self):
        work = self.root / 'fetch-work'
        real_command = M.command

        def fail(args, cwd=None, log=None, env=None):
            if args[0] == 'git' and 'clone' in args:
                source = Path(args[-1])
                source.mkdir()
                (source / 'partial.txt').write_text('Incomplete source retained\n')
                Path(log).write_text('Injected interrupted clone\n')
                raise RuntimeError('Injected interrupted clone')
            return real_command(args, cwd, log, env)

        with patch.object(M, 'command', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'interrupted clone'):
                M.prepare(self.tool, work)
        with self.assertRaisesRegex(RuntimeError, 'Partial or edited checkout retained'):
            M.prepare(self.tool, work)
        self.assertEqual((work / 'source/partial.txt').read_text(), 'Incomplete source retained\n')
        self.assert_original()


if __name__ == '__main__':
    unittest.main()
