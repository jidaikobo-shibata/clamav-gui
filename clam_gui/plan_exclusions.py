"""Folder-level report exploration and conservative exclusion validation."""
from collections import Counter
from dataclasses import dataclass
import os
from pathlib import Path
from .known_locations import trash_folder

ELIGIBLE_REASONS = {'権限不足', 'ディレクトリを開けない'}


@dataclass(frozen=True)
class FolderReport:
    path: str
    findings: tuple
    reasons: tuple
    eligible: bool
    detected: bool


def folder_reports(report):
    buckets = {}
    for group in report.detections + report.groups:
        for finding in group.examples:
            path = Path(finding.path)
            if not finding.path or not path.is_absolute():
                continue
            folder = path if group.title == 'ディレクトリを開けない' else path.parent
            key = os.path.normpath(str(folder))
            bucket = buckets.setdefault(key, [[], Counter(), False, False])
            bucket[0].append(finding)
            bucket[1][group.title] += 1
            bucket[2] |= group.title in ELIGIBLE_REASONS
            bucket[3] |= group in report.detections
    return tuple(FolderReport(key, tuple(value[0]), tuple(value[1].items()), value[2], value[3])
                 for key, value in sorted(buckets.items()))


def exclusion_state(plan, folder, threats=()):
    """Return a reason when a folder must not be offered for exclusion."""
    if plan.kind == 'media':
        return '外部メディア検査の除外はここでは変更できません。'
    root = Path(os.path.normpath(str(plan.targets[0])))
    path = Path(os.path.normpath(str(folder)))
    if not path.is_absolute() or path == root or root not in path.parents:
        return '検査対象全体・対象外の場所は除外候補にできません。'
    try:
        real_root, real_path = root.resolve(), path.resolve()
        if real_root == real_path or real_root not in real_path.parents:
            return 'リンク先が検査対象全体・対象外の場所になるため選択できません。'
        if not path.is_dir():
            # Register the known trash before first use without creating it.
            if not (path == trash_folder() and not path.exists() and not path.is_symlink()):
                return '現在のフォルダーを確認できません。'
        for target in threats:
            detected = Path(os.path.normpath(str(target)))
            resolved = detected.resolve()
            if detected == path or path in detected.parents or resolved == real_path or real_path in resolved.parents:
                return '脅威が報告された場所を含むため、除外候補にできません。'
    except (OSError, RuntimeError):
        return 'フォルダーの状態を確認できません。'
    for item in plan.excluded_dirs:
        excluded = Path(os.path.normpath(str(item)))
        if excluded == path or excluded in path.parents:
            return 'すでに除外されています。'
    return ''


def covered_by(folder, exclusions):
    path = Path(os.path.normpath(str(folder)))
    return any(path == Path(os.path.normpath(str(item))) or
               Path(os.path.normpath(str(item))) in path.parents for item in exclusions)


def highlight_path(path, query):
    """Literal, case-sensitive highlighting; never interpret log text as markup."""
    from html import escape
    if not query:
        return escape(path, quote=False)
    return ('<b>' + escape(query, quote=False) + '</b>').join(
        escape(part, quote=False) for part in path.split(query))
