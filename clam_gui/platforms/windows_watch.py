"""Combine GIO directory events with native removable-volume discovery."""
import ctypes
from pathlib import Path
import uuid

from gi.repository import GLib
from ..automation import Media
from ..gio_watch import Observers


class WindowsObservers(Observers):
    def observe_volumes(self, volumes):
        self.signals = []
        self.poll(False)
        self.volume_timer = GLib.timeout_add_seconds(2, self.poll)

    def poll(self, scan=True):
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        current = {}
        for i in range(26):
            if mask & (1 << i):
                root = chr(65 + i) + ':\\'
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:  # DRIVE_REMOVABLE
                    try:
                        info = Path(root).stat()
                        current[root] = info.st_dev, info.st_ino
                    except OSError:
                        pass
        for root, medium in list(self.mounts.items()):
            if current.get(root) != medium.device:
                self.engine.remove_media(medium.token)
                del self.mounts[root]
        for root, device in current.items():
            if root not in self.mounts:
                medium = Media(uuid.uuid4().hex, root, root, device)
                self.mounts[root] = medium
                self.engine.media[medium.token] = medium
                if scan:
                    self.engine.add_media(medium)
        return True

    def scan_connected(self):
        self.poll(False)
        for medium in self.mounts.values():
            self.engine.add_media(medium, force=True)
        return len(self.mounts)

    def close(self):
        GLib.source_remove(self.volume_timer)
        super().close()
