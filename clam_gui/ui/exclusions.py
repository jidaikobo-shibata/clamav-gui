"""Draft folder exclusions from retained logs, with explicit confirmation."""
from dataclasses import replace
import os
from gi.repository import GLib, Gtk, Pango
from ..async_reader import LatestReader
from ..plan_exclusions import exclusion_state, folder_reports, covered_by, highlight_path
from ..report import build_report

PAGE_SIZE = 50


class ExclusionWindow(Gtk.Window):
    def __init__(self, pane):
        super().__init__(title='フォルダー別の検査結果', transient_for=pane.get_root(), modal=True)
        self.set_application(pane.get_root().get_application())
        self.set_default_size(780, 700)
        self.pane, self.entry, self.key = pane, pane.entry, pane.plan_id
        self.store = pane.store
        self.plan = None
        self.initial_exclusions = []
        self.draft_exclusions = []
        self.folders = ()
        self.path_labels = []
        self.page = 0
        self.reader = LatestReader(GLib.idle_add)
        self.connect('unrealize', lambda *_: self.reader.close())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_start=16, margin_end=16, margin_top=16, margin_bottom=16)
        self.set_child(box)
        self.info = pane.label('保持したログをフォルダー別に整理しています…')
        box.append(self.info)
        row = Gtk.Box(spacing=8)
        caption = Gtk.Label(label='パス')
        self.path_entry = Gtk.Entry(hexpand=True, placeholder_text='除外するフォルダーの絶対パス')
        caption.set_mnemonic_widget(self.path_entry)
        self.path_entry.update_property([Gtk.AccessibleProperty.LABEL], ['除外するフォルダーのパス'])
        self.path_entry.connect('changed', self.highlight)
        self.path_entry.connect('activate', self.add_path)
        self.add_button = Gtk.Button(label='追加する', sensitive=False)
        self.add_button.connect('clicked', self.add_path)
        for widget in (caption, self.path_entry, self.add_button):
            row.append(widget)
        box.append(row)
        box.append(pane.label('フォルダーと配下すべてを除外します。入力中は一致部分を強調するだけです。保存するまで設定は変わりません。', 'dim-label'))
        self.counter = pane.label('')
        box.append(self.counter)
        self.message = pane.label('')
        box.append(self.message)
        box.append(pane.label('現在の除外設定（フォルダー・保存前の変更案）', 'heading'))
        self.excluded_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        excluded_scroll = Gtk.ScrolledWindow(min_content_height=55, max_content_height=140,
                                             propagate_natural_height=True)
        excluded_scroll.set_child(self.excluded_list)
        box.append(excluded_scroll)
        self.files_notice = pane.label('')
        self.files_notice.set_visible(False)
        box.append(self.files_notice)
        box.append(pane.label('ログに出たフォルダー（除外後に残る候補）', 'heading'))
        self.listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.listing)
        box.append(scroll)
        navigation = Gtk.Box(spacing=8)
        self.back = Gtk.Button(label='前へ', sensitive=False)
        self.next = Gtk.Button(label='次へ', sensitive=False)
        self.page_label = Gtk.Label(hexpand=True)
        self.back.connect('clicked', lambda _: self.turn(-1))
        self.next.connect('clicked', lambda _: self.turn(1))
        for widget in (self.back, self.page_label, self.next):
            navigation.append(widget)
        box.append(navigation)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label='キャンセル')
        cancel.connect('clicked', lambda _: self.destroy())
        self.review = Gtk.Button(label='保存…', sensitive=False)
        self.review.add_css_class('suggested-action')
        self.review.connect('clicked', self.confirm)
        actions.append(cancel); actions.append(self.review)
        box.append(actions)
        self.reader.submit(self.load, self.loaded)

    def load(self):
        report = build_report(self.entry, preview_limit=None)
        plan = dict(self.store.profiles()).get(self.key) if self.store and self.key else None
        threats = tuple(f.path for group in report.detections for f in group.examples)
        return report, folder_reports(report), plan, threats

    def loaded(self, result, error):
        if error:
            self.info.set_text('読み込めませんでした：' + str(error))
            return
        self.full_report, self.folders, self.plan, self.threats = result
        self.expected_target = tuple(self.plan.targets) if self.plan else ()
        self.initial_exclusions = list(self.plan.excluded_dirs) if self.plan else []
        self.draft_exclusions = list(self.initial_exclusions)
        name = self.plan.label if self.plan else '変更できる検査プランはありません'
        self.info.set_text('現在の検査プラン：' + name + '\n件数は保持したログに報告があったフォルダー・報告の件数です。')
        if self.full_report.coverage.startswith('この記録はログの一部が省略'):
            self.info.set_text(self.info.get_text() + '\nログが省略されており、全体の内訳ではありません。')
        self.files_notice.set_visible(bool(self.plan and self.plan.excludes))
        if self.plan and self.plan.excludes:
            self.files_notice.set_text(f'ファイル単位の除外：{len(self.plan.excludes)}件（この画面では変更しません）')
        self.render()

    def path_label(self, path):
        label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                          max_width_chars=65, hexpand=True)
        label.set_markup(highlight_path(path, self.path_entry.get_text()))
        self.path_labels.append((path, label))
        return label

    def highlight(self, *_args):
        query = self.path_entry.get_text()
        for path, label in self.path_labels:
            label.set_markup(highlight_path(path, query))
        self.add_button.set_sensitive(bool(query) and self.plan is not None and self.plan.kind != 'media')

    @staticmethod
    def clear_box(box):
        while box.get_first_child():
            box.remove(box.get_first_child())

    def render(self):
        self.clear_box(self.excluded_list)
        self.clear_box(self.listing)
        self.path_labels = []
        self.remove_buttons = {}
        editable = self.plan is not None and self.plan.kind != 'media'
        for path in self.draft_exclusions:
            row = Gtk.Box(spacing=8)
            row.append(self.path_label(path))
            remove = Gtk.Button(label='取り消す', sensitive=editable)
            remove.add_css_class('flat')
            remove.update_property([Gtk.AccessibleProperty.LABEL], [path + 'の除外を取り消す'])
            remove.connect('clicked', lambda _, value=path: self.remove_path(value))
            row.append(remove)
            self.remove_buttons[path] = remove
            self.excluded_list.append(row)
        if not self.draft_exclusions:
            self.excluded_list.append(self.pane.label('フォルダーの除外はありません。', 'dim-label'))
        remaining = [item for item in self.folders if not covered_by(item.path, self.draft_exclusions)]
        self.counter.set_text(f'ログ中のフォルダー：除外後に残る {len(remaining):,} / {len(self.folders):,}件')
        self.page = min(self.page, max(0, (len(remaining)-1)//PAGE_SIZE))
        start = self.page * PAGE_SIZE
        self.candidate_buttons = {}
        for folder in remaining[start:start+PAGE_SIZE]:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row = Gtk.Box(spacing=6)
            path_button = Gtk.Button(hexpand=True, tooltip_text='このパスを入力欄へ入れる（まだ除外しません）')
            path_button.add_css_class('flat')
            path_button.set_child(self.path_label(folder.path))
            path_button.update_property([Gtk.AccessibleProperty.LABEL], [folder.path + 'を入力欄へ'])
            path_button.connect('clicked', lambda _, path=folder.path: self.path_entry.set_text(path))
            self.candidate_buttons[folder.path] = path_button
            row.append(path_button)
            copy = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text='このフォルダーの明細をコピー')
            copy.add_css_class('flat')
            copy.update_property([Gtk.AccessibleProperty.LABEL], [folder.path + 'の明細をコピー'])
            copy.connect('clicked', lambda _, item=folder: self.get_clipboard().set(self.folder_text(item)))
            row.append(copy)
            box.append(row)
            box.append(self.pane.label(' / '.join(f'{name}：{count:,}件' for name, count in folder.reasons), 'dim-label'))
            expander = Gtk.Expander(label=f'明細：{len(folder.findings):,}件')
            expander.connect('notify::expanded', lambda widget, _, item=folder: self.fill_details(widget, item))
            box.append(expander)
            self.listing.append(box)
            self.listing.append(Gtk.Separator())
        if not remaining:
            self.listing.append(self.pane.label('ログに出たフォルダーは、すべて除外範囲に含まれています。' if self.folders else 'ログからフォルダーを抽出できませんでした。'))
        total = len(remaining)
        self.page_label.set_text(f'{start+1 if total else 0}–{min(start+PAGE_SIZE,total)} / {total:,}フォルダー')
        self.back.set_sensitive(self.page > 0)
        self.next.set_sensitive(start + PAGE_SIZE < total)
        self.review.set_sensitive(editable and set(self.initial_exclusions) != set(self.draft_exclusions))
        self.highlight()

    def add_path(self, *_args):
        if self.plan is None or not self.path_entry.get_text():
            return
        # normpath alone would turn a partial/relative input into a misleading candidate.
        raw = self.path_entry.get_text()
        draft = replace(self.plan, excluded_dirs=list(self.draft_exclusions))
        reason = exclusion_state(draft, raw, self.threats)
        if reason:
            self.message.set_text(reason)
            return
        path = os.path.normpath(raw)
        affected = [item for item in self.folders if covered_by(item.path, [path]) and
                    not covered_by(item.path, self.draft_exclusions)]
        self.draft_exclusions.append(path)
        self.message.set_text(f'変更案に追加しました。ログ中の{len(affected):,}フォルダー・'
                              f'{sum(len(item.findings) for item in affected):,}件の報告が除外範囲に入ります。まだ保存していません。')
        self.page = 0
        self.render()

    def remove_path(self, path):
        self.draft_exclusions.remove(path)
        self.message.set_text('変更案から除外を取り消しました。まだ保存していません。')
        self.render()

    def turn(self, step):
        self.page += step
        self.render()
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


    def confirm(self, _button):
        if self.plan is None:
            return
        additions = sorted(set(self.draft_exclusions) - set(self.initial_exclusions))
        removals = sorted(set(self.initial_exclusions) - set(self.draft_exclusions))
        if not additions and not removals:
            return
        try:
            current = dict(self.store.profiles()).get(self.key)
            if current is None or tuple(current.targets) != self.expected_target:
                raise ValueError('プランが削除されたか対象が変更されています。画面を開き直してください。')
            current_draft = replace(current, excluded_dirs=[path for path in current.excluded_dirs if path not in removals])
            for path in additions:
                reason = exclusion_state(current_draft, path, self.threats)
                if reason and reason != 'すでに除外されています。':
                    raise ValueError(reason)
        except Exception as error:
            self.message.set_text('変更を確認できませんでした：' + str(error))
            return
        dialog = Gtk.Window(title='除外設定の変更を確認', transient_for=self, modal=True)
        dialog.set_default_size(660, 440)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=16, margin_end=16, margin_top=16, margin_bottom=16)
        dialog.set_child(box)
        box.append(self.pane.label('変更する現在の検査プラン：' + current.label, 'heading'))
        box.append(self.pane.label('現在の対象：' + str(current.targets[0])))
        text = '追加するフォルダーと配下すべては、次回から検査対象外になります。取り消した除外範囲は、他の除外に含まれなければ再び検査対象になります。実際のファイルは変更しません。'
        box.append(self.pane.label(text))
        listing = Gtk.ScrolledWindow(vexpand=True)
        changes = ('追加（配下すべてを除外）\n' + '\n'.join(additions) if additions else '')
        changes += ('\n\n取り消す除外\n' + '\n'.join(removals) if removals else '')
        listing.set_child(self.pane.label(changes))
        box.append(listing)
        dialog.error_label = self.pane.label('')
        dialog.error_label.set_visible(False)
        box.append(dialog.error_label)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label='キャンセル')
        cancel.connect('clicked', lambda _: dialog.destroy())
        save = Gtk.Button(label='保存')
        save.add_css_class('suggested-action')
        save.connect('clicked', lambda _: self.save(dialog, additions, removals))
        buttons.append(cancel); buttons.append(save)
        box.append(buttons)
        dialog.present()

    def save(self, dialog, additions, removals):
        try:
            added, removed = self.store.edit_excluded_folders(self.key, additions, removals,
                                      expected_target=self.expected_target, threats=self.threats)
        except Exception as error:
            self.message.set_text('保存できませんでした：' + str(error))
            dialog.error_label.set_text(self.message.get_text())
            dialog.error_label.set_visible(True)
            return
        dialog.destroy()
        self.message.set_text(f'追加{len(added)}件・取り消し{len(removed)}件を保存しました。次回の検査から適用されます。')
        try:
            self.pane.on_plan_changed()
            self.reader.submit(self.load, self.loaded)
        except Exception as error:
            self.message.set_text('除外は保存済みですが、表示を更新できませんでした：' + str(error))
