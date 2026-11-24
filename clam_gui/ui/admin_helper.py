"""In-app setup for the optional, authenticated administrator scan helper."""
from gi.repository import GLib, Gtk, Gdk
from ..admin_helper_setup import AdminHelperSetup
from ..admin_scan import HELPER, POLICY
from ..jobs import start_worker


class AdminHelperWindow:
    def admin_helper_preferences(self, pages):
        if self.backend.name != 'linux':
            return
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_start=16, margin_end=16, margin_top=16, margin_bottom=16)
        content.append(Gtk.Label(label='管理者検査の補助機能', xalign=0))
        content.append(Gtk.Label(label='通常は不要です。読み取れない場所を手動で検査したい場合に導入します。検査のたびにUbuntuの画面で管理者認証し、GUIは通常権限で動作します。', wrap=True, xalign=0))
        state = Gtk.Label(label='導入状態を確認しています…', xalign=0, wrap=True, selectable=True)
        content.append(state)
        actions = Gtk.Box(spacing=8)
        install = Gtk.Button(label='導入する', sensitive=False)
        remove = Gtk.Button(label='削除する…', sensitive=False)
        actions.append(install)
        actions.append(remove)
        content.append(actions)
        pages.append_page(content, Gtk.Label(label='管理者検査'))
        manager = AdminHelperSetup()
        def refresh():
            install.set_sensitive(False)
            remove.set_sensitive(False)
            def worker():
                try:
                    current, error = manager.status(), None
                except Exception as caught:
                    current, error = None, str(caught)
                def done():
                    if not content.get_root():
                        return False
                    state.set_text(error or {'missing': '補助機能：未導入', 'ready': '補助機能：導入済み', 'update': '補助機能：更新が必要です'}[current])
                    install.set_label('更新する' if current == 'update' else '導入する')
                    install.set_sensitive(current in ('missing', 'update') and not self.schedule_busy and not self.busy)
                    remove.set_sensitive(current in ('ready', 'update') and not self.schedule_busy and not self.busy)
                    return False
                GLib.idle_add(done)
            start_worker(worker, lambda error: state.set_text('状態を確認できませんでした：' + str(error)))
        install.connect('clicked', lambda _: self.admin_helper_setup(content.get_root(), on_ready=refresh))
        remove.connect('clicked', lambda _: self.admin_helper_setup(content.get_root(), action='remove', on_ready=refresh))
        refresh()

    def admin_helper_setup(self, parent=None, *, action='install', on_ready=None):
        if self.schedule_busy or self.busy or self.active_runs:
            self.show_notice('検査や設定変更が終わってから、補助機能を変更してください。')
            return
        existing = getattr(self, 'admin_setup_window', None)
        if existing is not None:
            existing.present()
            return
        remove = action == 'remove'
        window = Gtk.Window(application=self, title='管理者検査の補助機能',
                            transient_for=parent or self.operation_parent(), modal=True)
        window.set_default_size(580, 320)
        self.admin_setup_window = window
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_start=20, margin_end=20, margin_top=16, margin_bottom=16)
        window.set_child(content)
        content.append(Gtk.Label(label='補助機能を削除しますか？' if remove else '管理者検査の補助機能を導入・更新しますか？', wrap=True, xalign=0))
        description = ('管理者検査用の補助プログラムと専用の認証設定を削除します。検査プラン・履歴・ClamAV本体は残します。' if remove else
            '管理者検査に必要な専用の補助プログラムと認証設定を、このアプリから導入します。Ubuntuの認証画面で承認してください。\n導入の認証と、検査を開始する認証は別々です。通常の検査や定期検査の権限は変わりません。')
        content.append(Gtk.Label(label=description, xalign=0, wrap=True))
        details = Gtk.Expander(label='変更する内容')
        details.set_child(Gtk.Label(label='対象パッケージ：clamav-gui-admin-helper\n' + str(HELPER) + '\n' + str(POLICY), wrap=True, selectable=True, xalign=0))
        content.append(details)
        message = Gtk.Label(xalign=0, wrap=True, selectable=True)
        content.append(message)
        state = {'busy': False}
        cancel = Gtk.Button(label='キャンセル')
        apply = Gtk.Button(label='削除する' if remove else '導入・更新する')
        apply.add_css_class('destructive-action' if remove else 'suggested-action')
        header = Gtk.HeaderBar(show_title_buttons=False)
        header.set_title_widget(Gtk.Label(label='管理者検査の補助機能'))
        header.pack_start(cancel)
        header.pack_end(apply)
        window.set_titlebar(header)
        def closed(*_):
            if state['busy']:
                return True
            self.admin_setup_window = None
            return False
        window.connect('close-request', closed)
        def dismiss(*_):
            if not state['busy']:
                self.admin_setup_window = None
                window.destroy()
        cancel.connect('clicked', dismiss)
        keys = Gtk.EventControllerKey()
        def key_pressed(_controller, keyval, _keycode, _modifiers):
            if keyval == Gdk.KEY_Escape and not state['busy']:
                dismiss()
                return True
            return False
        keys.connect('key-pressed', key_pressed)
        window.add_controller(keys)
        def execute(*_):
            if state['busy'] or self.busy or self.schedule_busy or self.active_runs:
                message.set_text('処理中です。検査や設定変更が終わってから再試行してください。')
                return
            state['busy'] = self.schedule_busy = True
            apply.set_sensitive(False)
            cancel.set_sensitive(False)
            message.set_text('Ubuntuの画面で管理者認証してください。処理が終わるまでお待ちください。')
            self.poll_plans()
            def done(error=None):
                state['busy'] = self.schedule_busy = False
                self.poll_plans()
                if error:
                    message.set_text(str(error))
                    apply.set_sensitive(True)
                    cancel.set_sensitive(True)
                else:
                    dismiss()
                    self.show_notice('管理者検査の補助機能を削除しました。' if remove else '管理者検査の補助機能を導入・更新しました。')
                    if on_ready:
                        on_ready()
                return False
            def worker():
                try:
                    AdminHelperSetup().execute(action)
                except Exception as error:
                    GLib.idle_add(done, error)
                else:
                    GLib.idle_add(done)
            start_worker(worker, done)
        apply.connect('clicked', execute)
        window.present()
