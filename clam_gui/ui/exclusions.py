"""Explore retained findings by folder, then explicitly confirm plan changes."""
from gi.repository import GLib, Gtk, Pango
from ..async_reader import LatestReader
from ..plan_exclusions import exclusion_state, folder_reports
from ..report import build_report

PAGE_SIZE = 50


class ExclusionWindow(Gtk.Window):
    def __init__(self, pane):
        super().__init__(title='フォルダー別の検査結果', transient_for=pane.get_root(), modal=True)
        self.set_application(pane.get_root().get_application())
        self.set_default_size(760, 600)
        self.pane, self.entry, self.key = pane, pane.entry, pane.plan_id
        self.store = pane.store
        self.reader = LatestReader(GLib.idle_add)
        self.connect('unrealize', lambda *_: self.reader.close())
        self.selected = set()
        self.page = 0
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                           margin_start=16, margin_end=16, margin_top=16, margin_bottom=16)
        self.set_child(self.box)
        self.info = pane.label('保持したログをフォルダー別に整理しています…')
        self.box.append(self.info)
        self.message = pane.label('')
        self.box.append(self.message)
        self.listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.listing)
        self.box.append(scroll)
        navigation = Gtk.Box(spacing=8)
        self.back = Gtk.Button(label='前へ', sensitive=False)
        self.next = Gtk.Button(label='次へ', sensitive=False)
        self.page_label = Gtk.Label(hexpand=True)
        self.back.connect('clicked', lambda _: self.turn(-1))
        self.next.connect('clicked', lambda _: self.turn(1))
        for widget in (self.back, self.page_label, self.next):
            navigation.append(widget)
        self.box.append(navigation)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        close = Gtk.Button(label='閉じる')
        close.connect('clicked', lambda _: self.destroy())
        self.review = Gtk.Button(label='選んだ除外候補を確認…', sensitive=False)
        self.review.add_css_class('suggested-action')
        self.review.connect('clicked', self.confirm)
        actions.append(close); actions.append(self.review)
        self.box.append(actions)
        self.reader.submit(self.load, self.loaded)

    def load(self):
        report = build_report(self.entry, preview_limit=None)
        folders = folder_reports(report)
        plan = dict(self.store.profiles()).get(self.key) if self.store and self.key else None
        threats = tuple(f.path for group in report.detections for f in group.examples)
        states = {folder.path: self.folder_reason(folder, plan, threats) for folder in folders}
        return report, folders, plan, threats, states

    def loaded(self, result, error):
        if error:
            self.info.set_text('読み込めませんでした：' + str(error))
            return
        self.full_report, self.folders, self.plan, self.threats, self.states = result
        self.expected_target = tuple(self.plan.targets) if self.plan else ()
        name = self.plan.label if self.plan else '変更できる検査プランはありません'
        self.info.set_text('現在の検査プラン：' + name + '\n'
                           'ファイルは直近の親フォルダーでまとめ、開けないディレクトリはその場所を表示します。'
                           '件数は保持された報告の件数です。権限不足・開けない場所を除外候補として選べます。')
        if self.full_report.coverage.startswith('この記録はログの一部が省略'):
            self.info.set_text(self.info.get_text() + '\nログが省略されており、全体の内訳ではありません。')
        self.render()

    @staticmethod
    def folder_reason(folder, plan, threats):
        if plan is None:
            return '検査プランが削除されているか、この履歴から変更できません。'
        if folder.detected or not folder.eligible:
            return 'この報告は除外候補の対象外です。内容の確認を優先してください。'
        return exclusion_state(plan, folder.path, threats)

    def render(self):
        while self.listing.get_first_child():
            self.listing.remove(self.listing.get_first_child())
        start = self.page * PAGE_SIZE
        self.checks = {}
        for folder in self.folders[start:start+PAGE_SIZE]:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row = Gtk.Box(spacing=8)
            reason = self.states[folder.path]
            check = Gtk.CheckButton(active=folder.path in self.selected,
                                    sensitive=not reason, hexpand=True)
            check.set_child(Gtk.Label(label=folder.path, xalign=0, wrap=True,
                                      wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=60))
            check.update_property([Gtk.AccessibleProperty.LABEL], [folder.path + 'を除外候補に選択'])
            check.set_tooltip_text(reason or 'このフォルダーと配下すべてを除外候補に追加')
            check.connect('toggled', lambda widget, path=folder.path: self.toggle(path, widget.get_active()))
            self.checks[folder.path] = check
            row.append(check)
            copy = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text='このフォルダーの明細をコピー')
            copy.add_css_class('flat')
            copy.update_property([Gtk.AccessibleProperty.LABEL], [folder.path + 'の明細をコピー'])
            copy.connect('clicked', lambda _, item=folder: self.get_clipboard().set(self.folder_text(item)))
            row.append(copy)
            box.append(row)
            box.append(self.pane.label(' / '.join(f'{name}：{count:,}件' for name, count in folder.reasons), 'dim-label'))
            if reason:
                box.append(self.pane.label(reason, 'dim-label'))
            expander = Gtk.Expander(label=f'明細：{len(folder.findings):,}件')
            expander.connect('notify::expanded', lambda widget, _, item=folder: self.fill_details(widget, item))
            box.append(expander)
            self.listing.append(box)
            self.listing.append(Gtk.Separator())
        total = len(self.folders)
        self.page_label.set_text(f'{start+1 if total else 0}–{min(start+PAGE_SIZE,total)} / {total:,}フォルダー')
        self.back.set_sensitive(self.page > 0)
        self.next.set_sensitive(start + PAGE_SIZE < total)
        self.review.set_sensitive(bool(self.selected))

    def folder_text(self, folder):
        return folder.path + '\n\n' + self.full_report.coverage + '\n\n' + '\n\n'.join(
            finding.path + '\n' + finding.detail for finding in folder.findings)

    def fill_details(self, widget, folder):
        if not widget.get_expanded() or widget.get_child():
            return
        view = Gtk.TextView(editable=False, cursor_visible=True, accepts_tab=False,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR)
        # The full category is available through copy, keeping UI layout bounded.
        text = '\n\n'.join(f.path + '\n' + f.detail for f in folder.findings[:100])
        if len(folder.findings) > 100:
            text += '\n\n先頭100件を表示しています。コピーには保持済み全件を含みます。'
        view.get_buffer().set_text(text)
        view.update_property([Gtk.AccessibleProperty.LABEL], [folder.path + 'の明細'])
        scroll = Gtk.ScrolledWindow(min_content_height=180, max_content_height=280, propagate_natural_height=True)
        scroll.set_child(view)
        widget.set_child(scroll)

    def toggle(self, path, active):
        if active:
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self.review.set_sensitive(bool(self.selected))
        self.message.set_text(f'除外候補：{len(self.selected)}フォルダー')

    def turn(self, step):
        self.page += step
        self.render()

    def confirm(self, _button):
        if not self.selected or not self.plan:
            return
        current = dict(self.store.profiles()).get(self.key)
        if current is None or tuple(current.targets) != self.expected_target:
            self.message.set_text('プランが削除されたか対象が変更されています。画面を開き直してください。')
            return
        paths = sorted(self.selected)
        for path in paths:
            reason = exclusion_state(current, path, self.threats)
            if reason and reason != 'すでに除外されています。':
                self.message.set_text(reason)
                return
        dialog = Gtk.Window(title='除外するフォルダーの確認', transient_for=self, modal=True)
        dialog.set_default_size(660, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=16, margin_end=16, margin_top=16, margin_bottom=16)
        dialog.set_child(box)
        box.append(self.pane.label('変更する現在の検査プラン：' + current.label, 'heading'))
        box.append(self.pane.label('現在の対象：' + str(current.targets[0])))
        box.append(self.pane.label('以下のフォルダーと配下すべてが、次回から検査対象外になります。実際のファイルは変更しません。'))
        listing = Gtk.ScrolledWindow(vexpand=True)
        listing.set_child(self.pane.label('\n\n'.join(path + ('（すでに除外済み）' if exclusion_state(current,path)=='すでに除外されています。' else '') for path in paths)))
        box.append(listing)
        dialog.error_label = self.pane.label('')
        dialog.error_label.set_visible(False)
        box.append(dialog.error_label)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label='キャンセル')
        cancel.connect('clicked', lambda _: dialog.destroy())
        save = Gtk.Button(label='除外を追加して保存')
        save.add_css_class('suggested-action')
        save.connect('clicked', lambda _: self.save(dialog, paths))
        buttons.append(cancel); buttons.append(save)
        box.append(buttons)
        dialog.present()

    def save(self, dialog, paths):
        try:
            added = self.store.add_excluded_folders(self.key, paths, expected_target=self.expected_target)
        except Exception as error:
            self.message.set_text('保存できませんでした：' + str(error))
            dialog.error_label.set_text(self.message.get_text())
            dialog.error_label.set_visible(True)
            return
        self.selected.clear()
        dialog.destroy()
        self.message.set_text(f'{len(added)}フォルダーを除外に追加しました。次回の検査から適用されます。')
        try:
            self.pane.on_plan_changed()
            self.reader.submit(self.load, self.loaded)
        except Exception as error:
            self.message.set_text('除外は保存済みですが、表示を更新できませんでした：' + str(error))
