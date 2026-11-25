"""Execution lanes and request outcomes shared by scanners and process state."""
import os
from pathlib import Path

FULL = 'full'
EVENT = 'event'


class ScanBusyError(ValueError):
    pass


class ScanAlreadyQueued(ValueError):
    pass


class ScanFilesChanged(ValueError):
    """An event batch contains paths that disappeared or left its allowed scope."""
    pass


class ScanQueueFull(ValueError):
    pass


def lane_for(plan, files=None):
    return EVENT if files is not None or getattr(plan, 'kind', None) == 'media' else FULL


def lock_path(lane):
    base = (Path(os.environ.get('XDG_DATA_HOME') or os.environ['LOCALAPPDATA'])/'clamav-gui'
            if os.name == 'nt' else Path.home()/'.clamdlog')
    return base/('clamscan-events.lock' if lane == EVENT else 'clamscan.lock')
