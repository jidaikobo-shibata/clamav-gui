"""GNOME notifications that activate the installed GUI when clicked."""
import subprocess
import uuid

APP_ID = 'com.jidaikobo.ClamAVGui'


def send(title, body, urgency='normal'):
    from gi.repository import Gio, GLib
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Same protocol as GNotification's GNOME backend, but await acceptance so
        # failed deliveries stay in the monitoring process's retry outbox.
        details = {'title': GLib.Variant('s', title), 'body': GLib.Variant('s', body),
                   'priority': GLib.Variant('s', 'urgent' if urgency == 'critical' else 'normal')}
        connection.call_sync('org.gtk.Notifications', '/org/gtk/Notifications',
            'org.gtk.Notifications', 'AddNotification',
            GLib.Variant('(ssa{sv})', (APP_ID, uuid.uuid4().hex, details)),
            GLib.VariantType.new('()'), Gio.DBusCallFlags.NONE, 10000, None)
    except GLib.Error as error:
        remote = Gio.DBusError.get_remote_error(error)
        if remote not in ('org.freedesktop.DBus.Error.ServiceUnknown',
                          'org.freedesktop.DBus.Error.UnknownMethod',
                          'org.freedesktop.DBus.Error.NameHasNoOwner'):
            raise RuntimeError('デスクトップ通知を送れませんでした：' + str(error)) from error
        # Preserve plain notifications on non-GNOME desktops. Click activation
        # through the application desktop entry is provided by GNOME above.
        result = subprocess.run(['notify-send', '--app-name=ClamAV GUI',
            '--hint=string:desktop-entry:' + APP_ID, '--urgency=' + urgency,
            '--expire-time=0', title, body], capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError('デスクトップ通知を送れませんでした。履歴を確認してください。')
