"""Execution lanes and request outcomes shared by scanners and process state."""
from pathlib import Path

FULL = 'full'
EVENT = 'event'


class ScanBusyError(ValueError):
    pass


class ScanAlreadyQueued(ValueError):
    pass


class ScanQueueFull(ValueError):
    pass


def lane_for(plan, files=None):
    return EVENT if files is not None or getattr(plan, 'kind', None) == 'media' else FULL


def lock_path(lane):
    return Path.home()/'.clamdlog'/('clamscan-events.lock' if lane == EVENT else 'clamscan.lock')
