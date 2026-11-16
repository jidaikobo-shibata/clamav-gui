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
app = Application()
def check():
    assert app.menu_model.get_n_items() == 2
    menu = app.menu_model.get_item_link(0, Gio.MENU_LINK_SUBMENU)
    assert menu.get_n_items() == 2
    key = app.store.save_profile(Plan('試験対象', [str(Path.home())], []))
    plan = dict(app.store.profiles())[key]
    app.store.save('2026-11-16T10:00:00+09:00', plan.describe(), '検査完了', 'normal log', key)
    app.store.save('2026-11-16T11:00:00+09:00', plan.describe(), '検出あり', 'sample: Test FOUND', key)
    app.refresh_profiles(key)
    assert len(app.log_entries) == 2
    assert app.history_list.get_row_at_index(0).get_child().get_attributes() is not None
    assert app.history_list.get_row_at_index(1).get_child().get_attributes() is None
    app.history_list.select_row(app.history_list.get_row_at_index(1))
    buffer = app.result.get_buffer()
    assert 'normal log' in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    app.manage_targets()
    assert app.manager.get_modal()
    def descendants(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from descendants(child)
            child = child.get_next_sibling()
    widgets = list(descendants(app.manager))
    action_buttons = [w for w in widgets if isinstance(w, Gtk.Button) and
                      w.get_action_name() in ('app.edit-target', 'app.duplicate-target', 'app.delete-target')]
    assert len(action_buttons) == 3
    assert all(w.get_sensitive() for w in action_buttons)
    assert 'フィッシング検査' not in app.description.get_text()
    assert '定義が3日' not in app.description.get_text()
    assert '削除・隔離' not in app.description.get_text()
    assert len(app.manager_profiles) == 1
    app.manager_list.select_row(app.manager_list.get_row_at_index(0))
    app.duplicate_selected()
    assert app.operation_profile()[1].label.endswith('（複製）')
    before = len(app.store.profiles())
    app.delete_selected()
    dialogs = [w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog)]
    assert len(dialogs) == 1
    dialogs[0].response(Gtk.ResponseType.CANCEL)
    assert len(app.store.profiles()) == before
    action_buttons[0].emit('clicked')
    editors = [w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査対象の設定']
    assert len(editors) == 1
    assert editors[0].get_transient_for() == app.manager
    editor_widgets = list(descendants(editors[0]))
    folder = next(w for w in editor_widgets if isinstance(w, Gtk.Button) and w.get_icon_name() == 'folder-open-symbolic')
    assert isinstance(folder.get_parent().get_first_child(), Gtk.Entry)
    exclusion_buttons = [w for w in editor_widgets if isinstance(w, Gtk.Button) and w.get_label() in
                         ('除外フォルダーを追加', '除外ファイルを追加')]
    assert exclusion_buttons[0].get_parent() == exclusion_buttons[1].get_parent()
    save = next(w for w in editor_widgets if isinstance(w, Gtk.Button) and w.get_label() == '対象設定を保存')
    cancel = next(w for w in editor_widgets if isinstance(w, Gtk.Button) and w.get_label() == 'キャンセル')
    assert save.get_parent() == cancel.get_parent()
    editors[0].destroy()
    app.edit_schedule()
    schedules = [w for w in Gtk.Window.list_toplevels() if w.get_title() == 'スケジュール設定']
    assert len(schedules) == 1
    schedules[0].destroy()
    app.close_manager()
    app.preferences()
    preferences = [w for w in Gtk.Window.list_toplevels() if w.get_title() == '環境設定']
    assert len(preferences) == 1
    widgets = list(descendants(preferences[0]))
    checks = [w for w in widgets if isinstance(w, Gtk.CheckButton)]
    assert len(checks) == 22
    phishing = next(w for w in checks if w.get_label() == 'メールのフィッシング検査')
    assert not phishing.get_active()
    phishing.set_active(True)
    assert not app.store.phishing_enabled()
    def finish_preferences():
        save = next(w for w in widgets if isinstance(w, Gtk.Button) and w.get_label() == '検査設定を保存')
        if not save.get_sensitive():
            return True
        spins = [w for w in widgets if isinstance(w, Gtk.SpinButton)]
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
        app.package_setup(backend)
        setup = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == 'ClamAVの導入・削除')
        setup_widgets = list(descendants(setup))
        remove = next(w for w in setup_widgets if isinstance(w, Gtk.Button) and w.get_label() == 'アンインストール')
        stage = [0]
        def package_check():
            confirmations = [w for w in Gtk.Window.list_toplevels() if w.get_title() == 'ClamAVの変更内容を確認']
            if stage[0] == 0:
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
