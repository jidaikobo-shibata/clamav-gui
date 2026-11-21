"""Progressive disclosure for scan results; large raw buffers are loaded on demand."""
from gi.repository import Gtk, Pango
from ..report_text import report_text


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
        self.summary = None
        self.summary_copy = None
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

    def show_entry(self, entry, report):
        self.clear()
        self.entry, self.report = entry, report
        self.summary_copy = Gtk.Button(label='表示内容をコピー', halign=Gtk.Align.END)
        self.summary_copy.connect('clicked', self.copy_summary)
        self.append(self.summary_copy)
        self.summary = Gtk.TextView(editable=False, cursor_visible=True, accepts_tab=False,
                                    wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                    pixels_above_lines=2, pixels_below_lines=2)
        self.summary.update_property([Gtk.AccessibleProperty.LABEL], ['検査結果と理由別の明細'])
        buffer = self.summary.get_buffer()
        text, ranges = report_text(report)
        buffer.set_text(text)
        buffer.create_tag('title', weight=Pango.Weight.BOLD, scale=1.25)
        buffer.create_tag('heading', weight=Pango.Weight.BOLD)
        for style, start, end in ranges:
            buffer.apply_tag_by_name(style, buffer.get_iter_at_offset(start), buffer.get_iter_at_offset(end))
        buffer.place_cursor(buffer.get_start_iter())
        self.summary_scroll = Gtk.ScrolledWindow(min_content_height=360, vexpand=True, hexpand=True)
        self.summary_scroll.set_child(self.summary)
        self.append(self.summary_scroll)
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

    def copy_summary(self, _button):
        if self.summary is not None:
            buffer = self.summary.get_buffer()
            self.get_clipboard().set(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))
