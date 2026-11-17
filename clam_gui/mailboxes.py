"""Bounded, read-only identification of detected mbox files. Never logs headers."""
from email.errors import MessageError
from email.parser import BytesHeaderParser
import os
from pathlib import Path
import re
import stat

MAILBOX_NOTE = 'メール単位の確認が必要です'
WARNING_PREFIXES = ('Heuristics.Limits.Exceeded.', 'Heuristics.Encrypted.',
                    'Heuristics.Broken.', 'Heuristics.OLE2.ContainsMacros')


def is_mbox(path):
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return False
            data = file.read(65536)
    except (OSError, ValueError):
        return False
    envelope, separator, rest = data.partition(b'\n')
    if not separator or not re.fullmatch(
            rb'From \S+ (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) '
            rb'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} '
            rb'\d{2}:\d{2}(?::\d{2})?(?: [^\r\n]+)? \d{4}\r?', envelope):
        return False
    # Require a complete header block, not an arbitrary text line that starts with From.
    end = re.search(rb'\r?\n\r?\n', rest)
    if not end:
        return False
    try:
        message = BytesHeaderParser().parsebytes(rest[:end.end()])
        return bool(message.get('From') and (message.get('Date') or message.get('Message-ID')))
    except (ValueError, TypeError, MessageError):
        return False


def thunderbird_storage(path):
    for parent in Path(path).parents:
        if parent.name in ('Mail', 'ImapMail'):
            return (parent.parent/'prefs.js').is_file()
    return False


def mailbox_findings(output, targets):
    roots = [Path(target).resolve() for target in targets]
    seen = set()
    findings = []
    for line in output.splitlines():
        match = re.fullmatch(r'(.+): ([^\n]+) FOUND', line)
        if not match or match[2].startswith(WARNING_PREFIXES):
            continue
        path = Path(match[1])
        if not path.is_absolute() or path in seen:
            continue
        seen.add(path)
        if len(seen) > 100:
            break
        try:
            resolved = path.resolve()
            if not any(resolved == root or root in resolved.parents for root in roots):
                continue
            if is_mbox(path):
                findings.append((str(path), thunderbird_storage(path)))
        except (OSError, RuntimeError):
            continue
    return findings


def mailbox_guidance(findings):
    if not findings:
        return ''
    lines = ['--- メール保存ファイルの確認 ---', f'検出された保存ファイル：{len(findings)}件',
             MAILBOX_NOTE,
             'mbox形式の保存ファイルです。複数のメールが含まれる場合があります。',
             '今回の結果では、対象のメールや添付ファイルは特定できていません。',
             'ファイル全体を削除・隔離すると、問題のないメールも利用できなくなる可能性があります。',
             '保存ファイル自体は削除せず、使用しているメールアプリで確認してください。']
    for path, thunderbird in findings:
        lines += ['\n対象ファイル：' + path]
        if thunderbird:
            lines.append('Thunderbirdのメール保存領域です。メール検査アドオンで対象フォルダーを検査してください。')
    return '\n'.join(lines)
