"""Conservative removal of the selected, identified ClamAV installation.

Only receipt-owned leaf files are removed from the official prefix. Configuration,
definitions and unknown files are retained. No recursive privileged deletion.
"""
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import plistlib
import shlex
import stat
import subprocess

from .paths import find_clamav
from .macos_process import process_executable

RECEIPTS = ('com.cisco.ClamAV.programs', 'com.cisco.ClamAV.libraries', 'com.cisco.ClamAV.documentation')
PREFIX = Path('/usr/local/clamav')
AUTH_SCRIPT = ('on run argv\n'
               'do shell script (item 1 of argv) with administrator privileges\n'
               'end run')


@dataclass(frozen=True)
class RemovalPlan:
    provider: str
    binary: str
    version: str
    files: tuple = ()
    receipts: tuple = ()
    brew: str = ''

    def describe(self):
        source = 'ClamAV公式インストーラー版' if self.provider == 'official' else 'Homebrew版ClamAV'
        return (source + '（' + self.version + '）をアンインストールします。\n\n'
                'この端末で同じClamAVを使う他のアプリからも検査できなくなります。\n'
                'このアプリの定義自動更新と定期検査を停止します。予定の時刻・頻度は残します。\n'
                '再導入後、必要なプランの定期検査をオンにしてください。\n'
                '検査対象の実ファイル、検査プラン、履歴、予定、アプリ専用の定義・設定は残します。\n'
                'ClamAVの既存設定と、所有元を確認できないファイルも残します。\n\n' +
                ('削除する公式パッケージのファイル：' + str(len(self.files)) + '件\n'
                 '必要な場合はmacOSの管理者認証を求めます。' if self.provider == 'official' else
                 'ClamAV以外のパッケージや依存ライブラリの自動削除は行いません。'))


