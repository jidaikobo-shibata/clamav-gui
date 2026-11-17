"""Process start identity from macOS libproc; PID existence alone is insufficient."""
import ctypes
import errno
import os


class BsdInfo(ctypes.Structure):
    # sys/proc_info.h: struct proc_bsdinfo, PROC_PIDTBSDINFO (3).
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid',
        'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        'nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_seconds', ctypes.c_uint64),
                 ('start_microseconds', ctypes.c_uint64)]


def process_executable(pid):
    """Kernel executable path; None means unknown, empty means already exited."""
    if type(pid) is not int or not 0 < pid <= 2147483647:
        return None
    try:
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        library.proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
        if library.proc_pidpath(pid, buffer, len(buffer)) <= 0:
            return '' if ctypes.get_errno() == errno.ESRCH else None
        path = os.fsdecode(buffer.value)
        return path if path.startswith('/') else None
    except (OSError, ValueError, OverflowError):
        return None


def process_identity(pid):
    try:
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                        ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        info = BsdInfo()
        count = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if count != ctypes.sizeof(info):
            return '' if ctypes.get_errno() == errno.ESRCH else None
        if info.pid != pid or not info.start_seconds:
            return None
        if info.status == 5:  # SZOMB (sys/proc.h)
            return ''
        # Epoch seconds plus microseconds distinguish a reused PID, including after reboot.
        return f'macos:{info.start_seconds}:{info.start_microseconds}'
    except (OSError, ValueError, OverflowError):
        return None
