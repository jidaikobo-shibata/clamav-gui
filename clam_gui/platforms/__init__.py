"""Select OS services lazily; importing common models does not load Linux APIs."""
import sys


class UnsupportedPlatformError(RuntimeError):
    pass


class UnsupportedBackend:
    def __init__(self, name):
        self.name = name

    def unavailable(self, *args, **kwargs):
        raise UnsupportedPlatformError(f'{self.name}向けのOS連携はまだ実装されていません。')

    scheduler = packages = updater = auto_update = background = lock = notify = unavailable
    observers = install_stop_handlers = can_update = unavailable
    definition_label = definition_status = unavailable

    def process_identity(self, pid):
        return None  # Unknown, never pretend that a process is stopped or safe to signal.


def get_backend(system=None):
    system = sys.platform if system is None else system
    if system.startswith('linux'):
        from .linux import LinuxBackend
        return LinuxBackend()
    return UnsupportedBackend(system)
