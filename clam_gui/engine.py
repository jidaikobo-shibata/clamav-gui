"""Locate the external engine without adding install directories to PATH."""
import os
from pathlib import Path
import shutil


def startup_error(code):
    if os.name == 'nt' and (code & 0xffffffff) == 0xc0000135:
        return ('ClamAV の起動に必要な DLL が見つかりません（0xC0000135）。'
                'Windows が再起動を求めている場合は、再起動後に再確認してください。'
                '問題が続き、winget で導入した場合は winget repair --id Cisco.ClamAV --exact で本体を修復してください。')
    return ''


def executable(name='clamscan'):
    import sys
    if sys.platform == 'darwin':
        from .paths import find_clamav
        return find_clamav(name)
    if os.name == 'nt':
        standard = Path(os.environ['ProgramFiles']) / 'ClamAV' / (name + '.exe')
        if standard.is_file():
            return str(standard)
    return shutil.which(name)


def shared_database():
    return Path(os.environ['LOCALAPPDATA']) / 'ClamAV' / 'database'


def legacy_database():
    return Path(os.environ['LOCALAPPDATA']) / 'clamav-gui' / 'database'


def definitions_present(folder):
    return all(any(path.is_file() and path.stat().st_size > 0
                   for path in (folder/(name+'.cvd'), folder/(name+'.cld')))
               for name in ('daily', 'main', 'bytecode'))


def database():
    shared = shared_database()
    if not (shared.parent/'gui-definitions-ready').is_file():
        legacy = legacy_database()
        if definitions_present(legacy):
            return legacy
    return shared
