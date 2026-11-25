"""PreferencesWindow view and its callbacks; OS services are injected through the application backend."""
import gi
gi.require_version('Gtk', '4.0')
import threading
from ..engine import executable
from gi.repository import GLib, Gtk
from ..core import engine_status
from ..jobs import start_worker
from ..options import OPTIONS, supported_options, standard_defaults, option_arguments, LIMITS, APP_LIMITS, limit_arguments
from ..retention import validate as validate_automation
from ..background import enabled as background_enabled, heartbeat


class PreferencesWindow:
    def background_controls(self, content):
        state = Gtk.Label(xalign=0, wrap=True)
        content.append(state)
        content.append(Gtk.Label(label='いずれかを有効にすると、ログイン時から画面を閉じても監視します。ログアウト中は停止します。\n'
            '定期検査のスケジュールとは別の設定です。設定変更は保存後に自動反映します。\n'
            '初回は比較元を作成し、次回起動・スリープ復帰時は追加・変更も確認します。\n'
            '停止中に作成・削除されたファイルや、接続・取り外しされたメディアは後から検査できません。',
            xalign=0, wrap=True))
        def refresh():
            snapshot = heartbeat(self.store)
            state.set_text(getattr(self, 'background_status', None) or
                ('自動検査：稼働中（ログイン中）' if snapshot and background_enabled(self.store) else
                 '自動検査：設定を保存すると起動状態を自動で調整します。'))
        refresh()
        def update():
            if not content.get_root():
                return False
            refresh()
            return True
        GLib.timeout_add_seconds(3, update)

    def automation_preferences(self, pages):
        values = self.store.automation_settings()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(content)
        pages.append_page(scroll, Gtk.Label(label='自動検査・履歴'))
        self.background_controls(content)
        watch = Gtk.CheckButton(label='フォルダー監視を実施する', active=values['watch_enabled'])
        content.append(watch)
        content.append(Gtk.Label(label='各検査プランで有効にしたフォルダーを監視します。オフにしてもプランの設定は保持します。', wrap=True, xalign=0))
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
            return dict(watch_enabled=watch.get_active(), media_enabled=media.get_active(), retention_enabled=retention.get_active(),
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
        self.admin_helper_preferences(pages)
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
        engine_limits = {'max-filesize': 100, 'max-scansize': 400}
        for key, (label, _explanation) in LIMITS.items():
            limit_page.append(Gtk.Label(label=label, xalign=0))
            row_limits = Gtk.Box(spacing=8)
            spin = Gtk.SpinButton.new_with_range(1, 2047, 1)
            spin.set_value(limit_values[key] if limit_values[key] is not None else APP_LIMITS[key])
            spin.set_numeric(True)
            spin.update_property([Gtk.AccessibleProperty.LABEL], [label + '（MiB）'])
            spin.set_sensitive(False)
            reference = Gtk.Label(label=f'（ClamAV標準は{engine_limits[key]} MiB）')
            reference.add_css_class('caption')
            reference.add_css_class('dim-label')
            row_limits.append(spin)
            row_limits.append(Gtk.Label(label='MiB'))
            row_limits.append(reference)
            limit_page.append(row_limits)
            limit_controls[key] = spin
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
            for key, spin in limit_controls.items():
                spin.set_value(engine_limits[key])
            message.set_text('表示中の対応項目をClamAV標準値に戻しました。「検査設定を保存」で確定します。')
        reset.connect('clicked', reset_values)
        row.append(reset)
        save = Gtk.Button(label='検査設定を保存', sensitive=False)
        supported = set()
        def save_preferences(_button):
            try:
                values = {key: control.get_active() for key, control in controls.items()}
                option_arguments(values, supported)
                limits = {key: spin.get_value_as_int() for key, spin in limit_controls.items()}
                limit_arguments(limits, supported)
                automation = read_automation()
                validate_automation(automation)
                def apply():
                    try:
                        self.store.set_scan_preferences(values, limits, automation)
                        self.refresh_profiles()
                        self.reconcile_background(force=True)
                        message.set_text('保存しました。自動検査の起動状態を調整します。')
                    except Exception as error:
                        message.set_text('保存できませんでした：' + str(error))
                previous = self.store.automation_settings()
                if automation['retention_enabled'] and any(automation[key] != previous[key] for key in automation if key not in ('media_enabled', 'watch_enabled')):
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
                binary = executable()
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
                for key, spin in limit_controls.items():
                    spin.set_sensitive(key in supported)
                    if key not in supported:
                        spin.set_tooltip_text('このClamAVでは数値指定に非対応です。')
                save.set_sensitive(True)
                return False
            GLib.idle_add(finish)
        window.present()
        start_worker(check_support, lambda error: state.set_text('対応項目の確認を開始できませんでした：' + str(error)))
