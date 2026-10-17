"""Portable service generation and opt-in configuration checks."""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
SPEC = importlib.util.spec_from_file_location('codex_style_automation', Path(__file__).parents[1] / 'automation.py')
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-service-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = self.root / 'tool'
        self.config = self.root / 'config'
        self.units = self.root / 'units'
        helpers = patch.object(C.maintenance, 'helpers', return_value={'DATA_ROOT': self.root / 'runtime'})
        helpers.start()
        self.addCleanup(helpers.stop)
        paths = patch.object(C, 'watch_paths', return_value=[str(self.root / 'codex')])
        paths.start()
        self.addCleanup(paths.stop)

    def test_watching_does_not_enable_scheduled_updates_by_default(self):
        files = C.build_files(self.tool, self.config, self.units)
        self.assertEqual({p.name for p in files}, {'watch.json', 'codex-style-auto.service'})
        self.assertIn(str(self.root / 'runtime'), files[self.units / 'codex-style-auto.service'])
        self.assertIn('EnvironmentFile=-' + str(self.config / 'environment') + '\n',
                      files[self.units / 'codex-style-auto.service'])

    def test_daily_update_requires_explicit_option_and_preserves_timezone(self):
        with patch.object(C.updater, 'discover_prefix', return_value=self.root / 'node'), \
             patch.object(C.updater, 'installed'), patch.object(C.updater, 'npm_command'):
            files = C.build_files(self.tool, self.config, self.units, daily=True,
                                  calendar='*-*-* 08:00:00 Asia/Shanghai')
        timer = files[self.units / 'codex-update.timer']
        self.assertIn('OnCalendar=*-*-* 08:00:00 Asia/Shanghai\n', timer)
        self.assertIn('Persistent=true', timer)
        self.assertIn('Requires=codex-style-auto.service', files[self.units / 'codex-update.service'])

    def test_optional_environment_copies_only_allowlisted_values(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:12345', 'OPENAI_API_KEY': 'do-not-copy'}):
            files = C.build_files(self.tool, self.config, self.units, inherit_proxy=True)
        environment = files[self.config / 'environment']
        self.assertIn('HTTPS_PROXY=', environment)
        self.assertNotIn('OPENAI_API_KEY', environment)
        self.assertNotIn('do-not-copy', environment)

    def test_unit_arguments_escape_specifiers_and_dollars(self):
        line = C.exec_line(['/path with spaces/50%/$tool', 'say "yes"'])
        self.assertIn('50%%/$$tool', line)
        self.assertIn('\\"yes\\"', line)
        with self.assertRaises(ValueError):
            C.unit_quote('line\nExecStart=bad')

    def test_environment_file_preserves_spaces_and_literal_glob_characters(self):
        self.assertEqual(
            C.environment_file_line(Path('/config with % spaces [literal]/environment')),
            'EnvironmentFile=-/config with %% spaces [[]literal]/environment\n')
        with self.assertRaises(ValueError):
            C.environment_file_line(Path('/config\nExecStart=bad'))

    def test_existing_different_configuration_prevents_all_writes(self):
        self.config.mkdir()
        original = self.config / 'watch.json'
        original.write_text('existing configuration')
        with self.assertRaisesRegex(RuntimeError, 'Existing configuration differs'):
            C.write_new_files({self.units / 'new.service': 'new', original: 'replacement'})
        self.assertFalse((self.units / 'new.service').exists())
        self.assertEqual(original.read_text(), 'existing configuration')

    def test_new_configuration_is_private_and_idempotent(self):
        files = {self.config / 'watch.json': '{}', self.units / 'a.service': '[Service]\n'}
        self.assertEqual(len(C.write_new_files(files)), 2)
        self.assertEqual(C.write_new_files(files), [])
        for path in files:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
