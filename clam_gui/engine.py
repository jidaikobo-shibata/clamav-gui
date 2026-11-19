"""Locate the external engine without adding install directories to PATH."""
import os
from pathlib import Path
import shutil


def executable(name='clamscan'):
    if os.name == 'nt':
        standard = Path(os.environ['ProgramFiles']) / 'ClamAV' / (name + '.exe')
        if standard.is_file():
            return str(standard)
    return shutil.which(name)


def database():
    return Path(os.environ['LOCALAPPDATA']) / 'clamav-gui' / 'database'
