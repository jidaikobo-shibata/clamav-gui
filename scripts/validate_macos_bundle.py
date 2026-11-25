"""Windowless checks for a frozen macOS application; sends no notifications."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clam_gui.core import Plan, Store
from clam_gui.schedule import Schedule, save_schedule
from scripts.collect_macos_sources import components


def run(arguments, **kwargs):
    result = subprocess.run([str(value) for value in arguments], capture_output=True, text=True,
                            timeout=kwargs.pop('timeout', 240), **kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve()
    app = output / 'bundle/ClamAV GUI.app'
    architecture = json.loads((app / 'Contents/Resources/BUILD_INFO.json').read_text())['architecture']
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])
    binaries = set()
    for path in app.rglob('*'):
        if not path.is_file():
            continue
        with path.open('rb') as file:
            magic = file.read(4)
        if magic in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
            binaries.add(path.resolve())
    for path in sorted(binaries):
        if architecture not in run(['/usr/bin/lipo', '-archs', path]).split():
            raise RuntimeError('Incorrect binary architecture: ' + str(path))
    libraries = run(['/usr/bin/otool', '-L', *sorted(binaries)])
    for line in libraries.splitlines():
        value = line.strip().split(' (', 1)[0]
        if line.startswith('\t') and value.startswith('/') and not value.startswith(('/usr/lib/', '/System/Library/')):
            raise RuntimeError('External library dependency: ' + value)
    policy = '(version 1)(allow default)(deny file-read* (subpath "/opt/homebrew")(subpath "/opt/err")(subpath "/usr/local/Cellar"))'
    prefix = ['/usr/bin/sandbox-exec', '-p', policy]
    executable = app / 'Contents/MacOS/ClamAV GUI'
    helper = app / 'Contents/MacOS/ClamAV Helper'
    checks = {}
    # Exercise the shipped codec libraries directly, without importing a host GTK
    # or displaying a window. Compilation needs only the matching build headers.
    with tempfile.TemporaryDirectory(prefix='clam-codec-check-') as temporary:
        root = Path(temporary)
        kegs = components(output)
        source = root / 'probe.c'
        source.write_text('''#include <gdk-pixbuf/gdk-pixbuf.h>
#include <stdio.h>
int main(int argc, char **argv) {
    GError *error = NULL;
    GdkPixbuf *image = gdk_pixbuf_new_from_file(argv[1], &error);
    if (!image) { fprintf(stderr, "%s\\n", error ? error->message : "No image"); return 1; }
    return gdk_pixbuf_get_width(image) == 16 && gdk_pixbuf_get_height(image) == 16 ? 0 : 2;
}
''')
        framework = app / 'Contents/Frameworks'
        executable_probe = root / 'probe'
        run(['/usr/bin/clang', '-arch', architecture,
             '-I' + str(kegs['gdk-pixbuf'] / 'include/gdk-pixbuf-2.0'),
             '-I' + str(kegs['glib'] / 'include/glib-2.0'),
             '-I' + str(kegs['glib'] / 'lib/glib-2.0/include'), str(source),
             framework / 'libgdk_pixbuf-2.0.0.dylib',
             '-Wl,-rpath,' + str(framework), '-o', executable_probe])
        svg = root / 'test.svg'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="green"/></svg>')
        cache = root / 'loaders.cache'
        cache.write_text((app / 'Contents/Resources/lib/gdk-pixbuf/loaders.cache').read_text().replace(
            '@executable_path/lib', str(framework / 'lib')))
        run([*prefix, executable_probe, svg], env={**os.environ, 'GDK_PIXBUF_MODULE_FILE': str(cache)})
        checks['svg_codec'] = 'Bundled SVG loader decoded a 16 by 16 image without host libraries.'
        print('svg_codec: ' + checks['svg_codec'], flush=True)
    for name, command in (
            ('gui_runtime', [executable, '--check-runtime']),
            ('helper_runtime', [helper, '--check-runtime']),
            ('hidden_widgets', [executable, '--hidden-smoke-test'])):
        checks[name] = run([*prefix, *command])
        print(name + ': ' + checks[name], flush=True)
    with tempfile.TemporaryDirectory(prefix='clam-frozen-check-') as temporary:
        store = Store(Path(temporary) / 'data')
        key = store.save_profile(Plan('無効な一時プラン', [temporary], []))
        save_schedule(store, key, Schedule(False))
        run([*prefix, helper, 'scan', '--data-dir', store.path.parent, '--profile', key,
             '--support-dir', Path(temporary) / 'support'])
        if store.recent():
            raise RuntimeError('Disabled plan unexpectedly ran')
        checks['disabled_plan'] = 'Saved plan loaded successfully; no scan or notification started.'
    notifier = app / 'Contents/Helpers/ClamAV Notifications.app/Contents/MacOS/ClamAV Notify'
    checks['notification_status'] = json.loads(run([notifier], input='{"operation":"status"}'))
    report = {'architecture': architecture, 'mach_o_files_checked': len(binaries),
              'external_developer_libraries': False, 'signature_verified': True, 'checks': checks}
    (output / 'VALIDATION.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
