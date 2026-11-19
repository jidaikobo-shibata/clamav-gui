"""Native process lifetime and lock support for Windows."""
import ctypes
from ctypes import wintypes
import errno
import msvcrt
from pathlib import Path
import subprocess
import sys

kernel = ctypes.WinDLL('kernel32', use_last_error=True)


def api(name, result, *args):
    fn = getattr(kernel, name)
    fn.argtypes, fn.restype = args, result
    return fn


handle = wintypes.HANDLE
pointer = ctypes.c_void_p
dword = wintypes.DWORD
close = api('CloseHandle', wintypes.BOOL, handle)
open_process = api('OpenProcess', handle, dword, wintypes.BOOL, dword)
times = api('GetProcessTimes', wintypes.BOOL, handle, pointer, pointer, pointer, pointer)
wait = api('WaitForSingleObject', dword, handle, dword)
create_job = api('CreateJobObjectW', handle, pointer, wintypes.LPCWSTR)
set_info = api('SetInformationJobObject', wintypes.BOOL, handle, ctypes.c_int, pointer, dword)
assign = api('AssignProcessToJobObject', wintypes.BOOL, handle, handle)


class FileLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        if self.file:
            return True
        stream = self.path.open('a+b')
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            stream.close()
            if error.errno == errno.EACCES:
                return False
            raise
        self.file = stream
        return True

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def identity(pid):
    proc = open_process(0x101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not proc:
        return '' if ctypes.get_last_error() == 87 else None
    try:
        if wait(proc, 0) == 0:
            return ''
        values = [ctypes.c_ulonglong() for _ in range(4)]
        if times(proc, *(ctypes.byref(v) for v in values)):
            return str(values[0].value)
        return None
    finally:
        close(proc)


class JobProcess:
    def __init__(self, command, **kwargs):
        # The helper waits for our byte before starting ClamAV. EOF before
        # assignment means its owner died during setup.
        self.job = create_job(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (64-bit Windows).
            if ctypes.sizeof(pointer) != 8:
                raise OSError('64ビット版Pythonが必要です。')
            info = ctypes.create_string_buffer(144)
            dword.from_buffer(info, 16).value = 0x2000  # KILL_ON_JOB_CLOSE
            if not set_info(self.job, 9, info, len(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            helper = Path(__file__).with_name('windows_scanner.py')
            self.proc = subprocess.Popen([sys.executable, str(helper), *command],
                stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW |
                subprocess.BELOW_NORMAL_PRIORITY_CLASS, **kwargs)
            with self.proc.stdin as gate:
                if not assign(self.job, int(self.proc._handle)):
                    raise ctypes.WinError(ctypes.get_last_error())
                gate.write(b'1')
        except BaseException:
            self.terminate()
            if hasattr(self, 'proc'):
                self.proc.wait()
            raise

    def terminate(self):
        if self.job:
            close(self.job)
            self.job = None

    kill = terminate

    def wait(self, timeout=None):
        code = self.proc.wait(timeout)
        self.terminate()
        return code
