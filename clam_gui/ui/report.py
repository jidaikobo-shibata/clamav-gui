"""Progressive disclosure for scan results; large raw buffers are loaded on demand."""
from gi.repository import Gtk, Pango


class ReportPane(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                         margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.entry = None
        self.report = None
        self.raw_loaded = False
        self.raw = Gtk.TextView(editable=False, cursor_visible=False,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.raw.update_property([Gtk.AccessibleProperty.LABEL], ['保持した詳細ログ'])
        self.show_message('検査記録を選んでください。')

    def label(self, text, style=None):
        widget = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if style:
            widget.add_css_class(style)
        return widget

    def clear(self):
        while self.get_first_child():
            self.remove(self.get_first_child())
        self.raw.get_buffer().set_text('')
        self.raw_loaded = False

    def show_message(self, message):
        self.clear()
        self.entry = self.report = None
        self.message = message
        self.append(self.label(message))

    def get_buffer(self):
        if not self.raw_loaded:
            if self.entry is None:
                text = self.message
            else:
                entry = self.entry
                text = entry.attention_notice() + entry.date_label() + '\n' + entry.status + '\n\n' + entry.target + '\n\n' + entry.output
            self.raw.get_buffer().set_text(text)
            self.raw_loaded = True
        return self.raw.get_buffer()

    def details(self, title, fill, expanded=False):
        expander = Gtk.Expander(label=title)
        def populate(widget, _property):
            if widget.get_expanded() and widget.get_child() is None:
                widget.set_child(fill())
        expander.connect('notify::expanded', populate)
        self.append(expander)
        expander.set_expanded(expanded)
        return expander

    def group(self, group, expanded=False):
        def fill():
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_start=12, margin_top=8, margin_bottom=8)
            box.append(self.label(group.action))
            for finding in group.examples:
                item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                if finding.path:
                    item.append(self.label(finding.path))
                item.append(self.label(finding.detail, 'dim-label'))
                box.append(item)
            if group.count > len(group.examples):
                box.append(self.label(f'先頭{len(group.examples):,}件を表示しています。残りは保持した詳細ログで確認してください。'))
            return box
        return self.details(f'{group.title}：{group.count:,}件', fill, expanded)

    def show_entry(self, entry, report):
        self.clear()
        self.entry, self.report = entry, report
        self.append(self.label(report.title, 'title-2'))
        self.append(self.label(report.description))
        self.append(self.label('次の対応', 'heading'))
        self.append(self.label(report.action))
        grid = Gtk.Grid(column_spacing=18, row_spacing=8)
        for row, (name, value) in enumerate(report.metrics):
            grid.attach(self.label(name, 'dim-label'), 0, row, 1, 1)
            grid.attach(self.label(value), 1, row, 1, 1)
        self.append(grid)
        for caution in report.cautions:
            self.append(self.label(caution))
        self.append(self.label('検出内容・確認が必要な理由', 'heading'))
        self.append(self.label(report.coverage, 'dim-label'))
        self.finding_expanders = [self.group(group, True) for group in report.detections]
        self.reason_expanders = [self.group(group) for group in report.groups]
        if not report.groups and not report.detections:
            self.append(self.label('保持したログに検出・警告・エラーの明細はありません。'))
        if report.skips:
            self.details('通常の検査省略（件数）', lambda: self.label('\n'.join(f'{name}：{value}' for name, value in report.skips)))
        self.details('この検査で使用した条件', lambda: self.label(report.conditions or '条件の記録はありません。'))
        def raw_details():
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            button = Gtk.Button(label='詳細ログをコピー', halign=Gtk.Align.START)
            def copy(_button):
                buffer = self.get_buffer()
                self.get_clipboard().set(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))
            button.connect('clicked', copy)
            box.append(button)
            scroll = Gtk.ScrolledWindow(min_content_height=240, height_request=320)
            # Release the previous expander's ownership when selecting another record.
            parent = self.raw.get_parent()
            if parent:
                parent.set_child(None)
            self.get_buffer()
            scroll.set_child(self.raw)
            box.append(scroll)
            return box
        self.raw_expander = self.details('保持した詳細ログ', raw_details)
