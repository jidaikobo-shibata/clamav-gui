"""Notification content for scheduled scans."""
from .results import assess, ResultKind, output_was_truncated, detection_uncertain, found_signature
from collections import Counter
import datetime
from dataclasses import dataclass
import time
from pathlib import Path
import re
from .mailboxes import MAILBOX_NOTE
from .paths import database_directory


def definition_status(directory=None, now=None):
    directory = Path(directory) if directory is not None else database_directory()
    from .definitions import daily_definition_age
    age = daily_definition_age(directory, now.timestamp() if now else None)
    if age is None:
        return '定義ファイル：状態を確認できませんでした'
    return f'定義ファイル：{"古い可能性があります" if age > 3*86400 else "正常"}（{age/86400:.1f}日前）'


def comparable_warnings(status, output):
    """Only compare complete full-folder reports with unambiguous warning details."""
    result = assess(status, output)
    if (result.detected or result.kind == ResultKind.CANCELLED or
            output_was_truncated(output) or detection_uncertain(output, status) or
            '定義の鮮度に注意：' in status or '監視による検査ファイル' in output or
            'WARNING:' in output or 'ERROR' in output or 'Permission denied' in output or
            re.search(r'^LibClamAV (?:Warning|Error):', output, re.M)):
        return None
    errors = re.findall(r'^Total errors:\s*(\d+)\s*$', output, re.M)
    infected = re.findall(r'^Infected files:\s*(\d+)\s*$', output, re.M)
    scanned = re.findall(r'^Scanned files:\s*(\d+)\s*$', output, re.M)
    if not errors or any(int(n) for n in errors) or not infected or not scanned:
        return None
    warnings = Counter()
    for line in output.splitlines():
        if not line.endswith(' FOUND'):
            continue
        signature = found_signature(line)
        if not signature.startswith(('Heuristics.Limits.Exceeded.', 'Heuristics.Encrypted.')):
            return None
        warnings[line] += 1
    if sum(warnings.values()) != sum(map(int, infected)):
        return None
    code = re.findall(r'^ClamAV exit code: (\d+)\s*$', output, re.M)
    if code != (['1'] if warnings else ['0']):
        return None
    if not warnings and not result.clean:
        return None
    if warnings and not str(status).startswith('検査不完了'):
        return None
    return warnings


def previous_warnings(store, key, status, output):
    """Use the immediately preceding persisted report, or fail closed."""
    if store is None or key is None or key == 'external-media':
        return None
    settings_marker = '--- この検査で使用した設定 ---'
    if settings_marker not in output:
        return None
    try:
        with store.connect() as db:
            rows = db.execute('SELECT target,status,output,result_kind,detected FROM scans '
                              'WHERE profile_id=? ORDER BY rowid DESC LIMIT 2', (key,)).fetchall()
        if len(rows) != 2:
            return None
        current, prior = rows
        if current[1:3] != (str(status), output) or current[0] != prior[0]:
            return None
        if settings_marker not in prior[2] or output.rsplit(settings_marker, 1)[1] != prior[2].rsplit(settings_marker, 1)[1]:
            return None
        if prior[4] or prior[3] == ResultKind.CANCELLED.value:
            return None
        return comparable_warnings(prior[1], prior[2])
    except Exception:
        # History may be pruned or temporarily unavailable. Never lower urgency.
        return None


def warning_delta(current, previous):
    parts = []
    for prefix, label in (('Heuristics.Limits.Exceeded.', '検査上限超過'),
                          ('Heuristics.Encrypted.', '暗号化による未検査')):
        def count(items):
            return sum(n for line, n in items.items() if found_signature(line).startswith(prefix))
        new = count(current - previous)
        continuing = count(current & previous)
        resolved = count(previous - current)
        if new:
            parts.append(f'新規の{label}：{new}件')
        if continuing:
            parts.append(f'継続中の{label}：{continuing}件')
        if resolved:
            parts.append(f'今回報告されなかった{label}：{resolved}件')
    return '、'.join(parts)


def scan_notification(label, status, output, started=None, finished=None, definitions=None,
                      *, store=None, key=None):
    result = assess(status, output)
    urgency = 'critical'
    if result.detected:
        title = 'ClamAV 検出報告'
    elif '定義の鮮度に注意：' in status:
        title = 'ClamAV 要確認：定義の鮮度'
    elif result.clean:
        title, urgency = 'ClamAV 検査完了', 'normal'
    else:
        title = 'ClamAV 検査不完了'
    if MAILBOX_NOTE in status:
        title = 'ClamAV メール保存ファイル内で脅威を検出'
        urgency = 'critical'
    # Preserve the concise result and actionable warnings; omit routine metrics.
    message = '検出なし。' if result.clean else '。'.join(
        line.rstrip('。') for line in str(status).splitlines() if line.strip()) + '。'
    current = comparable_warnings(status, output)
    if current is not None:
        previous = previous_warnings(store, key, status, output)
        if previous is not None and (current or previous):
            delta = warning_delta(current, previous)
            if current:
                message = '検査不完了。' + delta + '。完全には検査できていません。'
                if not (current - previous):
                    title, urgency = 'ClamAV 継続中の警告', 'normal'
            elif delta:
                message += delta + '。'
    return title, f'検査プラン：{label}\n{message}', urgency


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
        return AutomaticNotice(*scan_notification(label, status, output))

    def flush(self, key):
        value = self.pending.pop(key, None)
        if value:
            count, _, label = value
            return AutomaticNotice('ClamAV 自動検査完了', f'検査プラン：{label}\n検出なし（{count}回の検査）。')
        return None

    def due(self, force=False):
        return [key for key, (_, start, _) in self.pending.items() if force or self.clock()-start >= self.delay]
