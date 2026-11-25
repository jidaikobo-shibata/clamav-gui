"""Task Scheduler entry point for per-user definition updates."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clam_gui.platforms.windows import Updates

if __name__ == '__main__':
    Updates().update(scheduled=True)
