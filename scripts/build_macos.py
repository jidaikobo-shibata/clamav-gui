"""Build a local macOS trial with Python/GTK; never installs or bundles ClamAV.

Build dependency: PyInstaller 6.22.3 (GPL with its bootloader exception).
Runtime libraries retain their own licenses. Public distribution also needs
corresponding source packages, release signing and testing on another Mac.
"""
import argparse
import ast
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def refresh_loader_cache(output):
    """Use the matching toolchain, including when the host's PATH is ARM-only."""
    output = Path(output).resolve()
    app = output / 'bundle/ClamAV GUI.app'
    modules = []
    for section in ast.literal_eval((output / 'work/ClamAV-GUI/Analysis-00.toc').read_text()):
        if isinstance(section, list):
            modules.extend(Path(entry[1]).resolve() for entry in section
                           if isinstance(entry, tuple) and len(entry) == 3
                           and entry[0].endswith('libpixbufloader_svg.so'))
    if not modules or 'Cellar' not in modules[0].parts:
        raise ValueError('Cannot locate the matching SVG loader toolchain')
    prefix = Path(*modules[0].parts[:modules[0].parts.index('Cellar')])
    directory = prefix / 'lib/gdk-pixbuf-2.0/2.10.0/loaders'
    loaders = sorted(directory.glob('*.so'))
    env = {**os.environ, 'DYLD_LIBRARY_PATH': str(prefix / 'lib')}
    result = subprocess.run([str(prefix / 'bin/gdk-pixbuf-query-loaders'), *map(str, loaders)],
                            capture_output=True, text=True, check=True, env=env)
    if result.stderr.strip():
        raise ValueError('Loader introspection failed: ' + result.stderr)
    lines = []
    for line in result.stdout.splitlines():
        if line.startswith('#'):
            continue
        if line.startswith('"' + str(directory) + '/'):
            filename = line.split('"')[1].rsplit('/', 1)[1]
            if not (app / 'Contents/Frameworks/lib/gdk-pixbuf/loaders' / filename).is_file():
                raise ValueError('Loader missing from bundle: ' + filename)
            line = '"@executable_path/lib/gdk-pixbuf/loaders/' + filename + '"'
        lines.append(line)
    cache = '\n'.join(lines) + '\n'
    if '"svg"' not in cache or str(prefix) in cache:
        raise ValueError('Invalid relocatable SVG loader cache')
    (app / 'Contents/Resources/lib/gdk-pixbuf/loaders.cache').write_text(cache)


def sign_and_archive(output):
    output = Path(output).resolve()
    app = output / 'bundle/ClamAV GUI.app'
    metadata = json.loads((app / 'Contents/Resources/BUILD_INFO.json').read_text())
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(app)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    archive = output / ('ClamAV-GUI-' + metadata['architecture'] + '.zip')
    archive.unlink(missing_ok=True)
    subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                    str(app), str(archive)], check=True)
    print(archive)
    return archive


