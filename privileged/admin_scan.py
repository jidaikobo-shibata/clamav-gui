#!/usr/bin/python3 -I
"""Installed root-owned scanner broker. Never imports the user's application."""
import json
import fcntl
import ctypes
import stat
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile

VERSION = 1
MAX_REQUEST = 4096
BOOL_KEYS = frozenset(('scan-archive', 'scan-pdf', 'scan-ole2', 'scan-xmldocs',
    'scan-onenote', 'scan-html', 'scan-image', 'scan-image-fuzzy-hash', 'scan-pe',
    'scan-elf', 'scan-mail', 'scan-swf', 'scan-hwp3', 'phishing-sigs',
    'phishing-scan-urls', 'detect-pua', 'alert-encrypted', 'alert-macros',
    'alert-broken', 'alert-broken-media'))
SPECIAL = ('/proc', '/sys', '/dev', '/run')


def paths(values, *, targets=False):
    if not isinstance(values, list) or (targets and not values) or len(values) > 64:
        raise ValueError('検査対象・除外の件数が不正です。')
    for value in values:
        if not isinstance(value, str) or not value.startswith('/') or any(c in value for c in '\0\n\r'):
            raise ValueError('検査対象・除外の場所が不正です。')
        if targets:
            path = Path(value).resolve(strict=True)
            if not path.is_dir() or any(str(path) == p or p in map(str, path.parents) for p in SPECIAL):
                raise ValueError('仮想ファイルシステムは検査対象にできません。')
    return values


def arguments(request):
    if not isinstance(request, dict) or set(request) != {'version', 'targets', 'excludes', 'excluded_dirs', 'options', 'limits'} or type(request['version']) is not int or request['version'] != VERSION:
        raise ValueError('管理者検査の要求形式が不正です。')
    targets = paths(request['targets'], targets=True)
    excludes, excluded_dirs = paths(request['excludes']), paths(request['excluded_dirs'])
    settings, limits = request['options'], request['limits']
    if not isinstance(settings, dict) or not set(settings) <= BOOL_KEYS or any(type(v) is not bool for v in settings.values()):
        raise ValueError('許可されていない検査オプションです。')
    if not isinstance(limits, dict) or set(limits) != {'max-filesize', 'max-scansize'}:
        raise ValueError('サイズ上限の形式が不正です。')
    for value in limits.values():
        if value is not None and (type(value) is not int or not 1 <= value <= 2047):
            raise ValueError('サイズ上限が不正です。')
    if all(v is not None for v in limits.values()) and limits['max-scansize'] < limits['max-filesize']:
        raise ValueError('展開後の合計上限が小さすぎます。')
    args = ['/usr/bin/clamscan', '--recursive', '--suppress-ok-results',
        '--follow-file-symlinks=0', '--follow-dir-symlinks=0', '--cross-fs=no',
        '--heuristic-alerts=yes', '--alert-exceeds-max=yes',
        '--database=/var/lib/clamav']
    args += ['--' + k + '=' + ('yes' if settings[k] else 'no') for k in sorted(settings)]
    args += [f'--{k}={v}M' for k, v in limits.items() if v is not None]
    args += ['--exclude=^' + re.escape(p) + '$' for p in excludes]
    args += ['--exclude-dir=^' + re.escape(os.path.normpath(p)) + r'(/|$)' for p in [*SPECIAL, *excluded_dirs]]
    return args + ['--', *targets]


def filter_bpf(file):
    # Existing Ubuntu libseccomp exports a portable syscall filter. No filter
    # is loaded in this broker: bubblewrap installs it only in the scanner.
    lib = ctypes.CDLL('libseccomp.so.2')
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    ctx = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError('検査のシステムコール制限を準備できません。')
    try:
        for name in ('socket', 'socketpair', 'connect', 'ptrace', 'process_vm_writev',
                     'open_by_handle_at', 'name_to_handle_at', 'io_uring_setup',
                     'io_uring_enter', 'io_uring_register', 'mount', 'setns', 'unshare'):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or lib.seccomp_rule_add(ctx, 0x00050001, number, 0) != 0:
                raise RuntimeError('検査のシステムコール制限を設定できません。')
        if lib.seccomp_export_bpf(ctx, file.fileno()) != 0:
            raise RuntimeError('検査のシステムコール制限を出力できません。')
        file.seek(0)
    finally:
        lib.seccomp_release(ctx)


def sandbox(args, tmp, filter_fd):
    return ['/usr/bin/bwrap', '--ro-bind', '/', '/', '--dev', '/dev',
        '--unshare-net', '--unshare-pid', '--proc', '/proc', '--tmpfs', '/run', '--new-session',
        '--die-with-parent', '--cap-drop', 'ALL', '--cap-add', 'CAP_DAC_READ_SEARCH',
        '--bind', tmp, tmp, '--seccomp', str(filter_fd), '--', args[0], '--tempdir=' + tmp,
        '--exclude-dir=^' + re.escape(tmp) + r'(/|$)', *args[1:]]


def stop(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def supervise(process, input_fd=0, cancelled=lambda: False):
    try:
        while process.poll() is None:
            if cancelled() or select.select([input_fd], [], [], .2)[0]:
                stop(process)
                return 130
        return process.returncode
    finally:
        stop(process)


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1 or not os.environ.get('PKEXEC_UID', '').isdigit() or int(os.environ['PKEXEC_UID']) == 0:
        raise ValueError('この補助プログラムはアプリの管理者認証からだけ起動できます。')
    # Read without buffering: bytes remaining after the request are cancellation.
    line = bytearray()
    while len(line) <= MAX_REQUEST:
        byte = os.read(0, 1)
        if not byte:
            return 130
        line += byte
        if byte == b'\n':
            break
    if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
        raise ValueError('検査要求が大きすぎます。')
    args = arguments(json.loads(line))
    if select.select([0], [], [], 0)[0]:
        return 130
    lock = os.open('/run/clamav-gui-admin-scan.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(lock)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
        raise ValueError('管理者検査のロックを確認できません。')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError('別の管理者検査が実行中です。完了後に再実行してください。') from None
    os.chdir('/')
    os.umask(0o077)
    os.nice(10)
    cancelled = False
    def cancel(*_):
        nonlocal cancelled
        cancelled = True
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, cancel)
    # The scanner only has a private temporary directory, not a user-selected
    # log/database/config path. No delete, move, shell, or arbitrary flags exist.
    with tempfile.TemporaryDirectory(prefix='clam-gui-admin-', dir='/var/tmp') as tmp, tempfile.TemporaryFile(dir=tmp) as filter_file:
        filter_bpf(filter_file)
        process = subprocess.Popen(sandbox(args, tmp, filter_file.fileno()),
            stdin=subprocess.DEVNULL, pass_fds=(filter_file.fileno(),), close_fds=True,
            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': '/root', 'TMPDIR': tmp})
        return supervise(process, cancelled=lambda: cancelled)



if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('管理者検査を開始・継続できませんでした：' + str(error), flush=True)
        raise SystemExit(2)
