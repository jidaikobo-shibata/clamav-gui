"""Ubuntu package lifecycle via fixed, system-owned executables.

This module never runs user-writable Python or shell scripts as administrator.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile

PACKAGES = ('clamav', 'clamav-daemon', 'clamav-freshclam')


@dataclass(frozen=True)
class PackagePlan:
    action: str
    changes: tuple
    detail: str

    def describe(self):
        operation = 'インストール' if self.action == 'install' else 'アンインストール'
        text = 'ClamAVの' + operation + '\n\n'
        if self.action == 'remove':
            text += ('このGUI・既存の定期検査・ClamAVを利用する他のアプリから検査できなくなります。\n'
                     'ClamAVの検査・定義更新サービスも停止します。\n'
                     '検査対象のファイル、GUIの設定・履歴・予定は削除しません。\n'
                     'ClamAVの設定は残します。定義ファイルは削除されるため再導入後に再ダウンロードが必要です。\n')
        else:
            text += ('Ubuntuの登録済みAPT配布元から取得します。通信・空き容量が必要です。\n'
                     '不足する依存パッケージも追加します。既存の設定ファイルを優先して残します。\n'
                     'パッケージ標準の処理によりClamAVサービスが起動する場合があります。\n'
                     '初回の定義更新には時間がかかります。\n')
        groups = [('削除予定', 'Remv'), ('追加・更新予定', 'Inst'), ('設定予定', 'Conf')]
        installed = {line.split()[1] for line in self.changes if line.startswith('Inst ')}
        summaries = []
        for label, kind in groups:
            names = list(dict.fromkeys(line.split()[1] for line in self.changes
                                      if line.startswith(kind + ' ') and
                                      not (kind == 'Conf' and line.split()[1] in installed)))
            if names:
                summaries.append(f'{label}：{len(names)}件\n' + '\n'.join('・' + name for name in names))
        summary = '\n\n'.join(summaries) if summaries else '変更は不要です。'
        introduction = ('まだ削除していません。以下は実行前の変更予定です。' if self.action == 'remove' else
                        'まだインストールしていません。以下は実行前の変更予定です。')
        return (introduction + '\n\n' + summary + '\n\n' + text +
                '\n一覧にないパッケージの更新・削除や、不要パッケージの自動削除は行いません。\n'
                '\n「管理者認証して実行」を押すと、必要に応じてOSの認証画面が開きます。\n'
                '認証が許可されると処理を開始します。アプリをrootで起動する必要はありません。')


class AptManager:
    package_names = PACKAGES

    def __init__(self, run=None, release=Path('/etc/os-release')):
        self.run = run or subprocess.run
        self.release = Path(release)

    def check_platform(self):
        fields = dict(line.split('=', 1) for line in self.release.read_text().splitlines()
                      if '=' in line and not line.startswith('#'))
        if fields.get('ID', '').strip('"') != 'ubuntu':
            raise ValueError('この導入機能はUbuntu向けです。このOSの導入方式にはまだ対応していません。')

    def status(self):
        self.check_platform()
        result = self.run(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}\t${Status}\t${Version}\n', *PACKAGES],
                          capture_output=True, text=True, timeout=15, env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode not in (0, 1):
            raise ValueError('パッケージの導入状態を確認できません。')
        installed = {}
        for line in result.stdout.splitlines():
            fields = line.split('\t')
            if len(fields) == 3 and fields[1] == 'install ok installed':
                name = fields[0].split(':', 1)[0]
                if name in PACKAGES:
                    installed[name] = fields[2]
        return installed

    def engine_update_status(self):
        """Compare the installed APT package with the locally known candidate."""
        installed = self.status().get('clamav')
        if not installed:
            return ('ClamAV本体：APT未導入', 'APT管理下のclamavパッケージは未導入です。手動導入したClamAVの更新状態は判定しません。')
        env = {**os.environ, 'LC_ALL': 'C'}
        try:
            result = self.run(['/usr/bin/apt-cache', 'policy', 'clamav'],
                              capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            return (f'ClamAV本体（APT） {installed}：更新状態不明', str(error))
        candidates = re.findall(r'^\s*Candidate:\s*(\S+)\s*$', result.stdout, re.M)
        if result.returncode or len(candidates) != 1 or candidates[0] == '(none)':
            return (f'ClamAV本体（APT） {installed}：更新状態不明', 'APTの更新候補を確認できませんでした。')
        candidate = candidates[0]
        try:
            comparison = self.run(['/usr/bin/dpkg', '--compare-versions', installed, 'lt', candidate],
                                  capture_output=True, text=True, timeout=10, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            return (f'ClamAV本体（APT） {installed}：更新状態不明', str(error))
        if comparison.returncode not in (0, 1):
            return (f'ClamAV本体（APT） {installed}：更新状態不明', 'パッケージのバージョンを比較できませんでした。')
        label = f'更新あり → {candidate}' if comparison.returncode == 0 else '更新なし（APT情報）'
        return (f'ClamAV本体（APT） {installed}：{label}',
                'APT管理下のclamavパッケージの状態です。\n'
                'OSが保持するパッケージ情報との比較であり、情報が古い場合があります。\n'
                'この確認では配布情報の取得や本体の更新は行いません。Ubuntuのソフトウェア更新で更新してください。')

    @staticmethod
    def arguments(action):
        if action not in ('install', 'remove'):
            raise ValueError('未対応のパッケージ操作です。')
        arguments = ['--assume-yes', '--no-install-recommends', '--no-auto-remove',
                     '-o', 'Dpkg::Options::=--force-confold']
        if action == 'install':
            arguments.append('--no-remove')
        return arguments + [action, *PACKAGES]

    def scanning(self):
        for name in ('clamscan', 'clamdscan'):
            result = self.run(['/usr/bin/pgrep', '-x', name], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True
            if result.returncode != 1:
                raise ValueError('検査の実行状態を確認できません。')
        result = self.run(['/usr/bin/pgrep', '-x', 'clamd'], capture_output=True, text=True, timeout=10)
        if result.returncode not in (0, 1):
            raise ValueError('ClamAVデーモンの状態を確認できません。')
        if result.returncode == 0:
            for pid in result.stdout.split():
                if not pid.isdigit():
                    raise ValueError('ClamAVデーモンの状態を確認できません。')
                try:
                    group = Path('/proc')/pid/'cgroup'
                    if 'system.slice/clamav-daemon.service' not in group.read_text():
                        return True
                except FileNotFoundError:
                    continue
                except OSError:
                    raise ValueError('ClamAVデーモンを利用するアプリを終了してから再実行してください。') from None
        return False

    def preview(self, action):
        self.check_platform()
        arguments = self.arguments(action)
        if self.scanning():
            raise ValueError('実行中のClamAV検査または専用デーモンがあります。検査の完了を待ち、ClamAVを利用している他のアプリを終了してから再実行してください。')
        result = self.run(['/usr/bin/apt-get', '--simulate', *arguments], capture_output=True,
                          text=True, timeout=60, env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode:
            raise ValueError('APTの変更予定を確認できませんでした。\n' + result.stdout + result.stderr)
        changes = tuple(line for line in result.stdout.splitlines() if re.match(r'^(Inst|Remv|Conf) ', line))
        for line in changes:
            kind, name = line.split()[:2]
            name = name.split(':', 1)[0]
            if action == 'install' and kind == 'Remv':
                raise ValueError('既存パッケージの削除を伴うためインストールを停止しました。')
            if action == 'remove' and (kind != 'Remv' or name not in PACKAGES):
                raise ValueError('ClamAV以外の変更を伴うためアンインストールを停止しました。\n' + line)
        return PackagePlan(action, changes, result.stdout)

    def execute(self, plan):
        # Re-simulate after the user's confirmation. Never silently accept a changed plan.
        current = self.preview(plan.action)
        if current.changes != plan.changes:
            raise ValueError('変更予定が変わりました。もう一度、内容を確認してください。')
        if not current.changes:
            return '変更は不要でした。', 0
        command = ['/usr/bin/pkexec', '/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive',
                   'UCF_FORCE_CONFFOLD=1', '/usr/bin/apt-get', *self.arguments(plan.action)]
        # Do not kill dpkg midway or run arbitrary user-owned helpers with root privileges.
        with tempfile.TemporaryFile(mode='w+') as log:
            result = self.run(command, stdout=log, stderr=subprocess.STDOUT, text=True)
            size = log.seek(0, 2)
            log.seek(max(0, size - 2 * 1024 * 1024))
            output = log.read()
        if result.returncode in (126, 127):
            return '管理者認証がキャンセルされたか、許可されませんでした。\n' + output, result.returncode
        return output, result.returncode
