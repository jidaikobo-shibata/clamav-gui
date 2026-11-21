"""Plain-text serialization of scan results and retained category details."""


def report_text(report):
    chunks, ranges = [], []
    offset = 0

    def add(text, style=None):
        nonlocal offset
        if style:
            ranges.append((style, offset, offset + len(text)))
        chunks.append(text + '\n\n')
        offset += len(text) + 2

    add(report.title, 'title')
    add(report.description)
    add('次の対応', 'heading')
    add(report.action)
    add('\n'.join(f'{name}：{value}' for name, value in report.metrics))
    for caution in report.cautions:
        add(caution, 'heading')
    add('検出内容・確認が必要な理由', 'heading')
    add(report.coverage)
    for group in report.detections + report.groups:
        add(f'{group.title}：{group.count:,}件', 'heading')
        add(group.action)
        for finding in group.examples:
            add((finding.path + '\n' if finding.path else '') + finding.detail)
        if group.count > len(group.examples):
            add(f'先頭{len(group.examples):,}件を表示しています。残りは保持した詳細ログで確認してください。')
    if not report.groups and not report.detections:
        add('保持したログに検出・警告・エラーの明細はありません。')
    if report.skips:
        add('通常の検査省略（件数）', 'heading')
        add('\n'.join(f'{name}：{value}' for name, value in report.skips))
    return ''.join(chunks), tuple(ranges)


def group_text(group):
    """Copy a complete retained category, or render a bounded preview."""
    chunks = [f'{group.title}：{group.count:,}件', group.action]
    for finding in group.examples:
        chunks.append((finding.path + '\n' if finding.path else '') + finding.detail)
    if group.count > len(group.examples):
        chunks.append(f'先頭{len(group.examples):,}件を表示しています。残りは保持した詳細ログで確認してください。')
    return '\n\n'.join(chunks)
