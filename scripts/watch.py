#!/usr/bin/env python3
"""Listen for Linux filesystem events, including changes during a repair."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
import maintain as maintenance

# close-write, rename, create, delete, attributes and watched-inode replacement.
MASK = 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800 | 0x00000004
IGNORED = 0x00008000
HEADER = struct.Struct('iIII')


class Events:
    def __init__(self, paths):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'Cannot initialize inotify')
        self.paths = [Path(p) for p in paths]
        self.watches = set()
        try:
            self.refresh()
        except BaseException:
            os.close(self.fd)
            raise

    def refresh(self):
        current = set()
        for target in self.paths:
            path = target
            # Watch a surviving ancestor while npm temporarily removes a package.
            while not path.exists() and path != path.parent:
                path = path.parent
            wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), MASK)
            if wd < 0:
                error = ctypes.get_errno()
                if error == 2:  # Renamed between exists() and add_watch().
                    continue
                raise OSError(error, f'Cannot watch {path}')
            current.add(wd)
        for wd in self.watches - current:
            self.libc.inotify_rm_watch(self.fd, wd)
        self.watches = current

    def drain(self):
        changed = False
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            offset = 0
            while offset < len(data):
                wd, mask, _, size = HEADER.unpack_from(data, offset)
                offset += HEADER.size + size
                if mask & IGNORED:
                    self.watches.discard(wd)
                else:
                    # Queue overflow also requires a fresh installation check.
                    changed = True
        return changed


def watch(paths, command):
    events = Events(paths)
    child = None
    child_fd = None
    pending = True  # Catch changes that happened while WSL was stopped.
    print(f'Watching {len(paths)} Codex installation paths; event driven, no polling', flush=True)
    try:
        while True:
            if child is None and pending:
                child = subprocess.Popen(command)
                child_fd = os.pidfd_open(child.pid)
                pending = False
            ready, _, _ = select.select([events.fd] + ([child_fd] if child_fd is not None else []), [], [])
            if events.fd in ready:
                pending = events.drain() or pending
                events.refresh()
            if child_fd is not None and child_fd in ready:
                result = child.wait()
                os.close(child_fd)
                child = None
                child_fd = None
                if result:
                    print(f'Automatic repair exited {result}; watcher remains active for future changes',
                          file=sys.stderr, flush=True)
                events.refresh()
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        if child_fd is not None:
            os.close(child_fd)
        os.close(events.fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/codex-style/watch.json')
    parser.add_argument('--tool-dir', type=Path, default=maintenance.DEFAULT_TOOL)
    parser.add_argument('--quiet-seconds', type=float, default=10)
    parser.add_argument('--wait-seconds', type=float, default=180)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    paths = config['paths']
    if not paths or any(not Path(p).is_absolute() for p in paths):
        parser.error('watch paths must be a nonempty list of absolute paths')
    command = [sys.executable, str(Path(__file__).with_name('auto.py')), '--tool-dir', str(args.tool_dir),
               '--quiet-seconds', str(args.quiet_seconds), '--wait-seconds', str(args.wait_seconds)]
    watch(paths, command)


if __name__ == '__main__':
    main()
