from dataclasses import replace
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
try:
    from gi.repository import GLib
    from clam_gui.gio_watch import Observers, removable_mount
    HAS_GIO = True
except ImportError:
    HAS_GIO = False
from clam_gui.core import Store, Plan
from clam_gui.automation import Automation


class Volumes:
    def __init__(self):self.mounts=[]
    def get_mounts(self):return self.mounts
    def connect(self,*args):return len(args)
    def disconnect(self,*args):pass


@unittest.skipUnless(HAS_GIO, 'GIO is not installed')
class GioWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'watched';self.folder.mkdir()
        self.store=Store(self.root/'data')
        self.plan=Plan('watch',[str(self.folder)],[],kind='watch')
        self.engine=Automation(self.store,lambda callback:callback(),lambda *args:None)
        self.volumes=Volumes()
        self.source=Observers(self.engine,self.volumes)
        self.addCleanup(self.source.close)

    def wait(self,predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline:
            GLib.MainContext.default().iteration(False);time.sleep(.001)
        self.assertTrue(predicate())

    def mount(self,path=None,removable=True,native=True):
        path=str(path or self.folder)
        drive=SimpleNamespace(is_removable=lambda:removable,is_media_removable=lambda:removable)
        root=SimpleNamespace(is_native=lambda:native,get_path=lambda:path,get_uri=lambda:'file://'+path)
        return SimpleNamespace(get_root=lambda:root,get_name=lambda:'Synthetic USB',get_volume=lambda:SimpleNamespace(get_drive=lambda:drive))

    def test_only_removable_native_data_mounts_are_accepted(self):
        self.assertIsNotNone(removable_mount(self.mount()))
        self.assertIsNone(removable_mount(self.mount(removable=False)))
        self.assertIsNone(removable_mount(self.mount(native=False)))
        self.assertIsNone(removable_mount(self.mount(path='/')))

    def test_existing_mounts_wait_for_manual_request_and_reconnect_gets_new_identity(self):
        mount=self.mount();self.volumes.mounts=[mount]
        self.engine.configure([],True)
        self.source.remember_mount(mount,scan=False)
        self.assertFalse(self.engine.media_pending)
        self.assertEqual(self.source.scan_connected(),1)
        token=self.engine.media_pending[0]
        self.source.mount_added(None,mount)
        self.assertEqual(len(self.engine.media_pending),1)
        self.source.mount_removed(None,mount)
        self.assertFalse(self.engine.media_pending)
        self.source.mount_added(None,mount)
        self.assertNotEqual(token,self.engine.media_pending[0])

    def test_real_directory_events_ignore_partial_and_detect_final_rename(self):
        existing=self.folder/'old';existing.touch()
        self.engine.configure([('watch',self.plan)],False)
        self.source.configure([('watch',self.plan)])
        self.wait(lambda:self.source.idle is None)
        self.assertFalse(self.engine.queue.pending)
        temporary=self.folder/'new.crdownload';temporary.write_text('synthetic data')
        temporary.rename(self.folder/'new.txt')
        self.wait(lambda:('watch',str(self.folder/'new.txt')) in self.engine.queue.pending)
        self.assertFalse(any(key[1].endswith('.crdownload') for key in self.engine.queue.pending))

    def test_recursive_setup_excludes_subtree_and_does_not_follow_symlink(self):
        (self.folder/'child').mkdir();(self.folder/'excluded').mkdir()
        (self.folder/'link').symlink_to(self.root/'data',target_is_directory=True)
        plan=replace(self.plan,excluded_dirs=[str(self.folder/'excluded')])
        self.engine.configure([('watch',plan)],False);self.source.configure([('watch',plan)])
        self.wait(lambda:self.source.idle is None)
        self.assertEqual(len(self.source.monitors),2)

    def test_missing_root_is_reported_and_can_be_retried(self):
        missing=self.folder/'missing';plan=replace(self.plan,targets=[str(missing)])
        self.engine.configure([('watch',plan)],False);self.source.configure([('watch',plan)])
        self.wait(lambda:self.source.idle is None)
        self.assertIn('watch',self.engine.errors)
        missing.mkdir()
        self.source.configure([('watch',plan)])
        self.wait(lambda:self.source.idle is None)
        self.assertIn(('watch',str(missing)),self.source.monitors)
        self.assertNotIn('watch',self.engine.errors)

    def test_monitor_cap_reports_incomplete_coverage(self):
        (self.folder/'child').mkdir();self.source.limit=1
        self.engine.configure([('watch',self.plan)],False);self.source.configure([('watch',self.plan)])
        self.wait(lambda:self.source.idle is None)
        self.assertEqual(len(self.source.monitors),1)
        self.assertIn('上限',self.engine.errors['watch'])
