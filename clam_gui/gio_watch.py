"""GTK-main-thread directory and mounted removable-media observers. No mounting."""
from collections import deque
import os
import time
from pathlib import Path
import uuid
from gi.repository import Gio, GLib
from .automation import Media, allowed_path


def removable_mount(mount):
    try:
        root = mount.get_root()
        path = root.get_path() if root.is_native() else None
        volume = mount.get_volume()
        drive = volume.get_drive() if volume else None
        if not path or Path(path) == Path('/') or not drive:
            return None
        if not (drive.is_removable() or drive.is_media_removable()):
            return None
        info = Path(path).stat()
        if not Path(path).is_dir() or Path(path).is_symlink():
            return None
        return path, mount.get_name(), (info.st_dev, info.st_ino)
    except (OSError, GLib.Error):
        return None


class Observers:
    def __init__(self, engine, volumes=None, limit=1024):
        self.inventory = None
        self.engine = engine
        self.limit = limit
        self.plans = {}
        self.monitors = {}
        self.tasks = deque()
        self.queued = set()
        self.overflow = set()
        self.idle = None
        self.current = None
        self.mounts = {}
        self.volumes = volumes if volumes is not None else Gio.VolumeMonitor.get()
        self.signals = [self.volumes.connect('mount-added', self.mount_added),
                        self.volumes.connect('mount-removed', self.mount_removed)]
        # Existing mounts are remembered, but not automatically scanned on enable/startup.
        for mount in self.volumes.get_mounts():
            self.remember_mount(mount, scan=False)

    def configure(self, plans):
        plans = dict(plans)
        if plans == self.plans and all((key, str(plan.targets[0])) in self.monitors for key, plan in plans.items()):
            return
        for monitor in self.monitors.values():
            monitor.cancel()
        self.monitors.clear()
        self.finish_directory()
        self.tasks.clear()
        self.queued.clear()
        self.overflow.clear()
        self.plans = plans
        for key, plan in plans.items():
            self.engine.errors.pop(key, None)
            self.enqueue(key, plan.targets[0], False)

    def enqueue(self, key, path, scan_files):
        ident = key, str(path)
        if ident not in self.queued:
            if len(self.queued) >= self.limit:
                plan = self.plans.get(key)
                if plan and key not in self.overflow:
                    self.overflow.add(key)
                    self.engine.warn(key, plan, f'監視準備の待ちフォルダー数が上限{self.limit}に達しました。一部を監視できません。監視範囲を狭め、フォルダー全体の検査を併用してください。')
                return False
            self.queued.add(ident)
            self.tasks.append((key, str(path), scan_files))
        if self.idle is None:
            self.idle = GLib.idle_add(self.drain)
        return True

    @property
    def discovering(self):
        return bool(self.current or self.tasks)

    def finish_directory(self):
        if self.current:
            key, directory, _, entries, _ = self.current
            entries.close()
            self.queued.discard((key, directory))
            self.current = None

    def drain(self):
        # Both bounds matter: slow filesystems and very large directories yield.
        deadline = time.monotonic() + 0.008
        for _ in range(128):
            if self.current:
                key, directory, scan_files, entries, count = self.current
                plan = self.plans.get(key)
                if not plan or (_ == 0 and not allowed_path(plan, directory)):
                    self.finish_directory()
                    continue
                try:
                    entry = next(entries)
                    if count >= 10000:
                        self.engine.warn(key, plan, 'フォルダー内の項目数が監視準備の上限を超えました。範囲を狭めて全体検査を併用してください。')
                        self.finish_directory()
                    else:
                        self.current = key, directory, scan_files, entries, count + 1
                        if not entry.is_symlink():
                            if entry.is_dir(follow_symlinks=False) and plan.watch_recursive and allowed_path(plan, entry.path):
                                self.enqueue(key, entry.path, scan_files)
                            elif entry.is_file(follow_symlinks=False) and allowed_path(plan, entry.path):
                                if self.inventory:
                                    self.inventory(key, entry.path)
                                if scan_files:
                                    self.engine.file_event(key, entry.path)
                except StopIteration:
                    self.finish_directory()
                except OSError as error:
                    self.engine.warn(key, plan, '監視フォルダーを読み取れません：' + str(error))
                    self.finish_directory()
                if time.monotonic() >= deadline:
                    return True
                continue
            if not self.tasks:
                self.idle = None
                return False
            key, directory, scan_files = self.tasks.popleft()
            self.queued.discard((key, directory))
            plan = self.plans.get(key)
            if not plan:
                continue
            if not allowed_path(plan, directory) or not Path(directory).is_dir():
                if directory == str(plan.targets[0]):
                    self.engine.warn(key, plan, '監視フォルダーが見つからないか読み取れません。設定を保存し直してください。')
                continue
            if (key, directory) not in self.monitors:
                if len(self.monitors) >= self.limit:
                    self.engine.warn(key, plan, f'監視フォルダー数の上限{self.limit}に達しました。監視範囲を狭めるか定期検査を併用してください。')
                    self.tasks.clear()
                    self.queued.clear()
                    break
                try:
                    monitor = Gio.File.new_for_path(directory).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
                    monitor.connect('changed', self.event, key)
                    self.monitors[key, directory] = monitor
                except GLib.Error as error:
                    self.engine.warn(key, plan, 'フォルダーを監視できません：' + str(error))
                    continue
            try:
                self.current = key, directory, scan_files, os.scandir(directory), 0
                self.queued.add((key, directory))
            except OSError as error:
                self.engine.warn(key, plan, '監視フォルダーを読み取れません：' + str(error))
            if time.monotonic() >= deadline:
                return True
        return True

    def event(self, monitor, file, other, event, key):
        plan = self.plans.get(key)
        if not plan:
            return
        if event in (Gio.FileMonitorEvent.DELETED, Gio.FileMonitorEvent.MOVED_OUT):
            path = file.get_path()
            for ident in list(self.monitors):
                if ident[0] == key and path and (Path(ident[1]) == Path(path) or Path(path) in Path(ident[1]).parents):
                    self.monitors.pop(ident).cancel()
            if path == plan.targets[0]:
                self.engine.warn(key, plan, '監視フォルダーが見つかりません。設定を保存し直すと監視を再開します。')
            return
        if event not in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.CHANGED,
                         Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.RENAMED,
                         Gio.FileMonitorEvent.MOVED_IN):
            return
        target = other if event == Gio.FileMonitorEvent.RENAMED and other else file
        path = target.get_path()
        if not path or not allowed_path(plan, path):
            return
        if Path(path).is_dir():
            if plan.watch_recursive:
                self.enqueue(key, path, True)
        else:
            self.engine.file_event(key, path)

    def remember_mount(self, mount, scan):
        data = removable_mount(mount)
        if not data:
            return
        uri = mount.get_root().get_uri()
        if uri in self.mounts:
            return
        path, label, device = data
        medium = Media(uuid.uuid4().hex, path, label, device)
        self.mounts[uri] = medium
        self.engine.media[medium.token] = medium
        if scan:
            self.engine.add_media(medium)

    def mount_added(self, monitor, mount):
        self.remember_mount(mount, scan=True)

    def mount_removed(self, monitor, mount):
        medium = self.mounts.pop(mount.get_root().get_uri(), None)
        if medium:
            self.engine.remove_media(medium.token)

    def scan_connected(self):
        count = 0
        mounts = self.volumes.get_mounts()
        uris = {mount.get_root().get_uri() for mount in mounts}
        for uri, medium in list(self.mounts.items()):
            if uri not in uris or not medium.available():
                self.mounts.pop(uri)
                self.engine.remove_media(medium.token)
        for mount in mounts:
            self.remember_mount(mount, scan=False)
        for medium in self.mounts.values():
            if medium.available():
                self.engine.add_media(medium, force=True)
                count += 1
        return count

    def close(self):
        self.finish_directory()
        if self.idle is not None:
            GLib.source_remove(self.idle)
            self.idle = None
        for monitor in self.monitors.values():
            monitor.cancel()
        for signal in self.signals:
            self.volumes.disconnect(signal)
        self.monitors.clear()
        self.tasks.clear()
        self.queued.clear()
        self.overflow.clear()
