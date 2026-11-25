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
    def __init__(self, engine, volumes=None, limit=1024, clock=time.monotonic):
        self.inventory = None
        self.engine = engine
        self.limit = limit
        self.clock = clock
        self.retry = {}
        self.plans = {}
        self.monitors = {}
        self.tasks = deque()
        self.queued = set()
        self.overflow = set()
        self.capacity_pending = {}
        self.recovering = {}
        self.recovery_new = set()
        self.idle = None
        self.current = None
        self.active_key = None
        self.mounts = {}
        self.observe_volumes(volumes)

    def observe_volumes(self, volumes):
        self.volumes = volumes if volumes is not None else Gio.VolumeMonitor.get()
        self.signals = [self.volumes.connect('mount-added', self.mount_added),
                        self.volumes.connect('mount-removed', self.mount_removed)]
        # Existing mounts are remembered, but not automatically scanned on enable/startup.
        for mount in self.volumes.get_mounts():
            self.remember_mount(mount, scan=False)

    def discard_plan(self, key):
        for ident in list(self.monitors):
            if ident[0] == key:
                self.monitors.pop(ident).cancel()
        if self.current and self.current[0] == key:
            self.finish_directory()
        self.tasks = deque(task for task in self.tasks if task[0] != key)
        self.queued = {ident for ident in self.queued if ident[0] != key}
        self.overflow.discard(key)
        self.retry.pop(key, None)
        self.capacity_pending.pop(key, None)
        self.recovering.pop(key, None)
        self.recovery_new = {ident for ident in self.recovery_new if ident[0] != key}

    def configure(self, plans):
        plans = dict(plans)
        known = set(self.plans) | {key for key, _ in self.monitors} | set(self.retry)
        changed = {key for key, plan in plans.items() if self.plans.get(key) != plan}
        for key in changed | (known - plans.keys()):
            self.discard_plan(key)
        self.plans = plans
        now = self.clock()
        for key, plan in plans.items():
            if key in changed:
                self.engine.errors.pop(key, None)
                self.retry[key] = now + 30, 30
                self.enqueue(key, plan.targets[0], False)
                continue
            root = key, str(plan.targets[0])
            if key in self.capacity_pending:
                # One marker per plan bounds recovery memory. Re-enumerate the
                # current scope once capacity is free, retaining existing watches.
                if len(self.monitors) < self.limit and not any(ident[0] == key for ident in self.queued):
                    previous = self.capacity_pending.pop(key)
                    self.recovering[key] = previous
                    self.retry.pop(key, None)
                    if not self.enqueue(key, plan.targets[0], False):
                        self.recovering.pop(key, None)
                        self.capacity_pending[key] = previous
                continue
            if key in self.recovering:
                continue
            if root in self.monitors or root in self.queued:
                continue
            due, delay = self.retry.get(key, (now, 15))
            if now >= due:
                # A missing root must not tear down any other plan's monitors.
                self.discard_plan(key)
                delay = min(300, delay * 2)
                self.retry[key] = now + delay, delay
                self.enqueue(key, plan.targets[0], False)

    def capacity_message(self):
        return f'監視フォルダーまたは監視準備待ちフォルダーの数が上限{self.limit}に達しました。空きができたら自動で再試行します。フォルダー全体の検査を併用してください。'

    def enqueue(self, key, path, scan_files):
        ident = key, str(path)
        if ident not in self.queued:
            if len(self.queued) >= self.limit:
                plan = self.plans.get(key)
                if plan:
                    message = self.capacity_message()
                    self.capacity_pending[key] = message
                    if key not in self.overflow:
                        self.overflow.add(key)
                        self.engine.warn(key, plan, message)
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
            self.recovery_new.discard((key, directory))
            self.current = None

    def finish_recoveries(self):
        for key, previous in list(self.recovering.items()):
            if any(ident[0] == key for ident in self.queued):
                continue
            self.recovering.pop(key)
            # Preserve both a repeated capacity failure and unrelated warnings.
            if key not in self.capacity_pending:
                self.overflow.discard(key)
                if self.engine.errors.get(key) == previous:
                    self.engine.errors.pop(key, None)

    def drain(self):
        try:
            return self._drain()
        except Exception as error:
            # GLib removes a failed source. Reset our handle and isolate the
            # affected plan so configure() can rebuild it after a backoff.
            key = self.current[0] if self.current else self.active_key
            self.idle = None
            if key in self.plans:
                plan = self.plans[key]
                delay = min(300, self.retry.get(key, (0, 15))[1] * 2)
                self.discard_plan(key)
                self.retry[key] = self.clock() + delay, delay
                message = '監視準備に失敗しました。自動で再試行します：' + str(error)
                self.engine.errors[key] = message
                try:
                    self.engine.warn(key, plan, message)
                except Exception:
                    pass
            if self.tasks:
                self.idle = GLib.idle_add(self.drain)
            return False

    def _drain(self):
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
                                if scan_files or (key, directory) in self.recovery_new:
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
                self.finish_recoveries()
                self.idle = None
                return False
            key, directory, scan_files = self.tasks.popleft()
            self.active_key = key
            self.queued.discard((key, directory))
            plan = self.plans.get(key)
            if not plan:
                continue
            if not allowed_path(plan, directory) or not Path(directory).is_dir():
                if directory == str(plan.targets[0]):
                    self.engine.warn(key, plan, '監視フォルダーが見つからないか読み取れません。自動で再試行します。')
                continue
            if (key, directory) not in self.monitors:
                if len(self.monitors) >= self.limit:
                    message = self.capacity_message()
                    self.capacity_pending[key] = message
                    self.engine.warn(key, plan, message)
                    continue
                try:
                    monitor = Gio.File.new_for_path(directory).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
                    monitor.connect('changed', self.event, key)
                    self.monitors[key, directory] = monitor
                    if key in self.recovering:
                        self.recovery_new.add((key, directory))
                except GLib.Error as error:
                    self.engine.warn(key, plan, 'フォルダーを監視できません：' + str(error))
                    continue
            try:
                self.current = key, directory, scan_files, os.scandir(directory), 0
                self.queued.add((key, directory))
                if directory == str(plan.targets[0]) and key in self.retry:
                    # Clear the missing-root error only after reopening succeeds.
                    self.engine.errors.pop(key, None)
                    self.retry.pop(key, None)
            except OSError as error:
                monitor = self.monitors.pop((key, directory), None)
                self.recovery_new.discard((key, directory))
                if monitor:
                    monitor.cancel()
                self.engine.warn(key, plan, '監視フォルダーを読み取れません：' + str(error))
            if time.monotonic() >= deadline:
                return True
        return True

    def discard_subtree(self, key, path):
        self.engine.forget_subtree(key, path)
        def inside(directory):
            return Path(directory) == Path(path) or Path(path) in Path(directory).parents
        for ident in list(self.monitors):
            if ident[0] == key and inside(ident[1]):
                self.monitors.pop(ident).cancel()
                self.recovery_new.discard(ident)
        if self.current and self.current[0] == key and inside(self.current[1]):
            self.finish_directory()
        self.tasks = deque(task for task in self.tasks if not (task[0] == key and inside(task[1])))
        self.queued = {ident for ident in self.queued if not (ident[0] == key and inside(ident[1]))}

    def event(self, monitor, file, other, event, key):
        plan = self.plans.get(key)
        if not plan:
            return
        if event in (Gio.FileMonitorEvent.DELETED, Gio.FileMonitorEvent.MOVED_OUT):
            path = file.get_path()
            if path:
                self.discard_subtree(key, path)
            if path == plan.targets[0]:
                self.engine.warn(key, plan, '監視フォルダーが見つかりません。自動で再試行します。')
            return
        if event not in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.CHANGED,
                         Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.RENAMED,
                         Gio.FileMonitorEvent.MOVED_IN):
            return
        if event == Gio.FileMonitorEvent.RENAMED and file.get_path():
            self.discard_subtree(key, file.get_path())
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
        self.retry.clear()
        self.capacity_pending.clear()
        self.recovering.clear()
        self.recovery_new.clear()