def application_icon(output):
    image = output / 'icon.png'
    # This PNG is rendered from the adjacent SVG. Do not depend on an optional
    # system SVG loader when building on Intel or a minimal developer setup.
    shutil.copy2(ROOT / 'assets/clamav-gui.png', image)
    iconset = output / 'ClamAVGui.iconset'
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            destination = iconset / (f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png')
            subprocess.run(['/usr/bin/sips', '-z', str(size * scale), str(size * scale),
                            str(image), '--out', str(destination)], check=True, stdout=subprocess.DEVNULL)
    result = output / 'ClamAVGui.icns'
    subprocess.run(['/usr/bin/iconutil', '-c', 'icns', str(iconset), '-o', str(result)], check=True)
    return result


def build(output):
    if sys.platform != 'darwin':
        raise ValueError('Build this prototype on macOS.')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    icon = application_icon(output)
    native = output / 'ClamAV Notify'
    subprocess.run(['/usr/bin/clang', '-arch', platform.machine(), '-fobjc-arc',
                    '-mmacosx-version-min=15.0', '-framework', 'Foundation',
                    '-framework', 'AppKit',
                    '-framework', 'UserNotifications', str(ROOT / 'scripts/macos_notify.m'),
                    '-o', str(native)], check=True)
    # GI introspection needs metadata, not a display. Avoid Python's Dock activation
    # and test windows while the person using this Mac works in other applications.
    env = {**os.environ, 'PYINSTALLER_CONFIG_DIR': str(output / 'cache'), 'GDK_BACKEND': 'none',
           'CLAMAV_GUI_BUILD_ICON': str(icon)}
    import gi
    module = Path(gi.__file__).resolve()
    if 'Cellar' in module.parts:
        prefix = Path(*module.parts[:module.parts.index('Cellar')])
        # Introspection reports library basenames; custom Intel prefixes are not
        # in dyld's default search path. This is a build-only environment setting.
        env['DYLD_LIBRARY_PATH'] = str(prefix / 'lib') + (
            ':' + env['DYLD_LIBRARY_PATH'] if env.get('DYLD_LIBRARY_PATH') else '')
        env['PATH'] = str(prefix / 'bin') + ':' + str(prefix / 'sbin') + ':' + env.get('PATH', '')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath', str(output / 'bundle'), '--workpath', str(output / 'work'),
                    str(ROOT / 'scripts/ClamAV-GUI.spec')], check=True, cwd=ROOT, env=env)
    app = output / 'bundle/ClamAV GUI.app'
    notifications = app / 'Contents/Helpers/ClamAV Notifications.app'
    notifier = notifications / 'Contents/MacOS/ClamAV Notify'
    notifier.parent.mkdir(parents=True)
    shutil.copy2(native, notifier)
    resources = notifications / 'Contents/Resources'
    resources.mkdir()
    shutil.copy2(icon, resources / 'ClamAVGui.icns')
    (notifications / 'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleName': 'ClamAV GUIの検査通知', 'CFBundleDisplayName': 'ClamAV GUIの検査通知',
        'CFBundleIdentifier': 'com.jidaikobo.ClamAVGui.NotificationService',
        'CFBundleExecutable': 'ClamAV Notify', 'CFBundlePackageType': 'APPL',
        'CFBundleVersion': '10', 'CFBundleShortVersionString': '1.0.0',
        'CFBundleIconFile': 'ClamAVGui.icns',
        'LSUIElement': True, 'LSMinimumSystemVersion': '15.0',
        'NSUserNotificationAlertStyle': 'alert'}))
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-',
                    str(notifications)], check=True)
    notices = app / 'Contents/Resources/Licenses'
    notices.mkdir()
    components = {}
    for section in ast.literal_eval((output / 'work/ClamAV-GUI/Analysis-00.toc').read_text()):
        if not isinstance(section, list):
            continue
        for entry in section:
            if not isinstance(entry, tuple) or len(entry) != 3:
                continue
            path = Path(entry[1]).resolve()
            if 'Cellar' not in path.parts:
                continue
            index = path.parts.index('Cellar')
            components[path.parts[index + 1]] = Path(*path.parts[:index + 3])
    manifest = []
    for name, keg in sorted(components.items()):
        folder = notices / name
        folder.mkdir()
        recipe = keg / '.brew' / (name + '.rb')
        if recipe.is_file():
            shutil.copy2(recipe, folder / recipe.name)
        for path in keg.iterdir():
            if path.is_file() and path.name.lower().startswith(('license', 'copying', 'notice', 'copyright')):
                shutil.copy2(path, folder / path.name)
        manifest.append({'name': name, 'version': keg.name})
    for name in ('PyInstaller', 'pyinstaller-hooks-contrib', 'altgraph', 'macholib', 'packaging', 'pycairo'):
        dist = importlib.metadata.distribution(name)
        folder = notices / name
        folder.mkdir()
        for entry in dist.files or []:
            if 'license' in str(entry).lower() or Path(entry).name.lower().startswith(('copying', 'notice')):
                path = Path(dist.locate_file(entry))
                if path.is_file():
                    shutil.copy2(path, folder / path.name)
        manifest.append({'name': name, 'version': dist.version})
    metadata = {'architecture': platform.machine(), 'minimum_macos': '15.0',
                'signing': 'ad-hoc; not notarized', 'purpose': 'macOS release candidate',
                'clamav': 'download official signed installer on user request',
                'definitions': 'download using FreshClam on user request', 'components': manifest}
    (app / 'Contents/Resources/BUILD_INFO.json').write_text(json.dumps(metadata, indent=2) + '\n')
    refresh_loader_cache(output)
    return sign_and_archive(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/macos-trial')
    parser.add_argument('--refresh-loader-cache', action='store_true',
                        help='Regenerate resource metadata and re-sign an existing completed build.')
    args = parser.parse_args()
    if args.refresh_loader_cache:
        refresh_loader_cache(args.output)
        sign_and_archive(args.output)
    else:
        build(args.output)
