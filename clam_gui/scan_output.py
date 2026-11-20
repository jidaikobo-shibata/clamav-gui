"""Drain scanner output continuously into bounded memory, never a growing file."""
import os
import threading
from .results import warning_signature


class ScanOutput:
    TAIL = 1536 * 1024
    EVIDENCE = 256 * 1024
    LINE = 64 * 1024

    def __init__(self):
        self.tail = bytearray()
        self.pending = bytearray()
        self.evidence = []
        self.evidence_bytes = self.total = self.line_end = self.skipped = 0
        self.first_threat = None
        self.detected = self.issue = self.omitted = False
        self.error = None
        self.reader = self.writer = None
        self.thread = None

    def __enter__(self):
        self.reader, self.writer = os.pipe()
        try:
            self.thread = threading.Thread(target=self._drain, daemon=True)
            self.thread.start()
        except Exception:
            os.close(self.reader); os.close(self.writer)
            self.reader = self.writer = None
            raise
        return self

    def fileno(self):
        return self.writer

    def write(self, data):
        view = memoryview(data)
        count = 0
        while view:
            written = os.write(self.writer, view)
            view = view[written:]
            count += written
        return count

    def flush(self):
        pass

    def _line(self, line, end):
        text = line.decode('utf-8', errors='replace').rstrip('\r\n')
        found = text.endswith(' FOUND')
        if found and not warning_signature(text):
            self.detected = True
            if self.first_threat is None:
                self.first_threat = end, line
        if found or 'WARNING:' in text or 'ERROR' in text or 'Permission denied' in text:
            if not found or warning_signature(text):
                self.issue = True
            if self.evidence_bytes + len(line) <= self.EVIDENCE:
                self.evidence.append((end, line))
                self.evidence_bytes += len(line)
            else:
                self.omitted = True

    def _feed(self, chunk):
        self.total += len(chunk)
        self.tail.extend(chunk)
        if len(self.tail) > self.TAIL:
            del self.tail[:-self.TAIL]
        self.pending.extend(chunk)
        while True:
            newline = self.pending.find(b'\n')
            if newline < 0:
                break
            line = bytes(self.pending[:newline+1])
            del self.pending[:newline+1]
            self.line_end += self.skipped + len(line)
            self.skipped = 0
            self._line(line[-self.LINE:], self.line_end)
            if len(line) > self.LINE:
                self.omitted = True
        if len(self.pending) > self.LINE:
            extra = len(self.pending) - self.LINE
            self.skipped += extra
            del self.pending[:extra]
            self.omitted = True

    def _drain(self):
        try:
            while True:
                chunk = os.read(self.reader, 65536)
                if not chunk:
                    break
                self._feed(chunk)
            if self.pending:
                self._line(bytes(self.pending), self.total)
                self.pending.clear()
        except Exception as error:
            self.error = str(error)
        finally:
            os.close(self.reader)

    def finish(self):
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                self.error = '検査出力の受信完了を確認できませんでした。'
        cutoff = self.total - len(self.tail)
        tail = bytes(self.tail)
        if cutoff:
            newline = tail.find(b'\n')
            if newline >= 0:
                cutoff += newline + 1
                tail = tail[newline+1:]
        retained = [(end, line) for end, line in self.evidence if end <= cutoff]
        if self.first_threat and self.first_threat[0] <= cutoff and self.first_threat not in retained:
            retained.append(self.first_threat)
        # Keep the first actual threat ahead of verbose warning evidence so a
        # later storage-size cap cannot discard every detected signature.
        first = self.first_threat if self.first_threat and self.first_threat[0] <= cutoff else None
        prefix = (first[1] if first else b'') + b''.join(line for end, line in retained if (end, line) != first)
        text = (prefix + tail).decode('utf-8', errors='replace')
        if cutoff or self.omitted:
            text = 'WARNING: 出力の一部を省略しました。保持できた検出・警告と末尾のログを表示します。\n' + text
        if self.issue and 'WARNING:' not in text and 'ERROR' not in text and not any(warning_signature(line) for line in text.splitlines()):
            text += '\nWARNING: 保持範囲外にも検査上の警告・エラーがありました。\n'
        if self.error:
            text += '\nWARNING: 検査出力の読取りエラー：' + self.error
        return text

    def __exit__(self, *_):
        if self.writer is not None:
            self.finish()
