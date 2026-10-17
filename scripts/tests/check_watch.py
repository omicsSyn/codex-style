#!/usr/bin/env python3
"""Exercise a real event-watcher service in an isolated installation, never the live CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


STUB = r'''
import contextlib, hashlib, json, re, sys
from pathlib import Path
DEFAULT_TOOL = Path(__file__).parent / 'tool'
def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def atomic_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_bytes(value)
    tmp.replace(path)
@contextlib.contextmanager
def tool_lock(path, wait=False, name='maintenance'):
    yield
def require_no_transaction(helpers):
    if (helpers['DATA_ROOT'] / 'transaction.json').exists():
        raise RuntimeError('Incomplete operation')
def helpers(tool):
    tool = Path(tool)
    binary = Path((tool / 'binary-path').read_text())
    def state(version):
        p = tool / 'state' / (version + '.json')
        return json.loads(p.read_text()) if p.exists() else None
    return {'locate_codex_binary': lambda: binary,
            'VERSION_RE': re.compile(r'codex-cli\s+(\d+\.\d+\.\d+)'),
            'PATCH_FILE': tool / 'heading-colors.patch', 'load_state': state,
            'patch_path': lambda: tool / 'heading-colors.patch',
            'DATA_ROOT': tool, 'STATE_DIR': tool / 'state'}
if __name__ == '__main__':
    assert sys.argv[1:3] == ['repair', '--tool-dir']
    tool = Path(sys.argv[3])
    assert tool.parent.name.startswith('codex-style-watch-test-')
    count = tool / 'calls'
    atomic_bytes(count, str(int(count.read_text()) + 1 if count.exists() else 1).encode())
    if (tool / 'fail').exists():
        print('Injected fixture failure')
        sys.exit(1)
    h = helpers(tool)
    binary = h['locate_codex_binary']()
    version = h['VERSION_RE'].search(binary.read_text()).group(1)
    atomic_bytes(binary, binary.read_bytes() + b'# custom colors\n')
    binary.chmod(0o755)
    atomic_bytes(tool / 'state' / (version + '.json'), json.dumps({
        'custom_sha256': digest(binary), 'patch_sha256': digest(h['PATCH_FILE'])}).encode())
'''


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', '--user', *args], check=check,
                          capture_output=True, text=True, timeout=30)


def main():
    skill = Path(__file__).resolve().parents[2]
    root = Path(tempfile.mkdtemp(prefix='codex-style-watch-test-'))
    tool = root / 'tool'
    tool.mkdir()
    home = root / 'home'
    package = home / 'node/lib/node_modules/@openai/codex'
    binary = package / 'node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex'
    entrypoint = package / 'bin/codex.js'
    (tool / 'binary-path').write_text(str(binary))
    (tool / 'heading-colors.patch').write_text('fixture patch')
    shutil.copy2(skill / 'scripts/auto.py', root / 'auto.py')
    shutil.copy2(skill / 'scripts/watch.py', root / 'watch.py')
    (root / 'maintain.py').write_text(STUB)
    for directory in (home / '.local/bin', home / 'node/bin'):
        directory.mkdir(parents=True)
        (directory / 'codex').symlink_to(entrypoint)

    def install(version):
        binary.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text('// fixture entrypoint\n')
        entrypoint.chmod(0o755)
        (package / 'package.json').write_text(json.dumps({'version': version}))
        replacement = binary.with_name('replacement')
        replacement.write_text('#!/bin/sh\necho codex-cli ' + version + '\n')
        replacement.chmod(0o755)
        replacement.replace(binary)

    def calls():
        p = tool / 'calls'
        return int(p.read_text()) if p.exists() else 0

    def wait(expected_calls, expected_status='active', previous_time=None):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            p = tool / 'state/auto.json'
            state = json.loads(p.read_text()) if p.exists() else {}
            if (calls() == expected_calls and state.get('status') == expected_status
                    and (previous_time is None or state.get('checked_at') != previous_time)):
                return state
            time.sleep(0.2)
        raise AssertionError(f'Trigger failed: calls={calls()}, state={state}')

    install('0.1.0')
    name = 'codex-style-watch-test-' + str(os.getpid())
    runtime = Path(os.environ['XDG_RUNTIME_DIR']) / 'systemd/user'
    runtime.mkdir(parents=True, exist_ok=True)
    service_unit = runtime / (name + '.service')
    assert not service_unit.exists()
    config = root / 'watch.json'
    config.write_text(json.dumps({'paths': [str(p) for p in (
        package.parent, package / 'package.json', binary.parent, binary, entrypoint,
        home / '.local/bin/codex', home / 'node/bin/codex')]}))
    service_unit.write_text('[Service]\nType=simple\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
                            f'Environment=PATH={home}/.local/bin:/usr/bin:/bin\n'
                            f'ExecStart=/usr/bin/python3 {root}/watch.py --config {config} --tool-dir {tool}'
                            ' --quiet-seconds 0.2 --wait-seconds 12\nTimeoutStartSec=30\n')
    try:
        systemctl('daemon-reload')
        systemctl('start', name + '.service')
        wait(1)
        print('PASS startup check and fake repair', flush=True)
        install('0.1.1')
        wait(2)
        print('PASS version update triggers repair', flush=True)
        install('0.1.1')
        wait(3)
        print('PASS same-version reinstall triggers repair', flush=True)
        package.rename(package.with_name('previous-codex'))
        install('0.1.2')
        wait(4)
        print('PASS complete package-directory replacement triggers repair', flush=True)
        (tool / 'fail').write_text('fail')
        install('0.1.3')
        failure = wait(5, 'needs_attention')
        time.sleep(1)
        p = package / 'package.json'
        p.write_text(p.read_text())
        wait(5, 'needs_attention', failure['checked_at'])
        print('PASS duplicate event after failure does not rebuild', flush=True)
        (tool / 'fail').unlink()
        install('0.1.4')
        wait(6)
        time.sleep(3)
        assert calls() == 6, 'Repair self-event caused another build'
        assert systemctl('is-active', name + '.service').stdout.strip() == 'active'
        print('PASS watcher remains active after failure; self-events do not rebuild', flush=True)
        print('Evidence: ' + str(root), flush=True)
    finally:
        journal = subprocess.run(['journalctl', '--user', '-u', name + '.service', '--no-pager'],
                                 capture_output=True, text=True, timeout=15)
        (root / 'journal.log').write_text(journal.stdout)
        systemctl('stop', name + '.service', check=False)
        systemctl('reset-failed', name + '.service', check=False)
        service_unit.unlink(missing_ok=True)
        systemctl('daemon-reload')


if __name__ == '__main__':
    main()
