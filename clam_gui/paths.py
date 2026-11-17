"""Resolve scanner resources without relying on a Terminal's PATH on macOS."""
from pathlib import Path
import os
import shutil
import sys


def mac_data_directory():
    if os.environ.get('CLAMAV_GUI_SUPPORT_DIR'):
        return Path(os.environ['CLAMAV_GUI_SUPPORT_DIR']).expanduser().absolute()
    return Path.home() / 'Library/Application Support/ClamAV GUI'


def database_directory():
    if sys.platform == 'darwin':
        return mac_data_directory() / 'definitions'
    return Path('/var/lib/clamav')


def find_clamav(name):
    found = shutil.which(name)
    if found or sys.platform != 'darwin':
        return found
    for directory in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/local/clamav/bin',
                      '/usr/local/clamav/sbin'):
        candidate = Path(directory) / name
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return None
