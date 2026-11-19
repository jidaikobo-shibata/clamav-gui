"""Build the Windows source distribution with its double-click setup and launcher."""
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def build():
    output = ROOT/'dist'/'ClamAV-GUI-Windows11-x64.zip'
    output.parent.mkdir(exist_ok=True)
    sources = [ROOT/name for name in ('Setup-Windows.cmd', 'ClamAV-GUI.cmd', 'README.md',
                                      'LICENSE', 'CONTRIBUTING.md', 'assets/clamav-gui.svg')]
    for pattern in ('clam_gui/**/*.py', 'scripts/*.py', 'docs/*.md', 'tests/test_*.py'):
        sources.extend(sorted(ROOT.glob(pattern)))
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        archive = Path(temporary.name)
    try:
        with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as bundle:
            for path in sources:
                bundle.write(path, Path('ClamAV-GUI')/path.relative_to(ROOT))
        with ZipFile(archive) as bundle:
            if bundle.testzip():
                raise RuntimeError('ZIP integrity check failed')
        archive.replace(output)
    finally:
        archive.unlink(missing_ok=True)
    return output


if __name__ == '__main__':
    print(build())
