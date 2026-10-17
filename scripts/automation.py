#!/usr/bin/env python3
"""Explicitly install user-level event watching and an optional daily updater."""

import argparse
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
import maintain as maintenance
import update as updater

PROXY_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
              'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
              'SSL_CERT_FILE', 'SSL_CERT_DIR', 'GIT_SSL_CAINFO')


def one_line(value):
    if any(char in value for char in ('\n', '\r', '\0')):
        raise ValueError('Configuration values must not contain newlines or NUL')
    return value


def unit_quote(value, command=False):
    value = one_line(str(value)).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def exec_line(args):
    return 'ExecStart=' + ' '.join(unit_quote(arg, command=True) for arg in args) + '\n'


def environment_file_line(path):
    # EnvironmentFile takes one raw path/glob, unlike the quoted word lists
    # accepted by ExecStart and Environment. Spaces are preserved by systemd.
    value = glob.escape(one_line(str(path))).replace('%', '%%')
    return 'EnvironmentFile=-' + value + '\n'


def watch_paths(tool):
    command = shutil.which('codex')
    if not command:
        raise RuntimeError('Codex is not on PATH')
    path = Path(command).absolute()
    paths = set()
    for _ in range(16):
        paths.update((path, path.parent))
        if not path.is_symlink():
            break
        target = path.readlink()
        path = target if target.is_absolute() else path.parent / target
        path = Path(os.path.abspath(path))
    else:
        raise RuntimeError('Codex launcher has too many symlink levels')
    entry = Path(command).resolve()
    if entry.suffix == '.js':
        package = entry.parent.parent
        paths.update((package / 'package.json', package.parent))
    binary = maintenance.helpers(tool)['locate_codex_binary']()
    paths.update((binary, binary.parent))
    return sorted(str(p) for p in paths)


def build_files(tool, config_dir, unit_dir, daily=False,
                calendar='*-*-* 08:00:00', inherit_proxy=False):
    m = maintenance.helpers(tool)
    maintenance.require_no_transaction(m)
    runtime = m['DATA_ROOT']
    paths = watch_paths(tool)
    scripts = Path(__file__).resolve().parent
    python = Path(sys.executable).resolve()
    environment = ('Environment=' + unit_quote('PATH=' + os.environ.get('PATH', os.defpath)) + '\n'
                   'Environment=' + unit_quote('CODEX_STYLE_STATE_DIR=' + str(runtime)) + '\n'
                   'Environment=PYTHONDONTWRITEBYTECODE=1\n'
                   + environment_file_line(config_dir / 'environment')
                   + 'Nice=10\nUMask=0077\nStandardOutput=journal\nStandardError=journal\n')
    for key in ('CARGO_HOME', 'RUSTUP_HOME'):
        if key in os.environ:
            environment += 'Environment=' + unit_quote(key + '=' + os.environ[key]) + '\n'
    watch = ('[Unit]\nDescription=Restore Codex colors after installation changes\n'
             'StartLimitIntervalSec=300\nStartLimitBurst=10\n\n'
             '[Service]\nType=simple\n' + environment
             + exec_line([python, scripts / 'watch.py', '--config', config_dir / 'watch.json',
                          '--tool-dir', tool])
             + 'Restart=on-failure\nRestartSec=5\nKillMode=control-group\n\n'
             '[Install]\nWantedBy=default.target\n')
    files = {config_dir / 'watch.json': (json.dumps({'paths': paths}, indent=2) + '\n'),
             unit_dir / 'codex-style-auto.service': watch}
    if inherit_proxy:
        lines = []
        for key in PROXY_KEYS:
            if key in os.environ:
                value = one_line(os.environ[key]).replace('\\', '\\\\').replace('"', '\\"')
                lines.append(f'{key}="{value}"\n')
        files[config_dir / 'environment'] = ''.join(lines)
    if daily:
        prefix = updater.discover_prefix()
        updater.installed(prefix, tool)
        updater.npm_command(prefix, '--version')  # Check the launchers, without running npm.
        files[unit_dir / 'codex-update.service'] = (
            '[Unit]\nDescription=Check and install a newer stable Codex CLI\n'
            'Requires=codex-style-auto.service\nAfter=codex-style-auto.service\n\n'
            '[Service]\nType=oneshot\n' + environment
            + exec_line([python, scripts / 'update.py', '--prefix', prefix, '--tool-dir', tool])
            + 'TimeoutStartSec=infinity\n')
        files[unit_dir / 'codex-update.timer'] = (
            '[Unit]\nDescription=Daily stable Codex update check\n\n[Timer]\n'
            'OnCalendar=' + one_line(calendar) + '\nAccuracySec=1s\nRandomizedDelaySec=0\n'
            'Persistent=true\nUnit=codex-update.service\n\n[Install]\nWantedBy=timers.target\n')
    return files


def write_new_files(files):
    # Check every destination before writing any; never overwrite an existing setup.
    for path, value in files.items():
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_text() != value)):
            raise RuntimeError(f'Existing configuration differs; preserve and inspect it first: {path}')
    created = []
    try:
        for path, value in files.items():
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(value)
            created.append(path)
    except BaseException:
        for path in created:
            path.unlink()
        raise
    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install',))
    parser.add_argument('--daily-update', action='store_true', help='Also authorize automatic stable npm upgrades')
    parser.add_argument('--calendar', default='*-*-* 08:00:00', help='systemd calendar; default 08:00 local time')
    parser.add_argument('--inherit-proxy', action='store_true', help='Copy only current proxy/certificate variables to a private service file')
    parser.add_argument('--tool-dir', type=Path, default=maintenance.DEFAULT_TOOL)
    args = parser.parse_args()
    os.umask(0o077)
    if not sys.platform.startswith('linux') or not hasattr(os, 'pidfd_open'):
        raise RuntimeError('Automation requires Linux/WSL with pidfd_open support')
    subprocess.run(['systemctl', '--user', 'show-environment'], check=True, stdout=subprocess.DEVNULL)
    maintenance.helpers(args.tool_dir)['ensure_tools']()
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser().resolve()
    if args.daily_update:
        subprocess.run(['systemd-analyze', 'calendar', one_line(args.calendar)], check=True)
    files = build_files(args.tool_dir.resolve(), base / 'codex-style', base / 'systemd/user',
                        args.daily_update, args.calendar, args.inherit_proxy)
    with tempfile.TemporaryDirectory(prefix='codex-style-units-') as tmp:
        staged = []
        for path, value in files.items():
            if path.suffix in ('.service', '.timer'):
                stage = Path(tmp) / path.name
                stage.write_text(value)
                staged.append(str(stage))
        result = subprocess.run(['systemd-analyze', '--user', 'verify', *staged],
                                capture_output=True, text=True)
        if result.returncode or result.stderr.strip():
            raise RuntimeError('systemd unit validation failed: ' + result.stderr.strip())
    write_new_files(files)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'codex-style-auto.service'], check=True)
    if args.daily_update:
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'codex-update.timer'], check=True)
    print('Event watching enabled' + ('; daily stable updates enabled' if args.daily_update else ''), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f'Codex automation: {error}', file=sys.stderr)
        sys.exit(1)
