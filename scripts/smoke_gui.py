"""Launch the GUI in an isolated Wayland and D-Bus session."""
import os
from pathlib import Path
import subprocess
import tempfile

with tempfile.TemporaryDirectory(prefix='clam-gui-smoke-') as root:
    env = dict(os.environ)
    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS'):
        env.pop(key, None)
    for key, name in [('XDG_RUNTIME_DIR', 'runtime'), ('XDG_CONFIG_HOME', 'config'),
                      ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache')]:
        directory = Path(root) / name
        directory.mkdir(mode=0o700)
        env[key] = str(directory)
    env.update(GDK_BACKEND='wayland', WAYLAND_DISPLAY='clam-test', GTK_A11Y='none', GSK_RENDERER='cairo')
    code = """from clam_gui.__main__ import Application
from gi.repository import GLib, Gtk, Gio
from pathlib import Path
from clam_gui.core import Plan
from clam_gui.mailboxes import MAILBOX_NOTE, mailbox_guidance
from unittest.mock import patch
from clam_gui.definitions import AutoUpdateState
auto_patch = patch('clam_gui.__main__.AutoUpdateService')
auto_service = auto_patch.start()
auto_service.return_value.status.return_value = AutoUpdateState(True, True, True, 24)
from clam_gui.options import DEFAULTS, LIMITS
# Keep the GUI test independent of whether the real ClamAV package is installed.
patch('clam_gui.__main__.shutil.which', return_value='/mock/clamscan').start()
patch('clam_gui.__main__.supported_options', return_value=set(DEFAULTS) | set(LIMITS)).start()
standard = dict(DEFAULTS, **{'phishing-sigs': True, 'phishing-scan-urls': True, 'alert-encrypted': False})
patch('clam_gui.__main__.standard_defaults', return_value=standard).start()
patch('clam_gui.__main__.engine_status', return_value='ClamAV test engine').start()
from clam_gui.gio_watch import Observers
class TestVolumes:
    def get_mounts(self): return []
    def connect(self, *args): return len(args)
    def disconnect(self, *args): pass
patch('clam_gui.__main__.Observers', side_effect=lambda engine: Observers(engine, TestVolumes())).start()
app = Application()
def check():
    assert app.install_prompt_checked and app.install_prompt is None
    with patch('clam_gui.__main__.shutil.which', return_value=None), patch.object(app, 'package_setup') as setup:
        app.install_prompt_checked = False
        with patch.object(app.window, 'get_mapped', return_value=False):
            assert app.offer_install() is True
            assert app.install_prompt is None
            assert not app.install_prompt_checked
        app.offer_install()
        assert app.install_prompt.get_modal()
        assert app.install_prompt.get_transient_for() == app.window
        assert app.install_prompt.get_application() == app
        assert app.window.get_mapped()
        assert app.install_prompt.get_destroy_with_parent()
        setup.assert_not_called()
        app.install_prompt.response(Gtk.ResponseType.CANCEL)
        assert app.install_prompt is None
        app.offer_install()
        assert app.install_prompt is None
        setup.assert_not_called()
        app.install_prompt_checked = False
        app.offer_install()
        app.install_prompt.response(Gtk.ResponseType.ACCEPT)
        setup.assert_called_once_with()

    assert app.menu_model.get_n_items() == 3
    menu = app.menu_model.get_item_link(0, Gio.MENU_LINK_SECTION)
    assert menu.get_n_items() == 1
    actions = []
    for index in range(3):
        section = app.menu_model.get_item_link(index, Gio.MENU_LINK_SECTION)
        for item in range(section.get_n_items()):
            assert section.get_item_link(item, Gio.MENU_LINK_SUBMENU) is None
            actions.append(section.get_item_attribute_value(item, 'action', None).get_string())
    assert actions == ['app.new-target', 'app.preferences',
                       'app.package-setup', 'app.shortcuts', 'app.about']
    assert isinstance(app.get_active_window().get_titlebar(), Gtk.HeaderBar)
    assert app.main_menu.get_icon_name() == 'open-menu-symbolic'
    assert app.main_menu.get_tooltip_text() == 'メインメニュー'
    assert app.get_accels_for_action('app.quit') == ['<Control>q']
    assert app.get_accels_for_action('app.open-menu') == ['F10']
    app.activate_action('open-menu', None)
    app.main_menu.popdown()
    app.activate_action('shortcuts', None)
    assert app.shortcuts_window.get_modal()
    app.shortcuts_window.close()
    assert app.shortcuts_window is None
    app.activate_action('about', None)
    assert app.about_window.get_program_name() == 'ClamAV GUI'
    app.about_window.close()
    assert app.about_window is None
    with patch.object(app, 'quit') as quit_mock, patch.object(app, 'show_notice') as notice:
        app.busy = True
        app.activate_action('quit', None)
        quit_mock.assert_not_called()
        notice.assert_called_once()
        app.busy = False
        app.activate_action('quit', None)
        quit_mock.assert_called_once()
    key = app.store.save_profile(Plan('試験対象', [str(Path.home())], []))
    plan = dict(app.store.profiles())[key]
    app.store.save('2026-11-16T10:00:00+09:00', plan.describe(), '検査完了', 'normal log', key)
    app.store.save('2026-11-16T11:00:00+09:00', plan.describe(), '検出あり' + MAILBOX_NOTE, mailbox_guidance([('/example/INBOX', False)]), key)
    app.refresh_profiles(key)
    assert len(app.log_entries) == 2
    assert app.history_list.get_row_at_index(0).get_child().get_attributes() is not None
    assert app.history_list.get_row_at_index(1).get_child().get_attributes() is None
    app.history_list.select_row(app.history_list.get_row_at_index(0))
    buffer = app.result.get_buffer()
    assert '保存ファイル自体は削除せず' in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    app.history_list.select_row(app.history_list.get_row_at_index(1))
    buffer = app.result.get_buffer()
    assert 'normal log' in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    def descendants(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from descendants(child)
            child = child.get_next_sibling()
    assert not app.window.get_modal()
    assert app.main_panes.get_parent() == app.window.get_child()
    app.show_notice('')
    assert not app.notice.get_visible()
    app.show_notice('メッセージ表示の試験')
    assert app.notice.get_visible()
    assert not any(isinstance(w, Gtk.DropDown) for w in descendants(app.window))
    assert app.plan_list.get_selection_mode() == Gtk.SelectionMode.SINGLE
    assert key in app.plan_rows
    other_plan = Plan('別の検査プラン', [str(app.store.path.parent)], [])
    other_key = app.store.save_profile(other_plan)
    app.store.save('2026-11-17T09:00:00+09:00', other_plan.describe(), '検査完了', 'other plan log', other_key)
    app.refresh_profiles(key)
    app.plan_list.select_row(app.plan_rows[other_key]['row'])
    assert app.selected_profile()[0] == other_key
    assert app.selected_plan_title.get_text() == other_plan.label
    buffer = app.result.get_buffer()
    assert 'other plan log' in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    assert len(app.log_entries) == 1
    app.poll_plans()
    assert app.selected_profile()[0] == other_key
    assert app.plan_rows[key]['more'].activate_action('plan.edit', None)
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    assert app.selected_profile()[0] == other_key
    assert next(w for w in descendants(editor) if isinstance(w, Gtk.Entry)).get_text() == plan.label
    editor.destroy()
    app.plan_list.select_row(app.plan_rows[key]['row'])
    assert len(app.log_entries) == 2
    assert app.add_plan_button.get_action_name() == 'app.new-target'
    # Adding from the manager must not save on cancel.
    count = len(app.store.profiles())
    app.add_plan_button.emit('clicked')
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    assert editor.get_transient_for() == app.window
    editor.destroy()
    assert len(app.store.profiles()) == count
    row = app.plan_rows[key]
    labels = [w.get_text() for w in descendants(app.plan_list) if isinstance(w, Gtk.Label)]
    assert str(Path.home()) not in labels
    assert '停止中' in row['schedule'].get_text()
    assert not row['status_box'].get_visible()
    assert app.plan_rows[key]['more'].activate_action('plan.duplicate', None)
    assert app.selected_profile()[0] == key
    duplicate = app.action_key
    assert app.operation_profile()[1].label.endswith('（複製）')
    before = len(app.store.profiles())
    app.plan_action(duplicate, 'delete')
    dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
    dialog.response(Gtk.ResponseType.CANCEL)
    assert len(app.store.profiles()) == before
    assert app.plan_rows[key]['more'].activate_action('plan.edit', None)
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    fields = list(descendants(editor))
    folder = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_icon_name() == 'folder-open-symbolic')
    assert isinstance(folder.get_parent().get_first_child(), Gtk.Entry)
    save = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == '検査プランを保存')
    cancel = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == 'キャンセル')
    assert save.get_parent() == cancel.get_parent()
    editor.destroy()
    from clam_gui.runtime import track_scan, activities
    from clam_gui.schedule import Schedule, save_schedule, read_schedule
    import datetime, time
    def settle_schedule():
        deadline = time.monotonic() + 3
        while app.schedule_busy and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(.001)
        assert not app.schedule_busy
    save_schedule(app.store, key, Schedule(True, hour=2))
    with track_scan(app.store, key, datetime.datetime.now().astimezone().isoformat()):
        app.poll_plans()
        row = app.plan_rows[key]
        assert '02:00' in row['schedule'].get_text()
        assert row['status_box'].get_visible()
        assert row['spinner'].get_spinning()
        assert not row['actions']['edit'].get_enabled()
        assert row['actions']['stop'].get_enabled()
        # Disabling the schedule never cancels the current scan.
        with patch('clam_gui.__main__.Scheduler') as manager:
            manager.return_value.apply.side_effect = lambda k, schedule: save_schedule(app.store, k, schedule)
            row['switch'].set_active(False)
            settle_schedule()
        assert not read_schedule(app.store, key).enabled
        assert not activities(app.store)[0].stopping
        app.plan_action(key, 'stop')
        assert activities(app.store)[0].stopping
        assert '中止しています' in row['status'].get_text()
    app.poll_plans()
    assert not app.plan_rows[key]['status_box'].get_visible()
    with patch('clam_gui.__main__.activities', side_effect=OSError('mock unavailable')):
        app.poll_plans()
        assert app.plan_rows[key]['status'].get_text() == '実行状態を確認できません'
        assert not app.plan_rows[key]['actions']['run'].get_enabled()
    app.poll_plans()
    # Failed switch changes must show the stored state again.
    with patch('clam_gui.__main__.Scheduler') as manager:
        manager.return_value.apply.side_effect = RuntimeError('mock schedule failure')
        app.plan_rows[key]['switch'].set_active(True)
        dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
        dialog.response(Gtk.ResponseType.OK)
        settle_schedule()
        assert not app.plan_rows[key]['switch'].get_active()
        assert '変更できませんでした' in app.notice.get_text()
    # A manual preflight failure must be visible and stored under the executed plan.
    blocked = Plan('開始失敗の試験', [str(app.store.path.parent)], [])
    blocked_key = app.store.save_profile(blocked)
    reason = '別の検査が実行中です。完了後に再実行してください。'
    with patch('clam_gui.__main__.scan', side_effect=ValueError(reason)), patch.object(app, 'send_notification') as notify:
        app.confirm(blocked, blocked_key)
        dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
        dialog.response(Gtk.ResponseType.OK)
        deadline = time.monotonic() + 3
        while app.busy and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(.001)
        assert not app.busy
        assert reason in app.notice.get_text()
        assert app.selected_profile()[0] == blocked_key
        assert reason in app.log_entries[0].status
        buffer = app.result.get_buffer()
        assert reason in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        assert len(app.store.history_entries(blocked_key, blocked)) == 1
        notify.assert_called_once()
    # The main screen exposes local definition time and an update-only view.
    with patch('clam_gui.__main__.definition_update_label', return_value='定義ファイル更新：2026/11/17 08:00'):
        app.poll_plans()
        assert app.definition_label.get_text() == '定義ファイル更新：2026/11/17 08:00'
        assert app.definition_button.get_sensitive()
        with patch('clam_gui.packages.AptManager') as packages, patch('clam_gui.definitions.DefinitionUpdater.update') as update:
            packages.return_value.status.return_value = {'clamav': 'test', 'clamav-daemon': 'test', 'clamav-freshclam': 'test'}
            app.definition_button.emit('clicked')
            window = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '定義ファイルの更新設定')
            assert window.get_transient_for() == app.window
            widgets = list(descendants(window))
            install = next(w for w in widgets if isinstance(w, Gtk.Button) and w.get_label() == 'インストール')
            assert not install.get_parent().get_visible()
            assert any(isinstance(w, Gtk.Button) and w.get_label() == '今すぐ更新を確認' for w in widgets)
            update.assert_not_called()
            auto_switch = next(w for w in widgets if isinstance(w, Gtk.Switch))
            def wait_auto():
                deadline = time.monotonic() + 3
                while (not auto_switch.get_sensitive() or app.busy) and time.monotonic() < deadline:
                    GLib.MainContext.default().iteration(False)
                    time.sleep(.001)
                assert auto_switch.get_sensitive() and not app.busy
            wait_auto()
            assert auto_switch.get_active()
            assert '自動更新：有効' in app.definition_button.get_label()
            auto_switch.set_active(False)
            dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
            dialog.response(Gtk.ResponseType.CANCEL)
            wait_auto()
            auto_service.return_value.set_enabled.assert_not_called()
            assert auto_switch.get_active()
            def set_auto(value):
                snapshot = AutoUpdateState(value, value, True, 24)
                auto_service.return_value.status.return_value = snapshot
                return 'mock automatic update setting changed', snapshot
            auto_service.return_value.set_enabled.side_effect = set_auto
            auto_switch.set_active(False)
            dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
            dialog.response(Gtk.ResponseType.OK)
            wait_auto()
            assert not auto_switch.get_active()
            assert '自動更新：無効' in app.definition_button.get_label()
            auto_service.return_value.set_enabled.side_effect = RuntimeError('mock authorization rejected')
            auto_switch.set_active(True)
            dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
            dialog.response(Gtk.ResponseType.OK)
            wait_auto()
            assert not auto_switch.get_active()
            assert any(isinstance(w, Gtk.Label) and 'mock authorization rejected' in w.get_text() for w in widgets)
            auto_service.return_value.status.return_value = AutoUpdateState(True, True, True, 24)
            window.close()
    # Real local directory events and synthetic media, with a fake scanner only.
    from clam_gui.automation import MEDIA_ID, Media, media_plan
    watched = Path(app.store.path.parent).parent/'watched-fixture'
    watched.mkdir()
    (watched/'excluded').mkdir()
    watch_plan = Plan('監視試験', [str(watched)], [], excluded_dirs=[str(watched/'excluded')], kind='watch')
    watch_key = app.store.save_profile(watch_plan)
    save_schedule(app.store, watch_key, Schedule(enabled=True))
    calls = []
    def fake_auto_scan(plan, store, cancel, **kwargs):
        assert plan.kind in ('watch', 'media')
        assert str(plan.targets[0]) != '/'
        calls.append(kwargs)
        output = 'Scanned files: 2'
        store.save(datetime.datetime.now().astimezone().isoformat(), plan.describe(), '検査完了', output, kwargs['profile_id'])
        return '検査完了', output
    app.automation.scanner = fake_auto_scan
    app.automation.queue.quiet = .05
    app.refresh_profiles(watch_key)
    def wait_until(predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(.001)
        assert predicate()
    wait_until(lambda: (watch_key, str(watched)) in app.observers.monitors)
    temporary = watched/'browser.crdownload'
    temporary.write_text('synthetic download')
    temporary.rename(watched/'ready.txt')
    (watched/'second.txt').write_text('synthetic second file')
    (watched/'excluded'/'ignored.txt').write_text('not scanned')
    wait_until(lambda: len(calls) >= 1 and not app.automation.active)
    assert len(calls) == 1
    assert set(calls[0]['files']) == {str(watched/'ready.txt'), str(watched/'second.txt')}
    assert len(app.store.history_entries(watch_key, watch_plan)) == 1
    assert app.plan_rows[watch_key]['schedule'].get_text().startswith('追加・変更時')
    app.plan_rows[watch_key]['more'].activate_action('plan.edit', None)
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    widgets = list(descendants(editor))
    type_choice = next(w for w in widgets if isinstance(w, Gtk.DropDown) and w.get_model().get_n_items() == 2)
    assert type_choice.get_selected() == 1
    editor.destroy()
    with patch('clam_gui.__main__.Scheduler') as scheduler:
        scheduler.return_value.apply.side_effect = lambda key, schedule: save_schedule(app.store, key, schedule)
        app.plan_rows[watch_key]['switch'].set_active(False)
        wait_until(lambda: not app.schedule_busy)
    assert not any(ident[0] == watch_key for ident in app.observers.monitors)
    values = app.store.automation_settings()
    values['media_enabled'] = True
    app.store.set_automation_settings(values)
    app.refresh_profiles(MEDIA_ID)
    assert MEDIA_ID in app.plan_rows
    assert 'delete' not in app.plan_rows[MEDIA_ID]['actions']
    assert 'duplicate' not in app.plan_rows[MEDIA_ID]['actions']
    info = watched.stat()
    app.automation.add_media(Media('fake-usb', str(watched), 'Synthetic USB', (info.st_dev, info.st_ino)))
    wait_until(lambda: len(calls) == 2 and not app.automation.active)
    assert calls[1]['profile_id'] == MEDIA_ID
    app.plan_rows[MEDIA_ID]['switch'].set_active(False)
    assert MEDIA_ID not in app.plan_rows
    assert len(app.store.history_entries(MEDIA_ID, media_plan())) == 1
    app.media_history()
    media_window = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '外部メディア検査の履歴')
    media_window.destroy()
    with patch.object(app, 'send_notification') as notifications:
        app.flush_automatic_notice(watch_key)
        app.flush_automatic_notice(MEDIA_ID)
        assert notifications.call_count == 2
    app.automation.scanner = None
    # Exercise editor persistence and recovery without registering actual timers.
    from clam_gui.schedule import save_schedule, read_schedule
    import time
    before = len(app.store.profiles())
    app.edit_profile(plan=Plan('予定つき試験', [str(app.store.path.parent)], []))
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    fields = list(descendants(editor))
    enabled = next(w for w in fields if isinstance(w, Gtk.CheckButton) and w.get_label() == '定期検査を有効にする')
    assert not enabled.get_active()
    enabled.set_active(True)
    frequency = next(w for w in fields if isinstance(w, Gtk.DropDown) and w.get_model().get_n_items() == 5)
    frequency.set_selected(4)
    interval = next(w for w in fields if isinstance(w, Gtk.SpinButton) and w.get_parent().get_first_child().get_text() == '間隔（1〜1440分）')
    assert interval.get_parent().get_visible()
    interval.set_value(7)
    save_editor = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == '検査プランを保存')
    def confirmation(answer):
        dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
        dialog.response(answer)
    save_editor.emit('clicked')
    confirmation(Gtk.ResponseType.CANCEL)
    assert len(app.store.profiles()) == before
    def settle():
        deadline = time.monotonic() + 3
        while app.busy and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(.001)
        assert not app.busy
    with patch('clam_gui.__main__.Scheduler') as manager:
        manager.return_value.apply.side_effect = RuntimeError('mock registration failure')
        save_editor.emit('clicked')
        confirmation(Gtk.ResponseType.OK)
        settle()
        assert len(app.store.profiles()) == before + 1
        assert editor.get_visible()
        assert any(isinstance(w, Gtk.Label) and '検査プランは保存しましたが' in w.get_text() for w in fields)
        new_key = next(k for k, p in app.store.profiles() if p.label == '予定つき試験')
        manager.return_value.apply.side_effect = lambda key, schedule: save_schedule(app.store, key, schedule)
        save_editor.emit('clicked')
        confirmation(Gtk.ResponseType.OK)
        settle()
        assert len(app.store.profiles()) == before + 1
        assert not editor.get_visible()
        assert read_schedule(app.store, new_key).interval_minutes == 7
        manager.return_value.apply.reset_mock()
        app.edit_profile(new_key, dict(app.store.profiles())[new_key])
        editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
        fields = list(descendants(editor))
        assert next(w for w in fields if isinstance(w, Gtk.CheckButton)).get_active()
        next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == '検査プランを保存').emit('clicked')
        manager.return_value.apply.assert_not_called()
    app.preferences()
    preferences = [w for w in Gtk.Window.list_toplevels() if w.get_title() == '環境設定']
    assert len(preferences) == 1
    widgets = list(descendants(preferences[0]))
    checks = [w for w in widgets if isinstance(w, Gtk.CheckButton)]
    assert len(checks) == 24
    phishing = next(w for w in checks if w.get_label() == 'メールのフィッシング検査')
    assert not phishing.get_active()
    phishing.set_active(True)
    assert not app.store.phishing_enabled()
    def finish_preferences():
        save = next(w for w in widgets if isinstance(w, Gtk.Button) and w.get_label() == '検査設定を保存')
        if not save.get_sensitive():
            return True
        from clam_gui.background import put_preference, enabled as background_enabled
        background = next(w for w in widgets if isinstance(w, Gtk.Button) and w.get_label() == '常駐監視を有効にする…')
        with patch('clam_gui.__main__.BackgroundService.apply') as apply:
            background.emit('clicked')
            dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
            assert 'ユーザーサービス' in dialog.get_property('secondary-text')
            dialog.response(Gtk.ResponseType.CANCEL)
            apply.assert_not_called()
            assert not background_enabled(app.store)
            def apply_mock(enable):
                put_preference(app.store, 'background-enabled', enable)
            apply.side_effect = apply_mock
            background.emit('clicked')
            dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
            dialog.response(Gtk.ResponseType.OK)
            wait_until(lambda: not app.schedule_busy)
            apply.assert_called_once_with(True)
            assert background_enabled(app.store)
            app.configure_automation()
            assert not app.monitor_lease.file and not app.observers.monitors
            assert '接続できません' in app.monitor_status('watch')
            put_preference(app.store, 'background-state', dict(updated=time.time(), statuses={'watch':'service watching'}))
            assert app.monitor_status('watch') == 'service watching'
            assert not app.on_close(None)  # Service jobs do not keep the GUI open.
            from clam_gui.automation import MEDIA_ID
            settings = app.store.automation_settings();settings['media_enabled'] = True
            app.store.set_automation_settings(settings);app.refresh_profiles()
            with patch.object(app.observers,'scan_connected') as local_media:
                app.plan_action(MEDIA_ID,'run')
                local_media.assert_not_called()
            put_preference(app.store, 'background-enabled', False)
            app.configure_automation()
            assert app.monitor_lease.file
            settings['media_enabled'] = False;app.store.set_automation_settings(settings);app.refresh_profiles()
        spins = [w for w in widgets if isinstance(w, Gtk.SpinButton) and isinstance(w.get_parent().get_first_child(), Gtk.CheckButton)]
        limit_checks = [w for w in checks if w.get_label() == 'ClamAV標準値を使う']
        assert len(spins) == 2 and len(limit_checks) == 2
        for check in limit_checks:
            check.set_active(False)
        spins[0].set_value(256)
        spins[1].set_value(512)
        save.emit('clicked')
        assert app.store.scan_limits() == {'max-filesize': 256, 'max-scansize': 512}
        assert app.store.scan_options()['phishing-sigs']
        assert not app.store.scan_options()['phishing-scan-urls']
        reset = next(w for w in widgets if isinstance(w, Gtk.Button) and w.get_label() == 'ClamAV標準に戻す')
        reset.emit('clicked')
        assert phishing.get_active()
        assert all(check.get_active() for check in limit_checks)
        assert app.store.scan_limits()['max-filesize'] == 256
        assert app.store.scan_options()['phishing-sigs']
        preferences[0].destroy()
        from clam_gui.packages import PackagePlan, PACKAGES
        class FakePackages:
            installed = True
            executed = 0
            def status(self):
                return {name: 'test' for name in PACKAGES} if self.installed else {}
            def preview(self, action):
                return PackagePlan(action, ('Remv clamav [test]',), 'Remv clamav [test]')
            def execute(self, plan):
                self.executed += 1
                self.installed = False
                return 'simulated removal only', 0
        backend = FakePackages()
        class FakeUpdater:
            executed = 0
            def update(self, cancel, progress):
                self.executed += 1
                progress('simulated definition download')
                return '定義ファイルの取得・更新を確認しました。', 'simulated definition update only'
        updater = FakeUpdater()
        app.package_setup(backend, updater)
        setup = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == 'ClamAVの管理')
        setup_widgets = list(descendants(setup))
        remove = next(w for w in setup_widgets if isinstance(w, Gtk.Button) and w.get_label() == 'アンインストール')
        update = next(w for w in setup_widgets if isinstance(w, Gtk.Button) and w.get_label() == '今すぐ更新を確認')
        stage = [-3]
        def package_check():
            confirmations = [w for w in Gtk.Window.list_toplevels() if w.get_title() == 'ClamAVの変更内容を確認']
            if stage[0] == -3:
                if not update.get_sensitive():
                    return True
                update.emit('clicked')
                dialogs = [w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog)]
                assert len(dialogs) == 1
                dialogs[0].response(Gtk.ResponseType.OK)
                stage[0] = -2
            elif stage[0] == -2:
                if app.busy:
                    return True
                assert updater.executed == 1
                stage[0] = 0
            elif stage[0] == 0:
                if not remove.get_sensitive():
                    return True
                remove.emit('clicked')
                stage[0] = 1
            elif stage[0] == 1 and confirmations:
                dialog = confirmations[0]
                children = list(descendants(dialog))
                run = next(w for w in children if isinstance(w, Gtk.Button) and w.get_label() == '管理者認証して実行')
                assert not run.get_sensitive()
                cancel = next(w for w in children if isinstance(w, Gtk.Button) and w.get_label() == 'キャンセル')
                cancel.emit('clicked')
                assert backend.executed == 0
                assert not app.busy
                remove.emit('clicked')
                stage[0] = 2
            elif stage[0] == 2 and confirmations:
                children = list(descendants(confirmations[0]))
                ack = next(w for w in children if isinstance(w, Gtk.CheckButton))
                ack.set_active(True)
                run = next(w for w in children if isinstance(w, Gtk.Button) and w.get_label() == '管理者認証して実行')
                assert run.get_sensitive()
                run.emit('clicked')
                stage[0] = 3
            elif stage[0] == 3 and not app.busy:
                assert backend.executed == 1
                assert not backend.installed
                setup.close()
                app.quit()
                return False
            return True
        GLib.timeout_add(100, package_check)
        return False
    GLib.timeout_add(100, finish_preferences)
    return False
GLib.timeout_add(1500, check)
raise SystemExit(app.run())
"""
    subprocess.run(['dbus-run-session', '--', 'weston', '--backend=headless',
                    '--renderer=pixman', '--no-config', '--socket=clam-test',
                    '--log=' + str(Path(root) / 'weston.log'), '--',
                    '/usr/bin/python3', '-c', code], env=env, check=True, timeout=20)
    print('GUI startup and history: OK (isolated Wayland)')
