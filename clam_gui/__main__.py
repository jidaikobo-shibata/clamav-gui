"""Run with /usr/bin/python3 -m clam_gui."""
from dataclasses import replace
from pathlib import Path
import threading
import shutil
import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk, Pango
from .core import Store, Plan, engine_status, scan, record_scan_failure
from .options import OPTIONS, supported_options, standard_defaults, option_arguments, LIMITS, limit_arguments
from .schedule import Schedule, Scheduler, read_schedule
from .runtime import activities, request_stop
from .definitions import definition_update_label


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='com.jidaikobo.ClamAVGui')
        self.store = Store()
        self.busy = False
        self.cancel = threading.Event()
        self.scan_key = None
        self.schedule_busy = False
        self.active_runs = []

    def do_activate(self):
        if self.get_active_window():
            self.get_active_window().present()
            return
        window = Gtk.ApplicationWindow(application=self, title='ClamAV GUI — 開発版')
        window.set_default_size(1160, 680)
        self.window = window
        window.connect('close-request', self.on_close)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.main_panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        window.set_child(self.main_panes)
        self.main_panes.set_end_child(box)
        self.main_panes.set_position(350)
        self.main_panes.set_shrink_start_child(False)
        self.main_panes.set_shrink_end_child(False)
        def action(name, callback):
            item = Gio.SimpleAction.new(name, None)
            item.connect('activate', lambda *_: callback())
            self.add_action(item)
            return item
        self.new_action = action('new-target', self.edit_profile)
        action('preferences', self.preferences)
        action('package-setup', self.package_setup)
        action('quit', self.request_quit)
        self.set_accels_for_action('app.quit', ['<Control>q'])
        action('shortcuts', self.show_shortcuts)
        action('about', self.show_about)
        self.set_accels_for_action('app.preferences', ['<Control>comma'])
        self.set_accels_for_action('app.shortcuts', ['<Control>question'])
        self.menu_model = Gio.Menu()
        targets = Gio.Menu()
        targets.append('検査プランを追加…(_N)', 'app.new-target')
        self.menu_model.append_section(None, targets)
        environment = Gio.Menu()
        environment.append('検査設定…(_P)', 'app.preferences')
        environment.append('ClamAVの管理…(_C)', 'app.package-setup')
        self.menu_model.append_section(None, environment)
        information = Gio.Menu()
        information.append('キーボードショートカット(_K)', 'app.shortcuts')
        information.append('ClamAV GUIについて(_A)', 'app.about')
        self.menu_model.append_section(None, information)
        self.header = Gtk.HeaderBar()
        self.main_menu = Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=self.menu_model)
        self.main_menu.set_tooltip_text('メインメニュー')
        self.main_menu.update_property([Gtk.AccessibleProperty.LABEL], ['メインメニュー'])
        self.header.pack_end(self.main_menu)
        window.set_titlebar(self.header)
        self.set_accels_for_action('app.open-menu', ['F10'])
        action('open-menu', self.main_menu.popup)
        self.build_plan_sidebar()
        selector = Gtk.Box(spacing=12)
        self.selected_plan_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.selected_plan_title.add_css_class('heading')
        selector.append(self.selected_plan_title)
        refresh = Gtk.Button(label='ログを更新')
        refresh.connect('clicked', self.history)
        selector.append(refresh)
        box.append(selector)
        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        box.append(body)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=185, margin_end=12)
        left.append(Gtk.Label(label='検査完了日時（検出ありは太字）', xalign=0))
        self.history_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.history_list.update_property([Gtk.AccessibleProperty.LABEL], ['検査完了日時一覧'])
        self.history_list.connect('row-selected', self.log_selected)
        self.history_list.set_placeholder(Gtk.Label(label='検査記録はありません。', wrap=True))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.history_list)
        left.append(scroll)
        body.set_start_child(left)
        body.set_position(215)
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
        definitions = Gtk.Box(spacing=12)
        self.definition_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.definition_label.set_tooltip_text('端末にある定義ファイルのうち、最後に書き換わった日時です。更新の確認だけを行った時刻とは異なります。')
        definitions.append(self.definition_label)
        self.definition_button = Gtk.Button(label='定義ファイルを更新…')
        self.definition_button.connect('clicked', lambda _: self.package_setup(update_only=True))
        definitions.append(self.definition_button)
        box.append(definitions)
        self.refreshing_profiles = False
        self.action_key = None
        self.log_entries = []
        self.refresh_profiles()
        self.state_timer = GLib.timeout_add_seconds(1, self.poll_plans)
        window.present()

    def build_plan_sidebar(self):
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          width_request=300, margin_top=12, margin_bottom=12,
                          margin_start=8, margin_end=8)
        heading = Gtk.Box(spacing=8, margin_start=8, margin_end=8)
        title = Gtk.Label(label='検査プラン', xalign=0, hexpand=True)
        title.add_css_class('heading')
        heading.append(title)
        self.add_plan_button = Gtk.Button(icon_name='list-add-symbolic', action_name='app.new-target',
                                         tooltip_text='検査プランを追加')
        self.add_plan_button.update_property([Gtk.AccessibleProperty.LABEL], ['検査プランを追加'])
        self.add_plan_button.add_css_class('flat')
        heading.append(self.add_plan_button)
        sidebar.append(heading)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.plan_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, valign=Gtk.Align.START)
        self.plan_list.add_css_class('navigation-sidebar')
        self.plan_list.update_property([Gtk.AccessibleProperty.LABEL], ['ログを見る検査プラン'])
        self.plan_list.connect('row-selected', self.profile_changed)
        self.plan_list.set_placeholder(Gtk.Label(label='検査プランはありません。＋から追加できます。',
                                                    wrap=True, margin_top=24, margin_bottom=24))
        scroll.set_child(self.plan_list)
        sidebar.append(scroll)
        self.main_panes.set_start_child(sidebar)

    def refresh_plan_list(self, selected_key=None):
        self.list_profiles = self.saved_profiles
        while self.plan_list.get_first_child():
            self.plan_list.remove(self.plan_list.get_first_child())
        self.plan_rows = {}
        for key, plan in self.list_profiles:
            row = Gtk.ListBoxRow()
            row.plan_key = key
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                              margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
            row.set_child(content)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
            title = Gtk.Label(label=plan.label, xalign=0, wrap=True,
                              wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=24)
            title.add_css_class('heading')
            text.append(title)
            schedule = Gtk.Label(xalign=0, wrap=True)
            schedule.add_css_class('dim-label')
            text.append(schedule)
            status_box = Gtk.Box(spacing=6)
            spinner = Gtk.Spinner()
            status = Gtk.Label(xalign=0, wrap=True)
            status_box.append(spinner)
            status_box.append(status)
            text.append(status_box)
            content.append(text)
            switch_box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
            switch_label = Gtk.Label(label='定期検査', xalign=0, hexpand=True)
            switch_box.append(switch_label)
            switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            switch.update_property([Gtk.AccessibleProperty.LABEL], [plan.label + 'の定期検査'])
            switch.set_tooltip_text('次回以降の定期検査を切り替えます。実行中の検査は止めません。')
            switch_box.append(switch)
            content.append(switch_box)
            group = Gio.SimpleActionGroup()
            actions = {}
            menu = Gio.Menu()
            run_menu = Gio.Menu()
            edit_menu = Gio.Menu()
            for name, label, section in [('run', '今すぐ検査…(_R)', run_menu),
                                         ('stop', '検査を中止(_S)', run_menu),
                                         ('edit', '編集…(_E)', edit_menu),
                                         ('duplicate', '複製(_D)', edit_menu),
                                         ('delete', '削除(_X)', edit_menu)]:
                action = Gio.SimpleAction.new(name, None)
                action.connect('activate', lambda *_, k=key, n=name: self.plan_action(k, n))
                group.add_action(action)
                actions[name] = action
                item = Gio.MenuItem.new(label, 'plan.' + name)
                if name == 'stop':
                    item.set_attribute_value('hidden-when', GLib.Variant('s', 'action-disabled'))
                section.append_item(item)
            menu.append_section(None, run_menu)
            menu.append_section(None, edit_menu)
            more = Gtk.MenuButton(icon_name='view-more-symbolic', menu_model=menu, valign=Gtk.Align.CENTER,
                                  tooltip_text=plan.label + 'の操作')
            more.update_property([Gtk.AccessibleProperty.LABEL], [plan.label + 'の操作'])
            more.insert_action_group('plan', group)
            switch_box.append(more)
            self.plan_rows[key] = dict(plan=plan, switch=switch, schedule=schedule, status=status,
                                       status_box=status_box, spinner=spinner, actions=actions, more=more,
                                       updating=False, row=row)
            switch.connect('state-set', lambda _, value, k=key: self.toggle_schedule(k, value))
            self.plan_list.append(row)
        selected_row = next((self.plan_list.get_row_at_index(i) for i, (key, _) in enumerate(self.list_profiles)
                             if key == selected_key), self.plan_list.get_row_at_index(0))
        self.plan_list.select_row(selected_row)
        self.poll_plans()

    def poll_plans(self):
        self.definition_label.set_text(definition_update_label())
        self.definition_button.set_sensitive(not self.busy and not self.schedule_busy)
        self.new_action.set_enabled(not self.busy and not self.schedule_busy)
        try:
            previous = [(a.token, a.stopping, a.known) for a in self.active_runs]
            self.active_runs = activities(self.store)
            failed = False
        except Exception:
            failed = True
            previous = None
        for key, widgets in self.plan_rows.items():
            try:
                schedule = read_schedule(self.store, key)
                description = replace(schedule, enabled=True).describe().removeprefix('定期検査：')
                description = description.replace('（端末の現地時刻）', '').strip()
                if not schedule.enabled:
                    description += '・停止中'
                widgets['schedule'].set_text(description)
                widgets['updating'] = True
                widgets['switch'].set_active(schedule.enabled)
                widgets['switch'].set_state(schedule.enabled)
                widgets['updating'] = False
            except Exception:
                widgets['schedule'].set_text('予定を確認できません')
                schedule = None
            running = next((a for a in self.active_runs if a.profile_id == key), None)
            unknown = failed or bool(running and not running.known)
            widgets['status_box'].set_visible(bool(running) or unknown)
            widgets['status'].set_text('実行状態を確認できません' if unknown else running.describe() if running else '')
            widgets['spinner'].set_spinning(bool(running and running.known and not running.stopping))
            widgets['spinner'].set_visible(bool(running and running.known))
            blocked = self.schedule_busy or (self.busy and self.scan_key is None)
            widgets['switch'].set_sensitive(not blocked and schedule is not None and not widgets['plan'].requires_admin)
            for name in ('edit', 'duplicate', 'delete'):
                widgets['actions'][name].set_enabled(not self.busy and not self.schedule_busy and not running and not unknown)
            widgets['actions']['run'].set_enabled(not self.busy and not self.schedule_busy and not self.active_runs and
                                                   not failed and not widgets['plan'].requires_admin)
            widgets['actions']['stop'].set_enabled(bool(running and running.known and not running.stopping))
        if previous is not None and previous != [(a.token, a.stopping, a.known) for a in self.active_runs]:
            self.history(None)
        return True

    def plan_action(self, key, name):
        self.action_key = key
        if name == 'stop':
            self.stop_plan(key)
            return
        if self.schedule_busy or self.busy:
            return
        self.poll_plans()
        widgets = self.plan_rows.get(key)
        if not widgets or not widgets['actions'][name].get_enabled():
            return
        {'edit': self.edit_selected, 'duplicate': self.duplicate_selected,
         'delete': self.delete_selected, 'run': lambda: self.scan_selected(None)}[name]()

    def toggle_schedule(self, key, enabled):
        widgets = self.plan_rows.get(key)
        if not widgets or widgets['updating']:
            return True
        if self.schedule_busy or (self.busy and self.scan_key is None):
            self.poll_plans()
            return True
        try:
            saved = read_schedule(self.store, key)
            schedule = replace(saved, enabled=enabled)
            schedule.calendar()
        except Exception as error:
            self.show_notice('予定を変更できませんでした：' + str(error))
            self.poll_plans()
            return True
        def apply():
            self.schedule_busy = True
            self.poll_plans()
            def worker():
                error = None
                try:
                    Scheduler(self.store).apply(key, schedule)
                except Exception as failure:
                    error = '予定を変更できませんでした：' + str(failure)
                def finished():
                    self.schedule_busy = False
                    self.show_notice(error or ('定期検査を有効にしました。' if enabled else
                                                '定期検査を停止しました。実行中の検査は続行します。'))
                    self.poll_plans()
                    return False
                GLib.idle_add(finished)
            threading.Thread(target=worker, daemon=True).start()
        if enabled and not saved.enabled:
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                text='定期検査を有効にしますか？', secondary_text=schedule.describe() +
                '\n復帰後の検査がすぐ始まる場合があります。実行中の検査を中止する操作ではありません。')
            def response(window, answer):
                window.destroy()
                if answer == Gtk.ResponseType.OK:
                    apply()
                else:
                    self.poll_plans()
            dialog.connect('response', response)
            dialog.present()
        elif enabled != saved.enabled:
            apply()
        return True

    def stop_plan(self, key):
        try:
            for activity in activities(self.store):
                if activity.profile_id == key and activity.known:
                    request_stop(self.store, activity.token)
            if self.scan_key == key:
                self.cancel.set()
            self.poll_plans()
        except Exception as error:
            self.show_notice('中止を依頼できませんでした：' + str(error))

    def operation_profile(self):
        if self.action_key:
            return next((item for item in self.store.profiles() if item[0] == self.action_key), None)
        return None

    def operation_parent(self):
        return self.window

    def show_notice(self, text):
        self.notice.set_text(text)

    def package_setup(self, manager=None, updater=None, update_only=False):
        from .packages import AptManager, PACKAGES
        from .notifications import definition_status
        manager = manager or AptManager()
        from .definitions import DefinitionUpdater
        updater = updater or DefinitionUpdater()
        window = Gtk.Window(application=self, title='定義ファイルの更新' if update_only else 'ClamAVの管理',
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
                       '削除するとThunderbird連携などの検査も使えなくなります。')
        box.append(Gtk.Label(label=explanation, xalign=0, wrap=True))
        controls = Gtk.Box(spacing=8, homogeneous=True)
        install = Gtk.Button(label='インストール')
        remove = Gtk.Button(label='アンインストール')
        remove.add_css_class('destructive-action')
        controls.append(install)
        controls.append(remove)
        box.append(controls)
        controls.set_visible(not update_only)
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
                    text = (definition_update_label() + '\n' + definition_status() if update_only else
                            '\n'.join(lines) + '\n\n' + definition_status())
                    if not update_only and len(installed) == len(PACKAGES):
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
                        if update_only and 'clamav-freshclam' not in installed:
                            message.set_text('FreshClamが未導入です。メインメニューの「ClamAVの管理」からインストールしてください。')
                    else:
                        message.set_text('この環境では定義ファイルの更新を開始できません。' if update_only else
                                         'この環境では導入・削除を実行できません。')
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
        box.append(Gtk.Label(label='全プランの次回以降の手動・定期検査に共通適用します。\n'
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
        row = self.plan_list.get_selected_row()
        if row is None:
            return None
        return next((item for item in self.saved_profiles if item[0] == row.plan_key), None)

    def refresh_profiles(self, selected_key=None):
        if selected_key is None and hasattr(self, 'saved_profiles'):
            current = self.selected_profile()
            selected_key = current[0] if current else None
        self.saved_profiles = self.store.profiles()
        self.refreshing_profiles = True
        self.refresh_plan_list(selected_key)
        self.refreshing_profiles = False
        self.profile_changed()

    def profile_changed(self, *_args):
        if self.refreshing_profiles:
            return
        selected = self.selected_profile()
        self.selected_plan_title.set_text(selected[1].label if selected else '検査プランを追加してください')
        self.history(None)

    def set_controls(self, enabled):
        self.new_action.set_enabled(enabled)
        self.poll_plans()

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
                    Scheduler(self.store).remove_profile(key)
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
            threading.Thread(target=worker, daemon=True).start()
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
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, margin_top=8, margin_bottom=8))
        heading = Gtk.Label(label='スケジュール', xalign=0)
        heading.add_css_class('heading')
        box.append(heading)
        saved_schedule = read_schedule(self.store, key) if key else Schedule()
        read_fields = self.schedule_fields(box, saved_schedule, plan)
        state = {'running': False}
        editor.connect('close-request', lambda _: state['running'])
        error_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(error_label)
        save = Gtk.Button(label='検査プランを保存')
        save.add_css_class('suggested-action')
        def save_profile(_button):
            nonlocal key
            if state['running'] or self.busy:
                return
            try:
                from .core import validate_profile
                new = Plan(name.get_text().strip(), [root.get_text()],
                           [p for p, directory in exclusions if not directory], plan.phishing,
                           [p for p, directory in exclusions if directory], plan.requires_admin)
                validate_profile(new)
                schedule = read_fields()
                schedule.calendar()
                if schedule.enabled and new.requires_admin:
                    raise ValueError('管理者権限が必要な対象は定期実行できません。')
            except (ValueError, OSError) as error:
                error_label.set_text(str(error))
                return
            def refresh_saved():
                self.action_key = key
                self.refresh_profiles(key if is_new else None)
            changed = schedule != saved_schedule
            if not changed:
                try:
                    key = self.store.save_profile(new, key)
                    refresh_saved()
                    editor.destroy()
                except (ValueError, OSError) as error:
                    error_label.set_text(str(error))
                return
            detail = new.describe() + '\n\n' + schedule.describe()
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
                        Scheduler(self.store).apply(key, schedule)
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
                threading.Thread(target=worker, daemon=True).start()
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
        weekday = field('曜日', Gtk.DropDown.new_from_strings(['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']))
        weekday.set_selected(saved.weekday)
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
            return Schedule(enabled.get_active(), ['daily', 'weekly', 'monthly', 'hourly', 'interval'][frequency.get_selected()],
                            hour.get_value_as_int(), minute.get_value_as_int(),
                            weekday.get_selected(), monthday.get_value_as_int(), interval.get_value_as_int())
        return read

    def confirm(self, plan, key=None):
        if self.busy:
            return
        self.refresh_profiles(key)
        try:
            plan = self.store.effective_plan(plan)
        except Exception as error:
            self.finished(*record_scan_failure(self.store, plan, error, key))
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
                self.scan_key = key
                self.poll_plans()
                self.show_notice('検査中：' + plan.label)
                threading.Thread(target=self.run_scan, args=(plan, key), daemon=True).start()
        dialog.connect('response', response)
        dialog.present()

    def run_scan(self, target, key):
        try:
            status, output = scan(target, self.store, self.cancel, profile_id=key)
        except Exception as error:
            status, output = record_scan_failure(self.store, target, error, key)
        GLib.idle_add(self.finished, status, output)

    def finished(self, status, output):
        self.busy = False
        self.scan_key = None
        self.set_controls(True)
        self.history(None)
        self.history_list.select_row(self.history_list.get_row_at_index(0))
        self.show_notice(status)
        notification = Gio.Notification.new('ClamAV検査終了')
        from .mailboxes import MAILBOX_NOTE
        if MAILBOX_NOTE in status:
            notification.set_title('メール保存ファイル内で脅威を検出しました')
            notification.set_body('複数のメールをまとめた形式です。ファイル自体は削除せず、メールアプリで確認してください。詳細は検査ログをご覧ください。')
        else:
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

    def show_shortcuts(self):
        window = getattr(self, 'shortcuts_window', None)
        if window:
            window.present()
            return
        window = Gtk.Window(application=self, title='キーボードショートカット',
                            transient_for=self.operation_parent(), modal=True)
        self.shortcuts_window = window
        window.set_default_size(440, 280)
        header = Gtk.HeaderBar()
        window.set_titlebar(header)
        grid = Gtk.Grid(column_spacing=32, row_spacing=18, margin_top=24,
                        margin_bottom=24, margin_start=24, margin_end=24)
        for row, (title, accelerator) in enumerate([
                ('アプリケーションを終了', '<Control>q'),
                ('メインメニューを開く', 'F10'),
                ('検査設定を開く', '<Control>comma'),
                ('ショートカットを表示', '<Control>question'),
                ('この一覧を閉じる', 'Escape')]):
            grid.attach(Gtk.Label(label=title, xalign=0, hexpand=True), 0, row, 1, 1)
            grid.attach(Gtk.ShortcutLabel(accelerator=accelerator), 1, row, 1, 1)
        window.set_child(grid)
        controller = Gtk.ShortcutController()
        controller.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string('Escape'),
            Gtk.CallbackAction.new(lambda *_: (window.close(), True)[1])))
        window.add_controller(controller)
        def closed(*_):
            self.shortcuts_window = None
            return False
        window.connect('close-request', closed)
        window.present()

    def show_about(self):
        window = getattr(self, 'about_window', None)
        if window:
            window.present()
            return
        window = Gtk.AboutDialog(application=self, transient_for=self.operation_parent(),
                                 modal=True, program_name='ClamAV GUI', version='開発版',
                                 comments='ClamAVの検査対象・定期実行・検査履歴を管理します。',
                                 website='https://github.com/jidaikobo-shibata/clamav-gui',
                                 website_label='プロジェクトのページ',
                                 license_type=Gtk.License.CUSTOM, license='WTFPL Version 2')
        self.about_window = window
        def closed(*_):
            self.about_window = None
            return False
        window.connect('close-request', closed)
        window.present()

    def do_shutdown(self):
        if getattr(self, 'state_timer', None):
            GLib.source_remove(self.state_timer)
            self.state_timer = None
        Gtk.Application.do_shutdown(self)

    def request_quit(self):
        if not self.on_close(None):
            self.quit()

    def on_close(self, _window):
        if self.busy or self.schedule_busy:
            self.show_notice('処理中です。完了するまで終了できません。')
            return True
        return False


if __name__ == '__main__':
    raise SystemExit(Application().run())
