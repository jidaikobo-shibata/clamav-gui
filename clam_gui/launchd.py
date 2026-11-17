"""Manage only identified, user-owned LaunchAgents; never parses launchctl print."""
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile

MARKER = 'com.jidaikobo.ClamAVGui.launch-agent.v1'


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('保存先がシンボリックリンクのため停止しました。')
    descriptor, filename = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        Path(filename).replace(path)
    finally:
        Path(filename).unlink(missing_ok=True)


class LaunchAgent:
    def __init__(self, label, directory=None, run=None):
        if not re.fullmatch(r'com\.jidaikobo\.ClamAVGui\.[A-Za-z0-9.-]+', label):
            raise ValueError('定期実行の識別子が不正です。')
        self.label = label
        self.directory = Path(directory) if directory else Path.home() / 'Library/LaunchAgents'
        self.path = self.directory / (label + '.plist')
        self.domain = 'gui/' + str(os.getuid())
        self.target = self.domain + '/' + label
        self.run = run or subprocess.run

    def command(self, *arguments, check=True):
        result = self.run(['/bin/launchctl', *arguments], capture_output=True, text=True, timeout=20)
        if check and result.returncode:
            raise RuntimeError('macOSの定期実行設定を変更できませんでした。\n' + result.stderr.strip())
        return result

    def read(self):
        if self.path.is_symlink():
            raise ValueError('このアプリが管理していない予定ファイルのため停止しました。')
        if not self.path.exists():
            return None
        if self.path.stat().st_uid != os.getuid():
            raise ValueError('予定ファイルの所有者を確認できません。')
        try:
            data = plistlib.loads(self.path.read_bytes())
        except (ValueError, plistlib.InvalidFileException):
            raise ValueError('予定ファイルを読み取れません。既存ファイルは変更していません。') from None
        if data.get('ClamAVGuiManaged') != MARKER or data.get('Label') != self.label:
            raise ValueError('このアプリが管理していない予定ファイルのため停止しました。')
        return data

    def write(self, data):
        self.read()  # Protect unrelated files before a replacement.
        value = dict(data, Label=self.label, ClamAVGuiManaged=MARKER)
        atomic_write(self.path, plistlib.dumps(value, sort_keys=True))

    def loaded(self):
        return self.command('list', self.label, check=False).returncode == 0

    def enabled(self):
        data = self.read()
        if data is None:
            return False
        result = self.command('print-disabled', self.domain, check=False)
        if result.returncode:
            raise RuntimeError('定期実行の有効・無効を確認できませんでした。')
        match = re.search(r'"' + re.escape(self.label) + r'"\s*=>\s*(true|false|enabled|disabled)\b', result.stdout)
        if match:
            return match[1] in ('false', 'enabled')
        # Missing entries use the plist's Disabled default. Reject unfamiliar output.
        if 'disabled services' not in result.stdout or '{' not in result.stdout or '}' not in result.stdout:
            raise RuntimeError('このmacOSの定期実行状態を読み取れませんでした。')
        return not data.get('Disabled', False)

    def running(self):
        # launchctl list documents this three-column format; print output is not an API.
        result = self.command('list', check=False)
        if result.returncode:
            raise RuntimeError('定期実行の稼働状態を確認できませんでした。')
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2] == self.label:
                return fields[0].isdigit()
        return False

    def enable(self):
        if self.read() is None:
            raise ValueError('定期実行の設定ファイルがありません。')
        self.command('enable', self.target)
        if not self.loaded():
            self.command('bootstrap', self.domain, str(self.path))
        if not self.loaded() or not self.enabled():
            raise RuntimeError('定期実行の登録後の状態を確認できませんでした。')

    def disable(self):
        if self.read() is None:
            raise ValueError('予定ファイルの所有元を確認できないため変更できません。')
        # disable prevents future launches; a scan already running is allowed to finish.
        self.command('disable', self.target)
        if self.enabled():
            raise RuntimeError('定期実行を無効にできませんでした。')

    def unload(self):
        data = self.read()
        if self.loaded():
            if data is None:
                raise ValueError('登録済みジョブの所有元を確認できないため変更できません。')
            self.command('bootout', self.target)

    def remove(self):
        if self.read() is None:
            return
        self.disable()
        self.unload()
        self.path.unlink()


def helper_command():
    import sys
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).parent / 'ClamAV Helper'
        if not executable.is_file():
            raise ValueError('バックグラウンド実行に必要なファイルがありません。アプリを再配置してください。')
        return [str(executable)]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/macos_helper.py')]
