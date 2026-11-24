"""Draft folder exclusions from retained logs, with explicit confirmation."""
from dataclasses import replace
import os
from pathlib import Path
import threading
import time
from gi.repository import Gio, GLib, Gtk, Pango
from ..async_reader import LatestReader
from ..plan_exclusions import exclusion_state, folder_reports, covered_by, highlight_path
from ..report import build_report
from ..known_locations import trash_folder, resolve_location, location_label, location_confirmation
from ..folder_info import count_children, local_folder_path, total_children_label

PAGE_SIZE = 50


class ExclusionWindow(Gtk.Window):
    def __init__(self, pane):
        super().__init__(title='フォルダー別の検査結果', transient_for=pane.get_root(), modal=True)
        self.set_application(pane.get_root().get_application())
        self.set_default_size(1180, 760)
        self.pane, self.entry, self.key = pane, pane.entry, pane.plan_id
        self.store = pane.store
        self.plan = None
        self.initial_exclusions = []
        self.draft_exclusions = []
        self.folders = ()
        self.path_labels = []
        self.excluded_path_labels = []
        self.page = 0
        self.reader = LatestReader(GLib.idle_add)
        self.count_reader = LatestReader(GLib.idle_add)
        self.open_reader = LatestReader(GLib.idle_add)
        self.count_cancel = threading.Event()
        self.open_cancel = Gio.Cancellable()
        self.count_cache = {}
        self.count_generation = 0
        self.opening = False
        self.connect('unrealize', self.closed)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_start=16, margin_end=16, margin_top=16, margin_bottom=16)
        self.set_child(box)
        self.info = pane.label('保持したログをフォルダー別に整理しています…')
        box.append(self.info)
        self.panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, wide_handle=True)
        self.left_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_end=12)
        self.right_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=12)
        self.panes.set_start_child(self.left_pane)
        self.panes.set_end_child(self.right_pane)
        self.panes.set_position(680)
        box.append(self.panes)
        reason_row = Gtk.Box(spacing=8)
        reason_caption = Gtk.Label(label='理由')
        self.reason_names = ['すべて']
        self.reason_filter = Gtk.DropDown.new_from_strings(self.reason_names)
        self.reason_filter.set_sensitive(False)
        reason_caption.set_mnemonic_widget(self.reason_filter)
        self.reason_filter.update_property([Gtk.AccessibleProperty.LABEL], ['候補を絞り込む理由'])
        self.reason_handler = self.reason_filter.connect('notify::selected', self.search)
        reason_row.append(reason_caption)
        reason_row.append(self.reason_filter)
        self.trash_button = Gtk.Button(label='ごみ箱を追加')
        self.trash_button.set_tooltip_text('ホーム側のごみ箱の内容を除外候補に追加します。保存までは反映しません。外部メディアのごみ箱は含みません。\n' + str(trash_folder() or ''))
        self.trash_button.connect('clicked', self.add_trash)
        reason_row.append(self.trash_button)
        self.left_pane.append(reason_row)
        row = Gtk.Box(spacing=8)
        caption = Gtk.Label(label='パス')
        self.path_entry = Gtk.Entry(hexpand=True, placeholder_text='除外するフォルダーの絶対パス')
        caption.set_mnemonic_widget(self.path_entry)
        self.path_entry.update_property([Gtk.AccessibleProperty.LABEL], ['除外するフォルダーのパス'])
        self.path_search_handler = self.path_entry.connect('changed', self.search)
        self.path_entry.connect('activate', self.add_path)
        self.add_button = Gtk.Button(label='追加する', sensitive=False)
        self.add_button.connect('clicked', self.add_path)
        self.open_button = Gtk.Button(label='フォルダーを開く', sensitive=False,
                                      tooltip_text='入力したフォルダーをファイルマネージャーで開く')
        self.open_button.connect('clicked', self.open_folder)
        for widget in (caption, self.path_entry, self.open_button, self.add_button):
            row.append(widget)
        self.left_pane.append(row)
        self.counter = pane.label('')
        self.counter.set_visible(False)
        self.search_count = pane.label('ヒット：0件')
        count_row = Gtk.Box(spacing=16)
        count_row.append(self.search_count)
        self.impact_count = pane.label('除外される直下：確認中…')
        self.impact_count.set_hexpand(True)
        self.impact_count.set_tooltip_text('現在の検索にヒットした全フォルダーの直下件数の合計です。配下全体や除外範囲の確定値ではありません。最大1分前の取得結果を含み、確認できない分や集計上限は区別します。')
        count_row.append(self.impact_count)
        self.left_pane.append(count_row)
        self.message = pane.label('')
        self.right_pane.append(pane.label('現在の除外設定（フォルダー・保存前の変更案）', 'heading'))
        self.excluded_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.excluded_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.excluded_scroll.set_child(self.excluded_list)
        self.right_pane.append(self.excluded_scroll)
        self.files_notice = pane.label('')
        self.files_notice.set_visible(False)
        self.right_pane.append(self.files_notice)
        self.left_pane.append(pane.label('ログに出たフォルダー（除外後に残る候補）', 'heading'))
        self.listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.candidate_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.candidate_scroll.set_child(self.listing)
        self.left_pane.append(self.candidate_scroll)
        navigation = Gtk.Box(spacing=8)
        self.back = Gtk.Button(label='前へ', sensitive=False)
        self.next = Gtk.Button(label='次へ', sensitive=False)
        self.page_label = Gtk.Label(hexpand=True)
        self.back.connect('clicked', lambda _: self.turn(-1))
        self.next.connect('clicked', lambda _: self.turn(1))
        for widget in (self.back, self.page_label, self.next):
            navigation.append(widget)
        self.left_pane.append(navigation)
        box.append(self.message)
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
        selected_index = self.reason_filter.get_selected()
        previous = self.reason_names[selected_index] if selected_index < len(self.reason_names) else 'すべて'
        self.reason_names = ['すべて'] + list(dict.fromkeys(name for item in self.folders for name, _ in item.reasons))
        self.reason_filter.handler_block(self.reason_handler)
        try:
            self.reason_filter.set_model(Gtk.StringList.new(self.reason_names))
            self.reason_filter.set_selected(self.reason_names.index(previous) if previous in self.reason_names else 0)
        finally:
            self.reason_filter.handler_unblock(self.reason_handler)
        self.reason_filter.set_sensitive(len(self.reason_names) > 1)
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
        label.set_markup(highlight_path(location_label(path), self.path_entry.get_text()))
        label.set_tooltip_text(str(path))
        self.path_labels.append((path, label))
        return label

    def search(self, *_args):
        self.page = 0
        self.render(candidates_only=True)
        self.candidate_scroll.get_vadjustment().set_value(0)

    def highlight(self, *_args):
        query = self.path_entry.get_text()
        for path, label in self.path_labels:
            label.set_markup(highlight_path(location_label(path), query))
        self.add_button.set_sensitive(bool(query) and self.plan is not None and self.plan.kind != 'media')
        self.open_button.set_sensitive(os.path.isabs(resolve_location(query)) and not self.opening)
        trash = trash_folder()
        roots = self.plan.targets if self.plan else []
        in_scope = bool(trash is not None and roots and str(trash) not in roots and covered_by(trash, roots))
        editable = self.plan is not None and self.plan.kind != 'media'
        self.trash_button.set_visible(in_scope and editable)
        self.trash_button.set_sensitive(in_scope and editable and not covered_by(trash, self.draft_exclusions))

    @staticmethod
    def clear_box(box):
        while box.get_first_child():
            box.remove(box.get_first_child())

    def render(self, *, candidates_only=False):
        editable = self.plan is not None and self.plan.kind != 'media'
        if not candidates_only:
            self.clear_box(self.excluded_list)
            self.path_labels = []
            self.remove_buttons = {}
            for path in self.draft_exclusions:
                row = Gtk.Box(spacing=8)
                row.append(self.path_label(path))
                remove = Gtk.Button(label='取り消す', sensitive=editable)
                remove.add_css_class('flat')
                remove.update_property([Gtk.AccessibleProperty.LABEL], [location_label(path) + 'の除外を取り消す'])
                remove.connect('clicked', lambda _, value=path: self.remove_path(value))
                row.append(remove)
                self.remove_buttons[path] = remove
                self.excluded_list.append(row)
            if not self.draft_exclusions:
                self.excluded_list.append(self.pane.label('フォルダーの除外はありません。', 'dim-label'))
            self.excluded_path_labels = list(self.path_labels)
        else:
            self.path_labels = list(self.excluded_path_labels)
        self.clear_box(self.listing)
        remaining = [item for item in self.folders if not covered_by(item.path, self.draft_exclusions)]
        self.counter.set_text(f'ログ中のフォルダー：除外後に残る {len(remaining):,} / {len(self.folders):,}件')
        query = self.path_entry.get_text()
        reason_index = self.reason_filter.get_selected()
        reason = self.reason_names[reason_index] if 0 < reason_index < len(self.reason_names) else None
        matches = [item for item in remaining if (query in item.path or query in location_label(item.path)) and
                   (reason is None or any(name == reason for name, _ in item.reasons))]
        self.matched_paths = tuple(item.path for item in matches)
        self.search_count.set_text(f'ヒット：{len(matches):,}件')
        self.page = min(self.page, max(0, (len(matches)-1)//PAGE_SIZE))
        start = self.page * PAGE_SIZE
        self.candidate_buttons = {}
        self.detail_expanders = {}
        self.count_labels = {}
        for folder in matches[start:start+PAGE_SIZE]:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row = Gtk.Box(spacing=6)
            path_button = Gtk.Button(hexpand=True, tooltip_text=folder.path + '\nこのパスを入力欄へ入れる（まだ除外しません）')
            path_button.add_css_class('flat')
            path_button.set_child(self.path_label(folder.path))
            path_button.update_property([Gtk.AccessibleProperty.LABEL], [folder.path + 'を入力欄へ'])
            path_button.connect('clicked', lambda _, path=folder.path: self.path_entry.set_text(path))
            self.candidate_buttons[folder.path] = path_button
            row.append(path_button)
            copy = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text='フォルダーのパスをコピー')
            copy.add_css_class('flat')
            copy.update_property([Gtk.AccessibleProperty.LABEL], [folder.path + 'のパスをコピー'])
            copy.connect('clicked', lambda _, item=folder: self.get_clipboard().set(item.path))
            row.append(copy)
            box.append(row)
            counts = self.pane.label('直下：確認中…', 'dim-label')
            counts.set_tooltip_text('現在の直下の件数です（配下全体・ログ取得時の件数ではありません）。取得結果は最大1分間再利用します。リンクは追跡せず、大きいフォルダーは集計を打ち切ります。')
            self.count_labels[folder.path] = counts
            box.append(counts)
            expander = Gtk.Expander(label=' / '.join(f'{name}：{count:,}件' for name, count in folder.reasons))
            summary_label = expander.get_label_widget()
            summary_label.set_wrap(True)
            summary_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            summary_label.add_css_class('dim-label')
            self.detail_expanders[folder.path] = expander
            expander.connect('notify::expanded', lambda widget, _, item=folder: self.fill_details(widget, item))
            box.append(expander)
            self.listing.append(box)
            self.listing.append(Gtk.Separator())
        if not matches:
            text = '一致する候補はありません。' if query else ('ログに出たフォルダーは、すべて除外範囲に含まれています。' if self.folders else 'ログからフォルダーを抽出できませんでした。')
            self.listing.append(self.pane.label(text))
        total = len(matches)
        self.page_label.set_text(f'{start+1 if total else 0}–{min(start+PAGE_SIZE,total)} / {total:,}フォルダー')
        self.back.set_sensitive(self.page > 0)
        self.next.set_sensitive(start + PAGE_SIZE < total)
        self.review.set_sensitive(editable and set(self.initial_exclusions) != set(self.draft_exclusions))
        self.highlight()
        self.refresh_counts()

    def add_path(self, *_args):
        if self.plan is None or not self.path_entry.get_text():
            return
        # normpath alone would turn a partial/relative input into a misleading candidate.
        raw = resolve_location(self.path_entry.get_text())
        draft = replace(self.plan, excluded_dirs=list(self.draft_exclusions))
        reason = exclusion_state(draft, raw, self.threats)
        if reason:
            self.message.set_text(reason)
            return
        path = os.path.normpath(raw)
        affected = [item for item in self.folders if covered_by(item.path, [path]) and
                    not covered_by(item.path, self.draft_exclusions)]
        self.draft_exclusions.append(path)
        self.message.set_text(f'このフォルダーと配下すべてを除外する変更案を追加しました。ログ中の{len(affected):,}フォルダー・'
                              f'{sum(len(item.findings) for item in affected):,}件の報告が除外範囲に入ります。まだ保存していません。')
        self.path_entry.handler_block(self.path_search_handler)
        try:
            self.path_entry.set_text('')
        finally:
            self.path_entry.handler_unblock(self.path_search_handler)
        self.page = 0
        self.render()
        self.path_entry.grab_focus()

    def remove_path(self, path):
        self.draft_exclusions.remove(path)
        self.message.set_text('変更案から除外を取り消しました。まだ保存していません。')
        self.render()

    def turn(self, step):
        self.page += step
        self.render()
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
        box.append(self.pane.label('現在の対象：\n' + '\n'.join(str(target) for target in current.targets)))
        text = '追加するフォルダーと配下すべては、次回から検査対象外になります。取り消した除外範囲は、他の除外に含まれなければ再び検査対象になります。実際のファイルは変更しません。'
        box.append(self.pane.label(text))
        listing = Gtk.ScrolledWindow(vexpand=True)
        changes = ('追加（配下すべてを除外）\n' + '\n\n'.join(location_confirmation(path) for path in additions) if additions else '')
        changes += ('\n\n取り消す除外\n' + '\n\n'.join(location_confirmation(path) for path in removals) if removals else '')
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

    def closed(self, *_args):
        self.reader.close()
        self.count_cancel.set()
        self.count_reader.close()
        self.open_reader.close()
        self.open_cancel.cancel()

    def refresh_counts(self):
        self.count_cancel.set()
        cancel = self.count_cancel = threading.Event()
        self.count_generation += 1
        generation = self.count_generation
        labels = dict(self.count_labels)
        paths = self.matched_paths
        now = time.monotonic()
        values = {}
        for path in paths:
            cached = self.count_cache.get(path)
            if cached and now-cached[0] < 60:
                values[path] = cached
                if path in labels:
                    labels[path].set_text(cached[1].label())
        # Inspect the visible page first, then other matching folders.
        missing = list(dict.fromkeys([path for path in labels if path not in values] +
                                    [path for path in paths if path not in values]))
        self.impact_count.set_text(total_children_label((values[path][1] if path in values else None for path in paths),
                                                       checking=bool(missing)))
        if not missing:
            return
        def load():
            collected = {}
            started = time.monotonic()
            for path in missing:
                if cancel.is_set() or time.monotonic()-started >= 2:
                    break
                value = count_children(path, cancel=cancel)
                if value is not None:
                    collected[path] = (time.monotonic(), value)
            return collected
        def done(collected, error):
            if generation != self.count_generation:
                return
            if collected:
                self.count_cache.update(collected)
                values.update(collected)
            for path in labels:
                if path in values:
                    labels[path].set_text(values[path][1].label())
                else:
                    labels[path].set_text('直下：未確認（集計上限または取得失敗）')
            self.impact_count.set_text(total_children_label(values[path][1] if path in values else None for path in paths))
        try:
            self.count_reader.submit(load, done)
        except Exception as error:
            done(None, error)

    def open_folder(self, _button):
        if self.opening:
            return
        text = resolve_location(self.path_entry.get_text())
        self.opening = True
        self.highlight()
        def done(path, error):
            if not self.get_visible():
                return
            if error:
                self.opening = False
                self.highlight()
                self.message.set_text('開けませんでした：' + str(error))
                return
            try:
                self.folder_launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))
                self.folder_launcher.launch(self, self.open_cancel, self.open_finished)
            except Exception as error:
                self.opening = False
                self.highlight()
                self.message.set_text('開けませんでした：' + str(error))
        try:
            self.open_reader.submit(lambda: local_folder_path(text), done)
        except Exception as error:
            done(None, error)

    def open_finished(self, launcher, result):
        self.opening = False
        if not self.get_visible():
            return
        self.highlight()
        try:
            launcher.launch_finish(result)
            self.message.set_text('フォルダーを開きました。確認後、この画面で除外を追加できます。')
        except Exception as error:
            self.message.set_text('開けませんでした：' + str(error))

    def add_trash(self, _button):
        self.path_entry.set_text('ごみ箱')
        self.add_path()
