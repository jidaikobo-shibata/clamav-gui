"""Exercise the macOS setup UI with fake downloads and isolated application data."""
import os
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch


def accessory_application():
    """Keep a hidden widget test out of the Dock and away from input focus."""
    import ctypes
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    objc.objc_getClass.argtypes, objc.objc_getClass.restype = [ctypes.c_char_p], ctypes.c_void_p
    objc.sel_registerName.argtypes, objc.sel_registerName.restype = [ctypes.c_char_p], ctypes.c_void_p
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(address)
    setting = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(address)
    app = get(objc.objc_getClass(b'NSApplication'), objc.sel_registerName(b'sharedApplication'))
    setting(app, objc.sel_registerName(b'setActivationPolicy:'), 1)


def main(show_windows=False, hidden=False):
    if not show_windows and not hidden:
        os.write(1, b'GUI smoke skipped: --show-windows is required to display test windows.\n')
        return 0
    if hidden:
        accessory_application()
    from clam_gui.macos_setup import SetupCancelled, SetupError, SetupState
    from clam_gui.setup_window import SetupWindow
    from clam_gui.__main__ import Application
    from gi.repository import GLib, Gtk
    from clam_gui.macos_update import MacAutoUpdateState
    if hidden:
        accessory_application()

    class Backend:
        def __init__(self):
            self.state = SetupState(False, False, False, False)
            self.calls = 0
            self.fail = False
            self.wait = False
            self.last_output = 'simulated update output'

        def inspect(self):
            return self.state

        def prepare(self, cancel, report, force_update=False):
            self.calls += 1
            report('検査用データを取得しています…', None)
            self.state = SetupState(True, True, False, False)
            if self.wait:
                cancel.wait(3)
                raise SetupCancelled('準備を中止しました。')
            if self.fail:
                raise SetupError('接続できませんでした。再試行してください。')
            self.state = SetupState(True, True, True, True)
            return self.state

    backend = Backend()
    automatic = [False]
    authorization = [0]
    class AutoService:
        def status(self):
            return MacAutoUpdateState(automatic[0], False, backend.state.engine and backend.state.configuration, 12)
        def set_enabled(self, enabled):
            automatic[0] = enabled
            return 'changed', self.status()
        def update(self, cancel, report):
            return backend.prepare(cancel, report, True)
    def notification(operation, **kwargs):
        if operation == 'authorize':
            authorization[0] = 2
        return {'authorization': authorization[0]}
    failures = []
    app = None

    def until(condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError('GUI callback did not finish')
            GLib.MainContext.default().iteration(False)
            time.sleep(.005)

    def check():
        try:
            until(lambda: app.setup_window and app.setup_window.state is not None)
            window = app.setup_window
            assert window.get_visible()
            assert window.get_modal() and window.get_transient_for() == app.window
            assert backend.calls == 0
            assert window.primary.get_label() == '必要なものをインストール'
            assert window.details.get_visible() is False
            assert window.later.get_label() == 'あとで'
            window.close()
            assert backend.calls == 0
            assert app.setup_window is None

            backend.fail = True
            window = app.open_mac_setup()
            until(lambda: window.state is not None)
            window.primary.emit('clicked')
            assert app.busy and window.running
            window.primary.emit('clicked')
            until(lambda: not window.running)
            assert backend.calls == 1
            assert not app.busy
            assert window.primary.get_label() == '再試行'
            assert window.details.get_visible() and not window.details.get_expanded()
            assert '再試行' in window.message.get_text()
            assert window.state.engine and window.state.configuration

            backend.fail = False
            window.primary.emit('clicked')
            until(lambda: not window.running)
            assert backend.calls == 2 and window.completed
            assert window.primary.get_label() == '使い始める'
            window.primary.emit('clicked')
            assert app.setup_window is None

            app.install_prompt_checked = False
            app.offer_install()
            until(lambda: app.setup_window is None)
            assert backend.calls == 2  # A ready app does not display or perform setup.

            window = app.open_mac_setup()
            until(lambda: window.state is not None and window.auto_switch.get_sensitive())
            window.auto_switch.set_active(True)
            dialog = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.MessageDialog))
            dialog.response(Gtk.ResponseType.OK)
            until(lambda: not window.service_busy and automatic[0])
            assert not app.busy
            assert authorization[0] == 0
            window.notify_button.emit('clicked')
            until(lambda: authorization[0] == 2)
            assert window.message.get_layout().get_pixel_size()[1] > 0
            window.close()

            backend.wait = True
            window = app.open_mac_setup()
            until(lambda: window.state is not None)
            window.primary.emit('clicked')
            window.later.emit('clicked')
            until(lambda: not window.running)
            assert '中止しました' in window.message.get_text()
            assert not window.details.get_visible()
            assert not app.busy
            window.close()
            os.write(1, b'macOS setup GUI smoke test passed\n')
        except BaseException as error:
            failures.append(error)
        finally:
            app.quit()
        return False

    with tempfile.TemporaryDirectory(prefix='clam-gui-mac-smoke-') as root, ExitStack() as stack, \
            patch.dict(os.environ, {'XDG_DATA_HOME': root}), \
            patch('clam_gui.setup_window.MacSetup', return_value=backend), \
            patch('clam_gui.macos_update.MacAutoUpdateService', return_value=AutoService()), \
            patch('clam_gui.macos_notifications.request', side_effect=notification):
        backend.root = Path(root) / 'support'
        if hidden:
            stack.enter_context(patch.object(Gtk.Window, 'present', lambda *_: None))
            stack.enter_context(patch.object(Gtk.Window, 'get_visible', lambda *_: True))
            stack.enter_context(patch.object(Gtk.Window, 'get_mapped', lambda *_: True))
            def close(window):
                if isinstance(window, SetupWindow):
                    if not window.on_close(window):
                        window.destroy()
                else:
                    window.destroy()
            stack.enter_context(patch.object(Gtk.Window, 'close', close))
        app = Application()
        app.set_application_id('com.jidaikobo.ClamAVGui.Smoke')
        GLib.timeout_add(500, check)
        app.run([])
    if failures:
        raise failures[0]
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main(show_windows='--show-windows' in sys.argv, hidden='--hidden' in sys.argv))
