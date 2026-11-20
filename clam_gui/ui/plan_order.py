"""Accessible menu moves and internal drag-and-drop for display ordering."""
import uuid
from gi.repository import Gdk, Gio, GObject, Gtk


class PlanOrderWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app, title='検査プランの並べ替え',
                         transient_for=app.operation_parent(), modal=True)
        self.app = app
        self.drag_prefix = str(uuid.uuid4()) + ':'
        self.set_default_size(520, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=16, margin_end=16, margin_top=16, margin_bottom=16)
        self.set_child(box)
        box.append(Gtk.Label(label='左のハンドルをドラッグするか、各プランのメニューから移動できます。変更はすぐに保存されます。',
                             wrap=True, xalign=0))
        self.message = Gtk.Label(wrap=True, xalign=0, selectable=True, visible=False)
        box.append(self.message)
        self.listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listing.add_css_class('boxed-list')
        self.listing.set_placeholder(Gtk.Label(label='並べ替える検査プランはありません。', wrap=True))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.listing)
        box.append(scroll)
        close = Gtk.Button(label='閉じる', halign=Gtk.Align.END)
        close.connect('clicked', lambda _: self.destroy())
        box.append(close)
        self.rebuild()

    def rebuild(self, focus=None):
        while self.listing.get_first_child():
            self.listing.remove(self.listing.get_first_child())
        self.plans = self.app.store.display_profiles(self.app.store.automation_settings()['media_enabled'])
        self.rows = {}
        for index, (key, plan) in enumerate(self.plans):
            row = Gtk.ListBoxRow(activatable=False)
            line = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=10, margin_bottom=10)
            row.set_child(line)
            handle = Gtk.Image.new_from_icon_name('list-drag-handle-symbolic')
            handle.set_tooltip_text(plan.label + 'をドラッグして移動')
            line.append(handle)
            line.append(Gtk.Label(label=plan.label, xalign=0, wrap=True, hexpand=True))
            actions = Gio.SimpleActionGroup()
            menu = Gio.Menu()
            for name, label, step in [('up', '上へ移動(_U)', -1), ('down', '下へ移動(_D)', 1)]:
                action = Gio.SimpleAction.new(name, None)
                action.set_enabled(0 <= index + step < len(self.plans))
                action.connect('activate', lambda *_, k=key, d=step: self.move(k, d))
                actions.add_action(action)
                menu.append(label, 'order.' + name)
            more = Gtk.MenuButton(icon_name='view-more-symbolic', menu_model=menu,
                                  tooltip_text=plan.label + 'の表示順')
            more.update_property([Gtk.AccessibleProperty.LABEL], [plan.label + 'の表示順'])
            more.insert_action_group('order', actions)
            line.append(more)
            source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            source.connect('prepare', lambda *_, k=key: Gdk.ContentProvider.new_for_value(self.drag_prefix + k))
            source.connect('drag-begin', lambda source, drag, r=row: source.set_icon(Gtk.WidgetPaintable.new(r), 0, 0))
            handle.add_controller(source)
            target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
            target.connect('drop', lambda target, value, x, y, k=key, r=row:
                           self.drop(value, k, y >= r.get_height()/2))
            row.add_controller(target)
            self.listing.append(row)
            self.rows[key] = dict(row=row, menu=more, actions=actions, source=source, target=target)
        if focus in self.rows:
            self.rows[focus]['menu'].grab_focus()

    def apply(self, key, anchor, after=False):
        try:
            self.app.store.move_display_profile(key, anchor, after=after)
            self.app.refresh_profiles()
            self.rebuild(focus=key)
            self.message.set_visible(False)
            return True
        except Exception as error:
            self.message.set_text('並べ替えできませんでした：' + str(error))
            self.message.set_visible(True)
            return False

    def move(self, key, step):
        keys = [key for key, _ in self.plans]
        if key not in keys or step not in (-1, 1):
            return False
        index = keys.index(key) + step
        if not 0 <= index < len(keys):
            return False
        return self.apply(key, keys[index], after=step > 0)

    def drop(self, value, anchor, after):
        if not isinstance(value, str) or not value.startswith(self.drag_prefix):
            return False
        key = value[len(self.drag_prefix):]
        if key not in self.rows or anchor not in self.rows or key == anchor:
            return False
        return self.apply(key, anchor, after)
