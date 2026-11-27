"""Desktop entry launcher. No shell or package installation."""
import argparse
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--gapplication-service', action='store_true')
    args = parser.parse_args()
    root = args.source_root.resolve()
    if not (root / 'clam_gui/__main__.py').is_file():
        parser.error('GUI source directory was not found')
    sys.path.insert(0, str(root))
    sys.argv = [sys.argv[0]] + (['--gapplication-service'] if args.gapplication_service else [])
    runpy.run_module('clam_gui', run_name='__main__')


if __name__ == '__main__':
    main()
