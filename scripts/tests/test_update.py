"""Offline updater decisions and cross-process update/repair exclusion."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('codex_style_update', SCRIPTS / 'update.py')
U = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(U)


def installation(version='0.9.0', sha='old'):
    return {'version': version, 'binary': '/fixture/codex', 'sha256': sha}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-update-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = self.root / 'tool'
        self.prefix = self.root / 'node'
        self.tool.mkdir()
        self.runtime = self.root / 'runtime'
        helpers = patch.object(U.maintenance, 'helpers', return_value={
            'STATE_DIR': self.runtime / 'state', 'DATA_ROOT': self.runtime})
        helpers.start()
        self.addCleanup(helpers.stop)
        self.before = installation()
        self.installed = self.start_patch('installed', return_value=self.before)
        self.latest = self.start_patch('latest', return_value='0.10.0')
        self.backup = self.start_patch('backup_package', return_value=self.root / 'backup')
        self.install = self.start_patch('install')
        self.startup = patch.object(U.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0))
        self.startup.start()
        self.addCleanup(self.startup.stop)

    def start_patch(self, name, **kwargs):
        p = patch.object(U, name, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def state(self):
        return json.loads((self.runtime / 'state/update.json').read_text())

    def test_current_version_skips_backup_and_install(self):
        self.latest.return_value = '0.9.0'
        self.assertEqual(U.update(self.prefix, self.tool), 0)
        self.assertEqual(self.state()['status'], 'up_to_date')
        self.backup.assert_not_called()
        self.install.assert_not_called()

    def test_older_registry_never_downgrades_local_version(self):
        self.latest.return_value = '0.8.0'
        self.assertEqual(U.update(self.prefix, self.tool), 0)
        self.assertEqual(self.state()['status'], 'local_newer')
        self.install.assert_not_called()

    def test_check_only_reports_available_without_mutating_package(self):
        self.assertEqual(U.update(self.prefix, self.tool, check_only=True), 0)
        self.assertEqual(self.state()['status'], 'update_available')
        self.backup.assert_not_called()
        self.install.assert_not_called()

    def test_newer_stable_version_is_pinned_and_verified(self):
        self.installed.side_effect = [self.before, self.before, installation('0.10.0', 'new')]
        self.assertEqual(U.update(self.prefix, self.tool), 0)
        self.assertEqual(self.install.call_args.args[:2], (self.prefix, '0.10.0'))
        self.assertEqual(self.state()['status'], 'updated')
        self.assertIn('Pending', self.state()['style'])
        self.assertEqual(self.state()['after']['version'], '0.10.0')

    def test_query_failure_never_installs(self):
        self.latest.side_effect = RuntimeError('lookup failed')
        self.assertEqual(U.update(self.prefix, self.tool), 1)
        self.assertEqual(self.state()['status'], 'failed')
        self.backup.assert_not_called()
        self.install.assert_not_called()

    def test_npm_failure_is_not_reported_as_updated(self):
        self.install.side_effect = RuntimeError('npm failed')
        self.assertEqual(U.update(self.prefix, self.tool), 1)
        self.assertEqual(self.state()['status'], 'failed')
        self.assertIn('backup', self.state())

    def test_incorrect_installed_version_fails_verification(self):
        self.installed.side_effect = [self.before, self.before, installation('0.9.1')]
        self.assertEqual(U.update(self.prefix, self.tool), 1)
        self.assertEqual(self.state()['status'], 'failed')

    def test_external_change_during_backup_is_preserved(self):
        self.installed.side_effect = [self.before, installation('0.11.0', 'external')]
        self.assertEqual(U.update(self.prefix, self.tool), 1)
        self.install.assert_not_called()

    def test_prerelease_or_untrusted_version_is_rejected(self):
        for value in ('0.11.0-beta.1', 'latest', '0.11.0; echo bad', None, ['0.11.0']):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    U.version_tuple(value)

    def test_npm_uses_configured_runtime_and_official_registry(self):
        with patch.object(U.shutil, 'which', side_effect=lambda n: '/fixture/node' if n == 'node' else '/fixture/npm-cli.js'):
            command = U.npm_command(self.prefix, 'view', '@openai/codex', 'dist-tags.latest', '--json')
        self.assertEqual(command[0], '/fixture/node')
        self.assertEqual(command[1], '/fixture/npm-cli.js')
        self.assertIn('--registry=https://registry.npmjs.org', command)

    def test_npm_command_has_no_fragile_fetch_limits(self):
        # A short fetch timeout plus a single retry once silently dropped the
        # large platform helper tarball: npm reports optional-dependency fetch
        # failures as non-fatal, so the install "succeeded" without a binary.
        with patch.object(U.shutil, 'which', side_effect=lambda n: '/fixture/node' if n == 'node' else '/fixture/npm-cli.js'):
            command = U.npm_command(self.prefix, 'install')
        self.assertNotIn('--fetch-retries=1', command)
        self.assertFalse(any('--fetch-timeout' in part for part in command))
        self.assertIn('--fetch-retries=3', command)

    def test_platform_helper_check_matches_vendor_layout(self):
        helper = U.platform_helper_dir(self.prefix)
        if helper is None:
            self.skipTest('platform mapping not covered on this machine')
        self.assertFalse(U.platform_helper_ok(self.prefix))
        binary = helper / 'vendor' / 'test-triple' / 'bin' / 'codex'
        binary.parent.mkdir(parents=True)
        binary.write_text('fixture')
        self.assertTrue(U.platform_helper_ok(self.prefix))

    def test_pending_recovery_prevents_package_update(self):
        self.runtime.mkdir()
        (self.runtime / 'transaction.json').write_text('{}')
        self.assertEqual(U.update(self.prefix, self.tool), 1)
        self.latest.assert_not_called()
        self.install.assert_not_called()

    def test_global_prefix_is_inferred_without_machine_specific_paths(self):
        entry = self.prefix / 'lib/node_modules/@openai/codex/bin/codex.js'
        with patch.object(U.shutil, 'which', return_value=str(entry)):
            self.assertEqual(U.discover_prefix(), self.prefix)


class InstallTests(unittest.TestCase):
    """Real install() behavior: npm failure handling and helper-package verification."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-install-test-')
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / 'node'
        self.prefix.mkdir()
        which = patch.object(U.shutil, 'which',
                             side_effect=lambda n: str(self.prefix / 'bin/node') if n == 'node'
                             else str(self.prefix / 'bin/npm-cli.js'))
        self.addCleanup(which.stop)
        which.start()

    def run_install(self, helper_results, npm_returncode=0):
        run = patch.object(U.subprocess, 'run',
                           return_value=subprocess.CompletedProcess([], npm_returncode))
        helper = patch.object(U, 'platform_helper_ok', side_effect=helper_results)
        self.addCleanup(run.stop)
        self.addCleanup(helper.stop)
        return run.start(), helper.start()

    def test_install_retries_once_when_helper_is_missing(self):
        run, helper = self.run_install([False, True])
        U.install(self.prefix, '0.10.0', open(os.devnull, 'w'))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(helper.call_count, 2)

    def test_install_fails_when_helper_stays_missing(self):
        run, helper = self.run_install([False, False])
        with self.assertRaises(RuntimeError):
            U.install(self.prefix, '0.10.0', open(os.devnull, 'w'))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(helper.call_count, 2)

    def test_install_fails_without_helper_check_when_npm_fails(self):
        run, helper = self.run_install([True], npm_returncode=1)
        with self.assertRaises(RuntimeError):
            U.install(self.prefix, '0.10.0', open(os.devnull, 'w'))
        run.assert_called_once()
        helper.assert_not_called()


class BackupAndLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-update-backup-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = self.root / 'tool'
        self.tool.mkdir()
        shutil.copyfile(U.maintenance.DEFAULT_TOOL / 'codex-style', self.tool / 'codex-style')
        environment = patch.dict(os.environ, {'CODEX_STYLE_STATE_DIR': str(self.root / 'runtime')})
        environment.start()
        self.addCleanup(environment.stop)

    def test_complete_package_backup_preserves_files_and_links(self):
        prefix = self.root / 'node'
        package = prefix / 'lib/node_modules/@openai/codex'
        (package / 'bin').mkdir(parents=True)
        (package / 'package.json').write_text('{"version":"0.9.0"}')
        (package / 'bin/codex.js').write_text('fixture')
        (package / 'link').symlink_to('bin/codex.js')
        before = {'version': '0.9.0', 'binary': str(package / 'bin/codex.js'),
                  'sha256': U.maintenance.digest(package / 'bin/codex.js')}
        backup = U.backup_package(prefix, before, '0.10.0', root=self.root / 'backups')
        manifest = json.loads((backup / 'backup.json').read_text())
        self.assertEqual(manifest['target'], '0.10.0')
        self.assertEqual(manifest['symlinks']['link'], 'bin/codex.js')
        for name, expected in manifest['sha256'].items():
            self.assertEqual(U.maintenance.digest(backup / 'package' / name), expected)

    def test_hoisted_platform_executable_is_also_backed_up(self):
        prefix = self.root / 'node'
        package = prefix / 'lib/node_modules/@openai/codex'
        package.mkdir(parents=True)
        (package / 'package.json').write_text('{}')
        binary = self.root / 'hoisted-codex'
        binary.write_bytes(b'platform executable fixture')
        before = {'version': '0.9.0', 'binary': str(binary), 'sha256': U.maintenance.digest(binary)}
        backup = U.backup_package(prefix, before, '0.10.0', root=self.root / 'backups')
        record = json.loads((backup / 'backup.json').read_text())
        self.assertEqual(Path(record['external_binary_backup']).read_bytes(), binary.read_bytes())

    def test_waiting_lock_serializes_update_and_repair_processes(self):
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import maintain; '
                'from pathlib import Path; print("ready", flush=True); '
                '\nwith maintain.tool_lock(Path(sys.argv[2]), wait=True):\n'
                ' print("acquired", flush=True)\n')
        child = None
        try:
            with U.maintenance.tool_lock(self.tool):
                child = subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), str(self.tool)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertTrue(select.select([child.stdout], [], [], 3)[0])
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.2)
            output, errors = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 0, errors)
            self.assertIn('acquired', output)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate()


if __name__ == '__main__':
    unittest.main()
