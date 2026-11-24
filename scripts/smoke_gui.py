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
    env.update(GIO_USE_VFS='local', GTK_USE_PORTAL='0', GDK_BACKEND='wayland', WAYLAND_DISPLAY='clam-test', GTK_A11Y='none', GSK_RENDERER='cairo')
    env['SMOKE_RESULT'] = str(Path(root) / 'success')
    code = """from clam_gui.__main__ import Application
from gi.repository import GLib, Gtk, Gio
from pathlib import Path
from clam_gui.core import Plan
from clam_gui.mailboxes import MAILBOX_NOTE, mailbox_guidance
from unittest.mock import patch
from clam_gui.definitions import AutoUpdateState
patch('clam_gui.background.BackgroundService.reconcile', return_value='自動検査：テスト環境').start()
patch('clam_gui.admin_helper_setup.AdminHelperSetup.status', return_value='missing').start()
auto_patch = patch('clam_gui.definitions.AutoUpdateService')
auto_service = auto_patch.start()
patch('clam_gui.packages.AptManager.engine_update_status', return_value=('ClamAV本体（APT） test：更新なし（APT情報）', 'test')).start()
auto_service.return_value.status.return_value = AutoUpdateState(True, True, True, 24)
from clam_gui.options import DEFAULTS, LIMITS
# Keep the GUI test independent of whether the real ClamAV package is installed.
patch('clam_gui.__main__.shutil.which', return_value='/mock/clamscan').start()
patch('clam_gui.ui.preferences.supported_options', return_value=set(DEFAULTS) | set(LIMITS)).start()
standard = dict(DEFAULTS, **{'phishing-sigs': True, 'phishing-scan-urls': True, 'alert-encrypted': False})
patch('clam_gui.ui.preferences.standard_defaults', return_value=standard).start()
patch('clam_gui.ui.preferences.engine_status', return_value='ClamAV test engine').start()
from clam_gui.gio_watch import Observers
class TestVolumes:
    def get_mounts(self): return []
    def connect(self, *args): return len(args)
    def disconnect(self, *args): pass
patch('clam_gui.gio_watch.Observers', side_effect=lambda engine: Observers(engine, TestVolumes())).start()
app = Application()
def settle_history(controller=None):
    import time
    controller = controller or app.history_controller
    deadline = time.monotonic() + 5
    context = GLib.MainContext.default()
    while controller.reader.busy and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.001)
    assert not controller.reader.busy, 'history read timed out'


def clipboard_text(widget):
    import time
    values = []
    def done(clipboard, result):
        values.append(clipboard.read_text_finish(result))
    widget.get_clipboard().read_text_async(None, done)
    deadline = time.monotonic() + 3
    context = GLib.MainContext.default()
    while not values and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.001)
    assert values, 'clipboard read timed out'
    return values[0]

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
    assert menu.get_n_items() == 2
    actions = []
    for index in range(3):
        section = app.menu_model.get_item_link(index, Gio.MENU_LINK_SECTION)
        for item in range(section.get_n_items()):
            assert section.get_item_link(item, Gio.MENU_LINK_SUBMENU) is None
            actions.append(section.get_item_attribute_value(item, 'action', None).get_string())
    assert actions == ['app.new-target', 'app.order-plans', 'app.preferences',
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
    settle_history()
    assert app.engine_label.get_text() == 'ClamAV test'
    assert '更新なし（APT情報）' in app.engine_label.get_tooltip_text()
    assert app.definition_button.get_icon_name() == 'view-refresh-symbolic'
    assert app.definition_label.has_css_class('caption')
    assert app.engine_label.has_css_class('caption')
    wide_height = app.footer_status.measure(Gtk.Orientation.VERTICAL, 1000)[1]
    narrow_height = app.footer_status.measure(Gtk.Orientation.VERTICAL, 260)[1]
    assert narrow_height > wide_height, (narrow_height, wide_height)
    assert app.engine_refresh.get_sensitive()
    assert len(app.log_entries) == 2
    assert app.history_list.get_row_at_index(0).get_child().get_attributes() is not None
    assert app.history_list.get_row_at_index(1).get_child().get_attributes() is None
    app.history_list.select_row(app.history_list.get_row_at_index(0))
    settle_history()
    buffer = app.result.get_buffer()
    assert '保存ファイル自体は削除せず' in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    app.history_list.select_row(app.history_list.get_row_at_index(1))
    settle_history()
    buffer = app.result.get_buffer()
    assert 'normal log' in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    # Completion must select the new row after asynchronous headers arrive,
    # even if another refresh supersedes the first request.
    app.store.save('2026-11-20T12:00:00+09:00', plan.describe(), '検査完了', 'latest completed log', key)
    app.finished('検査完了', 'latest completed log', key)
    app.history(None)
    settle_history()
    assert app.history_list.get_selected_row().get_index() == 0
    buffer = app.result.get_buffer()
    assert 'latest completed log' in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    latest = app.log_entries[0].id
    # Plain refreshes preserve the user's explicit selection.
    app.history_list.select_row(app.history_list.get_row_at_index(2))
    settle_history()
    app.history(None)
    settle_history()
    assert app.history_list.get_selected_row().get_index() == 2
    with app.store.connect() as db:
        db.execute('DELETE FROM scans WHERE rowid=?', (latest,))
    app.history(None)
    settle_history()
    # A legacy false detection flag must not hide truncated positive summaries.
    from clam_gui.results import ScanStatus, ResultKind
    truncated = '出力が大きいため末尾のみ保存しています。' + chr(10)
    truncated += '/synthetic/large: Heuristics.Limits.Exceeded.MaxFileSize FOUND' + chr(10)
    truncated += ''.join('/synthetic/private-' + str(i) + ': Access denied' + chr(10) for i in range(110))
    truncated += "/synthetic/directory: Can't open directory." + chr(10)
    truncated += 'Infected files: 57' + chr(10)
    app.store.save('2026-11-20T02:00:00+09:00', plan.describe(),
                   ScanStatus('検査不完了', ResultKind.INCOMPLETE), truncated, key)
    app.history(None, latest=True)
    settle_history()
    caution_label = app.history_list.get_row_at_index(0).get_child()
    assert caution_label.get_attributes() is not None
    assert '要確認・内訳不明' in caution_label.get_text()
    assert app.result.report.title == '要確認・内訳不明'
    assert app.result.folder_button.get_label() == '除外設定を編集…'
    assert app.result.folder_button.has_css_class('suggested-action')
    assert app.result.summary_copy.get_parent() == app.result.folder_button.get_parent()
    assert app.result.summary_copy.get_next_sibling() == app.result.folder_button
    assert not app.result.raw_loaded
    assert not app.result.raw_expander.get_expanded()
    reasons = app.result.reason_expanders
    assert len(reasons) == 3
    assert app.result.findings_box.get_spacing() <= 4
    def report_labels(widget):
        if isinstance(widget, Gtk.Label):
            yield widget.get_text()
        child = widget.get_first_child()
        while child:
            yield from report_labels(child)
            child = child.get_next_sibling()
    labels = list(report_labels(app.result))
    assert '検出内容' in labels
    assert app.result.report.coverage not in labels
    assert 'ログの一部が省略されています。明細は保持された範囲のみです。' in labels
    for reason in reasons:
        assert not reason.get_expanded() and reason.get_child() is None
        assert reason.copy_button.get_visible()
        assert reason.copy_button.get_icon_name() == 'edit-copy-symbolic'
        assert reason.copy_button.has_css_class('flat')
    # Copy works while the category remains collapsed.
    reasons[1].copy_button.emit('clicked')
    settle_history(type('ReaderController', (), {'reader': app.result.copy_reader})())
    assert not reasons[1].get_expanded()
    copied = clipboard_text(app.result)
    assert '/synthetic/private-109' in copied and '権限不足：110件' in copied
    assert '/synthetic/directory' not in copied
    assert '全体の内訳ではありません' in copied
    assert 'コピーしました' in app.result.copy_notice.get_text()
    reasons[1].set_expanded(True)
    view = reasons[1].preview_view
    assert isinstance(view, Gtk.TextView) and not view.get_editable()
    assert view.get_cursor_visible() and not view.get_accepts_tab()
    buffer = view.get_buffer()
    text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    assert '権限不足：110件' in text and '/synthetic/private-99' in text
    assert '/synthetic/private-109' not in text and '先頭100件' in text and '/synthetic/private' in text and 'Access denied' in text
    assert view.activate_action('selection.select-all', None)
    assert buffer.get_text(*buffer.get_selection_bounds(), True) == text
    assert view.activate_action('clipboard.copy', None)
    app.result.summary_copy.emit('clicked')
    settle_history(type('ReaderController', (), {'reader': app.result.copy_reader})())
    assert app.result.copy_notice.get_text() == '結果全体をコピーしました。'
    copied = clipboard_text(app.result)
    assert '/synthetic/private-109' in copied and '/synthetic/directory' in copied
    assert not app.result.raw_loaded
    app.result.raw_expander.set_expanded(True)
    assert app.result.raw_loaded
    copy_button = app.result.raw_expander.get_child().get_first_child()
    copy_button.emit('clicked')

    assert caution_label.get_tooltip_text()
    buffer = app.result.get_buffer()
    caution_text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    assert caution_text.startswith('要確認・内訳不明')
    assert '「検出なし」とは判断できません' in caution_text
    with app.store.connect() as db:
        original = db.execute('SELECT detected,status,output FROM scans ORDER BY rowid DESC LIMIT 1').fetchone()
        assert original == (0, '検査不完了', truncated)
        db.execute('DELETE FROM scans WHERE rowid=?', (app.log_entries[0].id,))
    app.history(None)
    settle_history()
    def descendants(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from descendants(child)
            child = child.get_next_sibling()
    # Grow a current plan from an old log, without restoring old settings.
    from dataclasses import replace
    from clam_gui.results import ScanStatus, ResultKind
    fixture = app.store.path.parent/'folder-exclusion-tests'
    fixture.mkdir()
    db_folder, denied_folder, existing_folder, threat_folder = [fixture/name for name in ('database', 'blocked', 'existing', 'threat')]
    for directory in (db_folder, denied_folder, existing_folder, threat_folder):
        directory.mkdir()
    (denied_folder/'normal').touch()
    (threat_folder/'one').touch()
    (threat_folder/'two').touch()
    (existing_folder/'excluded').touch()
    exclusion_plan = Plan('古いプラン名', [str(fixture)], [])
    exclusion_key = app.store.save_profile(exclusion_plan)
    exclusion_output = str(db_folder/'a') + ': Access denied' + chr(10)
    exclusion_output += str(db_folder/'b') + ': Access denied' + chr(10)
    exclusion_output += str(db_folder/'a') + ': Heuristics.Limits.Exceeded.MaxFileSize FOUND' + chr(10)
    exclusion_output += str(denied_folder) + ": Can't open directory." + chr(10)
    exclusion_output += str(existing_folder/'a') + ': Access denied' + chr(10)
    exclusion_output += str(threat_folder/'test') + ': Test FOUND' + chr(10)
    app.store.save('2026-11-21T02:00:00+09:00', exclusion_plan.describe(),
                   ScanStatus('検査不完了', ResultKind.INCOMPLETE, True), exclusion_output, exclusion_key)
    exclusion_current = replace(exclusion_plan, label='現在のプラン名', excluded_dirs=[str(existing_folder)])
    app.store.save_profile(exclusion_current, exclusion_key)
    app.refresh_profiles(exclusion_key)
    settle_history()
    import clam_gui.ui.exclusions as exclusion_ui
    exclusion_ui.PAGE_SIZE = 2
    app.result.folder_button.emit('clicked')
    folders = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == 'フォルダー別の検査結果')
    settle_history(folders)
    assert '現在のプラン名' in folders.info.get_text()
    assert folders.panes.get_orientation() == Gtk.Orientation.HORIZONTAL
    assert folders.panes.get_start_child() == folders.left_pane
    assert folders.panes.get_end_child() == folders.right_pane
    assert folders.path_entry.is_ancestor(folders.left_pane)
    assert folders.candidate_scroll.get_parent() == folders.left_pane
    assert folders.excluded_scroll.get_parent() == folders.right_pane
    assert folders.excluded_scroll.get_max_content_height() == -1
    assert folders.excluded_scroll.get_vexpand() and folders.candidate_scroll.get_vexpand()

    assert not folders.counter.get_visible()
    assert folders.search_count.get_text() == 'ヒット：3件'
    settle_history(type('ReaderController', (), {'reader': folders.count_reader})())
    assert folders.impact_count.get_text() == '除外される直下：ファイル3件・フォルダー0件'
    assert folders.search_count.get_parent() == folders.impact_count.get_parent()
    assert set(folders.reason_names) == {'すべて', '権限不足', 'ディレクトリを開けない', 'サイズ上限', '脅威として報告された対象'}
    folders.reason_filter.set_selected(folders.reason_names.index('権限不足'))
    assert folders.search_count.get_text() == 'ヒット：1件'
    assert list(folders.candidate_buttons) == [str(db_folder)]
    assert 'サイズ上限：1件' in folders.detail_expanders[str(db_folder)].get_label()
    folders.path_entry.set_text('threat')
    assert folders.search_count.get_text() == 'ヒット：0件'
    folders.path_entry.set_text('')
    folders.reason_filter.set_selected(folders.reason_names.index('サイズ上限'))
    assert list(folders.candidate_buttons) == [str(db_folder)]
    folders.reason_filter.set_selected(folders.reason_names.index('ディレクトリを開けない'))
    assert list(folders.candidate_buttons) == [str(denied_folder)]
    settle_history(type('ReaderController', (), {'reader': folders.count_reader})())
    assert folders.impact_count.get_text() == '除外される直下：ファイル1件・フォルダー0件'
    folders.reason_filter.set_selected(0)
    assert folders.search_count.get_text() == 'ヒット：3件'
    assert str(existing_folder) not in folders.candidate_buttons
    assert str(existing_folder) in folders.remove_buttons
    assert folders.next.get_sensitive()
    folders.next.emit('clicked')
    assert str(threat_folder) in folders.candidate_buttons
    folders.path_entry.set_text('threat')
    assert folders.search_count.get_text() == 'ヒット：1件'
    assert list(folders.candidate_buttons) == [str(threat_folder)]
    assert not folders.back.get_sensitive()
    folders.path_entry.set_text('')
    folders.path_entry.set_text('datab')
    assert folders.search_count.get_text() == 'ヒット：1件'
    assert list(folders.candidate_buttons) == [str(db_folder)]
    settle_history(type('ReaderController', (), {'reader': folders.count_reader})())
    assert folders.count_labels[str(db_folder)].get_text() == '直下：ファイル0件・フォルダー0件'
    detail = folders.detail_expanders[str(db_folder)]
    assert '権限不足：2件' in detail.get_label() and 'サイズ上限：1件' in detail.get_label()
    detail.set_expanded(True)
    assert detail.get_child() is not None
    folders.path_entry.set_text('not-found-literal')
    assert folders.search_count.get_text() == 'ヒット：0件'
    assert not folders.candidate_buttons
    folders.path_entry.set_text('datab')
    assert any('<b>datab</b>' in label.get_label() for path, label in folders.path_labels if path == str(db_folder))
    folders.add_button.emit('clicked')
    assert folders.draft_exclusions == [str(existing_folder)]
    folders.path_entry.set_text(str(fixture))
    folders.add_button.emit('clicked')
    assert folders.draft_exclusions == [str(existing_folder)]
    folders.path_entry.set_text(str(threat_folder))
    folders.add_button.emit('clicked')
    assert '脅威' in folders.message.get_text()
    assert folders.path_entry.get_text() == str(threat_folder)
    folders.path_entry.set_text('')
    assert folders.search_count.get_text() == 'ヒット：3件'
    path_copy = folders.candidate_buttons[str(db_folder)].get_next_sibling()
    assert path_copy.get_tooltip_text() == 'フォルダーのパスをコピー'
    path_copy.emit('clicked')
    assert clipboard_text(folders) == str(db_folder)
    folders.candidate_buttons[str(db_folder)].emit('clicked')
    assert folders.path_entry.get_text() == str(db_folder)
    with patch('clam_gui.ui.exclusions.Gtk.FileLauncher.new') as launcher_new:
        folders.open_button.emit('clicked')
        settle_history(type('ReaderController', (), {'reader': folders.open_reader})())
        launcher_new.assert_called_once()
        assert launcher_new.call_args.args[0].get_path() == str(db_folder)
        launcher = launcher_new.return_value
        assert launcher.launch.call_args.args[0] == folders
        launcher.launch_finish.return_value = True
        folders.open_finished(launcher, None)
        folders.path_entry.set_text(str(fixture/'missing'))
        folders.open_button.emit('clicked')
        settle_history(type('ReaderController', (), {'reader': folders.open_reader})())
        assert launcher_new.call_count == 1
        assert '開けませんでした' in folders.message.get_text()
    folders.path_entry.set_text(str(db_folder))
    folders.add_button.emit('clicked')
    assert folders.path_entry.get_text() == ''
    assert folders.search_count.get_text() == 'ヒット：2件'
    assert str(db_folder) not in folders.candidate_buttons
    assert '1フォルダー・3件' in folders.message.get_text()
    assert dict(app.store.profiles())[exclusion_key].excluded_dirs == [str(existing_folder)]
    folders.remove_buttons[str(db_folder)].emit('clicked')
    assert str(db_folder) in folders.candidate_buttons
    folders.path_entry.set_text(str(db_folder))
    folders.add_button.emit('clicked')
    folders.path_entry.set_text(str(denied_folder))
    folders.add_button.emit('clicked')
    folders.remove_buttons[str(existing_folder)].emit('clicked')
    folders.path_entry.set_text('')
    assert str(existing_folder) in folders.candidate_buttons
    assert folders.search_count.get_text() == 'ヒット：2件'
    # Other edits made while this draft is open must survive save.
    concurrent_folder = fixture/'concurrent'
    concurrent_folder.mkdir()
    exclusion_current = replace(exclusion_current, excludes=[str(fixture/'later.txt')], phishing=False,
                                excluded_dirs=[str(existing_folder), str(concurrent_folder)])
    app.store.save_profile(exclusion_current, exclusion_key)
    folders.review.emit('clicked')
    confirmation = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '除外設定の変更を確認')
    labels = [w.get_text() for w in descendants(confirmation) if isinstance(w, Gtk.Label)]
    assert any('配下すべて' in label for label in labels)
    assert any(str(db_folder) in label and str(existing_folder) in label for label in labels)
    next(w for w in descendants(confirmation) if isinstance(w, Gtk.Button) and w.get_label() == 'キャンセル').emit('clicked')
    assert dict(app.store.profiles())[exclusion_key] == exclusion_current
    folders.review.emit('clicked')
    confirmation = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '除外設定の変更を確認')
    save_exclusions = next(w for w in descendants(confirmation) if isinstance(w, Gtk.Button) and w.get_label() == '保存')
    with patch.object(app.store, 'edit_excluded_folders', side_effect=OSError('synthetic storage failure')):
        save_exclusions.emit('clicked')
        assert confirmation.error_label.get_visible()
        assert 'synthetic storage failure' in confirmation.error_label.get_text()
        assert dict(app.store.profiles())[exclusion_key] == exclusion_current
    save_exclusions.emit('clicked')
    settle_history(folders)
    settle_history()
    merged = dict(app.store.profiles())[exclusion_key]
    assert merged.excludes == exclusion_current.excludes and not merged.phishing
    assert set(merged.excluded_dirs) == {str(concurrent_folder), str(db_folder), str(denied_folder)}
    assert str(db_folder) not in folders.candidate_buttons
    assert str(existing_folder) in folders.candidate_buttons
    assert not folders.review.get_sensitive()
    assert '次回' in folders.message.get_text()
    assert not any(isinstance(w, Gtk.CheckButton) for w in descendants(folders))
    folders.destroy()
    app.result.folder_button.emit('clicked')
    cancelled_draft = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == 'フォルダー別の検査結果')
    settle_history(cancelled_draft)
    cancelled_draft.remove_buttons[str(db_folder)].emit('clicked')
    assert cancelled_draft.review.get_sensitive()
    next(w for w in descendants(cancelled_draft) if isinstance(w, Gtk.Button) and w.get_label() == 'キャンセル').emit('clicked')
    assert dict(app.store.profiles())[exclusion_key] == merged
    exclusion_ui.PAGE_SIZE = 50
    app.store.delete_profile(exclusion_key)
    with app.store.connect() as db:
        assert db.execute('SELECT output FROM scans WHERE profile_id=?', (exclusion_key,)).fetchone()[0] == exclusion_output
        db.execute('DELETE FROM scans WHERE profile_id=?', (exclusion_key,))
    app.refresh_profiles(key)
    settle_history()
    from clam_gui.known_locations import trash_folder
    trash_path = str(trash_folder())
    trash_plan = Plan('ごみ箱テスト', [str(app.store.path.parent.parent)], [])
    trash_key = app.store.save_profile(trash_plan)
    app.store.save('2026-11-21T02:00:00+09:00', trash_plan.describe(), '検査完了', '', trash_key)
    app.refresh_profiles(trash_key)
    settle_history()
    app.result.folder_button.emit('clicked')
    trash_window = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == 'フォルダー別の検査結果')
    settle_history(trash_window)
    assert trash_window.trash_button.get_visible() and trash_window.trash_button.get_sensitive()
    trash_window.trash_button.emit('clicked')
    assert trash_window.path_entry.get_text() == ''
    assert trash_path in trash_window.draft_exclusions
    labels = [w for w in descendants(trash_window.excluded_list) if isinstance(w, Gtk.Label)]
    assert any(w.get_text() == 'ごみ箱' and w.get_tooltip_text() == trash_path for w in labels)
    assert dict(app.store.profiles())[trash_key].excluded_dirs == []
    assert not trash_window.trash_button.get_sensitive()
    trash_window.review.emit('clicked')
    confirmation = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '除外設定の変更を確認')
    labels = [w.get_text() for w in descendants(confirmation) if isinstance(w, Gtk.Label)]
    assert any('ごみ箱' in value and trash_path in value for value in labels)
    next(w for w in descendants(confirmation) if isinstance(w, Gtk.Button) and w.get_label() == '保存').emit('clicked')
    settle_history(trash_window)
    settle_history()
    assert dict(app.store.profiles())[trash_key].excluded_dirs == [trash_path]
    trash_window.destroy()
    app.edit_profile(trash_key, dict(app.store.profiles())[trash_key])
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    assert any(w.get_text() == 'フォルダー: ごみ箱' for w in descendants(editor) if isinstance(w, Gtk.Label))
    inherited = next(w for w in descendants(editor) if isinstance(w, Gtk.CheckButton) and w.get_label() == '環境設定のサイズ上限を使う')
    limits = [w for w in descendants(editor) if isinstance(w, Gtk.SpinButton) and w.get_accessible_role() == Gtk.AccessibleRole.SPIN_BUTTON and isinstance(w.get_parent().get_last_child(), Gtk.Label) and w.get_parent().get_last_child().get_text() == 'MiB']
    assert inherited.get_active() and len(limits) == 2
    assert all(not w.get_sensitive() for w in limits)
    inherited.set_active(False)
    assert all(w.get_sensitive() for w in limits)
    limits[0].set_value(512)
    limits[1].set_value(1024)
    next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_label() == '保存').emit('clicked')
    assert dict(app.store.profiles())[trash_key].limits == {'max-filesize': 512, 'max-scansize': 1024}
    editor.destroy()
    app.store.delete_profile(trash_key)
    with app.store.connect() as db:
        db.execute('DELETE FROM scans WHERE profile_id=?', (trash_key,))
    app.refresh_profiles(key)
    settle_history()
    assert not app.window.get_modal()
    with patch.object(app.window, 'unminimize') as restore, patch.object(app.window, 'present') as present:
        app.activate()
        restore.assert_called_once()
        present.assert_called_once()
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
    app.activate_action('order-plans', None)
    order_window = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの並べ替え')
    assert order_window.get_modal() and order_window.get_transient_for() == app.window
    assert not order_window.rows[key]['actions'].lookup_action('up').get_enabled()
    assert order_window.rows[key]['menu'].activate_action('order.down', None)
    assert [k for k, _ in app.saved_profiles] == [other_key, key]
    assert app.selected_profile()[0] == key
    assert not order_window.rows[key]['actions'].lookup_action('down').get_enabled()
    provider = order_window.rows[key]['source'].emit('prepare', 0.0, 0.0)
    assert provider is not None
    assert not order_window.rows[other_key]['target'].emit('drop', 'external text', 0.0, -1.0)
    assert order_window.rows[other_key]['target'].emit('drop', order_window.drag_prefix + key, 0.0, -1.0)
    assert [k for k, _ in app.saved_profiles] == [key, other_key]
    with patch.object(app.store, 'move_display_profile', side_effect=OSError('synthetic write failure')):
        assert not order_window.move(key, 1)
        assert order_window.message.get_visible()
        assert 'synthetic write failure' in order_window.message.get_text()
        assert [k for k, _ in app.saved_profiles] == [key, other_key]
    order_window.destroy()
    app.plan_list.select_row(app.plan_rows[other_key]['row'])
    assert app.selected_profile()[0] == other_key
    assert app.selected_plan_title.get_text() == other_plan.label
    settle_history()
    buffer = app.result.get_buffer()
    assert 'other plan log' in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    settle_history()
    assert len(app.log_entries) == 1
    app.poll_plans()
    assert app.selected_profile()[0] == other_key
    assert app.plan_rows[key]['more'].activate_action('plan.edit', None)
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    assert app.selected_profile()[0] == other_key
    assert next(w for w in descendants(editor) if isinstance(w, Gtk.Entry)).get_text() == plan.label
    editor.destroy()
    app.plan_list.select_row(app.plan_rows[key]['row'])
    settle_history()
    assert len(app.log_entries) == 2
    assert app.add_plan_button.get_action_name() == 'app.new-target'
    # Failure to create a worker must release the editor and normal quit path.
    app.edit_profile(key, plan)
    failed_editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    fields = list(descendants(failed_editor))
    enable = next(w for w in fields if isinstance(w, Gtk.CheckButton) and w.get_label() == '定期検査を有効にする')
    enable.set_active(True)
    save_failed = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == '保存')
    save_failed.emit('clicked')
    dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
    with patch('clam_gui.ui.plans.threading.Thread') as thread:
        thread.return_value.start.side_effect = RuntimeError('synthetic worker startup failure')
        dialog.response(Gtk.ResponseType.OK)
    assert not app.busy and not app.on_close(None)
    assert save_failed.is_sensitive()
    assert any(isinstance(w, Gtk.Label) and '保存処理を開始できませんでした' in w.get_text() for w in fields)
    failed_editor.close()
    assert not failed_editor.get_visible()
    # The schedule-switch path has a separate busy flag and must also recover.
    app.toggle_schedule(key, True)
    dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
    with patch('clam_gui.__main__.threading.Thread') as thread:
        thread.return_value.start.side_effect = RuntimeError('synthetic schedule startup failure')
        dialog.response(Gtk.ResponseType.OK)
    assert not app.schedule_busy and not app.on_close(None)
    assert '開始できませんでした' in app.notice.get_text()
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
    save = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == '保存')
    cancel = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == 'キャンセル')
    assert isinstance(editor.get_titlebar(), Gtk.HeaderBar)
    assert save.is_ancestor(editor.get_titlebar()) and cancel.is_ancestor(editor.get_titlebar())
    assert editor.get_default_widget() == save
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
        with patch('clam_gui.schedule.Scheduler') as manager:
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
    with patch('clam_gui.schedule.Scheduler') as manager:
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
        settle_history()
        assert reason in app.log_entries[0].status
        settle_history()
        buffer = app.result.get_buffer()
        assert reason in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        assert len(app.store.history_entries(blocked_key, blocked)) == 1
        notify.assert_called_once()
    # Two manual requests: another plan stays available, waiting is visible and cancellable.
    import threading
    from clam_gui.results import ScanStatus, ResultKind
    one = app.store.save_profile(Plan('手動キューA',[str(app.store.path.parent)],[]))
    two = app.store.save_profile(Plan('手動キューB',[str(app.store.path.parent)],[]))
    entered = {one:threading.Event(), two:threading.Event()}
    def fake_manual(plan, store, cancel, **kwargs):
        ident=kwargs['profile_id']
        assert kwargs['wait'] is True
        stamp=datetime.datetime.now().astimezone().isoformat()
        with track_scan(store, ident, stamp, phase='running' if ident==one else 'waiting'):
            entered[ident].set()
            assert cancel.wait(3)
        status=ScanStatus('検査待ち・検査を取り消しました',ResultKind.CANCELLED)
        store.save(stamp,plan.describe(),status,'synthetic cancellation',ident)
        return status,'synthetic cancellation'
    def wait_manual(predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline:
            GLib.MainContext.default().iteration(False);time.sleep(.001)
        assert predicate()
    with patch('clam_gui.__main__.scan',side_effect=fake_manual), patch.object(app,'send_notification'):
        app.refresh_profiles(one)
        app.plan_action(one,'run')
        dialog=next(w for w in Gtk.Window.list_toplevels() if isinstance(w,Gtk.MessageDialog))
        dialog.response(Gtk.ResponseType.OK)
        wait_manual(lambda:entered[one].is_set())
        app.poll_plans()
        assert app.busy and app.plan_rows[two]['actions']['run'].get_enabled()
        assert not app.plan_rows[one]['actions']['run'].get_enabled()
        app.plan_action(two,'run')
        dialog=next(w for w in Gtk.Window.list_toplevels() if isinstance(w,Gtk.MessageDialog))
        dialog.response(Gtk.ResponseType.OK)
        wait_manual(lambda:entered[two].is_set())
        app.poll_plans()
        assert '検査待ち' in app.plan_rows[two]['status'].get_text()
        assert not app.plan_rows[two]['spinner'].get_spinning()
        assert app.plan_rows[two]['actions']['stop'].get_enabled()
        app.plan_action(two,'stop')
        wait_manual(lambda:two not in app.manual_jobs)
        assert app.busy and one in app.manual_jobs
        app.plan_action(one,'stop')
        wait_manual(lambda:not app.busy)
        assert not app.manual_jobs
    # The main screen exposes local definition time and an update-only view.
    with patch('clam_gui.definitions.definition_update_label', return_value='定義ファイル更新：2026/11/17 08:00'):
        app.poll_plans()
        assert app.definition_label.get_text() == '定義ファイル更新：2026/11/17 08:00'
        assert app.definition_button.get_sensitive()
        with patch('clam_gui.packages.AptManager') as packages, patch('clam_gui.definitions.DefinitionUpdater.update') as update:
            packages.return_value.package_names = ('clamav', 'clamav-daemon', 'clamav-freshclam')
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
            assert '自動更新：有効' in app.definition_button.get_tooltip_text()
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
            assert '自動更新：無効' in app.definition_button.get_tooltip_text()
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
    app.automation.START_INTERVAL = .1
    app.automation.REST_INTERVAL = .05
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
    with patch('clam_gui.schedule.Scheduler') as scheduler:
        scheduler.return_value.apply.side_effect = lambda key, schedule: save_schedule(app.store, key, schedule)
        app.plan_rows[watch_key]['switch'].set_active(False)
        wait_until(lambda: not app.schedule_busy)
    assert not any(ident[0] == watch_key for ident in app.observers.monitors)
    values = app.store.automation_settings()
    values['media_enabled'] = True
    app.store.set_automation_settings(values)
    app.refresh_profiles(MEDIA_ID)
    assert MEDIA_ID in app.plan_rows
    app.activate_action('order-plans', None)
    order_window = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの並べ替え')
    first = app.saved_profiles[0][0]
    assert order_window.drop(order_window.drag_prefix + MEDIA_ID, first, False)
    assert app.saved_profiles[0][0] == MEDIA_ID
    assert app.selected_profile()[0] == MEDIA_ID
    last = app.saved_profiles[-1][0]
    assert order_window.drop(order_window.drag_prefix + MEDIA_ID, last, True)
    assert app.saved_profiles[-1][0] == MEDIA_ID
    order_window.destroy()

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
    settle_history(media_window.history_controller)
    assert len(media_window.history_controller.entries) == 1
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
    save_editor = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == '保存')
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
    with patch('clam_gui.schedule.Scheduler') as manager:
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
        next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == '保存').emit('clicked')
        manager.return_value.apply.assert_not_called()
        app.edit_profile(new_key, dict(app.store.profiles())[new_key])
        editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
        fields = list(descendants(editor))
        frequency = next(w for w in fields if isinstance(w, Gtk.DropDown) and w.get_model().get_n_items() == 5)
        frequency.set_selected(1)
        days = [w for w in fields if isinstance(w, Gtk.CheckButton) and w.get_label() in list('月火水木金土日')]
        assert len(days) == 7 and days[0].get_active()
        assert days[0].get_parent().get_parent().get_visible()
        for day in days:
            day.set_active(False)
        save = next(w for w in fields if isinstance(w, Gtk.Button) and w.get_label() == '保存')
        save.emit('clicked')
        manager.return_value.apply.assert_not_called()
        assert any(isinstance(w, Gtk.Label) and '曜日を1つ以上' in w.get_text() for w in fields)
        days[0].set_active(True)
        days[3].set_active(True)
        save.emit('clicked')
        confirmation(Gtk.ResponseType.OK)
        settle()
        assert read_schedule(app.store, new_key).weekdays == [0, 3]
        app.edit_profile(new_key, dict(app.store.profiles())[new_key])
        editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
        days = [w for w in descendants(editor) if isinstance(w, Gtk.CheckButton) and w.get_label() in list('月火水木金土日')]
        assert [i for i, w in enumerate(days) if w.get_active()] == [0, 3]
        editor.destroy()
    multi_a, multi_b = app.store.path.parent/'multi-a', app.store.path.parent/'multi-b'
    multi_a.mkdir()
    multi_b.mkdir()
    multi_key = app.store.save_profile(Plan('複数フォルダー', [str(multi_a)], []))
    app.edit_profile(multi_key, dict(app.store.profiles())[multi_key])
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    add_target = next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_label() == '対象フォルダーを追加')
    add_target.emit('clicked')
    additional = next(w for w in descendants(add_target.get_prev_sibling()) if isinstance(w, Gtk.Entry))
    save_multi = next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_label() == '保存')
    assert isinstance(editor.get_titlebar(), Gtk.HeaderBar)
    assert save_multi.has_css_class('suggested-action')
    additional.set_text('usr/local')
    assert not save_multi.get_sensitive()
    assert '2行目' in save_multi.get_tooltip_text()
    controllers = additional.observe_controllers()
    for i in range(controllers.get_n_items()):
        controller = controllers.get_item(i)
        if isinstance(controller, Gtk.EventControllerFocus):
            controller.emit('leave')
    assert additional.has_css_class('error')
    assert any(isinstance(w, Gtk.Label) and '2行目' in w.get_text() for w in descendants(editor))
    additional.set_text(str(multi_b/'missing'))
    save_multi.emit('clicked')
    assert additional.has_css_class('error')
    assert any(isinstance(w, Gtk.Label) and '2行目' in w.get_text() and '見つからない' in w.get_text() for w in descendants(editor))
    additional.set_text(str(multi_b))
    assert save_multi.get_sensitive() and not additional.has_css_class('error')
    add_target.emit('clicked')  # An unused third row must not block saving.
    next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_label() == '保存').emit('clicked')
    assert dict(app.store.profiles())[multi_key].targets == [str(multi_a), str(multi_b)]
    app.edit_profile(multi_key, dict(app.store.profiles())[multi_key])
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    assert any(isinstance(w, Gtk.Entry) and w.get_text() == str(multi_b) for w in descendants(editor))
    kind = next(w for w in descendants(editor) if isinstance(w, Gtk.DropDown) and w.get_model().get_n_items() == 2)
    kind.set_selected(1)
    next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_label() == '保存').emit('clicked')
    assert any(isinstance(w, Gtk.Label) and '1つだけ' in w.get_text() for w in descendants(editor))
    kind.set_selected(0)
    remove = next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_tooltip_text() == '対象フォルダーを取り消す')
    remove.emit('clicked')
    next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_label() == '保存').emit('clicked')
    assert dict(app.store.profiles())[multi_key].targets == [str(multi_a)]
    app.store.delete_profile(multi_key)
    app.refresh_profiles()
    admin_key = app.store.save_profile(Plan('旧管理者設定', [str(multi_a), str(multi_b)], [], requires_admin=True))
    app.edit_profile(admin_key, dict(app.store.profiles())[admin_key])
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    normal_user = next(w for w in descendants(editor) if isinstance(w, Gtk.CheckButton) and w.get_label() == '通常ユーザーで検査する')
    scheduled = next(w for w in descendants(editor) if isinstance(w, Gtk.CheckButton) and w.get_label() == '定期検査を有効にする')
    assert not normal_user.get_active() and not scheduled.get_sensitive()
    normal_user.set_active(True)
    assert scheduled.get_sensitive()
    assert dict(app.store.profiles())[admin_key].requires_admin
    next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_label() == 'キャンセル').emit('clicked')
    assert dict(app.store.profiles())[admin_key].requires_admin
    app.edit_profile(admin_key, dict(app.store.profiles())[admin_key])
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    normal_user = next(w for w in descendants(editor) if isinstance(w, Gtk.CheckButton) and w.get_label() == '通常ユーザーで検査する')
    normal_user.set_active(True)
    scheduled = next(w for w in descendants(editor) if isinstance(w, Gtk.CheckButton) and w.get_label() == '定期検査を有効にする')
    scheduled.set_active(True)
    with patch('clam_gui.schedule.Scheduler') as manager:
        manager.return_value.apply.side_effect = lambda k, s: save_schedule(app.store, k, s)
        next(w for w in descendants(editor) if isinstance(w, Gtk.Button) and w.get_label() == '保存').emit('clicked')
        confirmation(Gtk.ResponseType.OK)
        settle()
        manager.return_value.apply.assert_called_once()
    assert not dict(app.store.profiles())[admin_key].requires_admin
    assert read_schedule(app.store, admin_key).enabled
    app.edit_profile(admin_key, dict(app.store.profiles())[admin_key])
    editor = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '検査プランの設定')
    assert not any(isinstance(w, Gtk.CheckButton) and w.get_label() == '通常ユーザーで検査する' for w in descendants(editor))
    editor.destroy()
    app.store.delete_profile(admin_key)
    app.refresh_profiles()
    app.refresh_profiles(key)
    assert app.plan_rows[key]['actions']['run-admin'].get_enabled()
    with patch('clam_gui.admin_scan.available', return_value=False):
        app.plan_action(key, 'run-admin')
        setup = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == '管理者検査の補助機能')
        assert any(isinstance(w, Gtk.Label) and '認証は別々' in w.get_text() for w in descendants(setup))
        next(w for w in descendants(setup) if isinstance(w, Gtk.Button) and w.get_label() == 'キャンセル').emit('clicked')
        assert app.admin_setup_window is None
    ready = [False]
    from clam_gui.admin_helper_setup import SetupCancelled
    with patch('clam_gui.admin_scan.available', side_effect=lambda: ready[0]), patch('clam_gui.admin_helper_setup.AdminHelperSetup.execute') as install:
        app.plan_action(key, 'run-admin')
        setup = app.admin_setup_window
        apply = next(w for w in descendants(setup) if isinstance(w, Gtk.Button) and w.get_label() == '導入・更新する')
        install.side_effect = SetupCancelled('認証をキャンセルしました')
        apply.emit('clicked')
        wait_until(lambda: not app.schedule_busy)
        assert app.admin_setup_window is setup
        assert apply.get_sensitive()
        assert any(isinstance(w, Gtk.Label) and 'キャンセルしました' in w.get_text() for w in descendants(setup))
        install.side_effect = lambda action: ready.__setitem__(0, True)
        apply.emit('clicked')
        wait_until(lambda: not app.schedule_busy and app.admin_setup_window is None)
        install.assert_called_with('install')
        dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
        assert '管理者権限' in dialog.get_property('text')
        dialog.response(Gtk.ResponseType.CANCEL)
    with patch('clam_gui.admin_helper_setup.AdminHelperSetup.execute') as remove_helper:
        app.admin_helper_setup(action='remove')
        setup = app.admin_setup_window
        next(w for w in descendants(setup) if isinstance(w, Gtk.Button) and w.get_label() == '削除する').emit('clicked')
        wait_until(lambda: not app.schedule_busy and app.admin_setup_window is None)
        remove_helper.assert_called_once_with('remove')
    with patch('clam_gui.admin_scan.available', return_value=True), patch.object(app, 'run_scan') as admin_run:
        app.plan_action(key, 'run-admin')
        dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
        assert '管理者権限' in dialog.get_property('text')
        dialog.response(Gtk.ResponseType.CANCEL)
        admin_run.assert_not_called()
        app.plan_action(key, 'run-admin')
        dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
        dialog.response(Gtk.ResponseType.OK)
        wait_until(lambda: admin_run.called)
        assert admin_run.call_args.args[-1] is True
        app.release_manual_job(key)
    app.preferences()
    preferences = [w for w in Gtk.Window.list_toplevels() if w.get_title() == '環境設定']
    assert len(preferences) == 1
    widgets = list(descendants(preferences[0]))
    checks = [w for w in widgets if isinstance(w, Gtk.CheckButton)]
    assert len(checks) == 23
    phishing = next(w for w in checks if w.get_label() == 'メールのフィッシング検査')
    assert not phishing.get_active()
    phishing.set_active(True)
    assert not app.store.phishing_enabled()
    def finish_preferences():
        save = next(w for w in widgets if isinstance(w, Gtk.Button) and w.get_label() == '検査設定を保存')
        if not save.get_sensitive():
            return True
        assert any(w.get_label() == 'フォルダー監視を実施する' for w in checks)
        assert not any(isinstance(w, Gtk.Button) and '常駐監視' in (w.get_label() or '') for w in widgets)
        assert any(isinstance(w, Gtk.Label) and 'ログアウト中は停止' in w.get_text() for w in widgets)
        spins = [w for w in widgets if isinstance(w, Gtk.SpinButton) and w.get_parent().get_first_child() == w]
        assert len(spins) == 2
        assert [spin.get_value_as_int() for spin in spins] == [256, 256]
        assert not any(w.get_label() == 'ClamAV標準値を使う' for w in checks)
        assert not any(isinstance(w, Gtk.Button) and w.get_label() == 'サイズ上限をアプリ標準に戻す' for w in widgets)
        references = [w for w in widgets if isinstance(w, Gtk.Label) and w.get_text().startswith('（ClamAV標準は')]
        assert {w.get_text() for w in references} == {'（ClamAV標準は100 MiB）', '（ClamAV標準は400 MiB）'}
        assert all(w.has_css_class('caption') for w in references)
        spins[0].set_value(256)
        spins[1].set_value(512)
        save.emit('clicked')
        assert app.store.scan_limits() == {'max-filesize': 256, 'max-scansize': 512}
        reset = next(w for w in widgets if isinstance(w, Gtk.Button) and w.get_label() == 'ClamAV標準に戻す')
        reset.emit('clicked')
        assert [spin.get_value_as_int() for spin in spins] == [100, 400]
        assert app.store.scan_limits()['max-filesize'] == 256
        preferences[0].destroy()
        from clam_gui.packages import PackagePlan, PACKAGES
        class FakePackages:
            package_names = PACKAGES
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
                Path(__import__('os').environ['SMOKE_RESULT']).write_text('ok')
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
    assert Path(env['SMOKE_RESULT']).read_text() == 'ok'
    print('GUI startup and history: OK (isolated Wayland)')
