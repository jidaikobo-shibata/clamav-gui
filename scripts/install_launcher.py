"""Install a per-user Ubuntu desktop entry without elevation or autostart."""
import argparse
import os
from pathlib import Path
import re
import sys


def desktop_string(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def exec_arg(value):
    value = str(value)
    if any(char in value for char in '\n\r\x00'):
        raise ValueError('Launcher paths must not contain newlines')
    for char in ('\\', '"', '`', '$'):
        value = value.replace(char, '\\' + char)
    return '"' + value.replace('%', '%%') + '"'


def render_entry(source, python='/usr/bin/python3'):
    source = Path(source).resolve()
    project = Path(__file__).resolve().parents[1]
    text = (source/'clam_gui/__main__.py').read_text(encoding='utf-8')
    match = re.search(r"application_id=['\"]([A-Za-z0-9_.-]+)['\"]", text)
    if not match:
        raise ValueError('Application ID was not found')
    app_id = match[1]
    args = [python, project/'scripts/launch_gui.py', '--source-root', source]
    entry = ('[Desktop Entry]\nType=Application\nVersion=1.0\nName=ClamAV GUI\n'
             'Comment=Scan folders with ClamAV and review results\n'
             'Comment[ja]=ClamAVでフォルダーを検査し、結果を確認します\n'
             'Exec=' + ' '.join(exec_arg(arg) for arg in args) + '\n'
             'Icon=' + desktop_string(project/'assets/clamav-gui.svg') + '\n'
             'Terminal=false\nStartupNotify=true\nDBusActivatable=true\nX-GNOME-UsesNotifications=true\nCategories=Utility;Security;\n'
             'Keywords=ClamAV;antivirus;scan;\nStartupWMClass=' + app_id + '\n')
    return app_id + '.desktop', entry


def install(source, data_home=None):
    name, content = render_entry(source)
    data = Path(data_home) if data_home else Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share')))
    destination = data/'applications'/name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Existing launcher was left unchanged: ' + str(destination))
    with destination.open('x', encoding='utf-8') as file:
        file.write(content)
    sys.path.insert(0, str(Path(source).resolve()))
    from clam_gui.activation import install_activation
    install_activation(source, data)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    if args.preview:
        print(render_entry(args.source_root)[1])
    else:
        print(install(args.source_root))


if __name__ == '__main__':
    main()
