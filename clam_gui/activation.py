"""Per-user D-Bus activation for GNOME notification actions."""
import os
from pathlib import Path
import sys
from .desktop_notifications import APP_ID


def install_activation(source, data_home=None):
    source = Path(source).resolve()
    data = Path(data_home) if data_home else Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')
    desktop = data/'applications'/(APP_ID+'.desktop')
    if not desktop.is_file():
        return False
    launcher = source/'scripts/launch_gui.py'
    if not launcher.is_file():
        return False
    def quote(value):
        text = str(value)
        if any(c in text for c in '\n\r\0'):
            raise ValueError('起動パスに改行は使用できません。')
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
    args = [sys.executable, launcher, '--source-root', source, '--gapplication-service']
    content = '[D-BUS Service]\nName=' + APP_ID + '\nExec=' + ' '.join(map(quote, args)) + '\n'
    destination = data/'dbus-1/services'/(APP_ID+'.service')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open('x', encoding='utf-8') as file:
            file.write(content)
    except FileExistsError:
        if destination.is_symlink() or destination.read_text(encoding='utf-8') != content:
            raise ValueError('既存のD-Bus起動設定が異なるため変更しません：' + str(destination))
    return True
