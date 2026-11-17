"""Explicitly approved official-package round trip; preserves normal app data.

Requires --perform-removal-and-reinstall. Shows only native authorization and
Installer screens. Never targets Homebrew, ClamXAV, or normal LaunchAgents.
"""
import argparse
import datetime
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clam_gui.core import Plan, Store
from clam_gui.macos_packages import MacPackageManager
from clam_gui.macos_schedule import MacScheduler
from clam_gui.macos_setup import MacSetup, INSTALLER_NAME
from clam_gui.macos_update import MacAutoUpdateService
from clam_gui.paths import mac_data_directory
from clam_gui.schedule import Schedule, read_schedule


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def official(name):
    path = Path('/usr/local/clamav/bin') / name
    return str(path) if path.is_file() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--perform-removal-and-reinstall', action='store_true')
    args = parser.parse_args()
    manager = MacPackageManager(locate=official)
    plan = manager.preview()
    print(plan.describe(), flush=True)
    if not args.perform_removal_and_reinstall:
        return 0
    manager.check_idle(plan)
    normal = mac_data_directory()
    retained = [normal / 'freshclam.conf', *sorted((normal / 'definitions').glob('*'))]
    retained = {p: digest(p) for p in retained if p.is_file() and not p.is_symlink()}
    brew = Path('/opt/homebrew/bin/clamscan')
    brew_hash = digest(brew) if brew.is_file() else None
    token = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='clam-reinstall-') as temporary:
        root = Path(temporary)
        support = root / 'support'
        setup = MacSetup(support, locate=official)
        setup.configure()
        for path in (normal / 'definitions').glob('*'):
            if path.is_file() and not path.is_symlink():
                subprocess.run(['/bin/cp', '-c', str(path), str(setup.database / path.name)], check=True)
        cache = support / 'downloads'
        cache.mkdir()
        package = Path(__file__).resolve().parents[1] / 'dist/cache' / INSTALLER_NAME
        subprocess.run(['/bin/cp', '-c', str(package), str(cache / INSTALLER_NAME)], check=True)
        os.environ['CLAMAV_GUI_SUPPORT_DIR'] = str(support)
        store = Store(root / 'data')
        target = root / 'harmless.txt'
        target.write_text('This is harmless verification data.\n')
        key = store.save_profile(Plan('一時再導入検証', [str(root)], []))
        scheduler = MacScheduler(store, root / 'agents', namespace='validation.reinstall.' + token)
        automatic = MacAutoUpdateService(support, root / 'agents',
                                        label='com.jidaikobo.ClamAVGui.validation.update.' + token)
        agent = scheduler.agent(key)
        try:
            # A future calendar exercises removal of settings without starting a scan.
            day = 2 if datetime.date.today().day == 1 else 1
            scheduler.apply(key, Schedule(True, 'monthly', hour=3, minute=17, monthday=day))
            message, code = manager.remove_with_settings(plan, automatic, store, scheduler)
            print(message, flush=True)
            if code:
                raise RuntimeError('Removal was cancelled; installation was left unchanged.')
            if official('clamscan') or read_schedule(store, key).enabled or automatic.status().enabled:
                raise RuntimeError('Removal or disabling the temporary schedule failed')
            print('Official engine removed; temporary schedule disabled. Starting native Installer.', flush=True)
            state = setup.prepare(report=lambda message, fraction: print(message, flush=True))
            if not state.ready:
                raise RuntimeError('Reinstallation did not finish')
            print('Reinstallation ready:', manager.version(official('clamscan')), flush=True)
            result = subprocess.run([official('clamscan'), '--database=' + str(setup.database), str(target)],
                                    capture_output=True, text=True, timeout=600)
            print(result.stdout + result.stderr, flush=True)
            if result.returncode:
                raise RuntimeError('Reinstalled engine could not scan harmless data')
            if any(not path.is_file() or digest(path) != checksum for path, checksum in retained.items()):
                raise RuntimeError('Normal definitions/configuration changed during verification')
            if brew_hash is not None and (not brew.is_file() or digest(brew) != brew_hash):
                raise RuntimeError('Homebrew installation changed during verification')
            print('Round trip passed. Normal definitions/configuration and Homebrew engine are unchanged.', flush=True)
        finally:
            if agent.path.exists():
                agent.remove()
            if automatic.agent.path.exists():
                automatic.agent.remove()
            if not official('clamscan'):
                print('Official engine absent; reopening the verified native Installer for restoration.', flush=True)
                setup.install_engine(threading.Event(), lambda message, fraction: print(message, flush=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
