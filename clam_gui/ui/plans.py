"""PlanEditor view and its callbacks; OS services are injected through the application backend."""
import gi
gi.require_version('Gtk', '4.0')
from dataclasses import replace
from pathlib import Path
import threading
from gi.repository import GLib, Gtk
from ..core import Plan
from ..options import LIMITS, APP_LIMITS
from ..known_locations import trash_folder, location_label
from ..plan_exclusions import exclusion_state
from ..jobs import start_worker
from ..schedule import Schedule, read_schedule


class PlanEditor:
    def edit_selected(self):
        selected = self.operation_profile()
        if selected:
            self.edit_profile(*selected)

    def duplicate_selected(self):
        selected = self.operation_profile()
        if not selected or self.busy:
            return
        try:
            key = self.store.duplicate_profile(selected[0])
            self.action_key = key
            self.refresh_profiles()
            self.show_notice('検査プランを複製しました。定期検査は無効です。名前や条件を編集できます。')
        except Exception as error:
            self.show_notice('複製できませんでした：' + str(error))

    def delete_selected(self):
        selected = self.operation_profile()
        if not selected or self.busy:
            return
        key, plan = selected
        schedule = read_schedule(self.store, key)
        detail = plan.describe() + '\n\n' + schedule.describe()
        detail += ('\n\nこの検査プランと予定を削除します。実際のファイルと検査履歴は残します。'
                   '\n実行中の定期検査は完了まで継続します。既存cronは変更しません。')
        dialog = Gtk.MessageDialog(transient_for=self.operation_parent(), modal=True,
            message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL,
            text='「' + plan.label + '」を削除しますか？', secondary_text=detail)
        def response(window, answer):
            window.destroy()
            if answer != Gtk.ResponseType.OK or self.busy:
                return
            self.busy = True
            self.profile_changed()
            self.new_action.set_enabled(False)
            self.set_controls(False)
            self.show_notice('検査プランと予定を削除しています…')
            def worker():
                try:
                    self.backend.scheduler(self.store).remove_profile(key)
                    message = '検査プランと予定を削除しました。検査履歴と実際のファイルは残しています。'
                except Exception as error:
                    message = '削除できませんでした：' + str(error)
                def finished():
                    self.busy = False
                    self.new_action.set_enabled(True)
                    self.set_controls(True)
                    self.refresh_profiles()
                    self.show_notice(message)
                    return False
                GLib.idle_add(finished)
            def failed(error):
                self.busy = False
                self.new_action.set_enabled(True)
                self.set_controls(True)
                self.show_notice('削除処理を開始できませんでした：' + str(error))
            start_worker(worker, failed)
        dialog.connect('response', response)
        dialog.present()

    def edit_profile(self, key=None, plan=None):
        if self.busy or self.schedule_busy:
            return
        is_new = key is None
        plan = plan or Plan('', [str(Path.home())], [])
        editor = Gtk.Window(application=self, title='検査プランの設定', transient_for=self.operation_parent(), modal=True)
        editor.set_default_size(650, 550)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(box)
        editor.set_child(scroll)
        def entry(label, value, container=None):
            widget = Gtk.Entry(text=value, hexpand=True)
            caption = Gtk.Label(label=label, xalign=0)
            caption.set_mnemonic_widget(widget)
            box.append(caption)
            (container or box).append(widget)
            widget.update_property([Gtk.AccessibleProperty.LABEL], [label])
            return widget
        name = entry('プラン名（例：仕事の資料）', plan.label)
        box.append(Gtk.Label(label='検査プランの種類', xalign=0))
        kind = Gtk.DropDown.new_from_strings(['ディレクトリの定期検査', 'フォルダー監視'])
        kind.update_property([Gtk.AccessibleProperty.LABEL], ['検査プランの種類'])
        kind.set_selected(1 if plan.kind == 'watch' else 0)
        box.append(kind)
        root_row = Gtk.Box(spacing=8)
        root = entry('基本の対象フォルダー', str(plan.targets[0]), root_row)
        box.append(root_row)
        def pick(action, callback):
            chooser = Gtk.FileChooserNative(title='対象を選択', transient_for=editor,
                action=action, accept_label='選択', cancel_label='キャンセル')
            def response(dialog, answer):
                file = dialog.get_file()
                dialog.destroy()
                if answer == Gtk.ResponseType.ACCEPT and file and file.get_path():
                    callback(file.get_path())
            chooser.connect('response', response)
            chooser.show()
        browse = Gtk.Button(icon_name='folder-open-symbolic', tooltip_text='基本のフォルダーを選択')
        browse.update_property([Gtk.AccessibleProperty.LABEL], ['基本のフォルダーを選択'])
        browse.connect('clicked', lambda _: pick(Gtk.FileChooserAction.SELECT_FOLDER, root.set_text))
        root_row.append(browse)
        box.append(Gtk.Label(label='除外する対象（フォルダーは配下すべて、ファイルはその1件）',
                             xalign=0, wrap=True))
        exclusions = [(str(p), False) for p in plan.excludes] + [(str(p), True) for p in plan.excluded_dirs]
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.append(listing)
        def refresh():
            while listing.get_first_child():
                listing.remove(listing.get_first_child())
            for item in exclusions:
                row = Gtk.Box(spacing=8)
                text = Gtk.Label(label=('フォルダー: ' if item[1] else 'ファイル: ') + (location_label(item[0]) if item[1] else item[0]),
                                 xalign=0, wrap=True, hexpand=True, selectable=True)
                text.set_tooltip_text(item[0])
                row.append(text)
                remove = Gtk.Button(label='除外を取り消す')
                def remove_item(_button, value=item):
                    exclusions.remove(value)
                    refresh()
                remove.connect('clicked', remove_item)
                row.append(remove)
                listing.append(row)
        def add(path, directory):
            item = (path, directory)
            if item not in exclusions:
                exclusions.append(item)
                refresh()
        if trash_folder() is not None:
            trash = Gtk.Button(label='ごみ箱を除外に追加', halign=Gtk.Align.START)
            trash.set_tooltip_text(str(trash_folder()) + '\nホーム側のごみ箱の内容のみが対象です。保存するまで反映されません。')
            trash_notice = Gtk.Label(xalign=0, wrap=True)
            def add_trash(_button):
                path = str(trash_folder())
                draft = replace(plan, targets=[root.get_text()], excluded_dirs=[p for p, directory in exclusions if directory])
                reason = exclusion_state(draft, path)
                if reason:
                    trash_notice.set_text(reason)
                else:
                    add(path, True)
                    trash_notice.set_text('ごみ箱を除外候補に追加しました。保存するまで反映されません。')
            trash.connect('clicked', add_trash)
            box.append(trash)
            box.append(trash_notice)
        refresh()
        exclusion_actions = Gtk.Box(spacing=8, homogeneous=True)
        box.append(exclusion_actions)
        for label, directory, action in [('除外フォルダーを追加', True, Gtk.FileChooserAction.SELECT_FOLDER),
                                         ('除外ファイルを追加', False, Gtk.FileChooserAction.OPEN)]:
            button = Gtk.Button(label=label)
            button.connect('clicked', lambda _, d=directory, a=action: pick(a, lambda p: add(p, d)))
            exclusion_actions.append(button)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, margin_top=8, margin_bottom=8))
        heading = Gtk.Label(label='スケジュール', xalign=0)
        heading.add_css_class('heading')
        box.append(heading)
        saved_schedule = read_schedule(self.store, key) if key else Schedule()
        scheduled_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(scheduled_box)
        read_fields = self.schedule_fields(scheduled_box, saved_schedule, plan)
        watch_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        watch_enabled = Gtk.CheckButton(label='フォルダー監視を有効にする', active=saved_schedule.enabled)
        recursive = Gtk.CheckButton(label='サブフォルダーも監視する', active=plan.watch_recursive)
        watch_box.append(watch_enabled)
        watch_box.append(recursive)
        watch_box.append(Gtk.Label(label='追加・変更されたファイルを、書き込みが落ち着いてから検査します。\n'
            '一時ファイルは待機し、検査待ちをまとめます。アプリ自身のデータ・ロック用フォルダーは監視しません。\n'
            '常駐監視を有効にすると画面を閉じても動作します。初回は別途フォルダー全体を検査してください。', xalign=0, wrap=True))
        box.append(watch_box)
        def kind_changed(*_):
            scheduled_box.set_visible(kind.get_selected() == 0)
            watch_box.set_visible(kind.get_selected() == 1)
            heading.set_text('フォルダー監視' if kind.get_selected() == 1 else 'スケジュール')
        kind.connect('notify::selected', kind_changed)
        kind_changed()
        state = {'running': False}
        editor.connect('close-request', lambda _: state['running'])
        error_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(error_label)
        inherited = Gtk.CheckButton(label='環境設定のサイズ上限を使う', active=plan.limits is None)
        box.append(inherited)
        limit_fields = {}
        selected_limits = self.store.effective_limits(plan)
        for limit_key, (title, _) in LIMITS.items():
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            spin = Gtk.SpinButton.new_with_range(1, 2047, 1)
            spin.set_value(selected_limits[limit_key] or APP_LIMITS[limit_key])
            spin.update_property([Gtk.AccessibleProperty.LABEL], [title])
            row.append(spin)
            row.append(Gtk.Label(label='MiB'))
            limit_fields[limit_key] = spin
            box.append(row)
        def update_limits(*_):
            for spin in limit_fields.values():
                spin.set_sensitive(not inherited.get_active())
        inherited.connect('toggled', update_limits)
        update_limits()
        save = Gtk.Button(label='検査プランを保存')
        save.add_css_class('suggested-action')
        def save_profile(_button):
            nonlocal key
            if state['running'] or self.busy:
                return
            try:
                from ..core import validate_profile
                new = Plan(name.get_text().strip(), [root.get_text()],
                           [p for p, directory in exclusions if not directory], plan.phishing,
                           [p for p, directory in exclusions if directory], plan.requires_admin,
                           'watch' if kind.get_selected() == 1 else 'scheduled', recursive.get_active(),
                           None if inherited.get_active() else {k: v.get_value_as_int() for k, v in limit_fields.items()})
                validate_profile(new)
                schedule = (replace(saved_schedule, enabled=watch_enabled.get_active())
                            if new.kind == 'watch' else read_fields())
                schedule.calendar()
                if schedule.enabled and new.requires_admin:
                    raise ValueError('管理者権限が必要な対象は定期実行できません。')
            except (ValueError, OSError) as error:
                error_label.set_text(str(error))
                return
            def refresh_saved():
                self.action_key = key
                self.refresh_profiles(key if is_new else None)
            changed = schedule != saved_schedule or new.kind != plan.kind
            if not changed:
                try:
                    key = self.store.save_profile(new, key)
                    refresh_saved()
                    editor.destroy()
                except (ValueError, OSError) as error:
                    error_label.set_text(str(error))
                return
            detail = new.describe() + '\n\n' + (('フォルダー監視：有効（画面を閉じて使うには常駐監視を有効化）' if schedule.enabled else 'フォルダー監視：無効')
                                                     if new.kind == 'watch' else schedule.describe())
            detail += ('\n\n検査プランと予定を保存します。有効化時は検査がすぐ始まる場合があります。'
                       '\n無効化しても実行中の検査は完了まで続けます。')
            dialog = Gtk.MessageDialog(transient_for=editor, modal=True,
                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                text='検査プランとこの予定を保存しますか？', secondary_text=detail)
            def response(d, answer):
                d.destroy()
                if answer != Gtk.ResponseType.OK or self.busy:
                    return
                state['running'] = True
                self.busy = True
                box.set_sensitive(False)
                self.set_controls(False)
                error_label.set_text('検査プランと予定を保存しています…')
                def worker():
                    nonlocal key
                    profile_saved = False
                    error = None
                    try:
                        key = self.store.save_profile(new, key)
                        profile_saved = True
                        self.backend.scheduler(self.store).apply(key, schedule)
                    except Exception as failure:
                        prefix = ('検査プランは保存しましたが、予定を登録できませんでした：'
                                  if profile_saved else '検査プランを保存できませんでした：')
                        error = prefix + str(failure)
                    def finished():
                        state['running'] = False
                        self.busy = False
                        self.set_controls(True)
                        box.set_sensitive(True)
                        if profile_saved:
                            refresh_saved()
                        if error:
                            error_label.set_text(error + '\n内容を確認して再度保存してください。')
                        else:
                            editor.destroy()
                        return False
                    GLib.idle_add(finished)
                def failed(error):
                    state['running'] = False
                    self.busy = False
                    self.set_controls(True)
                    box.set_sensitive(True)
                    error_label.set_text('保存処理を開始できませんでした：' + str(error))
                start_worker(worker, failed)
            dialog.connect('response', response)
            dialog.present()
        save.connect('clicked', save_profile)
        save_actions = Gtk.Box(spacing=8, homogeneous=True)
        cancel = Gtk.Button(label='キャンセル')
        cancel.connect('clicked', lambda _: editor.destroy())
        save_actions.append(cancel)
        save_actions.append(save)
        box.append(save_actions)
        editor.present()

    def schedule_fields(self, box, saved, plan):
        enabled = Gtk.CheckButton(label='定期検査を有効にする', active=saved.enabled)
        enabled.set_sensitive(not plan.requires_admin)
        box.append(enabled)
        if plan.requires_admin:
            box.append(Gtk.Label(label='管理者権限が必要な対象は、現在は定期実行できません。', wrap=True))
        def field(label, widget):
            widget.update_property([Gtk.AccessibleProperty.LABEL], [label])
            row = Gtk.Box(spacing=12)
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            row.append(widget)
            box.append(row)
            return widget
        frequency = field('頻度', Gtk.DropDown.new_from_strings(['毎日', '毎週', '毎月', '毎時', '一定間隔（n分）']))
        frequency.set_selected(['daily', 'weekly', 'monthly', 'hourly', 'interval'].index(saved.frequency))
        hour = field('時（0〜23）', Gtk.SpinButton.new_with_range(0, 23, 1))
        hour.set_value(saved.hour)
        minute = field('分（0〜59）', Gtk.SpinButton.new_with_range(0, 59, 1))
        minute.set_value(saved.minute)
        weekday = field('曜日', Gtk.Box(spacing=8))
        selected_days = saved.selected_weekdays()
        day_checks = []
        for day, name in enumerate('月火水木金土日'):
            check = Gtk.CheckButton(label=name, active=day in selected_days)
            check.update_property([Gtk.AccessibleProperty.LABEL], [name + '曜日'])
            weekday.append(check)
            day_checks.append(check)
        monthday = field('日付（毎月1〜28日）', Gtk.SpinButton.new_with_range(1, 28, 1))
        monthday.set_value(saved.monthday)
        interval = field('間隔（1〜1440分）', Gtk.SpinButton.new_with_range(1, 1440, 1))
        interval.set_value(saved.interval_minutes)
        hint = Gtk.Label(xalign=0, wrap=True)
        box.append(hint)
        def frequency_changed(*_):
            index = frequency.get_selected()
            hour.get_parent().set_visible(index in (0, 1, 2))
            minute.get_parent().set_visible(index != 4)
            weekday.get_parent().set_visible(index == 1)
            monthday.get_parent().set_visible(index == 2)
            interval.get_parent().set_visible(index == 4)
            if index == 4:
                text = ('初回は登録から指定分後、その後は検査の完了から指定分後に繰り返します。\n'
                        '画面を閉じても動きます。ログアウト中の分をまとめて実行しません。\n'
                        '短い間隔の試験には、小さなテスト用フォルダーを使ってください。')
            else:
                text = ('端末の現地時刻で実行します。画面を閉じても動きます。\n'
                        'ログイン後に逃した予定を1回実行します。電源オフ中は動きません。')
            hint.set_text(text + '\n既存cronと同じ対象の予定を重複させないでください。')
        frequency.connect('notify::selected', frequency_changed)
        frequency_changed()
        def read():
            days = [day for day, check in enumerate(day_checks) if check.get_active()]
            stored_days = None if saved.weekdays is None and days == [saved.weekday] else days
            return Schedule(enabled.get_active(), ['daily', 'weekly', 'monthly', 'hourly', 'interval'][frequency.get_selected()],
                            hour.get_value_as_int(), minute.get_value_as_int(),
                            next((day for day, check in enumerate(day_checks) if check.get_active()), saved.weekday),
                            monthday.get_value_as_int(), interval.get_value_as_int(),
                            stored_days)
        return read
