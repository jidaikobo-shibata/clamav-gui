"""Language-independent scan outcomes, with a compatibility adapter for old logs."""
from dataclasses import dataclass
from enum import Enum
import re


class ResultKind(str, Enum):
    CLEAN = 'clean'
    DETECTED = 'detected'
    WARNING = 'warning'
    INCOMPLETE = 'incomplete'
    CANCELLED = 'cancelled'


@dataclass(frozen=True)
class Assessment:
    kind: ResultKind
    detected: bool = False

    @property
    def clean(self):
        return self.kind == ResultKind.CLEAN and not self.detected

    @property
    def completed(self):
        return self.kind in (ResultKind.CLEAN, ResultKind.DETECTED)


class ScanStatus(str):
    """Keep the existing (status, output) API while carrying structured metadata.

    SQLite stores kind and detected separately. Japanese parsing is used only
    when importing legacy/plain-text results, never to classify a new scan.
    """
    def __new__(cls, message, kind, detected=False, *, started_at=None, retry_files=False):
        value = super().__new__(cls, message)
        value.assessment = Assessment(ResultKind(kind), bool(detected))
        value.started_at = started_at
        value.retry_files = bool(retry_files)
        return value

    def __add__(self, detail):
        return ScanStatus(str.__add__(self, detail), self.assessment.kind, self.assessment.detected, started_at=self.started_at, retry_files=self.retry_files)


def assess(status, output='', *, kind=None, detected=None):
    if kind is not None:
        return Assessment(ResultKind(kind), bool(detected))
    if isinstance(status, ScanStatus):
        return status.assessment
    # Historical rows and external callers may still provide a plain string.
    found = has_detection(status, output) if detected is None else bool(detected)
    if status.startswith('検査を中止'):
        outcome = ResultKind.CANCELLED
    elif '検査不完了' in status:
        outcome = ResultKind.INCOMPLETE
    elif status.startswith(('検査完了', '検出なし')):
        outcome = ResultKind.CLEAN
    elif found:
        outcome = ResultKind.DETECTED
    elif status.startswith('確認が必要'):
        outcome = ResultKind.WARNING
    else:
        outcome = ResultKind.INCOMPLETE
    return Assessment(outcome, found)


def found_signature(line):
    return line[:-6].rpartition(': ')[2] if line.endswith(' FOUND') else ''


def warning_signature(line):
    return any(found_signature(line).startswith(prefix) for prefix in (
        'Heuristics.Limits.Exceeded.', 'Heuristics.Encrypted.', 'Heuristics.OLE2.ContainsMacros',
        'Heuristics.Broken.'))


def has_detection(status, output):
    if isinstance(status, ScanStatus):
        return status.assessment.detected
    lines = [line for line in output.splitlines() if line.endswith(' FOUND')]
    if lines:
        return any(not warning_signature(line) for line in lines)
    infected = re.search(r'^Infected files:\s*(\d+)\s*$', output, re.M)
    if infected:
        return int(infected[1]) > 0
    return status.startswith('検出あり')


def summarize(code, output, cancelled=False):
    def total(name):
        return sum(int(n) for n in re.findall(r'^' + name + r':\s*(\d+)\s*$', output, re.M))
    found = [line for line in output.splitlines() if line.endswith(' FOUND')]
    limits = [line for line in found if found_signature(line).startswith('Heuristics.Limits.Exceeded.')]
    encrypted = [line for line in found if found_signature(line).startswith('Heuristics.Encrypted.')]
    warnings = [line for line in found if warning_signature(line)]
    threats = [line for line in found if line not in warnings]
    errors = total('Total errors')
    incomplete = code not in (0, 1) or errors > 0 or 'WARNING:' in output or 'ERROR' in output
    detected = bool(threats) or (total('Infected files') > 0 and not warnings)
    if cancelled:
        title = '検査を中止しました（未検査の対象が残っています）'
    elif incomplete or limits or encrypted:
        title = '検出あり・検査不完了' if detected else '検査不完了'
    elif detected:
        title = '検出あり：詳細を確認してください'
    elif warnings:
        title = '確認が必要です：マクロ・破損などの警告があります'
    elif code == 1:
        title = '確認が必要です：ClamAVが警告を返しました'
    else:
        title = f"検査完了：{total('Scanned files')}ファイルを検査し、脅威は見つかりませんでした"
    details = []
    if limits:
        details.append(f'サイズ・展開などの検査上限に達した対象が{len(limits)}件あります。完全には検査できていません。')
    if encrypted:
        details.append(f'暗号化により内容を確認できない対象が{len(encrypted)}件あります。')
    if threats:
        details.append(f'脅威として報告された対象が{len(threats)}件あります。詳細ログを確認してください。')
    if errors:
        details.append(f'検査エラーが{errors}件あります。対象と理由は詳細ログを確認してください。')
    if 'Permission denied' in output:
        details.append('アクセス権限がないため、読み取れない対象があります。')
    kind = (ResultKind.CANCELLED if cancelled else ResultKind.INCOMPLETE if incomplete or limits or encrypted
            else ResultKind.DETECTED if detected else ResultKind.WARNING if warnings or code == 1 else ResultKind.CLEAN)
    return ScanStatus('\n'.join([title] + details), kind, detected)
