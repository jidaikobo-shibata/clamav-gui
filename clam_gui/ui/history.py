"""Small history headers and a single selected body, loaded off the UI thread."""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gtk, Pango
from ..automation import MEDIA_ID, media_plan
from ..async_reader import LatestReader
from ..report import build_report
from .report import ReportPane


class HistoryController:
    def __init__(self, store, listing, text, changed=lambda entries: None):
        self.store, self.listing, self.text, self.changed = store, listing, text, changed
        self.entries = []
        self.selected = None
        self.shown = None
        self.loading = False
        self.latest_requested = False
        self.requested_token = None
        self.requested_id = None
        self.reader = LatestReader(GLib.idle_add)

    def read(self, load, done):
        try:
            self.reader.submit(load, done)
        except Exception as error:
            self.loading = False
            self.text.show_message('履歴の読込みを開始できませんでした：' + str(error))

    def reload(self, selected, *, latest=False, token=None):
        if selected != self.selected:
            self.requested_token = None
            self.requested_id = None
        if token is not None:
            self.requested_token = token or None
            self.requested_id = None
        self.latest_requested = latest or (self.latest_requested and selected == self.selected)
        old = self.listing.get_selected_row()
        old_id = self.entries[old.get_index()].id if old and old.get_index() < len(self.entries) else None
        if selected != self.selected:
            self.shown = None
            self.text.show_message('履歴を読み込んでいます…')
        self.loading = True
        self.selected = selected
        if not selected:
            self.read(lambda: [], lambda rows, error: self._headers(rows, error, None))
            return
        key, plan = selected
        requested = self.requested_token
        def load():
            rows = self.store.history_headers(key, plan)
            entry = self.store.notification_entry(key, requested, header_only=True) if requested else None
            if entry and not any(row.id == entry.id for row in rows):
                rows.append(entry)
            return rows, entry.id if entry else None
        def done(value, error):
            rows, ident = value if value else ([], None)
            self._headers(rows, error, old_id, requested, ident)
        self.read(load, done)

    def _headers(self, entries, error, old_id, requested=None, requested_id=None):
        self.loading = True
        while self.listing.get_first_child():
            self.listing.remove(self.listing.get_first_child())
        self.entries = entries or []
        self.changed(self.entries)
        for entry in self.entries:
            attention = entry.attention_label()
            label = Gtk.Label(label=entry.date_label() + ('\n' + attention if attention else ''), xalign=0,
                              margin_start=10, margin_end=10, margin_top=12, margin_bottom=12)
            if attention:
                attributes = Pango.AttrList()
                attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
                label.set_attributes(attributes)
                label.update_property([Gtk.AccessibleProperty.LABEL], [entry.date_label() + ' ' + attention])
            if entry.is_uncertain():
                label.set_tooltip_text('検出情報がありますが、明細不足のため内訳を確認できません。再検査してください。')
            self.listing.append(label)
        self.requested_id = requested_id
        preferred = requested_id if requested_id is not None else old_id
        index = 0 if self.latest_requested and requested_id is None else next((i for i, entry in enumerate(self.entries) if entry.id == preferred), 0)
        self.latest_requested = False
        if self.entries and (not requested or requested_id is not None):
            self.listing.select_row(self.listing.get_row_at_index(index))
        self.loading = False
        if error:
            self.shown = None
            self.text.show_message('履歴を読み込めませんでした：' + str(error))
        elif requested and requested_id is None:
            self.shown = None
            self.text.show_message('通知に対応する検査ログは既に整理されたか、保存されていません。別の履歴を選択してください。')
        else:
            self.select()

    def select(self):
        if self.loading:
            return
        row = self.listing.get_selected_row()
        if not self.selected or row is None or row.get_index() >= len(self.entries):
            self.shown = None
            self.text.show_message('検査記録はありません。')
            return
        ident = self.entries[row.get_index()].id
        if self.requested_token and ident != self.requested_id:
            self.requested_token = None
            self.requested_id = None
        if self.shown == ident:
            return
        self.shown = None
        self.text.show_message('ログを読み込んでいます…')
        key, plan = self.selected
        token = self.requested_token
        def load():
            entry = self.store.notification_entry(key, token) if token else self.store.history_entry(ident, key, plan)
            return (entry, build_report(entry) if entry else None)
        self.read(load, self._body)

    def _body(self, loaded, error):
        entry, report = loaded if loaded else (None, None)
        self.shown = entry.id if entry else None
        if error:
            self.text.show_message('ログを読み込めませんでした：' + str(error))
        elif entry is None:
            self.text.show_message('この履歴は整理されたため参照できません。履歴を再読み込みしてください。')
        else:
            self.text.show_entry(entry, report, profile_id=self.selected[0])

    def close(self):
        self.reader.close()


class HistoryView:
    def media_history(self, *, token=None):
        window = Gtk.Window(application=self, title='外部メディア検査の履歴', transient_for=self.operation_parent(), modal=True)
        window.set_default_size(900, 550)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                      margin_bottom=12, margin_start=12, margin_end=12)
        window.set_child(box)
        stop = Gtk.Button(label='実行中の外部メディア検査を中止')
        stop.connect('clicked', lambda _: self.stop_plan(MEDIA_ID))
        box.append(stop)
        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        listing.update_property([Gtk.AccessibleProperty.LABEL], ['外部メディアの検査完了日時'])
        scroll = Gtk.ScrolledWindow(width_request=210);scroll.set_child(listing)
        panes.set_start_child(scroll)
        text = ReportPane(self.store, self.refresh_profiles)
        text.update_property([Gtk.AccessibleProperty.LABEL], ['選択した外部メディア検査のログ'])
        scroll = Gtk.ScrolledWindow();scroll.set_child(text)
        panes.set_end_child(scroll);panes.set_position(230)
        box.append(panes)
        controller = HistoryController(self.store, listing, text)
        listing.connect('row-selected', lambda *_: controller.select())
        window.history_controller = controller
        window.connect('unrealize', lambda *_: controller.close())
        close = Gtk.Button(label='閉じる')
        close.connect('clicked', lambda _: window.destroy())
        box.append(close)
        window.present()
        controller.reload((MEDIA_ID, media_plan()), token=token)

    def history(self, _button, *, latest=False, token=None):
        if not hasattr(self, 'history_controller'):
            self.history_controller = HistoryController(self.store, self.history_list, self.result,
                                                         lambda entries: setattr(self, 'log_entries', entries))
        self.history_controller.reload(self.selected_profile(), latest=latest, token=token)

    def log_selected(self, *_args):
        if hasattr(self, 'history_controller'):
            self.history_controller.select()
