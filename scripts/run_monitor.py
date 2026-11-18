"""Entry point for the login-session monitor service."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clam_gui.core import Store
from clam_gui.monitor import Monitor

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    Monitor(Store(args.data_dir)).run()
