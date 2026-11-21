"""Bounded metadata-only inspection of immediate folder contents."""
from dataclasses import dataclass
import os
from pathlib import Path
import time


@dataclass(frozen=True)
class FolderCounts:
    files: int = 0
    directories: int = 0
    links: int = 0
    other: int = 0
    unknown: int = 0
    complete: bool = True
    reason: str = ''

    def label(self):
        if self.reason == 'unavailable':
            return '直下：確認できません（権限・存在・接続状態など）'
        if self.reason == 'symlink':
            return '直下：リンクのため集計していません'
        prefix = '直下' if self.complete else '直下（確認できた範囲）'
        text = f'{prefix}：ファイル{self.files:,}件・フォルダー{self.directories:,}件'
        for name, count in [('リンク', self.links), ('特殊ファイル', self.other), ('未確認', self.unknown)]:
            if count:
                text += f'・{name}{count:,}件'
        if self.reason == 'limit':
            text += '（集計上限）'
        return text


def count_children(path, *, cancel=None, limit=20000, seconds=0.25):
    """Do not recurse, read file contents or follow links. None means cancelled."""
    if cancel is not None and cancel.is_set():
        return None
    files = directories = links = other = unknown = 0
    started = time.monotonic()
    try:
        if Path(path).is_symlink():
            return FolderCounts(complete=False, reason='symlink')
        with os.scandir(path) as entries:
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    return None
                if files+directories+links+other+unknown >= limit or time.monotonic()-started >= seconds:
                    return FolderCounts(files, directories, links, other, unknown, False, 'limit')
                try:
                    if entry.is_symlink():
                        links += 1
                    elif entry.is_file(follow_symlinks=False):
                        files += 1
                    elif entry.is_dir(follow_symlinks=False):
                        directories += 1
                    else:
                        other += 1
                except OSError:
                    unknown += 1
    except OSError:
        if not (files+directories+links+other+unknown):
            return FolderCounts(complete=False, reason='unavailable')
        return FolderCounts(files, directories, links, other, unknown, False, 'unavailable-partial')
    return FolderCounts(files, directories, links, other, unknown, not unknown)


def local_folder_path(text):
    path = Path(text)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('存在するフォルダーの絶対パスを指定してください。')
    return os.path.normpath(str(path))


def total_children_label(values, *, checking=False):
    """Values include None for uninspected folders, never silently assume zero."""
    values = tuple(values)
    known = [value for value in values if value is not None]
    text = (f'除外される直下：ファイル{sum(value.files for value in known):,}件・'
            f'フォルダー{sum(value.directories for value in known):,}件')
    for label, total in [('リンク', sum(value.links for value in known)),
                         ('特殊ファイル', sum(value.other for value in known))]:
        if total:
            text += f'・{label}{total:,}件'
    pending = sum(value is None for value in values)
    partial = sum(not value.complete for value in known)
    if checking and pending:
        text += f'（確認中：{pending:,}フォルダー）'
    if partial or (pending and not checking):
        text += f'（未確認・一部集計：{partial + (pending if not checking else 0):,}フォルダー）'
    return text
