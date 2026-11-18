"""HistoryView view and its callbacks; OS services are injected through the application backend."""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango
from ..automation import MEDIA_ID, media_plan


class HistoryView:
    def media_history(self):
        window = Gtk.Window(application=self, title='外部メディア検査の履歴', transient_for=self.operation_parent(), modal=True)
        window.set_default_size(780, 550)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                      margin_bottom=12, margin_start=12, margin_end=12)
        window.set_child(box)
        stop = Gtk.Button(label='実行中の外部メディア検査を中止')
        stop.connect('clicked', lambda _: self.stop_plan(MEDIA_ID))
        box.append(stop)
        text = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        entries = self.store.history_entries(MEDIA_ID, media_plan())
        text.get_buffer().set_text('\n\n'.join(entry.date_label() + '\n' + entry.status + '\n' + entry.target + '\n' + entry.output
                                                 for entry in entries) or '検査記録はありません。')
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(text)
        box.append(scroll)
        close = Gtk.Button(label='閉じる')
        close.connect('clicked', lambda _: window.destroy())
        box.append(close)
        window.present()

    def history(self, _button):
        selected = self.selected_profile()
        old = self.history_list.get_selected_row()
        old_id = self.log_entries[old.get_index()].id if old and old.get_index() < len(self.log_entries) else None
        self.log_entries = self.store.history_entries(*selected) if selected else []
        self.loading_history = True
        while self.history_list.get_first_child():
            self.history_list.remove(self.history_list.get_first_child())
        for entry in self.log_entries:
            label = Gtk.Label(label=entry.date_label(), xalign=0,
                              margin_start=10, margin_end=10, margin_top=12, margin_bottom=12)
            if entry.is_detected():
                attributes = Pango.AttrList()
                attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
                label.set_attributes(attributes)
                label.update_property([Gtk.AccessibleProperty.LABEL], [entry.date_label() + ' 検出あり'])
            self.history_list.append(label)
        index = next((i for i, entry in enumerate(self.log_entries) if entry.id == old_id), 0)
        self.loading_history = False
        if self.log_entries:
            self.history_list.select_row(self.history_list.get_row_at_index(index))
        self.log_selected()

    def log_selected(self, *_args):
        if getattr(self, 'loading_history', False):
            return
        row = self.history_list.get_selected_row()
        if row is None or row.get_index() >= len(self.log_entries):
            self.result.get_buffer().set_text('検査記録はありません。')
            return
        entry = self.log_entries[row.get_index()]
        self.result.get_buffer().set_text(entry.date_label() + '\n' + entry.status + '\n\n' +
                                          entry.target + '\n\n' + entry.output)