class MacPackageManager:
    def __init__(self, run=None, locate=None, prefix=PREFIX):
        self.run = run or subprocess.run
        self.locate = locate or find_clamav
        self.prefix = Path(prefix)

    def command(self, arguments, **kwargs):
        return self.run(arguments, capture_output=True, text=True, timeout=kwargs.pop('timeout', 30), **kwargs)

    def check_idle(self, plan=None):
        scope = (self.prefix if plan and plan.provider == 'official' else
                 Path(plan.binary).parents[2] if plan and plan.provider == 'homebrew' else None)
        for name in ('clamscan', 'clamdscan', 'clamd', 'freshclam'):
            result = self.command(['/usr/bin/pgrep', '-x', name])
            if result.returncode == 0:
                pids = result.stdout.split()
                if scope is None or not pids or any(not value.isdecimal() for value in pids):
                    raise ValueError('ClamAVの実行元を確認できませんでした。削除せず停止しました。')
                for value in pids:
                    executable = process_executable(int(value))
                    if executable is None:
                        raise ValueError('ClamAVの実行元を確認できませんでした。削除せず停止しました。')
                    if executable and scope in Path(executable).resolve().parents:
                        raise ValueError('削除対象のClamAVで検査・更新・デーモンが動作中です。完了または停止してから再試行してください。')
            elif result.returncode != 1:
                raise ValueError('ClamAVの実行状態を確認できませんでした。')

    def version(self, binary):
        result = self.command([str(binary), '--version'])
        if result.returncode:
            raise ValueError('ClamAVのバージョンを確認できませんでした。')
        return result.stdout.strip().split('/', 1)[0]

    def receipt_files(self):
        receipts, paths = [], set()
        for receipt in RECEIPTS:
            result = self.command(['/usr/sbin/pkgutil', '--pkg-info-plist', receipt])
            if result.returncode:
                continue
            info = plistlib.loads(result.stdout.encode())
            if (info.get('pkgid') != receipt or info.get('volume', '/') != '/' or
                    info.get('install-location', '') not in ('', '/')):
                raise ValueError('公式パッケージの登録情報が一致しません。削除せず停止しました。')
            receipts.append(receipt)
            result = self.command(['/usr/sbin/pkgutil', '--files', receipt])
            if result.returncode:
                raise ValueError('削除対象の一覧を取得できませんでした。')
            for line in result.stdout.splitlines():
                relative = PurePosixPath(line)
                if relative.is_absolute() or '..' in relative.parts or any(c in line for c in '\r\x00'):
                    raise ValueError('不正な削除対象が含まれるため停止しました。')
                path = Path('/') / str(relative)
                if self.prefix not in path.parents:
                    continue
                # Preserve configuration, certs and database directories in the shared prefix.
                part = path.relative_to(self.prefix).parts
                if part[0] not in ('bin', 'sbin', 'lib', 'include', 'share'):
                    continue
                if part[0] == 'share' and (len(part) < 2 or part[1] not in ('doc', 'man')):
                    continue
                if path.is_symlink() or path.is_file():
                    self.validate_leaf(path)
                    paths.add(str(path))
        if 'com.cisco.ClamAV.programs' not in receipts:
            raise ValueError('公式ClamAVの所有元を確認できないため、自動削除できません。')
        return tuple(sorted(paths)), tuple(receipts)

    def validate_leaf(self, path):
        if self.prefix.is_symlink() or self.prefix not in path.parents:
            raise ValueError('削除先を確認できませんでした。')
        for directory in path.parents:
            # Every ancestor must be immutable to an unprivileged user while the
            # OS authentication prompt is open. A writable /usr/local is refused.
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('削除先の所有者・アクセス権が標準と異なります。削除せず停止しました。')
        info = path.lstat()
        if info.st_uid != 0 or not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise ValueError('所有者を確認できないファイルがあるため削除せず停止しました。')

    def preview(self):
        binary = self.locate('clamscan')
        if not binary:
            raise ValueError('アンインストールするClamAVは見つかりません。')
        resolved = Path(binary).resolve()
        version = self.version(binary)
        if self.prefix in resolved.parents:
            files, receipts = self.receipt_files()
            if str(resolved) not in files:
                raise ValueError('検査プログラムの所有元を確認できません。')
            return RemovalPlan('official', str(resolved), version, files, receipts)
        for prefix in (Path('/opt/homebrew'), Path('/usr/local')):
            if prefix / 'Cellar/clamav' in resolved.parents:
                brew = prefix / 'bin/brew'
                if not brew.is_file():
                    break
                result = self.command([str(brew), 'uses', '--installed', '--recursive', 'clamav'], env=self.brew_env())
                if result.returncode or result.stdout.strip():
                    raise ValueError('ClamAVに依存する別パッケージがあるか、依存関係を確認できないため削除できません。')
                return RemovalPlan('homebrew', str(resolved), version, brew=str(brew))
        raise ValueError('このClamAVの導入元を確認できないため自動削除できません。導入元の手順で削除してください。')

    @staticmethod
    def brew_env():
        return {**os.environ, 'HOMEBREW_NO_AUTO_UPDATE': '1', 'HOMEBREW_NO_ANALYTICS': '1',
                'HOMEBREW_NO_AUTOREMOVE': '1', 'HOMEBREW_NO_INSTALL_CLEANUP': '1'}

    def execute(self, plan):
        self.check_idle(plan)
        current = self.preview()
        if current != plan:
            raise ValueError('削除対象が変わりました。もう一度内容を確認してください。')
        if plan.provider == 'homebrew':
            result = self.command([plan.brew, 'uninstall', '--formula', 'clamav'], env=self.brew_env(), timeout=600)
        elif plan.provider == 'official':
            if self.prefix != PREFIX:
                raise ValueError('標準以外の保存先を管理者権限で削除することはできません。')
            # Only fixed system executables and validated, root-owned leaf paths.
            # No -r, no arbitrary scripts run as root, no interpolation into AppleScript.
            remove = shlex.join(['/bin/rm', '-f', '--', *plan.files])
            forget = [shlex.join(['/usr/sbin/pkgutil', '--forget', receipt]) for receipt in plan.receipts]
            command = ' && '.join([remove, *forget])
            result = self.command(['/usr/bin/osascript', '-e', AUTH_SCRIPT, command], timeout=600)
        else:
            raise ValueError('未対応の削除方法です。')
        if result.returncode:
            if '-128' in result.stderr or 'User canceled' in result.stderr:
                return 'アンインストールをキャンセルしました。', result.returncode
            raise RuntimeError('アンインストールを完了できませんでした。\n' + result.stdout + result.stderr)
        if Path(plan.binary).exists():
            raise RuntimeError('ClamAVの削除完了を確認できませんでした。')
        message = 'ClamAVをアンインストールしました。プラン・履歴・設定・定義ファイルは残しています。'
        if self.locate('clamscan'):
            message += '\n別に導入済みのClamAVが残っているため、そちらで検査を続けられます。'
        return message, 0

    def remove_with_settings(self, plan, auto_service, store, scheduler=None):
        from dataclasses import replace
        from .schedule import read_schedule
        from .macos_schedule import MacScheduler
        scheduler = scheduler or MacScheduler(store)
        enabled = [(key, read_schedule(store, key)) for key, _ in store.profiles()
                   if read_schedule(store, key).enabled]
        automatic = auto_service.status().enabled
        if automatic is None:
            raise ValueError('自動更新の設定を確認してからアンインストールしてください。')

        def unchanged():
            if not Path(plan.binary).exists():
                return False
            if plan.provider == 'official' and not all(Path(path).exists() or Path(path).is_symlink() for path in plan.files):
                return False
            try:
                return self.version(plan.binary) == plan.version
            except Exception:
                return False

        def restore():
            errors = []
            for key, schedule in enabled:
                try:
                    scheduler.apply(key, schedule)
                except Exception as error:
                    errors.append(str(error))
            if automatic:
                try:
                    auto_service.set_enabled(True)
                except Exception as error:
                    errors.append(str(error))
            if errors:
                raise RuntimeError('削除前の自動更新・予定の復元を完了できませんでした。現在の状態を確認してください。')

        try:
            auto_service.set_enabled(False)
            for key, schedule in enabled:
                scheduler.apply(key, replace(schedule, enabled=False))
            message, code = self.execute(plan)
        except Exception as error:
            if unchanged():
                restore()
            else:
                raise RuntimeError(str(error) + '\n自動更新・定期検査は停止したままです。再導入後に設定を確認してください。') from error
            raise
        if code and unchanged():
            restore()
        return message, code
