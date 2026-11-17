"""A small, accessible first-run flow; blocking work never runs on GTK's thread."""
import threading

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gtk

from .macos_setup import MacSetup, SetupCancelled, SetupError


class SetupWindow(Gtk.Window):
    def __init__(self, application, backend=None, only_if_needed=False):
        super().__init__(application=application, title='検査の準備',
                         transient_for=application.window, modal=True)
        self.set_destroy_with_parent(True)
        self.set_default_size(600, 650)
        self.app = application
        self.backend = backend or MacSetup()
        self.only_if_needed = only_if_needed
        self.running = False
        self.closed = False
        self.cancel = threading.Event()
        self.state = None
        self.completed = False
        self.service_busy = False
        self.pending_result = None
        self.setting_auto = False
        self.connect('close-request', self.on_close)
        self.set_titlebar(Gtk.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                      margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(layout)
        content = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        content.set_child(box)
        layout.append(content)
        title = Gtk.Label(label='安心して検査を始めるための準備', xalign=0, wrap=True)
        title.add_css_class('title-2')
        box.append(title)
        self.description = Gtk.Label(xalign=0, wrap=True, label=
            '必要なものを自動で確認します。準備ボタンを押すまで、ダウンロードや設定変更は行いません。')
        box.append(self.description)
        self.checklist = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.checklist.update_property([Gtk.AccessibleProperty.LABEL], ['検査に必要なものの確認結果'])
        box.append(self.checklist)
        self.auto_service = None
        if hasattr(self.backend, 'root'):
            from .macos_update import MacAutoUpdateService
            self.auto_service = MacAutoUpdateService(self.backend.root)
            row = Gtk.Box(spacing=12)
            row.append(Gtk.Label(label='検査用データの自動更新（推奨）', xalign=0, hexpand=True))
            self.auto_switch = Gtk.Switch(sensitive=False, valign=Gtk.Align.CENTER)
            self.auto_switch.update_property([Gtk.AccessibleProperty.LABEL], ['検査用データの自動更新'])
            self.auto_switch.connect('state-set', self.change_auto)
            row.append(self.auto_switch)
            box.append(row)
            self.auto_label = Gtk.Label(label='自動更新の状態を確認しています…', xalign=0, wrap=True)
            box.append(self.auto_label)
            row = Gtk.Box(spacing=12)
            self.notification_label = Gtk.Label(label='通知の状態を確認しています…', xalign=0, hexpand=True)
            row.append(self.notification_label)
            self.notify_button = Gtk.Button(label='通知を許可する')
            self.notify_button.connect('clicked', self.authorize_notifications)
            row.append(self.notify_button)
            box.append(row)
        self.message = Gtk.Label(label='必要なものを確認しています…', xalign=0, wrap=True, selectable=True)
        self.message.update_property([Gtk.AccessibleProperty.LABEL], ['準備の状況'])
        box.append(self.message)
        self.progress = Gtk.ProgressBar(show_text=False, visible=False)
        self.progress.update_property([Gtk.AccessibleProperty.LABEL], ['準備の進捗'])
        box.append(self.progress)
        self.details = Gtk.Expander(label='詳しい情報', visible=False)
        self.output = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.output.update_property([Gtk.AccessibleProperty.LABEL], ['準備の詳しい処理結果'])
        scroll = Gtk.ScrolledWindow(min_content_height=100, max_content_height=180,
                                   propagate_natural_height=True)
        scroll.set_child(self.output)
        self.details.set_child(scroll)
        box.append(self.details)
        if self.auto_service:
            self.remove_button = Gtk.Button(label='ClamAVをアンインストール…', halign=Gtk.Align.START)
            self.remove_button.add_css_class('destructive-action')
            self.remove_button.connect('clicked', self.preview_removal)
            box.append(self.remove_button)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.END, margin_start=24,
                          margin_end=24, margin_top=12, margin_bottom=16)
        self.later = Gtk.Button(label='あとで')
        self.later.connect('clicked', lambda _: self.close())
        buttons.append(self.later)
        self.primary = Gtk.Button(label='確認中…', sensitive=False)
        self.primary.add_css_class('suggested-action')
        self.primary.connect('clicked', self.start)
        buttons.append(self.primary)
        layout.append(buttons)
        if not only_if_needed:
            self.present()
        self.check()
        if self.auto_service and not only_if_needed:
            self.refresh_services()

    def check(self):
        def worker():
            try:
                state, error = self.backend.inspect(), None
            except Exception as exception:
                state, error = None, exception
            GLib.idle_add(self.checked, state, error)
        threading.Thread(target=worker, daemon=True).start()

    def checked(self, state, error):
        if self.closed:
            return False
        if self.only_if_needed and state and state.ready:
            # Gtk.Window.close() does not emit close-request before a first presentation.
            self.closed = True
            if self.app.setup_window is self:
                self.app.setup_window = None
            self.destroy()
            return False
        self.only_if_needed = False
        self.state = state
        self.draw_state()
        if self.auto_service:
            self.remove_button.set_sensitive(bool(state and state.engine) and not self.running and not self.service_busy)
        if error:
            self.show_error('準備の状態を確認できませんでした。保存先のアクセス権を確認して再試行してください。', error)
            self.primary.set_label('状態を再確認')
        elif state.ready:
            self.description.set_text('検査に必要なものは揃っています。検査用データはここから手動で更新できます。')
            self.message.set_text('検査の準備ができています。自動更新を有効にすると、アプリを閉じても更新を確認します。')
            self.primary.set_label('今すぐ更新を確認')
            self.later.set_label('閉じる')
        else:
            if not state.engine:
                self.description.set_text('ClamAV本体と検査用データを取得します。初回は数分以上かかることがあります。\n'
                    'macOSのインストール画面では「続ける」を押してください。必要な場合は管理者の認証を求められます。')
                self.primary.set_label('必要なものをインストール')
                self.message.set_text('ClamAV本体のダウンロードは約159 MiBです。検査用データも取得するため、'
                                      '数GBの空き容量とインターネット接続をご用意ください。')
            else:
                self.description.set_text('ClamAVは導入済みです。不足する初期設定と検査用データを準備します。')
                self.primary.set_label('検査の準備をする')
                self.message.set_text('検査用データをインターネットから取得・更新します。\n'
                                      '検査プラン・履歴や、他のアプリの設定は変更しません。')
        self.primary.set_sensitive(True)
        if self.pending_result:
            message, error = self.pending_result
            self.pending_result = None
            if error:
                self.show_error(message, error)
            else:
                self.message.set_text(message)
        self.present()
        if self.auto_service:
            self.refresh_services()
        return False

    def draw_state(self):
        while self.checklist.get_first_child():
            self.checklist.remove(self.checklist.get_first_child())
        if self.state:
            for text, ready in self.state.items():
                label = Gtk.Label(label=('✓ 準備済み：' if ready else '○ 準備が必要：') + text,
                                  xalign=0, wrap=True)
                self.checklist.append(label)

    def start(self, _button=None):
        if self.running:
            return
        if self.completed:
            self.close()
            return
        if self.state is None:
            self.primary.set_sensitive(False)
            self.check()
            return
        if self.app.busy or self.app.schedule_busy:
            self.message.set_text('別の処理が終わってから準備を始めてください。')
            return
        self.running = self.app.busy = True
        self.app.set_controls(False)
        self.cancel.clear()
        self.primary.set_sensitive(False)
        self.later.set_label('準備を中止')
        # During setup this button requests a cooperative stop; Installer is never killed.
        self.later.set_sensitive(True)
        self.details.set_visible(False)
        if self.auto_service:
            self.auto_switch.set_sensitive(False)
            self.remove_button.set_sensitive(False)
        self.progress.set_visible(True)
        self.message.set_text('準備を始めています…')
        self.pulse_timer = GLib.timeout_add(150, self.pulse)
        force_update = self.state.ready
        def report(message, fraction):
            GLib.idle_add(self.report, message, fraction)
        def worker():
            try:
                if self.auto_service and self.state.engine and self.state.configuration and self.auto_service.status().enabled:
                    self.auto_service.update(self.cancel, report)
                    state = self.backend.inspect()
                else:
                    state = self.backend.prepare(cancel=self.cancel, report=report, force_update=force_update)
                error = None
            except Exception as exception:
                error = exception
                try:
                    state = self.backend.inspect()
                except Exception:
                    state = self.state
            GLib.idle_add(self.finished, state, error)
        threading.Thread(target=worker, daemon=True).start()

    def pulse(self):
        if not self.running:
            return False
        if getattr(self, 'indeterminate', True):
            self.progress.pulse()
        return True

    def report(self, message, fraction):
        if self.closed or not self.running:
            return False
        self.message.set_text(message)
        self.indeterminate = fraction is None
        if fraction is not None:
            self.progress.set_fraction(fraction)
        return False

    def finished(self, state, error):
        self.running = self.app.busy = False
        GLib.source_remove(self.pulse_timer)
        self.app.set_controls(True)
        self.state = state
        self.draw_state()
        self.primary.set_sensitive(True)
        self.later.set_sensitive(True)
        self.later.set_label('あとで')
        if error:
            self.progress.set_visible(False)
            message = str(error) if isinstance(error, SetupError) else (
                '準備を完了できませんでした。詳しい情報を確認し、再試行してください。')
            self.show_error(message, error)
            self.primary.set_label('再試行')
        else:
            self.completed = True
            self.description.set_text('必要なものが揃いました。準備画面を閉じて、左上の「＋」から検査するフォルダーを選べます。')
            self.message.set_text('準備ができました。検査用データは「検査の準備・更新」から更新できます。\n'
                                  '自動更新を有効にすると、アプリを閉じても更新を確認します。')
            self.progress.set_fraction(1)
            self.primary.set_label('使い始める')
            self.later.set_visible(False)
        self.app.refresh_auto_status(force=True)
        if self.auto_service:
            self.remove_button.set_sensitive(bool(state and state.engine))
            self.refresh_services()
        return False

    def refresh_services(self):
        if self.closed or not self.auto_service:
            return
        def worker():
            try:
                status, error = self.auto_service.status(), None
            except Exception as exception:
                status, error = None, exception
            from .macos_notifications import request
            notification = request('status')
            def finish():
                if self.closed:
                    return False
                if status:
                    self.setting_auto = True
                    self.auto_switch.set_active(status.enabled is True)
                    self.auto_switch.set_state(status.enabled is True)
                    self.setting_auto = False
                    self.auto_switch.set_sensitive(status.manageable and not self.running and not self.service_busy)
                    self.auto_label.set_text(status.label() + '\n約2時間ごとに確認します。ログイン中はアプリを閉じても動作します。' +
                                             ('\n' + status.detail if status.detail else ''))
                    self.app.show_auto_status(status)
                else:
                    self.auto_label.set_text('自動更新の状態を確認できませんでした。')
                from .macos_notifications import status_label
                self.notification_label.set_text(status_label(notification))
                self.notification_state = notification.get('authorization')
                self.notify_button.set_sensitive(self.notification_state in (0, 1) and not self.running)
                if notification.get('authorization') == 1:
                    self.notify_button.set_label('通知の設定を開く')
                    self.notification_label.set_text('通知：未許可。「ClamAV GUIの検査通知」を通知設定で許可してください。')
                elif notification.get('authorization') in (2, 3):
                    self.notify_button.set_label('許可済み')
                else:
                    self.notify_button.set_label('通知を許可する')
                return False
            GLib.idle_add(finish)
        threading.Thread(target=worker, daemon=True).start()

    def change_auto(self, _switch, enabled):
        if self.setting_auto or self.running or self.service_busy or self.app.busy:
            return True
        message = ('ログイン中はアプリを閉じても約2時間ごとに検査用データを取得・更新します。' if enabled else
                   '今後の自動更新を停止し、実行中の更新があれば中止します。手動での更新は引き続き使えます。')
        dialog = Gtk.MessageDialog(application=self.app, transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                                   text='自動更新を有効にしますか？' if enabled else '自動更新を無効にしますか？',
                                   secondary_text=message)
        def response(window, answer):
            window.destroy()
            if answer != Gtk.ResponseType.OK:
                return
            self.service_busy = self.app.busy = True
            self.app.set_controls(False)
            self.auto_switch.set_sensitive(False)
            self.primary.set_sensitive(False)
            def worker():
                try:
                    message, status = self.auto_service.set_enabled(enabled)
                except Exception as error:
                    message = '自動更新を変更できませんでした：' + str(error)
                def finish():
                    self.service_busy = self.app.busy = False
                    self.app.set_controls(True)
                    self.message.set_text(message)
                    self.primary.set_sensitive(True)
                    self.refresh_services()
                    return False
                GLib.idle_add(finish)
            threading.Thread(target=worker, daemon=True).start()
        dialog.connect('response', response)
        dialog.present()
        return True

    def authorize_notifications(self, _button):
        if getattr(self, 'notification_state', None) == 1:
            import subprocess
            subprocess.Popen(['/usr/bin/open', 'x-apple.systempreferences:com.apple.Notifications-Settings.extension'])
            return
        self.notify_button.set_sensitive(False)
        def worker():
            from .macos_notifications import request
            request('authorize')  # Only this explicit button can display the OS permission prompt.
            GLib.idle_add(self.refresh_services)
        threading.Thread(target=worker, daemon=True).start()

    def preview_removal(self, _button):
        if self.running or self.service_busy or self.app.busy:
            return
        self.remove_button.set_sensitive(False)
        def worker():
            from .macos_packages import MacPackageManager
            manager = MacPackageManager()
            try:
                plan, error = manager.preview(), None
            except Exception as exception:
                plan, error = None, exception
            def finish():
                self.remove_button.set_sensitive(True)
                if error:
                    self.show_error('削除内容を確認できませんでした：' + str(error), error)
                    return False
                dialog = Gtk.MessageDialog(application=self.app, transient_for=self, modal=True,
                    message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL,
                    text='ClamAVをアンインストールしますか？', secondary_text=plan.describe())
                def response(window, answer):
                    window.destroy()
                    if answer == Gtk.ResponseType.OK:
                        self.remove_engine(manager, plan)
                dialog.connect('response', response)
                dialog.present()
                return False
            GLib.idle_add(finish)
        threading.Thread(target=worker, daemon=True).start()

    def remove_engine(self, manager, plan):
        self.service_busy = self.app.busy = True
        self.app.set_controls(False)
        self.primary.set_sensitive(False)
        self.remove_button.set_sensitive(False)
        self.message.set_text('ClamAVをアンインストールしています。必要な場合はmacOSの認証画面で許可してください。')
        def worker():
            try:
                message, code = manager.remove_with_settings(plan, self.auto_service, self.app.store)
                error = None
            except Exception as exception:
                message, error = 'アンインストールを完了できませんでした。', exception
            def finish():
                self.service_busy = self.app.busy = False
                self.app.set_controls(True)
                self.completed = False
                self.later.set_visible(True)
                self.primary.set_sensitive(True)
                self.pending_result = (message, error)
                self.check()
                return False
            GLib.idle_add(finish)
        threading.Thread(target=worker, daemon=True).start()

    def show_error(self, message, error):
        self.message.set_text(message)
        self.output.get_buffer().set_text(type(error).__name__ + ': ' + str(error) + '\n\n' +
                                          self.backend.last_output)
        self.details.set_visible(not isinstance(error, SetupCancelled))

    def on_close(self, _window):
        if self.service_busy:
            self.message.set_text('設定変更・アンインストールの完了を待ってから閉じてください。')
            return True
        if self.running:
            self.cancel.set()
            self.message.set_text('中止を依頼しました。macOSのインストール画面が開いている場合は、'
                                  'その画面で取り消すか、完了して閉じてください。')
            self.later.set_sensitive(False)
            return True
        self.closed = True
        if getattr(self.app, 'setup_window', None) is self:
            self.app.setup_window = None
        return False
