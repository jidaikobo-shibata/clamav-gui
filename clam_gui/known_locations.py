"""Friendly names for explicitly supported locations; stored exclusions stay paths."""
import os
from pathlib import Path
import sys


def trash_folder(*, system=None, env=None, home=None):
    system = sys.platform if system is None else system
    if not system.startswith('linux'):
        return None
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    data = env.get('XDG_DATA_HOME', '')
    base = Path(data) if data and Path(data).is_absolute() else home/'.local/share'
    return Path(os.path.normpath(str(base/'Trash/files')))


def resolve_location(text):
    trash = trash_folder()
    return str(trash) if text == 'ごみ箱' and trash is not None else text


def location_label(path):
    trash = trash_folder()
    if trash is None:
        return str(path)
    normalized = Path(os.path.normpath(str(path)))
    if normalized == trash:
        return 'ごみ箱'
    if trash in normalized.parents:
        return 'ごみ箱 / ' + str(normalized.relative_to(trash))
    return str(path)


def location_confirmation(path):
    name = location_label(path)
    return name + '\n' + str(path) if name != str(path) else str(path)
