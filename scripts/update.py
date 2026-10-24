#!/usr/bin/env python3
"""Check npm's stable Codex release and update this installation when newer."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
import maintain as maintenance

REGISTRY = 'https://registry.npmjs.org'


def discover_prefix():
    command = shutil.which('codex')
    if not command:
        raise RuntimeError('Codex is not on PATH')
    entry = Path(command).resolve()
    if entry.suffix != '.js' or len(entry.parents) < 6:
        raise RuntimeError('Scheduled updates require a standard global npm installation')
    prefix = entry.parents[5]
    if (prefix / 'lib/node_modules/@openai/codex/bin/codex.js').resolve() != entry:
        raise RuntimeError('Cannot identify the global npm prefix; use --prefix after inspecting the layout')
    return prefix


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise RuntimeError(f'Expected a stable version, received {value!r}')
    return tuple(map(int, value.split('.')))


def installed(prefix, tool):
    package = prefix / 'lib/node_modules/@openai/codex'
    entry = shutil.which('codex')
    if not entry or Path(entry).resolve() != (package / 'bin/codex.js').resolve():
        raise RuntimeError('Active Codex does not belong to the configured npm prefix; inspect installation paths')
    data = json.loads((package / 'package.json').read_text())
    if data.get('name') != '@openai/codex':
        raise RuntimeError('Configured directory is not the official Codex package')
    version_tuple(data['version'])
    helpers = maintenance.helpers(tool)
    binary = helpers['locate_codex_binary']()
    output = subprocess.check_output([str(binary), '--version'], text=True, timeout=20)
    match = helpers['VERSION_RE'].fullmatch(output.strip())
    if not match or match.group(1) != data['version']:
        raise RuntimeError('Codex package and executable versions differ; inspect before updating')
    return {'version': data['version'], 'binary': str(binary),
            'sha256': maintenance.digest(binary)}


def npm_command(prefix, *args):
    node = shutil.which('node')
    npm = shutil.which('npm')
    if not node or not npm or Path(npm).resolve().suffix != '.js':
        raise RuntimeError('Expected node and a standard npm JavaScript launcher on PATH')
    # Default npm fetch-timeout; retries above the npm default because the
    # platform binary tarball is large and optional-dependency fetch failures
    # are non-fatal for npm (exit 0 with the helper package silently missing).
    return [node, str(Path(npm).resolve()),
            *args, '--registry=' + REGISTRY, '--fetch-retries=3',
            '--update-notifier=false']


def platform_helper_name():
    """Expected @openai/codex-<platform> helper package name for this machine."""
    os_name = {'linux': 'linux', 'darwin': 'darwin', 'windows': 'win32'}.get(platform.system().lower())
    arch = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    if not os_name or not arch:
        return None
    return f'codex-{os_name}-{arch}'


def platform_helper_roots(prefix):
    """Package roots that may hold the vendor binary, mirroring the locator.

    Covers the package-local vendor layout, the nested helper package, and a
    hoisted sibling helper package; symlinked layouts resolve through them.
    """
    name = platform_helper_name()
    if name is None:
        return []
    package = prefix / 'lib/node_modules/@openai/codex'
    return [package, package / 'node_modules/@openai' / name, package.parent / name]


def platform_helper_ok(prefix):
    """True when a vendor binary for this platform is present somewhere expected."""
    roots = platform_helper_roots(prefix)
    if not roots:
        return True  # unknown platform: rely on the installed() verification instead
    for root in roots:
        vendor = root / 'vendor'
        if vendor.is_dir() and any(vendor.glob('*/bin/codex*')):
            return True
    return False


def latest(prefix, log):
    result = subprocess.run(npm_command(prefix, 'view', '@openai/codex', 'dist-tags.latest', '--json'),
                            capture_output=True, text=True, timeout=90)
    log.write(result.stderr)
    log.flush()
    if result.returncode:
        raise RuntimeError(f'npm stable-version lookup failed (exit {result.returncode}); inspect update.log')
    value = json.loads(result.stdout)
    version_tuple(value)
    return value


def backup_package(prefix, before, target, root):
    source = prefix / 'lib/node_modules/@openai/codex'
    destination = root / f'{before["version"]}_{datetime.now():%Y%m%d_%H%M%S_%f}_before_npm_update'
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source, destination / 'package', symlinks=True)
    hashes = {}
    links = {}
    for path in source.rglob('*'):
        name = str(path.relative_to(source))
        copy = destination / 'package' / name
        if path.is_symlink():
            links[name] = str(path.readlink())
            if not copy.is_symlink() or str(copy.readlink()) != links[name]:
                raise RuntimeError(f'Package backup link mismatch: {name}')
        elif path.is_file():
            hashes[name] = maintenance.digest(path)
            if maintenance.digest(copy) != hashes[name]:
                raise RuntimeError(f'Package backup checksum mismatch: {name}')
    manifest = {'before': before, 'target': target, 'source': str(source),
                'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                'sha256': hashes, 'symlinks': links}
    binary = Path(before['binary']).resolve()
    if not binary.is_relative_to(source.resolve()):
        # npm can hoist the platform package next to @openai/codex.
        copy = destination / 'external-codex'
        shutil.copy2(binary, copy)
        if maintenance.digest(copy) != before['sha256']:
            raise RuntimeError('External platform executable backup checksum mismatch')
        manifest['external_binary_backup'] = str(copy)
    maintenance.atomic_bytes(destination / 'backup.json', (json.dumps(manifest, indent=2) + '\n').encode())
    return destination


def install(prefix, target, log):
    command = npm_command(prefix, 'install', '--global', '--prefix', str(prefix),
                          '@openai/codex@' + target, '--no-audit', '--no-fund')
    for attempt in (1, 2):
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        if result.returncode:
            raise RuntimeError(f'npm update failed (exit {result.returncode}); inspect update.log')
        if platform_helper_ok(prefix):
            return
        log.write(f'platform helper package missing after npm install (attempt {attempt}); '
                  + ('retrying once\n' if attempt == 1 else 'giving up\n'))
        log.flush()
    raise RuntimeError('npm install finished without the platform binary package; inspect update.log')


def save(tool, state):
    maintenance.atomic_bytes(maintenance.helpers(tool)['STATE_DIR'] / 'update.json', (json.dumps(state, indent=2) + '\n').encode(), 0o600)


def update(prefix, tool, check_only=False):
    state = {'checked_at': datetime.now().astimezone().isoformat(timespec='seconds'),
             'check_only': check_only, 'registry': REGISTRY, 'status': 'checking'}
    helpers = maintenance.helpers(tool)
    log_path = helpers['STATE_DIR'] / 'update.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a') as log:
        log.write(f'\n--- Codex update check {state["checked_at"]} ---\n')
        log.flush()
        try:
            # The style repair subprocess waits for this same lock. It cannot
            # install a patched binary while npm is replacing the package.
            with maintenance.tool_lock(tool, wait=True):
                maintenance.require_no_transaction(helpers)
                before = installed(prefix, tool)
                target = latest(prefix, log)
                state.update(before=before, latest=target)
                if version_tuple(target) <= version_tuple(before['version']):
                    state['status'] = 'up_to_date' if target == before['version'] else 'local_newer'
                elif check_only:
                    state['status'] = 'update_available'
                else:
                    state['backup'] = str(backup_package(prefix, before, target, root=helpers['DATA_ROOT'] / 'npm-backups'))
                    if installed(prefix, tool) != before:
                        raise RuntimeError('Installation changed during backup; refusing to overwrite it')
                    state['status'] = 'installing'
                    save(tool, state)
                    install(prefix, target, log)
                    after = installed(prefix, tool)
                    state['after'] = after
                    if after['version'] != target:
                        raise RuntimeError('Installed Codex does not match the requested stable version')
                    subprocess.run([after['binary'], '--help'], check=True,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=20)
                    state.update(status='updated', style='Pending verification by codex-style-auto.service')
                save(tool, state)
                message = f'Codex installed={before["version"]}, latest={target}: {state["status"]}'
                log.write(message + '\n')
                print(message, flush=True)
                return 0
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
            state.update(status='failed', error=str(error))
            save(tool, state)
            log.write(str(error) + '\n')
            print(f'Codex update: {error}', file=sys.stderr, flush=True)
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, help='Global npm prefix; inferred from the active Codex when omitted')
    parser.add_argument('--tool-dir', type=Path, default=maintenance.DEFAULT_TOOL)
    parser.add_argument('--check-only', action='store_true', help='Query and record versions without installing')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        prefix = args.prefix.expanduser().resolve() if args.prefix else discover_prefix()
        return update(prefix, args.tool_dir, args.check_only)
    except (OSError, RuntimeError) as error:
        print(f'Codex update: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
