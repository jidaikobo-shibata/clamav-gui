"""Run with /usr/bin/python3 -m clam_gui."""
from pathlib import Path
import threading
import shutil
import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk, Pango
from .core import Store, Plan, engine_status, scan
from .options import OPTIONS, supported_options, standard_defaults, option_arguments, LIMITS, limit_arguments
from .schedule import Schedule, Scheduler, read_schedule


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='com.jidaikobo.ClamAVGui')
        self.store = Store()
        self.busy = False
        self.cancel = threading.Event()

    def do_activate(self):
        if self.get_active_window():
            self.get_active_window().present()
            return
        window = Gtk.ApplicationWindow(application=self, title='ClamAV GUI — 開発版')
        window.set_default_size(800, 600)
        window.connect('close-request', self.on_close)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_bottom=12, margin_start=12, margin_end=12)
        window.set_child(box)
        def action(name, callback):
            item = Gio.SimpleAction.new(name, None)
            item.connect('activate', lambda *_: callback())
            self.add_action(item)
            return item
        self.new_action = action('new-target', self.edit_profile)
        self.manage_action = action('manage-targets', self.manage_targets)
        self.edit_action = action('edit-target', self.edit_selected)
        self.duplicate_action = action('duplicate-target', self.duplicate_selected)
        self.delete_action = action('delete-target', self.delete_selected)
        action('preferences', self.preferences)
        action('package-setup', self.package_setup)
        self.menu_model = Gio.Menu()
        targets = Gio.Menu()
        targets.append('対象を追加...', 'app.new-target')
        targets.append('対象を管理...', 'app.manage-targets')
        self.menu_model.append_submenu('対象管理', targets)
        environment = Gio.Menu()
        environment.append('検査設定...', 'app.preferences')
        environment.append('ClamAVの管理...', 'app.package-setup')
        self.menu_model.append_submenu('環境設定', environment)
        box.append(Gtk.PopoverMenuBar.new_from_model(self.menu_model))
        selector = Gtk.Box(spacing=12)
        selector.append(Gtk.Label(label='対象', xalign=0))
        self.profile_names = Gtk.StringList.new([])
        self.profiles = Gtk.DropDown(model=self.profile_names, hexpand=True)
        self.profiles.update_property([Gtk.AccessibleProperty.LABEL], ['ログを見る対象'])
        self.profiles.connect('notify::selected', self.profile_changed)
        selector.append(self.profiles)
        refresh = Gtk.Button(label='ログを更新')
        refresh.connect('clicked', self.history)
        selector.append(refresh)
        box.append(selector)
        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        box.append(body)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=240, margin_end=12)
        left.append(Gtk.Label(label='検査完了日時（検出ありは太字）', xalign=0))
        self.history_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.history_list.update_property([Gtk.AccessibleProperty.LABEL], ['検査完了日時一覧'])
        self.history_list.connect('row-selected', self.log_selected)
        self.history_list.set_placeholder(Gtk.Label(label='検査記録はありません。', wrap=True))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.history_list)
        left.append(scroll)
        body.set_start_child(left)
        body.set_position(270)
        body.set_shrink_start_child(False)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12)
        right.append(Gtk.Label(label='選択した検査のログ', xalign=0))
        self.result = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.result.update_property([Gtk.AccessibleProperty.LABEL], ['選択した日時の検査ログ'])
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroll.set_child(self.result)
        right.append(scroll)
        body.set_end_child(right)
        self.notice = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.notice)
        self.refreshing_profiles = False
        self.manager = None
        self.manager_key = None
        self.log_entries = []
        self.refresh_profiles()
        window.present()

    def manage_targets(self):
        if self.manager:
            self.manager.present()
            return
        self.manager = Gtk.Window(application=self, title='対象を管理', transient_for=self.get_active_window(), modal=True)
        self.manager.insert_action_group('app', self)
        self.manager.set_default_size(780, 550)
        area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.manager.set_child(area)
        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        area.append(body)
        self.manager_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.manager_list.update_property([Gtk.AccessibleProperty.LABEL], ['管理する対象一覧'])
        self.manager_list.connect('row-selected', lambda *_: self.manager_selected())
        scroll = Gtk.ScrolledWindow(width_request=230, vexpand=True)
        scroll.set_child(self.manager_list)
        body.set_start_child(scroll)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12)
        body.set_end_child(details)
        actions = Gtk.Box(spacing=8)
        for label, name in [('フォルダー編集', 'edit-target'), ('複製', 'duplicate-target'), ('削除', 'delete-target')]:
            actions.append(Gtk.Button(label=label, action_name='app.' + name))
        details.append(actions)
        self.schedule_button = Gtk.Button(label='スケジュール')
        self.schedule_button.connect('clicked', lambda _: self.edit_schedule())
        details.append(self.schedule_button)
        manual = Gtk.Box(spacing=8)
        self.button = Gtk.Button(label='今すぐ検査')
        self.button.connect('clicked', self.scan_selected)
        self.stop = Gtk.Button(label='中止', sensitive=self.busy)
        self.stop.connect('clicked', self.stop_scan)
        manual.append(self.button)
        manual.append(self.stop)
        details.append(manual)
        self.description = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.description)
        details.append(scroll)
        self.manager_notice = Gtk.Label(xalign=0, wrap=True, selectable=True)
        area.append(self.manager_notice)
        close = Gtk.Button(label='閉じる', halign=Gtk.Align.END)
        close.connect('clicked', lambda _: self.close_manager())
        area.append(close)
        self.manager.connect('close-request', lambda _: self.close_manager())
        self.refresh_manager()
        self.manager.present()

    def close_manager(self):
        if self.busy:
            self.manager_notice.set_text('処理中です。検査を中止するか、完了をお待ちください。')
            return True
        window = self.manager
        self.manager = None
        self.manager_key = None
        window.destroy()
        return True

    def refresh_manager(self):
        if not self.manager:
            return
        selected_key = self.manager_key
        self.manager_refreshing = True
        self.manager_profiles = self.store.profiles()
        while self.manager_list.get_first_child():
            self.manager_list.remove(self.manager_list.get_first_child())
        for key, plan in self.manager_profiles:
            self.manager_list.append(Gtk.Label(label=plan.label, xalign=0, wrap=True,
                max_width_chars=24, margin_start=10, margin_end=10, margin_top=10, margin_bottom=10))
        index = next((i for i, (key, _) in enumerate(self.manager_profiles) if key == selected_key), 0)
        if self.manager_profiles:
            self.manager_list.select_row(self.manager_list.get_row_at_index(index))
        self.manager_refreshing = False
        self.manager_selected()

    def manager_selected(self):
        if getattr(self, 'manager_refreshing', False):
            return
        row = self.manager_list.get_selected_row()
        selected = self.manager_profiles[row.get_index()] if row else None
        self.manager_key = selected[0] if selected else None
        self.description.set_text(selected[1].describe(include_policy=False) + '\n' + read_schedule(self.store, selected[0]).describe()
                                  if selected else '対象はありません。「対象管理」から追加できます。')
        for item in (self.edit_action, self.duplicate_action, self.delete_action):
            item.set_enabled(bool(selected) and not self.busy)
        self.schedule_button.set_sensitive(bool(selected) and not self.busy)
        compatible = bool(selected) and len(selected[1].targets) == 1
        self.button.set_sensitive(compatible and not selected[1].requires_admin and not self.busy)
        self.manager_list.set_sensitive(not self.busy)

    def operation_profile(self):
        if self.manager and self.manager_key:
            return next((item for item in self.store.profiles() if item[0] == self.manager_key), None)
        return None

    def operation_parent(self):
        return self.manager or self.get_active_window()

    def show_notice(self, text):
        self.notice.set_text(text)
        if self.manager:
            self.manager_notice.set_text(text)

    def package_setup(self, manager=None, updater=None):
        from .packages import AptManager, PACKAGES
        from .notifications import definition_status
        manager = manager or AptManager()
        from .definitions import DefinitionUpdater
        updater = updater or DefinitionUpdater()
        window = Gtk.Window(application=self, title='ClamAVの管理', transient_for=self.operation_parent(), modal=True)
        window.set_default_size(720, 580)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        window.set_child(box)
        status = Gtk.Label(label='導入状態を確認しています…', xalign=0, wrap=True, selectable=True)
        box.append(status)
        box.append(Gtk.Label(label='ClamAVは、このGUI以外のアプリも利用する共通のソフトウェアです。\n'
            '削除するとThunderbird連携などの検査も使えなくなります。', xalign=0, wrap=True))
        controls = Gtk.Box(spacing=8, homogeneous=True)
        install = Gtk.Button(label='インストール')
        remove = Gtk.Button(label='アンインストール')
        remove.add_css_class('destructive-action')
        controls.append(install)
        controls.append(remove)
        box.append(controls)
        update_controls = Gtk.Box(spacing=8)
        update = Gtk.Button(label='定義ファイルを取得・更新', hexpand=True)
        stop_monitor = Gtk.Button(label='監視を終了', sensitive=False)
        update_controls.append(update)
        update_controls.append(stop_monitor)
        box.append(update_controls)
        progress = Gtk.Spinner(halign=Gtk.Align.START)
        box.append(progress)
        message = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(message)
        output = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        output.update_property([Gtk.AccessibleProperty.LABEL], ['ClamAV管理の処理結果'])
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(output)
        box.append(scroll)
        refresh = Gtk.Button(label='状態を再確認')
        box.append(refresh)
        close = Gtk.Button(label='閉じる')
        close.connect('clicked', lambda _: window.close())
        box.append(close)
        state = {'running': False, 'closed': False, 'installed': {}, 'updating': False, 'cancel': threading.Event()}
        def close_request(_window):
            if state['updating']:
                state['cancel'].set()
                message.set_text('監視を終了しています。更新サービスは停止しません。')
                return True
            if state['running']:
                message.set_text('処理中です。完了までお待ちください。OSのパッケージ処理は途中で終了しません。')
                return True
            state['closed'] = True
            return False
        window.connect('close-request', close_request)
        def buttons():
            ready = not state['running'] and not self.busy
            install.set_sensitive(ready and len(state['installed']) < len(PACKAGES))
            remove.set_sensitive(ready and bool(state['installed']))
            update.set_sensitive(ready and 'clamav-freshclam' in state['installed'])
            refresh.set_sensitive(ready)
        def check_state():
            install.set_sensitive(False)
            remove.set_sensitive(False)
            update.set_sensitive(False)
            refresh.set_sensitive(False)
            def worker():
                try:
                    installed = manager.status()
                    lines = [package + ': ' + installed.get(package, '未導入') for package in PACKAGES]
                    text = '\n'.join(lines) + '\n\n' + definition_status()
                    if len(installed) == len(PACKAGES):
                        text += '\n初回導入後は、定義ファイルの更新が終わってから検査してください。'
                    error = None
                except Exception as exception:
                    installed, text, error = {}, str(exception), exception
                def finished():
                    if state['closed']:
                        return False
                    state['installed'] = installed
                    status.set_text(text)
                    if error is None:
                        buttons()
                    else:
                        message.set_text('この環境では導入・削除を実行できません。')
                        refresh.set_sensitive(True)
                    return False
                GLib.idle_add(finished)
            threading.Thread(target=worker, daemon=True).start()
        def release():
            state['running'] = False
            state['updating'] = False
            stop_monitor.set_sensitive(False)
            self.busy = False
            self.set_controls(True)
            progress.stop()
            close.set_sensitive(True)
            buttons()
        def execute(plan, confirmation):
            confirmation.destroy()
            message.set_text('管理者認証後に処理します。ダウンロードやパッケージ処理の完了までお待ちください。')
            def worker():
                install_succeeded = False
                try:
                    text, code = manager.execute(plan)
                    after = manager.status()
                    expected = len(after) == len(PACKAGES) if plan.action == 'install' else not after
                    if code == 0 and expected:
                        install_succeeded = plan.action == 'install'
                        result = 'インストールが完了しました。「定義ファイルを取得・更新」で更新状態を確認してください。' if plan.action == 'install' else 'アンインストールが完了しました。'
                    else:
                        result = '処理が完了していません。管理者認証と下の詳細・現在の状態を確認してください。'
                except Exception as error:
                    text, result = str(error), '処理を完了できませんでした。下の詳細を確認してください。'
                def finished():
                    output.get_buffer().set_text(text)
                    message.set_text(result)
                    release()
                    check_state()
                    if install_succeeded:
                        begin_update()
                    return False
                GLib.idle_add(finished)
            threading.Thread(target=worker, daemon=True).start()
        def confirm(plan):
            confirmation = Gtk.Window(application=self, title='ClamAVの変更内容を確認', transient_for=window, modal=True)
            confirmation.set_default_size(700, 550)
            area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
            confirmation.set_child(area)
            detail = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            detail.update_property([Gtk.AccessibleProperty.LABEL], ['変更対象と影響'])
            detail.get_buffer().set_text(plan.describe())
            scroll = Gtk.ScrolledWindow(vexpand=True)
            scroll.set_child(detail)
            area.append(scroll)
            agreed = Gtk.CheckButton(label='変更対象と、他のアプリへの影響を確認しました')
            area.append(agreed)
            actions = Gtk.Box(spacing=8, homogeneous=True)
            cancel = Gtk.Button(label='キャンセル')
            run = Gtk.Button(label='管理者認証して実行', sensitive=False)
            agreed.connect('toggled', lambda check: run.set_sensitive(check.get_active()))
            actions.append(cancel)
            actions.append(run)
            area.append(actions)
            def cancelled(*_):
                release()
                message.set_text('変更をキャンセルしました。')
                confirmation.destroy()
                return True
            cancel.connect('clicked', cancelled)
            confirmation.connect('close-request', cancelled)
            run.connect('clicked', lambda _: execute(plan, confirmation))
            confirmation.present()
        def prepare(action):
            if self.busy:
                message.set_text('別の処理が実行中です。完了後に再実行してください。')
                return
            self.busy = True
            state['running'] = True
            self.set_controls(False)
            buttons()
            close.set_sensitive(False)
            progress.start()
            message.set_text('変更予定を確認しています。まだインストール・削除は実行していません。')
            def worker():
                try:
                    plan, error = manager.preview(action), None
                except Exception as exception:
                    plan, error = None, str(exception)
                def finished():
                    if error is not None:
                        output.get_buffer().set_text(error)
                        message.set_text('変更予定を確認できませんでした。')
                        release()
                    elif not plan.changes:
                        message.set_text('変更は不要です。')
                        release()
                        check_state()
                    else:
                        progress.stop()
                        confirm(plan)
                    return False
                GLib.idle_add(finished)
            threading.Thread(target=worker, daemon=True).start()
        def begin_update():
            if self.busy:
                message.set_text('別の処理が実行中です。完了後に再実行してください。')
                return
            dialog = Gtk.MessageDialog(application=self, transient_for=window, modal=True,
                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                text='定義ファイルを取得・更新しますか？',
                secondary_text='自動更新サービスを利用して、公式の更新先へ接続します。\n'
                    '停止中の場合は開始します。稼働中のサービスを止めたり、別のFreshClamを起動したりしません。\n'
                    '管理者認証が必要な場合があります。取得中の場合はその処理を監視します。')
            def response(d, answer):
                d.destroy()
                if answer != Gtk.ResponseType.OK or self.busy:
                    return
                self.busy = True
                state['running'] = state['updating'] = True
                state['cancel'].clear()
                self.set_controls(False)
                buttons()
                stop_monitor.set_sensitive(True)
                progress.start()
                message.set_text('定義ファイルの取得・更新を確認しています…')
                def report(text):
                    GLib.idle_add(output.get_buffer().set_text, text)
                def worker():
                    try:
                        result, text = updater.update(cancel=state['cancel'], progress=report)
                    except Exception as error:
                        result, text = '定義の取得・更新を確認できませんでした。', str(error)
                    def finished():
                        output.get_buffer().set_text(text)
                        message.set_text(result)
                        release()
                        check_state()
                        return False
                    GLib.idle_add(finished)
                threading.Thread(target=worker, daemon=True).start()
            dialog.connect('response', response)
            dialog.present()
        update.connect('clicked', lambda _: begin_update())
        stop_monitor.connect('clicked', lambda _: state['cancel'].set())
        install.connect('clicked', lambda _: prepare('install'))
        remove.connect('clicked', lambda _: prepare('remove'))
        refresh.connect('clicked', lambda _: check_state())
        window.present()
        check_state()

    def preferences(self):
        window = Gtk.Window(application=self, title='環境設定', transient_for=self.operation_parent(), modal=True)
        window.set_default_size(700, 650)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        window.set_child(box)
        state = Gtk.Label(label='ClamAVの対応項目を確認しています…', xalign=0, wrap=True, selectable=True)
        box.append(state)
        box.append(Gtk.Label(label='全対象の次回以降の手動・定期検査に共通適用します。\n'
            '形式の解析を無効にしても、その拡張子のファイル全体を除外する意味ではありません。',
            xalign=0, wrap=True))
        pages = Gtk.Notebook(vexpand=True)
        box.append(pages)
        settings = self.store.scan_options()
        controls = {}
        for group in ('ファイル形式', '検出方法', '警告'):
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
            scroll = Gtk.ScrolledWindow(vexpand=True)
            scroll.set_child(content)
            pages.append_page(scroll, Gtk.Label(label=group))
            for option in OPTIONS:
                if option.group != group:
                    continue
                check = Gtk.CheckButton(label=option.label, active=settings[option.key], sensitive=False)
                check.set_tooltip_text(option.description + '\n--' + option.key)
                controls[option.key] = check
                content.append(check)
                content.append(Gtk.Label(label=option.description, xalign=0, wrap=True, margin_start=24))
            if group == '警告':
                content.append(Gtk.Label(label='検査上限への到達とヒューリスティック警告は有効に固定します。\n'
                    '暗号化の報告を無効にしても、その内容を検査できるようにはなりません。', xalign=0, wrap=True))
        limit_values = self.store.scan_limits()
        limit_controls = {}
        limit_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        pages.append_page(limit_page, Gtk.Label(label='サイズ上限'))
        for key, (label, explanation) in LIMITS.items():
            limit_page.append(Gtk.Label(label=label, xalign=0))
            limit_page.append(Gtk.Label(label=explanation, xalign=0, wrap=True))
            row_limits = Gtk.Box(spacing=12)
            standard = Gtk.CheckButton(label='ClamAV標準値を使う', active=limit_values[key] is None)
            standard.update_property([Gtk.AccessibleProperty.LABEL], [label + 'にClamAV標準値を使う'])
            spin = Gtk.SpinButton.new_with_range(1, 2047, 1)
            spin.set_value(limit_values[key] or 100)
            spin.set_numeric(True)
            spin.update_property([Gtk.AccessibleProperty.LABEL], [label + '（MiB）'])
            spin.set_sensitive(False)
            standard.set_sensitive(False)
            standard.connect('toggled', lambda check, control=spin:
                             control.set_sensitive(check.get_sensitive() and not check.get_active()))
            row_limits.append(standard)
            row_limits.append(spin)
            row_limits.append(Gtk.Label(label='MiB'))
            limit_page.append(row_limits)
            limit_controls[key] = (standard, spin)
        limit_page.append(Gtk.Label(label='1 MiB = 1,048,576バイト。指定範囲は1〜2047 MiBです。\n'
            '標準値を使う場合はClamAVへ数値を指定しません。\n'
            '両方を指定するときは合計上限を1ファイルの上限以上にしてください。\n'
            '上限を超えた対象は検査不完了として報告します。', xalign=0, wrap=True))
        info = Gtk.Label(label='保存先：' + str(self.store.path) + '\nThunderbirdアドオンの設定は変更しません。',
                         xalign=0, wrap=True, selectable=True)
        box.append(info)
        message = Gtk.Label(xalign=0, wrap=True)
        box.append(message)
        row = Gtk.Box(spacing=8, homogeneous=True)
        reset = Gtk.Button(label='ClamAV標準に戻す', sensitive=False)
        standards = {}
        def reset_values(_):
            for key, check in controls.items():
                if key in standards:
                    check.set_active(standards[key])
            for standard, _spin in limit_controls.values():
                standard.set_active(True)
            message.set_text('表示中の対応項目をClamAV標準値に戻しました。「検査設定を保存」で確定します。')
        reset.connect('clicked', reset_values)
        row.append(reset)
        save = Gtk.Button(label='検査設定を保存', sensitive=False)
        supported = set()
        def save_preferences(_button):
            try:
                values = {key: control.get_active() for key, control in controls.items()}
                option_arguments(values, supported)
                limits = {key: None if standard.get_active() else spin.get_value_as_int()
                          for key, (standard, spin) in limit_controls.items()}
                limit_arguments(limits, supported)
                self.store.set_scan_preferences(values, limits)
                message.set_text('保存しました。次回の検査から反映します。')
            except Exception as error:
                message.set_text('保存できませんでした：' + str(error))
        save.connect('clicked', save_preferences)
        row.append(save)
        close = Gtk.Button(label='閉じる')
        close.connect('clicked', lambda _: window.destroy())
        row.append(close)
        box.append(row)
        def check_support():
            try:
                binary = shutil.which('clamscan')
                if not binary:
                    raise ValueError('ClamAVが見つかりません。')
                available = supported_options(binary)
                defaults = standard_defaults(binary)
                status = engine_status()
            except Exception as error:
                GLib.idle_add(state.set_text, '対応項目を確認できませんでした：' + str(error))
                return
            def finish():
                supported.update(available)
                standards.update(defaults)
                reset.set_sensitive(bool(standards))
                state.set_text(status)
                for option in OPTIONS:
                    controls[option.key].set_sensitive(option.key in supported)
                    if option.key not in supported:
                        controls[option.key].set_label(option.label + '（このClamAVでは非対応）')
                for key, (standard, spin) in limit_controls.items():
                    standard.set_sensitive(key in supported)
                    spin.set_sensitive(key in supported and not standard.get_active())
                    if key not in supported:
                        standard.set_label('このClamAVでは数値指定に非対応')
                save.set_sensitive(True)
                return False
            GLib.idle_add(finish)
        window.present()
        threading.Thread(target=check_support, daemon=True).start()

    def selected_profile(self):
        index = self.profiles.get_selected()
        return self.saved_profiles[index] if index < len(self.saved_profiles) else None

    def refresh_profiles(self, selected_key=None):
        if selected_key is None and hasattr(self, 'saved_profiles'):
            current = self.selected_profile()
            selected_key = current[0] if current else None
        self.saved_profiles = self.store.profiles()
        self.refreshing_profiles = True
        self.profile_names.splice(0, self.profile_names.get_n_items(), [p.label for _, p in self.saved_profiles])
        index = next((i for i, (key, _) in enumerate(self.saved_profiles) if key == selected_key), 0)
        self.profiles.set_selected(index if self.saved_profiles else Gtk.INVALID_LIST_POSITION)
        self.refreshing_profiles = False
        self.profile_changed()
        self.refresh_manager()

    def profile_changed(self, *_args):
        if self.refreshing_profiles:
            return
        self.history(None)

    def set_controls(self, enabled):
        self.new_action.set_enabled(enabled)
        if self.manager:
            self.manager_selected()

    def scan_selected(self, _button):
        selected = self.operation_profile()
        if selected:
            self.confirm(selected[1], selected[0])

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
            self.manager_key = key
            self.refresh_profiles()
            self.show_notice('対象を複製しました。スケジュールは無効です。名前や条件を編集できます。')
        except Exception as error:
            self.show_notice('複製できませんでした：' + str(error))

    def delete_selected(self):
        selected = self.operation_profile()
        if not selected or self.busy:
            return
        key, plan = selected
        schedule = read_schedule(self.store, key)
        detail = plan.describe() + '\n\n' + schedule.describe()
        detail += ('\n\nこの対象設定と予定を削除します。実際のファイルと検査履歴は残します。'
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
            self.show_notice('対象と予定を削除しています…')
            def worker():
                try:
                    Scheduler(self.store).remove_profile(key)
                    message = '対象設定と予定を削除しました。検査履歴と実際のファイルは残しています。'
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
            threading.Thread(target=worker, daemon=True).start()
        dialog.connect('response', response)
        dialog.present()

    def edit_profile(self, key=None, plan=None):
        if self.busy:
            return
        plan = plan or Plan('', [str(Path.home())], [])
        editor = Gtk.Window(application=self, title='検査対象の設定', transient_for=self.operation_parent(), modal=True)
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
        name = entry('設定の名前（例：仕事の資料）', plan.label)
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
                text = Gtk.Label(label=('フォルダー: ' if item[1] else 'ファイル: ') + item[0],
                                 xalign=0, wrap=True, hexpand=True, selectable=True)
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
        refresh()
        exclusion_actions = Gtk.Box(spacing=8, homogeneous=True)
        box.append(exclusion_actions)
        for label, directory, action in [('除外フォルダーを追加', True, Gtk.FileChooserAction.SELECT_FOLDER),
                                         ('除外ファイルを追加', False, Gtk.FileChooserAction.OPEN)]:
            button = Gtk.Button(label=label)
            button.connect('clicked', lambda _, d=directory, a=action: pick(a, lambda p: add(p, d)))
            exclusion_actions.append(button)
        error_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(error_label)
        save = Gtk.Button(label='対象設定を保存')
        def save_profile(_button):
            try:
                new = Plan(name.get_text().strip(), [root.get_text()],
                           [p for p, directory in exclusions if not directory], plan.phishing,
                           [p for p, directory in exclusions if directory], plan.requires_admin)
                new_key = self.store.save_profile(new, key)
                if self.manager:
                    self.manager_key = new_key
                    self.refresh_profiles()
                else:
                    self.refresh_profiles(new_key)
                editor.destroy()
            except (ValueError, OSError) as error:
                error_label.set_text(str(error))
        save.connect('clicked', save_profile)
        save_actions = Gtk.Box(spacing=8, homogeneous=True)
        save_actions.append(save)
        cancel = Gtk.Button(label='キャンセル')
        cancel.connect('clicked', lambda _: editor.destroy())
        save_actions.append(cancel)
        box.append(save_actions)
        editor.present()

    def edit_schedule(self):
        selected = self.operation_profile()
        if not selected or self.busy:
            return
        key, plan = selected
        saved = read_schedule(self.store, key)
        window = Gtk.Window(application=self, title='スケジュール設定', transient_for=self.operation_parent(), modal=True)
        window.set_default_size(600, 450)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        window.set_child(box)
        box.append(Gtk.Label(label=plan.label, xalign=0, wrap=True))
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
        frequency = field('頻度', Gtk.DropDown.new_from_strings(['毎日', '毎週', '毎月']))
        frequency.set_selected(['daily', 'weekly', 'monthly'].index(saved.frequency))
        hour = field('時（0〜23）', Gtk.SpinButton.new_with_range(0, 23, 1))
        hour.set_value(saved.hour)
        minute = field('分（0〜59）', Gtk.SpinButton.new_with_range(0, 59, 1))
        minute.set_value(saved.minute)
        weekday = field('曜日', Gtk.DropDown.new_from_strings(['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']))
        weekday.set_selected(saved.weekday)
        monthday = field('日付（毎月1〜28日）', Gtk.SpinButton.new_with_range(1, 28, 1))
        monthday.set_value(saved.monthday)
        def frequency_changed(*_):
            weekday.set_sensitive(frequency.get_selected() == 1)
            monthday.set_sensitive(frequency.get_selected() == 2)
        frequency.connect('notify::selected', frequency_changed)
        frequency_changed()
        box.append(Gtk.Label(label='端末の現地時刻で実行します。画面を閉じても動きます。\n'
            'ログイン後に逃した予定を1回実行します。電源オフ中は動きません。\n'
            '既存cronは残るため、同じ対象の予定を重複させないでください。', xalign=0, wrap=True))
        message = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(message)
        apply = Gtk.Button(label='登録内容を確認')
        box.append(apply)
        def prepare(_button):
            schedule = Schedule(enabled.get_active(), ['daily', 'weekly', 'monthly'][frequency.get_selected()],
                                hour.get_value_as_int(), minute.get_value_as_int(),
                                weekday.get_selected(), monthday.get_value_as_int())
            manager = Scheduler(self.store)
            try:
                schedule.calendar()
                paths = manager.paths(key)
                detail = schedule.describe() + '\n\n' + plan.describe() + '\n\n'
                detail += 'ユーザー用systemdの登録先：\n' + '\n'.join(str(p) for p in paths)
                detail += '\n有効化時は復帰後の検査がすぐ始まる場合があります。\n無効化しても実行中の検査は完了まで続けます。'
                dialog = Gtk.MessageDialog(transient_for=window, modal=True,
                    message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                    text='この予定を登録しますか？', secondary_text=detail)
                def response(d, answer):
                    d.destroy()
                    if answer != Gtk.ResponseType.OK:
                        return
                    apply.set_sensitive(False)
                    message.set_text('予定を登録しています…')
                    def worker():
                        try:
                            manager.apply(key, schedule)
                            result = '登録しました。' + schedule.describe()
                        except Exception as error:
                            result = '予定を登録できませんでした：' + str(error)
                        def finished():
                            message.set_text(result)
                            apply.set_sensitive(True)
                            self.profile_changed()
                            if self.manager:
                                self.manager_selected()
                            return False
                        GLib.idle_add(finished)
                    threading.Thread(target=worker, daemon=True).start()
                dialog.connect('response', response)
                dialog.present()
            except (ValueError, OSError) as error:
                message.set_text(str(error))
        apply.connect('clicked', prepare)
        close = Gtk.Button(label='閉じる')
        close.connect('clicked', lambda _: window.destroy())
        box.append(close)
        window.present()

    def confirm(self, plan, key=None):
        plan = self.store.effective_plan(plan)
        if self.busy:
            return
        dialog = Gtk.MessageDialog(transient_for=self.operation_parent(), modal=True,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
            text='この範囲を検査しますか？', secondary_text=plan.describe())
        def response(window, answer):
            window.destroy()
            if answer == Gtk.ResponseType.OK and not self.busy:
                self.busy = True
                self.cancel.clear()
                self.set_controls(False)
                self.stop.set_sensitive(True)
                self.show_notice('検査中：' + plan.label)
                threading.Thread(target=self.run_scan, args=(plan, key), daemon=True).start()
        dialog.connect('response', response)
        dialog.present()

    def stop_scan(self, _button):
        if self.busy:
            self.cancel.set()
            self.stop.set_sensitive(False)
            self.show_notice('検査を停止しています…')

    def run_scan(self, target, key):
        try:
            status, output = scan(target, self.store, self.cancel, profile_id=key)
        except Exception as error:
            status, output = '検査不完了', str(error)
        GLib.idle_add(self.finished, status, output)

    def finished(self, status, output):
        self.busy = False
        self.set_controls(True)
        self.history(None)
        self.show_notice(status)
        if self.manager:
            self.stop.set_sensitive(False)
        notification = Gio.Notification.new('ClamAV検査終了')
        notification.set_body(status.splitlines()[0])
        self.send_notification('scan-result', notification)
        return False

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

    def on_close(self, _window):
        if self.busy:
            self.show_notice('処理中です。完了するまで終了できません。')
            return True
        return False


if __name__ == '__main__':
    raise SystemExit(Application().run())
