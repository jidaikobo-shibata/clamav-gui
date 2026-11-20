"""PackageWindow view and its callbacks; OS services are injected through the application backend."""
import gi
gi.require_version('Gtk', '4.0')
import threading
import time
from gi.repository import GLib, Gtk
from ..definitions import AutoUpdateState
from ..jobs import start_worker


class PackageWindow:
    def show_auto_status(self, status):
        self.auto_generation += 1
        self.auto_status = status
        self.definition_button.set_label(status.label() + '…')
        self.definition_button.set_tooltip_text('定義ファイルの更新設定を開きます。\n' + status.frequency() +
                                               ('\n' + status.detail if status.detail else ''))

    def refresh_auto_status(self, force=False):
        if self.auto_checking or (not force and self.auto_checked_at is not None and
                                 time.monotonic() - self.auto_checked_at < 15):
            return
        self.auto_checking = True
        generation = self.auto_generation
        def worker():
            try:
                status = self.backend.auto_update().status()
            except Exception as error:
                status = AutoUpdateState(detail=str(error))
            def finished():
                self.auto_checking = False
                self.auto_checked_at = time.monotonic()
                if generation == self.auto_generation:
                    self.show_auto_status(status)
                return False
            GLib.idle_add(finished)
        def failed(error):
            self.auto_checking = False
            self.auto_checked_at = time.monotonic()
            self.show_auto_status(AutoUpdateState(detail='状態確認を開始できませんでした：' + str(error)))
        start_worker(worker, failed)

    def refresh_engine_status(self, force=False):
        if self.engine_checking:
            self.engine_check_pending |= force
            return
        if not force and self.engine_checked_at is not None and time.monotonic()-self.engine_checked_at < 300:
            return
        self.engine_checking = True
        self.engine_refresh.set_sensitive(False)
        def deliver(label, detail):
            self.engine_checking = False
            self.engine_checked_at = time.monotonic()
            self.engine_label.set_text(label)
            self.engine_label.set_tooltip_text(detail)
            self.engine_refresh.set_sensitive(True)
            if self.engine_check_pending:
                self.engine_check_pending = False
                self.refresh_engine_status(force=True)
            return False
        def worker():
            try:
                label, detail = self.backend.packages().engine_update_status()
            except Exception as error:
                label, detail = 'ClamAV本体：更新状態を確認できません', str(error)
            GLib.idle_add(deliver, label, detail)
        start_worker(worker, lambda error: deliver('ClamAV本体：更新状態を確認できません', str(error)))

    def package_setup(self, manager=None, updater=None, update_only=False, auto_manager=None):
        manager = manager or self.backend.packages()
        updater = updater or self.backend.updater()
        auto_manager = auto_manager or self.backend.auto_update()
        window = Gtk.Window(application=self, title='定義ファイルの更新設定' if update_only else 'ClamAVの管理',
                            transient_for=self.operation_parent(), modal=True)
        window.set_default_size(720, 580)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        window.set_child(box)
        status = Gtk.Label(label='更新機能の状態を確認しています…' if update_only else '導入状態を確認しています…',
                           xalign=0, wrap=True, selectable=True)
        box.append(status)
        explanation = ('既存のFreshClam自動更新サービスを利用して、定義ファイルを取得・更新します。'
                       if update_only else 'ClamAVは、このGUI以外のアプリも利用する共通のソフトウェアです。\n'
                       '削除すると、ClamAVを利用する他のアプリの検査も使えなくなります。')
        box.append(Gtk.Label(label=explanation, xalign=0, wrap=True))
        controls = Gtk.Box(spacing=8, homogeneous=True)
        install = Gtk.Button(label='インストール')
        remove = Gtk.Button(label='アンインストール')
        remove.add_css_class('destructive-action')
        controls.append(install)
        controls.append(remove)
        box.append(controls)
        controls.set_visible(not update_only)
        auto_row = Gtk.Box(spacing=12)
        auto_row.append(Gtk.Label(label='定義ファイルの自動更新', xalign=0, hexpand=True))
        auto_switch = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=False)
        auto_switch.update_property([Gtk.AccessibleProperty.LABEL], ['定義ファイルの自動更新'])
        auto_row.append(auto_switch)
        box.append(auto_row)
        auto_details = Gtk.Label(label='自動更新の状態を確認しています…', xalign=0, wrap=True)
        box.append(auto_details)
        update_controls = Gtk.Box(spacing=8)
        update = Gtk.Button(label='今すぐ更新を確認', hexpand=True)
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
        state = {'running': False, 'closed': False, 'installed': {}, 'updating': False, 'cancel': threading.Event(), 'auto': None, 'setting_switch': False, 'temporary_update': False}
        def close_request(_window):
            if state['updating']:
                state['cancel'].set()
                message.set_text('更新を中断し、一時起動したサービスを停止しています…' if state['temporary_update'] else
                                 '監視を終了しています。更新サービスは停止しません。')
                return True
            if state['running']:
                message.set_text('処理中です。完了までお待ちください。OSのパッケージ処理は途中で終了しません。')
                return True
            state['closed'] = True
            return False
        window.connect('close-request', close_request)
        def buttons():
            ready = not state['running'] and not self.busy
            install.set_sensitive(ready and len(state['installed']) < len(manager.package_names))
            remove.set_sensitive(ready and bool(state['installed']))
            update.set_sensitive(ready and self.backend.can_update(state['installed']) and
                                 bool(state['auto'] and state['auto'].manageable))
            refresh.set_sensitive(ready)
            auto_switch.set_sensitive(ready and bool(state['auto'] and state['auto'].manageable))
        def check_state():
            install.set_sensitive(False)
            remove.set_sensitive(False)
            update.set_sensitive(False)
            refresh.set_sensitive(False)
            auto_switch.set_sensitive(False)
            def worker():
                try:
                    auto_state = auto_manager.status()
                except Exception as auto_error:
                    auto_state = AutoUpdateState(detail=str(auto_error))
                try:
                    installed = manager.status()
                    lines = [package + ': ' + installed.get(package, '未導入') for package in manager.package_names]
                    text = (self.backend.definition_label() + '\n' + self.backend.definition_status() if update_only else
                            '\n'.join(lines) + '\n\n' + self.backend.definition_status())
                    if not update_only and len(installed) == len(manager.package_names):
                        text += '\n初回導入後は、定義ファイルの更新が終わってから検査してください。'
                    error = None
                except Exception as exception:
                    installed, text, error = {}, str(exception), exception
                def finished():
                    if state['closed']:
                        return False
                    self.refresh_engine_status(force=True)
                    state['installed'] = installed
                    state['auto'] = auto_state
                    state['setting_switch'] = True
                    auto_switch.set_active(auto_state.enabled is True)
                    auto_switch.set_state(auto_state.enabled is True)
                    state['setting_switch'] = False
                    auto_details.set_text(auto_state.label() + '\n' + auto_state.frequency() +
                                          ('\n' + auto_state.detail if auto_state.detail else ''))
                    self.show_auto_status(auto_state)
                    status.set_text(text)
                    if error is None:
                        buttons()
                        if update_only and 'clamav-freshclam' not in installed:
                            message.set_text('FreshClamが未導入です。メインメニューの「ClamAVの管理」からインストールしてください。')
                    else:
                        message.set_text('この環境では定義ファイルの更新を開始できません。' if update_only else
                                         'この環境では導入・削除を実行できません。')
                        refresh.set_sensitive(True)
                    return False
                GLib.idle_add(finished)
            def failed(error):
                if not state['closed']:
                    status.set_text('状態確認を開始できませんでした：' + str(error))
                    refresh.set_sensitive(True)
            start_worker(worker, failed)
        def release():
            state['running'] = False
            state['updating'] = False
            state['temporary_update'] = False
            stop_monitor.set_label('監視を終了')
            stop_monitor.set_sensitive(False)
            self.busy = False
            self.set_controls(True)
            progress.stop()
            close.set_sensitive(True)
            buttons()
        def failed_operation(error):
            release()
            output.get_buffer().set_text(str(error))
            message.set_text('処理を開始できませんでした。状態を再確認して再実行してください。')

        def execute(plan, confirmation):
            confirmation.destroy()
            message.set_text('管理者認証後に処理します。ダウンロードやパッケージ処理の完了までお待ちください。')
            def worker():
                install_succeeded = False
                try:
                    text, code = manager.execute(plan)
                    after = manager.status()
                    expected = len(after) == len(manager.package_names) if plan.action == 'install' else not after
                    if code == 0 and expected:
                        install_succeeded = plan.action == 'install'
                        result = 'インストールが完了しました。「今すぐ更新を確認」で更新状態を確認してください。' if plan.action == 'install' else 'アンインストールが完了しました。'
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
                        message.set_text(result + '\n自動更新の状態を確認し、無効ならスイッチで有効にしてください。')
                    return False
                GLib.idle_add(finished)
            start_worker(worker, failed_operation)
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
            advanced = Gtk.Expander(label='詳細な処理予定（APTの出力）')
            raw = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            raw.update_property([Gtk.AccessibleProperty.LABEL], ['APTの事前確認結果'])
            raw.get_buffer().set_text(plan.detail)
            raw_scroll = Gtk.ScrolledWindow(min_content_height=130, max_content_height=180)
            raw_scroll.set_child(raw)
            advanced.set_child(raw_scroll)
            area.append(advanced)
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
            start_worker(worker, failed_operation)
        def change_auto(_switch, enabled):
            if state['setting_switch']:
                return True
            if state['running'] or self.busy or not state['auto'] or not state['auto'].manageable:
                check_state()
                return True
            if state['auto'].enabled == enabled and state['auto'].running == enabled:
                return True
            detail = ('OS起動時の自動起動を有効にして、更新サービスを開始します。アプリを閉じても動きます。'
                      if enabled else '更新サービスを停止し、OS起動時の自動起動も無効にします。取得中の処理があれば中断します。')
            detail += '\nこの端末でClamAVを使う他の機能にも影響します。管理者認証が必要な場合があります。'
            dialog = Gtk.MessageDialog(transient_for=window, modal=True, message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.OK_CANCEL, text='自動更新を有効にしますか？' if enabled else '自動更新を無効にしますか？',
                secondary_text=detail)
            def response(d, answer):
                d.destroy()
                if answer != Gtk.ResponseType.OK or self.busy:
                    check_state()
                    return
                self.busy = True
                state['running'] = True
                self.set_controls(False)
                buttons()
                progress.start()
                message.set_text('自動更新の設定を変更しています…')
                def worker():
                    try:
                        result, _ = auto_manager.set_enabled(enabled)
                    except Exception as error:
                        result = '自動更新の設定を変更できませんでした：' + str(error)
                    def finished():
                        message.set_text(result)
                        release()
                        check_state()
                        return False
                    GLib.idle_add(finished)
                start_worker(worker, failed_operation)
            dialog.connect('response', response)
            dialog.present()
            return True
        auto_switch.connect('state-set', change_auto)
        def begin_update():
            if self.busy:
                message.set_text('別の処理が実行中です。完了後に再実行してください。')
                return
            temporary = bool(state['auto'] and state['auto'].enabled is False and state['auto'].running is False)
            dialog = Gtk.MessageDialog(application=self, transient_for=window, modal=True,
                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                text='定義ファイルを取得・更新しますか？',
                secondary_text='自動更新サービスを利用して、公式の更新先へ接続します。\n'
                    '既存のサービスを使い、別のFreshClamを並行起動しません。\n'
                    '管理者認証が必要な場合があります。取得中の場合はその処理を監視します。' +
                    ('\n自動更新は無効のまま、一時的にサービスを起動します。完了・監視終了・エラー時に停止します。' if temporary else ''))
            def response(d, answer):
                d.destroy()
                if answer != Gtk.ResponseType.OK or self.busy:
                    return
                self.busy = True
                state['running'] = state['updating'] = True
                state['temporary_update'] = temporary
                stop_monitor.set_label('更新を中断' if temporary else '監視を終了')
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
                        options = {'restore_stopped': True} if temporary else {}
                        result, text = updater.update(cancel=state['cancel'], progress=report, **options)
                    except Exception as error:
                        result, text = '定義の取得・更新を確認できませんでした。', str(error)
                    def finished():
                        output.get_buffer().set_text(text)
                        message.set_text(result)
                        release()
                        check_state()
                        return False
                    GLib.idle_add(finished)
                start_worker(worker, failed_operation)
            dialog.connect('response', response)
            dialog.present()
        update.connect('clicked', lambda _: begin_update())
        stop_monitor.connect('clicked', lambda _: state['cancel'].set())
        install.connect('clicked', lambda _: prepare('install'))
        remove.connect('clicked', lambda _: prepare('remove'))
        refresh.connect('clicked', lambda _: check_state())
        window.present()
        check_state()
