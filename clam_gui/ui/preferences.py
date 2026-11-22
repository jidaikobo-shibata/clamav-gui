"""PreferencesWindow view and its callbacks; OS services are injected through the application backend."""
import gi
gi.require_version('Gtk', '4.0')
import threading
import shutil
from gi.repository import GLib, Gtk
from ..core import engine_status
from ..jobs import start_worker
from ..options import OPTIONS, supported_options, standard_defaults, option_arguments, LIMITS, APP_LIMITS, limit_arguments
from ..retention import validate as validate_automation
from ..background import enabled as background_enabled, heartbeat


class PreferencesWindow:
    def background_controls(self, content):
        service = self.backend.background(self.store)
        state = Gtk.Label(xalign=0, wrap=True)
        content.append(state)
        control = Gtk.Button(halign=Gtk.Align.START)
        content.append(control)
        content.append(Gtk.Label(label='有効にするとログイン時から、画面を閉じても監視します。ログアウト時は停止します。\n'
            '初回は比較元を作成し、次回起動・スリープ復帰時は前回からの追加・変更も確認します。\n'
            '停止中に作成・削除されたファイルや、接続・取り外しされたメディアは後から検査できません。',
            xalign=0, wrap=True))
        def refresh():
            active = background_enabled(self.store)
            snapshot = heartbeat(self.store)
            connected = bool(snapshot)
            state.set_text('常駐監視：' + ('稼働中（ログイン中）' if connected and active else
                '有効・接続できません。再起動するかシステムのユーザーサービスを確認してください。' if active else
                '無効（画面を開いている間だけ監視します）'))
            delivery = snapshot.get('notifications', {})
            if delivery.get('pending'):
                state.set_text(state.get_text() + f"\n通知待ち：{delivery['pending']}件" + ('（配信失敗のため再試行中）' if delivery.get('error') else ''))
            control.set_label('常駐監視を無効にする…' if active else '常駐監視を有効にする…')
            control.set_sensitive(service.supported() and not self.schedule_busy)
            return True
        refresh()
        restart = Gtk.Button(label='常駐監視を再起動…', halign=Gtk.Align.START)
        content.append(restart)
        def change(enable):
            if self.schedule_busy or self.automation.active:
                self.show_notice('処理中です。監視の検査が終わってから変更してください。')
                return
            dialog = Gtk.MessageDialog(transient_for=content.get_root(), modal=True,
                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                text='ログイン中の常駐監視を有効にしますか？' if enable else '常駐監視を停止して無効にしますか？',
                secondary_text=('ユーザーサービスを登録・起動します。次回ログイン時も起動します。再起動の場合、実行中の自動検査は中止します。管理者権限は不要です。' if enable else
                    'ユーザーサービスを停止し、自動起動を解除します。実行中の自動検査も中止します。画面が開いていれば画面側の監視に戻ります。')+
                    '\n変更する設定：'+str(service.path)+'\nClamAV本体・定義更新・定期検査のサービスは変更しません。')
            def response(window, result):
                window.destroy()
                if result != Gtk.ResponseType.OK:
                    return
                self.schedule_busy = True
                refresh()
                def worker():
                    try:
                        service.apply(enable)
                        error = None
                    except Exception as caught:
                        error = str(caught)
                    def done():
                        self.schedule_busy = False
                        self.configure_automation()
                        refresh()
                        state.set_text(('変更できませんでした：'+error) if error else
                            ('常駐監視を有効にしました。起動状態は数秒後に反映されます。' if enable else '常駐監視を無効にしました。'))
                        return False
                    GLib.idle_add(done)
                def failed(error):
                    self.schedule_busy = False
                    refresh()
                    state.set_text('設定の変更処理を開始できませんでした：' + str(error))
                start_worker(worker, failed)
            dialog.connect('response', response)
            dialog.present()
        control.connect('clicked', lambda _: change(not background_enabled(self.store)))
        restart.connect('clicked', lambda _: change(True))
        def update():
            if not content.get_root():
                return False
            refresh()
            restart.set_sensitive(background_enabled(self.store) and service.supported() and not self.schedule_busy)
            return True
        restart.set_sensitive(background_enabled(self.store) and service.supported() and not self.schedule_busy)
        GLib.timeout_add_seconds(3, update)

    def automation_preferences(self, pages):
        values = self.store.automation_settings()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(content)
        pages.append_page(scroll, Gtk.Label(label='自動検査・履歴'))
        self.background_controls(content)
        media = Gtk.CheckButton(label='外部メディア検査を実施する', active=values['media_enabled'])
        content.append(media)
        content.append(Gtk.Label(label='接続後に読み取り可能になったUSBメモリーやデータCD/DVDを検査します。\n'
            'オンにすると左ペインに専用プランを表示します。既に接続済みのものは専用プランの「今すぐ検査」から検査できます。\n'
            'オフでも実行中の検査は続行します。メディアの利用は禁止しません。', wrap=True, xalign=0))
        history = Gtk.Button(label='外部メディアの保存済み履歴を表示')
        history.connect('clicked', lambda _: self.media_history())
        content.append(history)
        retention = Gtk.CheckButton(label='履歴の保存上限を有効にする', active=values['retention_enabled'])
        content.append(retention)
        fields = {}
        for key, title, low, high in [('retention_days', '通常結果の保存日数', 1, 3650),
                                      ('alert_days', '検出・不完了の保存日数', 1, 3650),
                                      ('max_mib', '履歴本文の上限（MiB）', 16, 4096),
                                      ('max_records', '履歴件数の上限', 100, 100000)]:
            row = Gtk.Box(spacing=12)
            row.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            spin = Gtk.SpinButton.new_with_range(low, high, 1)
            spin.set_value(values[key])
            spin.update_property([Gtk.AccessibleProperty.LABEL], [title])
            fields[key] = spin
            row.append(spin)
            content.append(row)
        content.append(Gtk.Label(label='保存時に確認してから適用します。古い通常結果から整理し、容量・件数超過時は検出・不完了も削除対象です。\n'
            '本文の論理容量の上限です。SQLiteファイルは削除直後に縮まず、空き領域を再利用します。\n'
            'フォルダー監視は検査待ち1000件、1回50ファイル、監視フォルダー最大1024です。上限超過は不完了として記録します。\n'
            '正常な自動検査の通知は20秒ごとにまとめます。監視停止中や監視準備中の取りこぼし対策として定期的な全体検査を併用してください。',
            xalign=0, wrap=True))
        def read():
            return dict(media_enabled=media.get_active(), retention_enabled=retention.get_active(),
                        **{key: spin.get_value_as_int() for key, spin in fields.items()})
        return read

    def preferences(self):
        window = Gtk.Window(application=self, title='環境設定', transient_for=self.operation_parent(), modal=True)
        window.set_default_size(700, 650)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        window.set_child(box)
        state = Gtk.Label(label='ClamAVの対応項目を確認しています…', xalign=0, wrap=True, selectable=True)
        box.append(state)
        box.append(Gtk.Label(label='全プランの次回以降の手動・定期検査に共通適用します。\n'
            '形式の解析を無効にしても、その拡張子のファイル全体を除外する意味ではありません。',
            xalign=0, wrap=True))
        pages = Gtk.Notebook(vexpand=True)
        box.append(pages)
        read_automation = self.automation_preferences(pages)
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
            limit_page.append(Gtk.Label(label=explanation + (' アプリ標準は256 MiBです。' if key == 'max-filesize' else ' アプリ標準はClamAV標準値です。'), xalign=0, wrap=True))
            row_limits = Gtk.Box(spacing=12)
            standard = Gtk.CheckButton(label='ClamAV標準値を使う', active=limit_values[key] is None)
            standard.update_property([Gtk.AccessibleProperty.LABEL], [label + 'にClamAV標準値を使う'])
            spin = Gtk.SpinButton.new_with_range(1, 2047, 1)
            spin.set_value(limit_values[key] or APP_LIMITS[key] or 100)
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
        app_limits_reset = Gtk.Button(label='サイズ上限をアプリ標準に戻す', sensitive=False, halign=Gtk.Align.START)
        def reset_app_limits(_button):
            for key, (standard, spin) in limit_controls.items():
                standard.set_active(APP_LIMITS[key] is None)
                spin.set_value(APP_LIMITS[key] or 100)
            message.set_text('サイズ上限をアプリ標準に戻しました。「検査設定を保存」で確定します。')
        app_limits_reset.connect('clicked', reset_app_limits)
        limit_page.append(app_limits_reset)
        info = Gtk.Label(label='保存先：' + str(self.store.path),
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
                automation = read_automation()
                validate_automation(automation)
                def apply():
                    try:
                        self.store.set_scan_preferences(values, limits, automation)
                        self.refresh_profiles()
                        message.set_text('保存しました。監視設定は画面または常駐監視へ反映します。')
                    except Exception as error:
                        message.set_text('保存できませんでした：' + str(error))
                previous = self.store.automation_settings()
                if automation['retention_enabled'] and any(automation[key] != previous[key] for key in automation if key != 'media_enabled'):
                    confirmation = Gtk.MessageDialog(transient_for=window, modal=True,
                        message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL,
                        text='保存上限に従って過去の履歴を自動整理しますか？',
                        secondary_text=f"通常結果は{automation['retention_days']}日、検出・不完了は{automation['alert_days']}日保存します。\n" +
                        f"本文合計{automation['max_mib']}MiBまたは{automation['max_records']}件を超えると、古い通常結果から削除します。\n" +
                        '上限を超える場合は検出・不完了も削除対象です。削除は元に戻せません。')
                    def confirmed(dialog, answer):
                        dialog.destroy()
                        if answer == Gtk.ResponseType.OK:
                            apply()
                    confirmation.connect('response', confirmed)
                    confirmation.present()
                else:
                    apply()
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
                app_limits_reset.set_sensitive('max-filesize' in supported)
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
        start_worker(check_support, lambda error: state.set_text('対応項目の確認を開始できませんでした：' + str(error)))
