import unittest
from unittest.mock import Mock, patch
from gi.repository import Gio, GLib
from clam_gui.desktop_notifications import send, APP_ID


class DesktopNotificationTests(unittest.TestCase):
    def test_headless_notification_identifies_gui_and_leaves_default_activation(self):
        connection = Mock()
        with patch.object(Gio, 'bus_get_sync', return_value=connection):
            send('result', 'details', 'critical')
        args = connection.call_sync.call_args.args
        self.assertEqual(args[:4], ('org.gtk.Notifications', '/org/gtk/Notifications', 'org.gtk.Notifications', 'AddNotification'))
        app, ident, payload = args[4].unpack()
        self.assertEqual(app, APP_ID)
        self.assertTrue(ident)
        self.assertEqual(payload['body'], 'details')
        self.assertEqual(payload['priority'], 'urgent')
        self.assertNotIn('default-action', payload)  # GNOME activates the application.
        self.assertEqual(args[-2], 10000)

    def test_delivery_error_is_propagated_for_retry(self):
        connection = Mock()
        error = GLib.Error.new_literal(Gio.io_error_quark(), 'failed', Gio.IOErrorEnum.TIMED_OUT)
        connection.call_sync.side_effect = error
        with patch.object(Gio, 'bus_get_sync', return_value=connection), self.assertRaises(RuntimeError):
            send('title', 'body')

    def test_non_gnome_desktop_keeps_plain_notification_fallback(self):
        connection = Mock()
        connection.call_sync.side_effect = Gio.dbus_error_new_for_dbus_error('org.freedesktop.DBus.Error.ServiceUnknown', 'no GNOME')
        with patch.object(Gio, 'bus_get_sync', return_value=connection), patch('clam_gui.desktop_notifications.subprocess.run', return_value=Mock(returncode=0)) as run:
            send('title', 'body')
        self.assertIn('--hint=string:desktop-entry:' + APP_ID, run.call_args.args[0])
