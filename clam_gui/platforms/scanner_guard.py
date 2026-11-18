"""Linux exec helper: single-threaded setup before becoming the scanner process.

Invoked only as an unprivileged child. No preexec_fn is used in the threaded GUI.
The inherited execution lock stays open through exec and is released on exit.
"""
import argparse
import ctypes
import os
import signal
import shutil
import subprocess
import sys


def lower_priority():
    """Best effort only: missing I/O scheduling support must not disable scans.

    This runs exclusively in the guarded child, never the GUI/monitor parent.
    """
    try:
        current = os.getpriority(os.PRIO_PROCESS, 0)
        if current < 10:
            os.setpriority(os.PRIO_PROCESS, 0, 10)
    except OSError as error:
        print('NOTE: 検査のCPU優先度を下げられませんでした：' + str(error), flush=True)
    ionice = shutil.which('ionice')
    if not ionice:
        print('NOTE: ioniceが見つからないため、I/O優先度はOSの標準設定を使用します。', flush=True)
        return
    try:
        result = subprocess.run([ionice, '-c', '3', '-p', str(os.getpid())],
                                capture_output=True, text=True, timeout=2)
        if result.returncode:
            print('NOTE: 検査のI/O優先度を下げられませんでした。OSの標準設定を使用します。', flush=True)
    except (OSError, subprocess.TimeoutExpired):
        print('NOTE: 検査のI/O優先度を下げられませんでした。OSの標準設定を使用します。', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', required=True, type=int)
    parser.add_argument('--lock-fd', required=True, type=int)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.parent <= 0 or args.lock_fd < 3:
        parser.error('invalid scanner ownership parameters')
    try:
        os.fstat(args.lock_fd)
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), 'cannot protect scanner ownership')
        # If the creator died before prctl(), do not start an orphan scanner.
        if os.getppid() != args.parent:
            return 125
        lower_priority()
        os.set_inheritable(args.lock_fd, True)
        os.execvpe(command[0], command, os.environ)
    except (OSError, AttributeError) as error:
        print('ERROR: 検査プロセスの保護・起動に失敗しました：' + str(error), file=sys.stderr)
        return 125


if __name__ == '__main__':
    raise SystemExit(main())
