#!/usr/bin/env python3
"""Repair colors after an installation change; invoked by the event watcher."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import maintain as maintenance


def stamp(path):
    st = Path(path).stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def observe(tool):
    helpers = maintenance.helpers(tool)
    maintenance.require_no_transaction(helpers)
    command = shutil.which('codex')
    if not command:
        raise RuntimeError('Codex command is unavailable while installation is changing')
    entrypoint = Path(command).resolve()
    binary = helpers['locate_codex_binary']()
    before = stamp(binary)
    result = subprocess.run([str(binary), '--version'], check=True,
                            capture_output=True, text=True, timeout=15)
    match = helpers['VERSION_RE'].fullmatch(result.stdout.strip())
    if not match:
        raise RuntimeError('Codex does not yet report a supported version')
    version = match.group(1)
    if entrypoint.suffix == '.js':
        package = json.loads((entrypoint.parent.parent / 'package.json').read_text())
        if package['version'] != version:
            raise RuntimeError('Package and executable versions differ; update is in progress')
    binary_hash = maintenance.digest(binary)
    patch_hash = maintenance.digest(helpers['patch_path']())
    if before != stamp(binary):
        raise RuntimeError('Executable changed while checking it')
    identity = {'entrypoint': str(entrypoint), 'binary': str(binary),
                'version': version, 'binary_stat': before,
                'binary_sha256': binary_hash, 'patch_sha256': patch_hash}
    state = helpers['load_state'](version)
    active = bool(state and state.get('custom_sha256') == binary_hash
                  and state.get('patch_sha256') == patch_hash)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    restored = bool(state and state.get('active') is False
                    and state.get('backup_sha256') == binary_hash)
    return {'key': key, 'identity': identity, 'active': active, 'restored': restored}


def wait_stable(observer, quiet=10, timeout=180, interval=2,
                clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    previous = None
    since = clock()
    error = 'Installation is still changing'
    while clock() <= deadline:
        try:
            current = observer()
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            previous = None
            since = clock()
            error = str(exc)
        else:
            if previous is None or current['key'] != previous['key']:
                since = clock()
            previous = current
            if clock() - since >= quiet:
                return current
        sleep(interval)
    raise RuntimeError(f'No stable Codex installation within {timeout:g}s: {error}')


def save_state(path, state):
    state['checked_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    maintenance.atomic_bytes(path, (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode())


def run_repair(tool, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a') as handle:
        handle.write(f'\n--- Automatic repair {datetime.now().astimezone().isoformat()} ---\n')
        handle.flush()
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('maintain.py')),
                                 'repair', '--tool-dir', str(tool), '--wait-lock'],
                                stdout=handle, stderr=subprocess.STDOUT)
    return result.returncode


def repair_changes(tool, stable, repair, retry=False):
    """An interrupted or failed installation is not retried blindly."""
    state_path = maintenance.helpers(tool)['STATE_DIR'] / 'auto.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    for _ in range(3):
        current = stable()
        state['installation'] = current
        if current.get('restored'):
            state.update(status='restored', message='Explicit same-version rollback preserved; no automatic repair')
            save_state(state_path, state)
            print(state['message'], flush=True)
            return 0
        if current['active']:
            state.update(status='active', message='Custom colors already active; no build needed')
            save_state(state_path, state)
            print(state['message'], flush=True)
            return 0
        attempt = state.get('attempt', {})
        if (not retry and attempt.get('key') == current['key']
                and attempt.get('status') in ('running', 'failed')):
            state.update(status='needs_attention',
                         message='Previous repair failed or was interrupted; inspect auto.log before retrying')
            save_state(state_path, state)
            print(state['message'], file=sys.stderr, flush=True)
            return 1
        retry = False
        state['attempt'] = {'key': current['key'], 'status': 'running',
                            'version': current['identity']['version'],
                            'started_at': datetime.now().astimezone().isoformat(timespec='seconds')}
        state.update(status='repairing', message='Installation changed; starting verified color repair')
        save_state(state_path, state)
        print(state['message'], flush=True)
        try:
            code = repair()
            after = stable()
        except BaseException as exc:
            state['attempt'].update(status='failed', error=str(exc))
            state.update(status='needs_attention', message='Automatic repair could not complete')
            save_state(state_path, state)
            raise
        state['attempt']['returncode'] = code
        state['installation'] = after
        if after['active']:
            state['attempt']['status'] = 'completed' if code == 0 else 'active_after_error'
            state.update(status='active', message='Custom colors active; restart Codex to load them')
            save_state(state_path, state)
            print(state['message'], flush=True)
            return 0
        if after['key'] != current['key']:
            state['attempt']['status'] = 'superseded'
            state.update(status='changed_again', message='A newer installation appeared; checking it next')
            save_state(state_path, state)
            continue
        state['attempt']['status'] = 'failed'
        state.update(status='needs_attention',
                     message=f'Repair did not activate colors (exit {code}); inspect auto.log')
        save_state(state_path, state)
        print(state['message'], file=sys.stderr, flush=True)
        return 1
    state.update(status='needs_attention', message='Installation changed repeatedly; inspect before retrying')
    save_state(state_path, state)
    print(state['message'], file=sys.stderr, flush=True)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool-dir', type=Path, default=maintenance.DEFAULT_TOOL)
    parser.add_argument('--quiet-seconds', type=float, default=10)
    parser.add_argument('--wait-seconds', type=float, default=180)
    parser.add_argument('--retry', action='store_true', help='Retry after diagnosing the recorded failure')
    args = parser.parse_args()
    os.umask(0o077)
    if args.quiet_seconds < 0 or args.wait_seconds <= args.quiet_seconds:
        parser.error('wait-seconds must exceed quiet-seconds >= 0')
    stable = lambda: wait_stable(lambda: observe(args.tool_dir), args.quiet_seconds, args.wait_seconds)
    try:
        # A separate lock also protects against a manual auto.py invocation.
        # The repair subprocess uses maintain.py's existing maintenance lock.
        with maintenance.tool_lock(args.tool_dir, name='automatic-trigger'):
            return repair_changes(args.tool_dir, stable,
                                  lambda: run_repair(args.tool_dir, maintenance.helpers(args.tool_dir)['STATE_DIR'] / 'auto.log'),
                                  args.retry)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Automatic color repair: {exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
