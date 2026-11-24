"""Build a data-only local package and let system-owned dpkg manage its files."""
import hashlib
import os
from pathlib import Path
import subprocess
import stat
import tempfile
from .admin_scan import HELPER, POLICY, SOURCE, available

PACKAGE = 'clamav-gui-admin-helper'
MAINTAINER = 'ClamAV GUI contributors <noreply@example.invalid>'
POLICY_SOURCE = SOURCE.parent/POLICY.name


class SetupCancelled(ValueError):
    pass


class AdminHelperSetup:
    def __init__(self, run=None):
        self.run = run or subprocess.run

    def check_platform(self):
        from .packages import AptManager
        AptManager().check_platform()
        if not all(Path(path).is_file() for path in ('/usr/bin/dpkg', '/usr/bin/dpkg-deb', '/usr/bin/pkexec')):
            raise ValueError('この環境では補助機能の導入に必要なUbuntuのツールが見つかりません。')
        for path in (HELPER, POLICY):
            for folder in path.parents:
                info = folder.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError('安全な設置先を確認できないため変更できません：' + str(folder))

    def status(self):
        self.check_platform()
        result = self.run(['/usr/bin/dpkg-query', '-W', '-f=${Status}\t${Maintainer}', PACKAGE],
                          capture_output=True, text=True, timeout=15)
        if result.returncode not in (0, 1):
            raise ValueError('補助機能の導入状態を確認できません。')
        fields = result.stdout.strip().split('\t')
        package_state = fields[0].split()[-1] if fields[0] else ''
        present = result.returncode == 0 and package_state not in ('not-installed', 'config-files')
        if present and (len(fields) != 2 or fields[1] != MAINTAINER):
            raise ValueError('同名の別パッケージが存在するため変更できません。')
        for path in (HELPER, POLICY):
            if path.exists() or path.is_symlink():
                owner = self.run(['/usr/bin/dpkg-query', '-S', str(path)],
                                 capture_output=True, text=True, timeout=15)
                if not present or owner.returncode or owner.stdout.strip() != PACKAGE + ': ' + str(path):
                    raise ValueError('別の方法で設置されたファイルがあるため、自動では上書き・削除しません：' + str(path))
        if not present:
            return 'missing'
        return 'ready' if package_state == 'installed' and available() and POLICY.read_bytes() == POLICY_SOURCE.read_bytes() else 'update'

    def build(self, directory):
        directory = Path(directory)
        root = directory/'package'
        control = root/'DEBIAN'
        control.mkdir(parents=True, mode=0o755)
        helper_bytes, policy_bytes = SOURCE.read_bytes(), POLICY_SOURCE.read_bytes()
        version = hashlib.sha256(helper_bytes + b'\0' + policy_bytes).hexdigest()[:16]
        (control/'control').write_text(
            'Package: ' + PACKAGE + '\nVersion: 1.0+' + version + '\nArchitecture: all\n'
            'Maintainer: ' + MAINTAINER + '\n'
            'Depends: python3, pkexec, bubblewrap, libseccomp2\n'
            'Section: utils\nPriority: optional\n'
            'Description: Authenticated manual scan helper for ClamAV GUI\n')
        # No maintainer scripts, triggers, services, conffiles, or user-selected paths.
        for path, data, mode in ((HELPER, helper_bytes, 0o755), (POLICY, policy_bytes, 0o644)):
            target = root/str(path).lstrip('/')
            target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        for folder, dirs, _ in os.walk(root):
            Path(folder).chmod(0o755)
        destination = directory/(PACKAGE + '.deb')
        result = self.run(['/usr/bin/dpkg-deb', '--build', '--root-owner-group', str(root), str(destination)],
                          capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError('補助機能の準備に失敗しました：' + (result.stderr or result.stdout).strip())
        return destination

    def execute(self, action):
        if action not in ('install', 'remove'):
            raise ValueError('補助機能の操作が不正です。')
        current = self.status()  # Recheck immediately before preparing the transaction.
        if action == 'remove' and current == 'missing':
            return
        with tempfile.TemporaryDirectory(prefix='clam-gui-helper-package-') as directory:
            command = (['/usr/bin/dpkg', '--install', str(self.build(directory))] if action == 'install'
                       else ['/usr/bin/dpkg', '--remove', PACKAGE])
            env = {key: os.environ[key] for key in ('DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR') if key in os.environ}
            env.update(PATH='/usr/bin:/bin', LC_ALL='C')
            # Do not kill an in-progress dpkg transaction on a timeout.
            result = self.run(['/usr/bin/pkexec', *command], capture_output=True, text=True, env=env)
        if result.returncode == 126:
            raise SetupCancelled('管理者認証がキャンセルされました。')
        if result.returncode:
            raise RuntimeError('補助機能を変更できませんでした：' + (result.stderr or result.stdout or '管理者認証を確認してください。').strip()[:4000])
        updated = self.status()
        if action == 'install' and updated != 'ready':
            raise RuntimeError('導入後の確認が完了しませんでした。状態を確認して再試行してください。')
        if action == 'remove' and updated != 'missing':
            raise RuntimeError('削除後も補助機能が残っています。状態を確認してください。')
