"""Notification content for scheduled scans."""
from .results import assess
import datetime
from dataclasses import dataclass
import time
from pathlib import Path
import re
from .mailboxes import MAILBOX_NOTE


def definition_status(directory=Path('/var/lib/clamav'), now=None):
    try:
        dates = [path.stat().st_mtime for pattern in ('*.cvd', '*.cld', '*.cud')
                 for path in Path(directory).glob(pattern)]
        if not dates:
            return '定義ファイル：状態を確認できませんでした'
        now = now or datetime.datetime.now().astimezone()
        days = max(0, int((now.timestamp() - max(dates)) / 86400))
        return f'定義ファイル：{"古い可能性があります" if days > 3 else "正常"}（{days}日前）'
    except OSError:
        return '定義ファイル：状態を確認できませんでした'


def scan_notification(label, status, output, started, finished, definitions):
    clean = assess(status, output).clean
    if clean:
        title, urgency = 'ClamAV 検査完了', 'normal'
    elif assess(status, output).detected:
        title, urgency = 'ClamAV 検出報告', 'critical'
    else:
        title, urgency = 'ClamAV 検査不完了', 'critical'
    if MAILBOX_NOTE in status:
        title = 'ClamAV メール保存ファイル内で脅威を検出'
    counts = re.findall(r'^Scanned files:\s*(\d+)\s*$', output, re.M)
    files = str(sum(map(int, counts))) if counts else '不明'
    seconds = max(0, int((finished - started).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    versions = re.findall(r'^Engine version:\s*(.+)$', output, re.M)
    body = (f'検査プラン：{label}\n{status}\n完了：{finished:%Y/%m/%d %H:%M:%S}\n'
            f'検査ファイル数：{files}\n所要時間：{hours}時間{minutes}分{seconds}秒\n{definitions}')
    if versions:
        body += '\nClamAV：' + versions[-1]
    if not clean:
        body += '\n検査プランを選び、該当日時のログを確認してください。'
    return title, body, urgency


@dataclass(frozen=True)
class AutomaticNotice:
    title: str
    body: str
    urgency: str = 'normal'


class AutomaticNotices:
    """Shared policy for GUI and headless monitors; delivery belongs to adapters."""
    def __init__(self, clock=time.monotonic, delay=20):
        self.clock, self.delay = clock, delay
        self.pending = {}

    def record(self, key, label, status, output=''):
        if assess(status, output).clean:
            count, start, _ = self.pending.get(key, (0, self.clock(), label))
            self.pending[key] = count + 1, start, label
            return None
        return AutomaticNotice('ClamAV 自動検査の結果', label+'\n'+status+'\n検査プランのログを確認してください。', 'critical')

    def flush(self, key):
        value = self.pending.pop(key, None)
        if value:
            count, _, label = value
            return AutomaticNotice('ClamAV 自動検査完了', f'{label}：{count}回の検査が正常に完了しました。')
        return None

    def due(self, force=False):
        return [key for key, (_, start, _) in self.pending.items() if force or self.clock()-start >= self.delay]
