"""Notification content for scheduled scans."""
import datetime
from pathlib import Path
import re


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
    clean = status.startswith('検査完了')
    if clean:
        title, urgency = 'ClamAV 検査完了', 'normal'
    elif status.startswith('検出あり'):
        title, urgency = 'ClamAV 検出報告', 'critical'
    else:
        title, urgency = 'ClamAV 検査不完了', 'critical'
    counts = re.findall(r'^Scanned files:\s*(\d+)\s*$', output, re.M)
    files = str(sum(map(int, counts))) if counts else '不明'
    seconds = max(0, int((finished - started).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    versions = re.findall(r'^Engine version:\s*(.+)$', output, re.M)
    body = (f'対象：{label}\n{status}\n完了：{finished:%Y/%m/%d %H:%M:%S}\n'
            f'検査ファイル数：{files}\n所要時間：{hours}時間{minutes}分{seconds}秒\n{definitions}')
    if versions:
        body += '\nClamAV：' + versions[-1]
    if not clean:
        body += '\n対象を選び、該当日時のログを確認してください。'
    return title, body, urgency
