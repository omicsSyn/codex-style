"""Behavioral tests for automatic triggering without changing the installed CLI."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
SPEC = importlib.util.spec_from_file_location('codex_style_auto', Path(__file__).parents[1] / 'auto.py')
A = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)


def observation(key='official', active=False, version='0.163.0'):
    return {'key': key, 'active': active, 'identity': {'version': version}}


class AutomaticRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-style-auto-test-')
        self.addCleanup(self.tmp.cleanup)
        self.tool = Path(self.tmp.name)
        shutil.copyfile(A.maintenance.DEFAULT_TOOL / 'codex-style', self.tool / 'codex-style')
        self.runtime = self.tool / 'runtime'
        self.state_dir = self.runtime / 'state'
        environment = patch.dict(os.environ, {'CODEX_STYLE_STATE_DIR': str(self.runtime)})
        environment.start()
        self.addCleanup(environment.stop)

    def state(self):
        return json.loads((self.state_dir / 'auto.json').read_text())

    def test_active_installation_never_builds(self):
        repair = Mock()
        self.assertEqual(A.repair_changes(self.tool, lambda: observation(active=True), repair), 0)
        repair.assert_not_called()

    def test_changed_installation_repairs_and_self_event_is_ignored(self):
        stable = Mock(side_effect=[observation(), observation('custom', True)])
        repair = Mock(return_value=0)
        self.assertEqual(A.repair_changes(self.tool, stable, repair), 0)
        self.assertEqual(self.state()['attempt']['status'], 'completed')
        self.assertEqual(A.repair_changes(self.tool, lambda: observation('custom', True), repair), 0)
        repair.assert_called_once()

    def test_failed_installation_is_not_rebuilt_on_duplicate_events(self):
        stable = lambda: observation()
        repair = Mock(return_value=1)
        self.assertEqual(A.repair_changes(self.tool, stable, repair), 1)
        self.assertEqual(A.repair_changes(self.tool, stable, repair), 1)
        repair.assert_called_once()

    def test_success_exit_without_active_colors_is_a_failure(self):
        self.assertEqual(A.repair_changes(self.tool, lambda: observation(), lambda: 0), 1)
        self.assertEqual(self.state()['attempt']['status'], 'failed')

    def test_interrupted_attempt_requires_explicit_retry(self):
        A.save_state(self.state_dir / 'auto.json', {'attempt': {'key': 'official', 'status': 'running'}})
        repair = Mock(return_value=0)
        self.assertEqual(A.repair_changes(self.tool, lambda: observation(), repair), 1)
        repair.assert_not_called()
        stable = Mock(side_effect=[observation(), observation('custom', True)])
        self.assertEqual(A.repair_changes(self.tool, stable, repair, retry=True), 0)
        repair.assert_called_once()

    def test_reinstallation_of_same_version_allows_a_fresh_attempt(self):
        repair = Mock(return_value=1)
        A.repair_changes(self.tool, lambda: observation('old-inode'), repair)
        stable = Mock(side_effect=[observation('new-inode'), observation('custom', True)])
        repair.return_value = 0
        self.assertEqual(A.repair_changes(self.tool, stable, repair), 0)
        self.assertEqual(repair.call_count, 2)

    def test_new_update_during_build_is_checked_in_same_activation(self):
        stable = Mock(side_effect=[observation('old'), observation('new'),
                                  observation('new'), observation('custom', True)])
        repair = Mock(side_effect=[1, 0])
        self.assertEqual(A.repair_changes(self.tool, stable, repair), 0)
        self.assertEqual(repair.call_count, 2)

    def test_exception_records_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'test failure'):
            A.repair_changes(self.tool, lambda: observation(),
                             Mock(side_effect=RuntimeError('test failure')))
        self.assertEqual(self.state()['attempt']['status'], 'failed')

    def test_unstable_and_missing_installation_waits_for_quiet_period(self):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        observer = Mock(side_effect=[FileNotFoundError(), observation('a'),
                                    observation('b'), observation('b'), observation('b')])
        result = A.wait_stable(observer, quiet=2, timeout=8, interval=1,
                               clock=lambda: now[0], sleep=sleep)
        self.assertEqual(result['key'], 'b')
        self.assertEqual(now[0], 4)

    def test_unavailable_installation_has_bounded_wait(self):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        with self.assertRaisesRegex(RuntimeError, 'No stable Codex installation'):
            A.wait_stable(Mock(side_effect=FileNotFoundError('missing')), quiet=2,
                          timeout=3, interval=1, clock=lambda: now[0], sleep=sleep)

    def test_observation_detects_same_version_executable_replacement(self):
        binary = self.tool / 'codex'
        binary.write_text('#!/bin/sh\necho codex-cli 0.163.0\n')
        binary.chmod(0o755)
        patch_file = self.tool / 'heading-colors.patch'
        patch_file.write_text('patch')
        helpers = {'locate_codex_binary': lambda: binary,
                   'VERSION_RE': __import__('re').compile(r'codex-cli\s+(\d+\.\d+\.\d+)'),
                   'patch_path': lambda: patch_file, 'load_state': lambda _: None,
                   'DATA_ROOT': self.runtime}
        with patch.object(A.maintenance, 'helpers', return_value=helpers), \
             patch.object(A.shutil, 'which', return_value=str(binary)):
            first = A.observe(self.tool)
            replacement = binary.with_name('replacement')
            replacement.write_bytes(binary.read_bytes())
            replacement.chmod(0o755)
            replacement.replace(binary)
            second = A.observe(self.tool)
        self.assertEqual(first['identity']['binary_sha256'], second['identity']['binary_sha256'])
        self.assertNotEqual(first['key'], second['key'])

    def test_explicit_rollback_is_not_undone_by_the_watcher(self):
        repair = Mock()
        state = observation()
        state['restored'] = True
        self.assertEqual(A.repair_changes(self.tool, lambda: state, repair), 0)
        repair.assert_not_called()
        self.assertEqual(self.state()['status'], 'restored')

    def test_pending_recovery_stops_observation_before_execution(self):
        self.runtime.mkdir()
        (self.runtime / 'transaction.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'Incomplete operation'):
            A.observe(self.tool)


if __name__ == '__main__':
    unittest.main()
