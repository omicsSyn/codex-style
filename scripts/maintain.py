#!/usr/bin/env python3
"""Maintain version-matched Codex transcript colors with a bundled, portable helper."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib

sys.dont_write_bytecode = True
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOOL = SKILL_ROOT / 'scripts/tool'
FILES = ('codex-rs/tui/src/markdown_render.rs',
         'codex-rs/tui/src/markdown_render_tests.rs')
STYLE_FIELDS = {
    'h1': ('Style::new().bold().underlined()', 'Style::new().light_red().bold().underlined()'),
    'h2': ('Style::new().bold()', 'Style::new().magenta().bold()'),
    'h3': ('Style::new().bold().italic()', 'Style::new().yellow().bold().italic()'),
    'h4': ('Style::new().italic()', 'Style::new().cyan().bold().italic()'),
    'h5': ('Style::new().italic()', 'Style::new().blue().bold().italic()'),
    'h6': ('Style::new().italic()', 'Style::new().light_cyan().bold().italic()'),
    'strong': ('Style::new().bold()', 'Style::new().fg(Color::Rgb(209, 154, 102)).bold()'),
}
REQUIRED_TESTS = ('headings', 'blockquote_heading_inherits_heading_style',
                  'strong', 'strong_emphasis')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic_bytes(path, value, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_job(work, job):
    atomic_bytes(Path(work) / 'job.json',
                 (json.dumps(job, ensure_ascii=False, indent=2) + '\n').encode())


def load_job(work):
    return json.loads((Path(work) / 'job.json').read_text())


def command(args, cwd=None, log=None, env=None):
    if log:
        with Path(log).open('w') as handle:
            result = subprocess.run(args, cwd=cwd, env=env, stdout=handle,
                                    stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f'Command failed ({result.returncode}); inspect {log}')
        return ''
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or f'Command failed: {args[0]}')
    return result.stdout


def helpers(tool):
    module = runpy.run_path(str(Path(tool) / 'codex-style'), run_name='codex_style_helpers')
    if module['DATA_ROOT'].is_relative_to(SKILL_ROOT):
        raise RuntimeError('Runtime state must be outside the skill checkout')
    return module


def transaction_path(m):
    return m['DATA_ROOT'] / 'transaction.json'


def require_no_transaction(m):
    if transaction_path(m).exists():
        raise RuntimeError('Incomplete operation; run python3 scripts/maintain.py recover with the same runtime directory')


def bound_job(work):
    job = load_job(work)
    m = helpers(job['tool'])
    if not job.get('runtime_root'):
        raise RuntimeError('Job predates runtime-directory binding; preserve it and prepare a new job')
    if job['runtime_root'] != str(m['DATA_ROOT']):
        raise RuntimeError('Job runtime directory differs from the current environment; restore the original setting')
    require_no_transaction(m)
    return job


def begin_transaction(m, operation, binary, version, backup, before_sha, after_sha, work=None):
    """Persist everything needed to undo an interrupted mutation before it starts."""
    require_no_transaction(m)
    paths = [m['ACTIVE_PATCH_FILE'], m['state_path'](version)]
    if work is not None:
        paths.append(Path(work).resolve() / 'job.json')
    saved = {}
    for path in paths:
        value = path.read_bytes() if path.exists() else None
        saved[str(path)] = None if value is None else {
            'data': base64.b64encode(value).decode('ascii'),
            'sha256': hashlib.sha256(value).hexdigest(), 'mode': path.stat().st_mode & 0o777,
        }
    if digest(backup) != before_sha or digest(binary) != before_sha:
        raise RuntimeError('Pre-operation image changed; refusing to create recovery journal')
    record = {'schema': 1, 'operation': operation, 'runtime_root': str(m['DATA_ROOT']),
              'binary': str(binary), 'version': version, 'backup': str(backup),
              'before_sha256': before_sha, 'after_sha256': after_sha,
              'work': str(Path(work).resolve()) if work is not None else None, 'files': saved}
    atomic_bytes(transaction_path(m), (json.dumps(record, indent=2) + '\n').encode(), 0o600)


def finish_transaction(m):
    transaction_path(m).unlink()
    sync_directory(m['DATA_ROOT'])


def recover(tool):
    """Restore the pre-operation image; remain retryable if recovery is interrupted."""
    m = helpers(tool)
    path = transaction_path(m)
    if not path.exists():
        return {'phase': 'no_recovery_needed', 'changed': False}
    record = json.loads(path.read_text())
    if (record.get('schema') != 1 or record.get('operation') not in ('install', 'rollback')
            or record.get('runtime_root') != str(m['DATA_ROOT'])):
        raise RuntimeError('Recovery journal does not match this runtime directory or schema')
    binary = m['locate_codex_binary']()
    version = record['version']
    if str(binary) != record['binary']:
        raise RuntimeError('Recovery binary path differs from the current installation')
    backup = Path(record['backup'])
    if (digest(backup) != record['before_sha256'] or m['codex_version'](backup) != version):
        raise RuntimeError('Recovery backup is missing, changed, or has the wrong version')
    if digest(binary) not in (record['before_sha256'], record['after_sha256']):
        raise RuntimeError('Installed Codex changed outside this transaction; refusing recovery')
    expected = {m['ACTIVE_PATCH_FILE'], m['state_path'](version)}
    job_path = Path(record['work']) / 'job.json' if record.get('work') else None
    if job_path is not None:
        expected.add(job_path)
    if set(record['files']) != {str(p) for p in expected}:
        raise RuntimeError('Recovery metadata paths do not match the recorded operation')
    # Decode and check all metadata before touching the executable.
    saved = {}
    for name, value in record['files'].items():
        data = base64.b64decode(value['data'], validate=True) if value is not None else None
        if value is not None and hashlib.sha256(data).hexdigest() != value['sha256']:
            raise RuntimeError('Recovery metadata checksum mismatch')
        saved[Path(name)] = None if data is None else (data, value['mode'])
    if digest(binary) != record['before_sha256']:
        m['install_binary'](backup, binary, version)
    if digest(binary) != record['before_sha256']:
        raise RuntimeError('Recovery binary checksum mismatch; preserve the journal and backups')
    for target, value in saved.items():
        if target == job_path and not target.parent.exists():
            continue  # Temporary source jobs can expire; executable/state recovery still works.
        if value is None:
            target.unlink(missing_ok=True)
            if target.parent.exists():
                sync_directory(target.parent)
        else:
            atomic_bytes(target, *value)
    finish_transaction(m)
    return {'phase': 'recovered', 'operation': record['operation'], 'version': version}


def active(m):
    binary = m['locate_codex_binary']()
    version = m['codex_version'](binary)
    state = m['load_state'](version)
    return bool(state and digest(binary) == state.get('custom_sha256')
                and digest(m['patch_path']()) == state.get('patch_sha256'))


def snapshot(tool, purpose, root=None, skill=SKILL_ROOT):
    m = helpers(tool)
    root = Path(root) if root is not None else m['DATA_ROOT'] / 'snapshots'
    for source in (skill, tool, m['STATE_DIR']):
        if root.resolve().is_relative_to(Path(source).resolve()):
            raise RuntimeError('Snapshot destination must be outside the directories being copied')
    version = re.search(r'version:\s*"([^"]+)"', (skill / 'SKILL.md').read_text())
    label = version.group(1) if version else 'unknown'
    target = root / f'{label}_{datetime.now():%Y%m%d_%H%M%S_%f}_before_repair'
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(skill, target / 'skill', symlinks=True)
    shutil.copytree(tool, target / 'tool', symlinks=True)
    if m['STATE_DIR'].is_dir():
        shutil.copytree(m['STATE_DIR'], target / 'runtime-state', symlinks=True)
    if m['ACTIVE_PATCH_FILE'].is_file():
        shutil.copy2(m['ACTIVE_PATCH_FILE'], target / 'active.patch')
    if transaction_path(m).is_file():
        shutil.copy2(transaction_path(m), target / 'transaction.json')
    hashes = {str(p.relative_to(target)): digest(p)
              for p in target.rglob('*') if p.is_file() and not p.is_symlink()}
    manifest = {'skill_version': label, 'created_at': datetime.now().astimezone().isoformat(),
                'purpose': purpose, 'skill': str(skill), 'tool': str(tool), 'sha256': hashes}
    atomic_bytes(target / 'backup.json', (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode())
    check_snapshot(target)
    return target


def check_snapshot(target):
    target = Path(target)
    manifest = json.loads((target / 'backup.json').read_text())
    for name, expected in manifest['sha256'].items():
        if digest(target / name) != expected:
            raise RuntimeError(f'Backup checksum mismatch: {name}')


@contextmanager
def tool_lock(tool, wait=False, name='maintenance'):
    root = helpers(tool)['DATA_ROOT']
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if name not in ('maintenance', 'automatic-trigger'):
        raise ValueError('Unsupported lock name')
    path = root / (name + '.lock')
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise RuntimeError('Another codex-style maintenance operation is running') from error
        yield


def prepare(tool, work=None):
    m = helpers(tool)
    require_no_transaction(m)
    m['ensure_tools']()
    binary = m['locate_codex_binary']()
    version = m['codex_version'](binary)
    work = Path(work) if work else Path(tempfile.mkdtemp(prefix=f'codex-style-work-{version}-'))
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        if not (work / 'job.json').is_file():
            raise RuntimeError('Existing directory is not a maintenance job; preserve it and select a new directory')
        job = bound_job(work)
        if (job.get('phase') != 'preparing' or job.get('tool') != str(Path(tool).resolve())
                or job.get('version') != version or job.get('binary') != str(binary)
                or job.get('starting_sha256') != digest(binary)):
            raise RuntimeError('Existing job cannot resume preparation; use its recorded stage or prepare a new version')
        check_snapshot(job['snapshot'])
        if (work / 'source.log').exists():
            shutil.copy2(work / 'source.log', work / f'source_failed_{datetime.now():%Y%m%d_%H%M%S_%f}.log')
    else:
        backup = snapshot(tool, f'Automatic color maintenance for Codex {version}')
        job = {'version': version, 'tool': str(Path(tool).resolve()),
               'runtime_root': str(m['DATA_ROOT']),
               'binary': str(binary), 'starting_sha256': digest(binary),
               'snapshot': str(backup), 'phase': 'preparing'}
    save_job(work, job)
    print(json.dumps({'work_dir': str(work), 'phase': 'preparing'}), flush=True)
    try:
        source = work / 'source'
        if source.exists():
            if (not (source / '.git').exists()
                    or command(['git', 'remote', 'get-url', 'origin'], source).strip() != m['REPOSITORY']
                    or command(['git', 'status', '--porcelain'], source).strip()
                    or command(['git', 'rev-parse', 'HEAD'], source).strip()
                    != command(['git', 'rev-parse', f'refs/tags/rust-v{version}^{{commit}}'], source).strip()):
                raise RuntimeError('Partial or edited checkout retained; inspect it before retrying or select a fresh directory')
        else:
            command(['git', '-c', 'http.version=HTTP/1.1', '-c', 'advice.detachedHead=false',
                     'clone', '--quiet', '--depth', '1', '--branch', f'rust-v{version}',
                     m['REPOSITORY'], str(source)], log=work / 'source.log')
        job['source_commit'] = command(['git', 'rev-parse', 'HEAD'], work / 'source').strip()
        source_version = tomllib.loads((work / 'source/codex-rs/Cargo.toml').read_text())['workspace']['package']['version']
        if source_version != version:
            raise RuntimeError('Official source version does not match installed Codex')
        job['phase'] = 'prepared'
        job.pop('failure', None)
        save_job(work, job)
        return work
    except BaseException as error:
        job['failure'] = {'stage': 'prepare', 'error': str(error)}
        save_job(work, job)
        raise


def rewrite_styles(text):
    """Recognize a known constructor; leave unfamiliar style semantics untouched."""
    pattern = r'(impl MarkdownStyles\s*\{\s*fn for_theme\([^\n]*\) -> Self\s*\{\s*Self\s*\{)(.*?)(\n\s*\}\s*\n\s*\}\s*\n\s*\})'
    match = re.search(pattern, text, re.S)
    if not match:
        raise RuntimeError('Unknown MarkdownStyles constructor; inspect source before adapting')
    body = match.group(2)
    for field, (old, new) in STYLE_FIELDS.items():
        field_pattern = re.compile(r'^(\s*' + field + r':\s*)([^\n]+),[ \t]*$', re.M)
        fields = list(field_pattern.finditer(body))
        if len(fields) != 1:
            raise RuntimeError(f'Expected one {field} field; inspect source before adapting')
        observed = re.sub(r'\s+', '', fields[0].group(2))
        if observed not in (re.sub(r'\s+', '', old), re.sub(r'\s+', '', new)):
            raise RuntimeError(f'Unfamiliar {field} semantics; inspect source before adapting')
        body = field_pattern.sub(lambda m: m.group(1) + new + ',', body, count=1)
    text = text[:match.start(2)] + body + text[match.end(2):]
    if 'use ratatui::style::Color;' not in text:
        anchor = 'use ratatui::style::Style;'
        if text.count(anchor) != 1:
            raise RuntimeError('Cannot locate ratatui Color import safely')
        text = text.replace(anchor, 'use ratatui::style::Color;\n' + anchor)
    return text


def capture(work):
    work = Path(work)
    job = bound_job(work)
    source = work / 'source'
    validate_source(work)
    patch = command(['git', 'diff', 'HEAD', '--', *FILES], source).encode()
    if not patch:
        raise RuntimeError('No renderer changes found')
    atomic_bytes(work / 'candidate.patch', patch)
    job.pop('validation', None)
    job.pop('failure', None)
    job['patch_sha256'] = digest(work / 'candidate.patch')
    job['phase'] = 'adapted'
    save_job(work, job)


def adapt(work):
    work = Path(work)
    job = bound_job(work)
    source = work / 'source'
    validate_source(work)
    changed = source_changes(source)
    if changed - {'codex-rs/Cargo.lock'}:
        raise RuntimeError('Source already edited; use capture to preserve manual repairs')
    patch_path = helpers(job['tool'])['patch_path']()
    try:
        command(['git', 'apply', '--check', str(patch_path)], source)
    except RuntimeError as error:
        atomic_bytes(work / 'patch-failure.log', (str(error) + '\n').encode())
        renderer = source / FILES[0]
        original = renderer.read_text()
        updated = rewrite_styles(original)
        sections = re.split(r'(?=^diff --git )', patch_path.read_text(), flags=re.M)
        tests = [s for s in sections if s.startswith(f'diff --git a/{FILES[1]} b/{FILES[1]}\n')]
        if len(tests) != 1:
            raise RuntimeError('Cannot isolate known renderer tests; inspect tests before adapting')
        test_patch = work / 'tests.patch'
        atomic_bytes(test_patch, tests[0].encode())
        command(['git', 'apply', '--check', str(test_patch)], source)
        atomic_bytes(renderer, updated.encode(), renderer.stat().st_mode)
        command(['git', 'apply', str(test_patch)], source)
        job['adaptation'] = 'Rebuilt recognized style constructor and preserved upstream surrounding code'
    else:
        command(['git', 'apply', str(patch_path)], source)
        job['adaptation'] = 'Existing patch applied'
    save_job(work, job)
    capture(work)


def check_lockfile(source, version):
    before = tomllib.loads(command(['git', 'show', 'HEAD:codex-rs/Cargo.lock'], source))
    after = tomllib.loads((source / 'codex-rs/Cargo.lock').read_text())
    expected = json.loads(json.dumps(before))
    for entry in expected['package']:
        if entry.get('version') == '0.0.0' and 'source' not in entry:
            entry['version'] = version
    if after != before and after != expected:
        raise RuntimeError('Cargo.lock changed external dependencies or workspace dependency structure')


def source_changes(source):
    return (set(command(['git', 'diff', 'HEAD', '--name-only'], source).splitlines())
            | set(command(['git', 'diff', '--cached', '--name-only'], source).splitlines()))


def validate_source(work):
    work = Path(work)
    job = bound_job(work)
    source = work / 'source'
    if command(['git', 'rev-parse', 'HEAD'], source).strip() != job['source_commit']:
        raise RuntimeError('Source commit changed; prepare a new job')
    if source_changes(source) - set(FILES) - {'codex-rs/Cargo.lock'}:
        raise RuntimeError('Unexpected source changes outside reviewed renderer files')
    if command(['git', 'ls-files', '--others', '--exclude-standard'], source).strip():
        raise RuntimeError('Untracked source files require review before validation')
    check_lockfile(source, job['version'])


def assert_candidate(work):
    work = Path(work)
    validate_source(work)
    job = load_job(work)
    patch = command(['git', 'diff', 'HEAD', '--', *FILES], work / 'source').encode()
    if (hashlib.sha256(patch).hexdigest() != job['patch_sha256']
            or digest(work / 'candidate.patch') != job['patch_sha256']):
        raise RuntimeError('Candidate changed; capture it again before verification')


def fingerprint(work):
    source = Path(work) / 'source'
    validate_source(work)
    content = [command(['git', 'rev-parse', 'HEAD'], source),
               command(['git', 'diff', 'HEAD', '--binary'], source),
               command(['git', 'diff', '--cached', '--binary'], source)]
    return hashlib.sha256(json.dumps(content).encode()).hexdigest()


def verify(work):
    work = Path(work)
    job = bound_job(work)
    if job.get('phase') not in ('adapted', 'verified'):
        raise RuntimeError('Capture an adapted candidate before verification')
    job.pop('validation', None)
    save_job(work, job)
    source = work / 'source'
    rust = source / 'codex-rs'
    assert_candidate(work)
    environment = os.environ.copy()
    environment.update({'CARGO_TERM_COLOR': 'never', 'RUST_MIN_STACK': '8388608',
                        'NEXTEST_PROFILE': 'local'})
    if environment.get('CARGO_TARGET_DIR'):
        raise RuntimeError('CARGO_TARGET_DIR is set; select an explicit build output path before continuing')
    command(['cargo', 'fmt', '--all', '--', '--check'], rust, work / 'format.log', environment)
    command(['just', 'test', '-p', 'codex-tui', 'markdown_render_tests'], rust, work / 'tests.log', environment)
    tests = (work / 'tests.log').read_text()
    summary = re.search(r'Summary[^\n]*?(\d+) tests run:\s*(\d+) passed', tests)
    if not summary or int(summary[1]) < len(REQUIRED_TESTS) or summary[1] != summary[2]:
        raise RuntimeError('Renderer tests did not report a complete nonempty pass')
    for name in REQUIRED_TESTS:
        if not re.search(r'PASS[^\n]*markdown_render_tests::' + re.escape(name) + r'(?:\s|$)', tests):
            raise RuntimeError(f'Required renderer test was not actually run: {name}')
    check_lockfile(source, job['version'])
    assert_candidate(work)
    before_build = fingerprint(work)
    command(['cargo', 'build', '-p', 'codex-cli', '--release', '--locked'], rust, work / 'build.log', environment)
    if fingerprint(work) != before_build:
        raise RuntimeError('Source changed during build; recapture and verify')
    assert_candidate(work)
    candidate = rust / 'target/release/codex'
    command(['strip', str(candidate)])
    m = helpers(job['tool'])
    if m['codex_version'](candidate) != job['version']:
        raise RuntimeError('Build version does not match target version')
    command([str(candidate), '--help'])
    job['validation'] = {'source_sha256': before_build, 'binary_sha256': digest(candidate),
                         'patch_sha256': job['patch_sha256'], 'tests_run': int(summary[1]),
                         'tests_passed': int(summary[2]), 'format': 'passed',
                         'version': 'passed', 'startup': 'passed'}
    job['phase'] = 'verified'
    job.pop('failure', None)
    save_job(work, job)


def install(work):
    work = Path(work)
    job = bound_job(work)
    validation = job.get('validation')
    if job.get('phase') != 'verified' or not validation:
        raise RuntimeError('Installation requires successful verification')
    assert_candidate(work)
    check_snapshot(job['snapshot'])
    if fingerprint(work) != validation['source_sha256']:
        raise RuntimeError('Source changed after verification')
    candidate = work / 'source/codex-rs/target/release/codex'
    if digest(candidate) != validation['binary_sha256'] or digest(work / 'candidate.patch') != validation['patch_sha256']:
        raise RuntimeError('Candidate changed after verification')
    m = helpers(job['tool'])
    binary = m['locate_codex_binary']()
    version = m['codex_version'](binary)
    if str(binary) != job['binary'] or version != job['version'] or digest(binary) != job['starting_sha256']:
        raise RuntimeError('Installed Codex changed during maintenance; prepare against the new version')
    if m['codex_version'](candidate) != version:
        raise RuntimeError('Candidate version mismatch')
    previous = m['load_state'](version)
    recovery = snapshot(Path(job['tool']), f'Before installing verified Codex {version}')
    preinstall, preinstall_sha = m['create_backup'](binary, version)
    rollback = Path(previous['backup_path']) if previous and previous.get('backup_path') else preinstall
    if not rollback.is_file() or m['codex_version'](rollback) != version:
        raise RuntimeError('Same-version rollback backup is unavailable')
    rollback_sha = digest(rollback)
    if previous and rollback_sha != previous.get('backup_sha256'):
        raise RuntimeError('Rollback backup checksum mismatch')
    patch_path = m['ACTIVE_PATCH_FILE']
    if digest(binary) != job['starting_sha256'] or digest(preinstall) != preinstall_sha:
        raise RuntimeError('Pre-install image changed during backup')
    begin_transaction(m, 'install', binary, version, preinstall, preinstall_sha,
                      validation['binary_sha256'], work)
    try:
        m['install_binary'](candidate, binary, version)
        if digest(binary) != validation['binary_sha256']:
            raise RuntimeError('Installed binary checksum mismatch')
        command([str(binary), '--help'])
        atomic_bytes(patch_path, (work / 'candidate.patch').read_bytes(), 0o600)
        state = {'version': version, 'active': True,
                 'applied_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                 'installed_binary': str(binary), 'custom_sha256': digest(binary),
                 'patch_sha256': digest(patch_path), 'backup_path': str(rollback),
                 'backup_sha256': rollback_sha, 'palette': m['PALETTE'],
                 'source_commit': job['source_commit'], 'validation': validation,
                 'skill_backup': job['snapshot'], 'preinstall_tool_backup': str(recovery),
                 'preinstall_binary_backup': str(preinstall)}
        m['write_state'](version, state)
        if not active(m):
            raise RuntimeError('Installed state does not report active colors')
        job['phase'] = 'installed'
        job.pop('failure', None)
        job['recovery'] = {'tool': str(recovery), 'binary': str(preinstall)}
        save_job(work, job)
    except BaseException:
        recover(job['tool'])
        raise
    finish_transaction(m)
    return {'version': version, 'phase': 'installed', 'tests_passed': validation['tests_passed'],
            'skill_backup': job['snapshot'], 'work_dir': str(work)}


def rollback(tool):
    """Restore only the tracked same-version image, preserving a recovery copy."""
    m = helpers(tool)
    require_no_transaction(m)
    binary = m['locate_codex_binary']()
    version = m['codex_version'](binary)
    state = m['load_state'](version)
    if not state:
        raise RuntimeError('No tracked installation for the current Codex version')
    backup = Path(state['backup_path'])
    if (not backup.is_file() or digest(backup) != state['backup_sha256']
            or m['codex_version'](backup) != version):
        raise RuntimeError('Same-version rollback backup is missing or changed')
    current = digest(binary)
    if current == state['backup_sha256']:
        return {'version': version, 'phase': 'already_restored', 'changed': False}
    if current != state['custom_sha256']:
        raise RuntimeError('Installed Codex changed; refusing to overwrite an untracked update')
    recovery, recovery_sha = m['create_backup'](binary, version)
    if digest(binary) != current or digest(recovery) != recovery_sha:
        raise RuntimeError('Installed Codex changed during rollback preparation')
    begin_transaction(m, 'rollback', binary, version, recovery, recovery_sha, state['backup_sha256'])
    try:
        m['install_binary'](backup, binary, version)
        if digest(binary) != state['backup_sha256']:
            raise RuntimeError('Restored binary checksum mismatch')
        command([str(binary), '--help'])
        state['active'] = False
        state['rolled_back_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
        state['pre_rollback_backup'] = str(recovery)
        m['write_state'](version, state)
    except BaseException:
        recover(tool)
        raise
    finish_transaction(m)
    return {'version': version, 'phase': 'restored', 'recovery': str(recovery)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'backup', 'repair', 'apply', 'rollback', 'recover', 'prepare', 'adapt', 'capture', 'verify', 'install'))
    parser.add_argument('--tool-dir', type=Path, default=DEFAULT_TOOL)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--purpose', default='Before evidence-based maintenance changes')
    parser.add_argument('--wait-lock', action='store_true', help='Wait for scheduled updates or another repair')
    args = parser.parse_args()
    if args.action == 'status':
        helpers(args.tool_dir)['status']()
        return
    os.umask(0o077)
    work = args.work_dir.resolve() if args.work_dir else None
    if work and (work / 'job.json').exists():
        job = load_job(work)
        if Path(job['tool']) != args.tool_dir.resolve():
            raise RuntimeError('Job tool directory does not match --tool-dir')
        bound_job(work)  # Refuse a changed runtime root before creating directories or logging.
    if args.action == 'recover' and work:
        parser.error('recover uses the runtime journal; omit --work-dir')
    with tool_lock(args.tool_dir, wait=args.wait_lock):
        try:
            if args.action == 'backup':
                print(json.dumps({'backup': str(snapshot(args.tool_dir, args.purpose))}))
            elif args.action == 'prepare':
                prepare(args.tool_dir, work)
            elif args.action == 'rollback':
                print(json.dumps(rollback(args.tool_dir)))
            elif args.action == 'recover':
                print(json.dumps(recover(args.tool_dir)))
            elif args.action in ('repair', 'apply'):
                require_no_transaction(helpers(args.tool_dir))
                if active(helpers(args.tool_dir)):
                    print(json.dumps({'phase': 'already_active', 'changed': False}))
                    return
                work = prepare(args.tool_dir, work)
                adapt(work)
                verify(work)
                print(json.dumps(install(work)))
            elif not work:
                parser.error('--work-dir is required to resume a job')
            else:
                job = bound_job(work)
                if Path(job['tool']) != args.tool_dir.resolve():
                    raise RuntimeError('Job tool directory does not match --tool-dir')
                result = {'adapt': adapt, 'capture': capture, 'verify': verify, 'install': install}[args.action](work)
                print(json.dumps(result or {'work_dir': str(work), 'phase': load_job(work)['phase']}))
        except BaseException as error:
            if work and (Path(work) / 'job.json').is_file():
                job = load_job(work)
                if job.get('runtime_root') == str(helpers(args.tool_dir)['DATA_ROOT']):
                    job['failure'] = {'stage': args.action, 'error': str(error)}
                    save_job(work, job)
            raise


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'codex-style maintenance: {error}', file=sys.stderr)
        sys.exit(1)
