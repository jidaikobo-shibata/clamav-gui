"""Run with /usr/bin/python3 -m clam_gui."""
from .notifications import AutomaticNotices
from dataclasses import replace
import threading
import shutil
import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk, Pango
from .core import Store, scan, record_scan_failure
from .jobs import execute_scan
from .execution import ScanAlreadyQueued
from .schedule import Schedule, read_schedule, read_schedules
from .automation import Automation, MEDIA_ID, media_plan
from .background import Lease, enabled as background_enabled, heartbeat, request_media, watched_plans
from .platforms import get_backend
from .runtime import activities, request_stop

from .ui.plans import PlanEditor
from .ui.preferences import PreferencesWindow
from .ui.packages import PackageWindow
from .ui.history import HistoryView


class Application(PlanEditor, PreferencesWindow, PackageWindow, HistoryView, Gtk.Application):
    def __init__(self):
        super().__init__(application_id='com.jidaikobo.ClamAVGui')
        self.store = Store()
        self.backend = get_backend()
        self.busy = False
        self.cancel = threading.Event()
        self.scan_key = None
        self.manual_jobs = {}
        self.schedule_busy = False
        self.active_runs = []
        self.auto_generation = 0
        self.auto_status = None
        self.auto_checking = False
        self.auto_checked_at = None
        self.install_prompt_checked = False
        self.install_prompt = None

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
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        window.set_child(layout)
        self.main_panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        layout.append(self.main_panes)
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
        environment.append('環境設定…(_P)', 'app.preferences')
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
        self.log_scroll = scroll
        right.append(scroll)
        body.set_end_child(right)
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_start=12, margin_end=12, margin_bottom=12)
        layout.append(footer)
        self.notice = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False)
        footer.append(self.notice)
        definitions = Gtk.Box(spacing=12, halign=Gtk.Align.END)
        self.definition_label = Gtk.Label(xalign=1, wrap=True)
        self.definition_label.set_tooltip_text('端末にある定義ファイルのうち、最後に書き換わった日時です。更新の確認だけを行った時刻とは異なります。')
        definitions.append(self.definition_label)
        self.definition_button = Gtk.Button(label='自動更新：確認中…', tooltip_text='定義ファイルの自動更新を設定')
        self.definition_button.connect('clicked', lambda _: self.package_setup(update_only=True))
        definitions.append(self.definition_button)
        footer.append(definitions)
        self.refreshing_profiles = False
        self.action_key = None
        self.log_entries = []
        self.automatic_notices = AutomaticNotices()
        self.automatic_notice_timers = {}
        self.monitor_lease = Lease(self.store)
        self.automation = Automation(self.store, GLib.idle_add, self.automatic_finished, lambda: self.history(None))
        self.observers = self.backend.observers(self.automation)
        self.refresh_profiles()
        self.automation_timer = GLib.timeout_add(500, self.tick_automation)
        self.state_timer = GLib.timeout_add_seconds(1, self.poll_plans)
        window.present()
        self.install_prompt_timer = GLib.timeout_add(250, self.offer_install)

    def offer_install(self):
        if self.install_prompt_checked:
            return False
        if not self.window.get_visible():
            self.install_prompt_timer = None
            return False
        if not self.window.get_mapped():
            return True
        self.install_prompt_timer = None
        self.install_prompt_checked = True
        if shutil.which('clamscan'):
            return False
        dialog = Gtk.MessageDialog(application=self, transient_for=self.window, modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.NONE,
            text='ClamAVが見つかりません',
            secondary_text='検査を行うには、ClamAV本体のインストールが必要です。\n'
                '次の画面で変更内容を確認し、必要なOSの管理者認証を経てインストールできます。\n'
                '「あとで」を選んでも、保存済みのログや設定は確認できます。')
        dialog.add_button('あとで', Gtk.ResponseType.CANCEL)
        install = dialog.add_button('インストールへ進む', Gtk.ResponseType.ACCEPT)
        install.add_css_class('suggested-action')
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        self.install_prompt = dialog
        def response(window, answer):
            window.destroy()
            self.install_prompt = None
            if answer == Gtk.ResponseType.ACCEPT:
                self.package_setup()
        dialog.connect('response', response)
        dialog.present()
        return False

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
        self.plan_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.plan_list.add_css_class('navigation-sidebar')
        self.plan_list.update_property([Gtk.AccessibleProperty.LABEL], ['ログを見る検査プラン'])
        self.plan_list.connect('row-selected', self.profile_changed)
        self.plan_list.set_placeholder(Gtk.Label(label='検査プランはありません。＋から追加できます。',
                                                    wrap=True, margin_top=24, margin_bottom=24))
        scroll.set_child(self.plan_list)
        self.plan_scroll = scroll
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
            switch_label = Gtk.Label(label='定期検査' if plan.kind == 'scheduled' else '監視', xalign=0, hexpand=True)
            switch_box.append(switch_label)
            switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            switch.update_property([Gtk.AccessibleProperty.LABEL], [plan.label + 'の自動実行'])
            switch.set_tooltip_text('今後の自動実行を切り替えます。待機中の定期検査は取り消し、実行中の検査は続けます。')
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
                if plan.kind == 'media' and name in ('duplicate', 'delete'):
                    continue
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
        background = background_enabled(self.store)
        remote = heartbeat(self.store) if background else {}
        if background:
            revision = (remote.get('generation'), remote.get('revision'))
            if revision != getattr(self, 'monitor_revision', None):
                self.monitor_revision = revision
                self.history(None)
            token = getattr(self, 'media_request_token', None)
            if token:
                from .background import preference
                reply = preference(self.store, 'background-command-result', {})
                if reply.get('token') == token:
                    count = reply.get('count', 0)
                    self.show_notice(f'接続済みの外部メディア{count}件を検査待ちに追加しました。' if count else '検査できる外部メディアが接続されていません。')
                    self.media_request_token = None
        self.refresh_auto_status()
        self.definition_label.set_text(self.backend.definition_label())
        self.definition_button.set_sensitive(not self.busy and not self.schedule_busy)
        self.new_action.set_enabled(not self.busy and not self.schedule_busy)
        try:
            previous = [(a.token, a.stopping, a.known) for a in self.active_runs]
            self.active_runs = activities(self.store)
            failed = False
        except Exception:
            failed = True
            previous = None
        try:
            schedules = read_schedules(self.store)
            media_enabled = self.store.automation_settings()['media_enabled']
        except Exception:
            schedules, media_enabled = None, False
        for key, widgets in self.plan_rows.items():
            try:
                if schedules is None:
                    raise ValueError('予定を確認できません')
                schedule = schedules.get(key, Schedule())
                if widgets['plan'].kind == 'media':
                    schedule = replace(schedule, enabled=media_enabled)
                description = replace(schedule, enabled=True).describe().removeprefix('定期検査：')
                description = description.replace('（端末の現地時刻）', '').strip()
                if widgets['plan'].kind != 'scheduled':
                    description = ('外部メディア接続時' if key == MEDIA_ID else '追加・変更時') + ('（ログイン中）' if background else '（画面を開いている間）')
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
            plan_runs = [a for a in self.active_runs if a.profile_id == key]
            running = next((a for a in plan_runs if a.phase == 'running'), next(iter(plan_runs), None))
            unknown = failed or any(not a.known for a in plan_runs)
            automatic = widgets['plan'].kind != 'scheduled' and schedule is not None and schedule.enabled
            widgets['status_box'].set_visible(bool(running) or unknown or automatic)
            widgets['status'].set_text('実行状態を確認できません' if unknown else running.describe() if running else self.monitor_status(key, background, remote) if automatic else '')
            waiting_count = sum(a.phase == 'waiting' for a in plan_runs if a is not running)
            if waiting_count and not unknown:
                widgets['status'].set_text(widgets['status'].get_text() + f'\nほかに検査待ち：{waiting_count}件')
            widgets['spinner'].set_spinning(bool(running and running.known and not running.stopping and running.phase == 'running'))
            widgets['spinner'].set_visible(bool(running and running.known and running.phase == 'running'))
            blocked = self.schedule_busy or (self.busy and self.scan_key is None)
            widgets['switch'].set_sensitive(not blocked and schedule is not None and not widgets['plan'].requires_admin)
            for name in ('edit', 'duplicate', 'delete'):
                if name in widgets['actions']:
                    widgets['actions'][name].set_enabled(not self.busy and not self.schedule_busy and not running and not unknown)
            widgets['actions']['run'].set_enabled((not self.busy or bool(self.manual_jobs)) and not self.schedule_busy and not running and key not in self.manual_jobs and
                                                   not failed and not widgets['plan'].requires_admin)
            widgets['actions']['stop'].set_enabled(bool(running and running.known and not running.stopping) or key in self.manual_jobs)
        if previous is not None and previous != [(a.token, a.stopping, a.known) for a in self.active_runs]:
            self.history(None)
        return True

    def plan_action(self, key, name):
        self.action_key = key
        if name == 'stop':
            self.stop_plan(key)
            return
        if self.schedule_busy or (self.busy and (name != 'run' or not self.manual_jobs)):
            return
        self.poll_plans()
        widgets = self.plan_rows.get(key)
        if not widgets or name not in widgets['actions'] or not widgets['actions'][name].get_enabled():
            return
        if key == MEDIA_ID:
            if name == 'edit':
                self.preferences()
            elif name == 'run':
                if background_enabled(self.store):
                    self.media_request_token = request_media(self.store)
                    self.show_notice('常駐監視へ接続済みメディアの検査を依頼しました。履歴で結果を確認してください。')
                    return
                count = self.observers.scan_connected()
                self.show_notice(f'接続済みの外部メディア{count}件を検査待ちに追加しました。' if count else '検査できる外部メディアが接続されていません。')
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
        if key == MEDIA_ID:
            values = self.store.automation_settings()
            values['media_enabled'] = enabled
            self.store.set_automation_settings(values)
            self.refresh_profiles()
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
                    self.backend.scheduler(self.store).apply(key, schedule)
                except Exception as failure:
                    error = '予定を変更できませんでした：' + str(failure)
                def finished():
                    self.schedule_busy = False
                    self.configure_automation()
                    self.show_notice(error or ('自動実行を有効にしました。' if enabled else
                                                '自動実行を停止しました。実行中の検査は続行します。'))
                    self.poll_plans()
                    return False
                GLib.idle_add(finished)
            threading.Thread(target=worker, daemon=True).start()
        if enabled and not saved.enabled:
            watching = widgets['plan'].kind == 'watch'
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                text='フォルダー監視を有効にしますか？' if watching else '定期検査を有効にしますか？',
                secondary_text=('追加・変更ファイルを検査します。常駐監視が有効なら画面を閉じても動作します。既存ファイルは自動では全体検査しません。'
                                if watching else schedule.describe() + '\n復帰後の検査がすぐ始まる場合があります。') +
                                '\nオフにしても実行中の検査は続行します。')
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
            if key in self.manual_jobs:
                self.manual_jobs[key].set()
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
        self.notice.set_visible(bool(text))

    def configure_automation(self):
        if not hasattr(self, 'automation'):
            return
        if background_enabled(self.store):
            # Yield all observation to the service; a current scan is allowed to finish.
            self.observers.configure([])
            self.automation.configure([], False)
            if not self.automation.active:
                self.monitor_lease.close()
            return
        if not self.monitor_lease.acquire():
            self.observers.configure([])
            self.automation.configure([], False)
            return
        watches = watched_plans(self.store)
        self.automation.configure(watches, self.store.automation_settings()['media_enabled'])
        self.observers.configure(watches)

    def monitor_status(self, key, background=None, remote=None):
        background = background_enabled(self.store) if background is None else background
        if background:
            state = heartbeat(self.store) if remote is None else remote
            if not state:
                return '常駐監視に接続できません。環境設定で確認してください。'
            return state.get('statuses', {}).get(key, '常駐監視へ設定を反映中…').replace('アプリ起動中のみ', 'ログイン中')
        if not self.monitor_lease.file:
            return '別のプロセスが監視しています'
        return self.automation.status(key)

    def tick_automation(self):
        self.configure_automation()
        if not background_enabled(self.store) and self.monitor_lease.file:
            self.automation.tick(blocked=self.schedule_busy or (self.busy and not self.manual_jobs))
        return True


    def automatic_finished(self, key, status, output, label=None):
        self.history(None)
        self.poll_plans()
        plan = media_plan() if key == MEDIA_ID else dict(self.store.profiles()).get(key)
        label = plan.label if plan else label or '検査プラン'
        notice = self.automatic_notices.record(key, label, status, output)
        if notice:
            self.deliver_automatic_notice(key, notice)
        elif key not in self.automatic_notice_timers:
            self.automatic_notice_timers[key] = GLib.timeout_add_seconds(20, self.flush_automatic_notice, key, True)

    def deliver_automatic_notice(self, key, notice):
        notification = Gio.Notification.new(notice.title)
        notification.set_body(notice.body)
        if notice.urgency == 'critical':
            notification.set_priority(Gio.NotificationPriority.HIGH)
        self.send_notification('automatic-' + key, notification)

    def flush_automatic_notice(self, key, from_timer=False):
        timer = self.automatic_notice_timers.pop(key, None)
        if timer and not from_timer:
            GLib.source_remove(timer)
        notice = self.automatic_notices.flush(key)
        if notice:
            self.deliver_automatic_notice(key, notice)
        return False


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
        if self.store.automation_settings()['media_enabled']:
            self.saved_profiles.append((MEDIA_ID, media_plan()))
        self.configure_automation()
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


    def confirm(self, plan, key=None):
        if self.busy and not self.manual_jobs:
            return
        if key in self.manual_jobs:
            self.show_notice('この検査プランはすでに実行中または検査待ちです。')
            return
        self.refresh_profiles(key)
        try:
            plan = self.store.effective_plan(plan)
        except Exception as error:
            self.finished(*record_scan_failure(self.store, plan, error, key), key=key)
            return
        dialog = Gtk.MessageDialog(transient_for=self.operation_parent(), modal=True,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
            text='この範囲を検査しますか？', secondary_text=plan.describe())
        def response(window, answer):
            window.destroy()
            if answer == Gtk.ResponseType.OK and (not self.busy or self.manual_jobs):
                if key in self.manual_jobs:
                    self.show_notice('この検査プランはすでに実行中または検査待ちです。')
                    return
                cancel = threading.Event()
                self.manual_jobs[key] = cancel
                self.busy = True
                self.scan_key = key
                self.set_controls(False)
                self.show_notice('検査を受け付けました：' + plan.label)
                try:
                    threading.Thread(target=self.run_scan, args=(plan, key, cancel), daemon=True).start()
                except Exception as error:
                    self.finished(*record_scan_failure(self.store, plan, error, key), key=key)
        dialog.connect('response', response)
        dialog.present()

    def run_scan(self, target, key, cancel=None):
        try:
            status, output = execute_scan(target, self.store, cancel or self.cancel, scanner=scan,
                                          profile_id=key, wait=True)
        except ScanAlreadyQueued as error:
            GLib.idle_add(self.duplicate_scan, key, str(error))
            return
        except Exception as error:
            status, output = record_scan_failure(self.store, target, error, key)
        GLib.idle_add(self.finished, status, output, key)

    def release_manual_job(self, key):
        self.manual_jobs.pop(key, None)
        self.busy = bool(self.manual_jobs)
        self.scan_key = next(iter(self.manual_jobs), None)
        self.set_controls(not self.busy)

    def duplicate_scan(self, key, message):
        self.release_manual_job(key)
        self.show_notice(message)
        return False

    def finished(self, status, output, key=None):
        self.release_manual_job(key)
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
        if getattr(self, 'automation_timer', None):
            GLib.source_remove(self.automation_timer)
        if getattr(self, 'observers', None):
            self.observers.close()
            self.automation.close()
        if getattr(self, 'monitor_lease', None):
            self.monitor_lease.close()
        for key in list(self.automatic_notices.pending) if hasattr(self, 'automatic_notices') else []:
            self.flush_automatic_notice(key)
        if getattr(self, 'install_prompt_timer', None):
            GLib.source_remove(self.install_prompt_timer)
            self.install_prompt_timer = None
        if getattr(self, 'state_timer', None):
            GLib.source_remove(self.state_timer)
            self.state_timer = None
        Gtk.Application.do_shutdown(self)

    def request_quit(self):
        if not self.on_close(None):
            self.quit()

    def on_close(self, _window):
        if self.busy or self.schedule_busy or (hasattr(self, 'automation') and self.automation.active):
            self.show_notice('処理中です。完了するまで終了できません。')
            return True
        return False


if __name__ == '__main__':
    raise SystemExit(Application().run())
