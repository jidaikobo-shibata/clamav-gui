"""Protect private application paths even when a watched path uses an alias."""
from dataclasses import replace
from pathlib import Path


def safe_watch_plan(plan, data_directory):
    if len(plan.targets) != 1:
        raise ValueError('フォルダー監視の対象は1つだけ指定してください。')
    root = Path(plan.targets[0])
    real_root = root.resolve()
    protected = (Path(data_directory).resolve(), (Path.home()/'.clamdlog').resolve())
    if any(real_root == path or path in real_root.parents for path in protected):
        raise ValueError('アプリ自身の保存先・ロック用フォルダーは監視できません。')
    # Express resolved private descendants through the user's selected root.
    # GIO events and clamscan exclusions use that same spelling of the path.
    excluded = list(plan.excluded_dirs)
    for path in protected:
        if real_root in path.parents:
            translated = str(root/path.relative_to(real_root))
            if translated not in excluded:
                excluded.append(translated)
    return replace(plan, excluded_dirs=excluded)
