# Run using the native Python/GTK architecture. ClamAV itself is never bundled.
from pathlib import Path
import os

root = Path(SPECPATH).parent
analysis = Analysis(
    [str(root / 'scripts/macos_app.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'LICENSE'), '.')],
    hiddenimports=['gi.repository.Gtk', 'gi.repository.Pango', 'gi.repository.PangoCairo', 'cairo'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'numpy', 'IPython', 'setuptools', 'pip'],
    hooksconfig={'gi': {'module-versions': {'Gtk': '4.0', 'Gdk': '4.0'},
                       'icons': [], 'themes': [], 'languages': ['ja', 'en']}},
    optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='ClamAV GUI',
          debug=False, strip=False, upx=False, console=False,
          codesign_identity=None, entitlements_file=None)
helper_analysis = Analysis(
    [str(root / 'scripts/macos_helper.py')], pathex=[str(root)], binaries=[], datas=[],
    hiddenimports=[], hookspath=[], runtime_hooks=[],
    excludes=['gi', 'tkinter', 'pytest', 'numpy', 'IPython', 'setuptools', 'pip'], optimize=0,
)
helper_pyz = PYZ(helper_analysis.pure)
helper = EXE(helper_pyz, helper_analysis.scripts, [], exclude_binaries=True,
             name='ClamAV Helper', debug=False, strip=False, upx=False, console=True,
             codesign_identity=None, entitlements_file=None)
collection = COLLECT(exe, helper, analysis.binaries, analysis.datas,
                     helper_analysis.binaries, helper_analysis.datas, name='ClamAV GUI',
                     strip=False, upx=False)
app = BUNDLE(collection, name='ClamAV GUI.app',
             icon=os.environ.get('CLAMAV_GUI_BUILD_ICON'),
             bundle_identifier='com.jidaikobo.ClamAVGui',
             info_plist={'CFBundleDisplayName': 'ClamAV GUI',
                         'CFBundleShortVersionString': '1.0.0', 'CFBundleVersion': '10',
                         'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '15.0'})
