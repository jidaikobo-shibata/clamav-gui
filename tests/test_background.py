from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from clam_gui.core import Store, Plan
from clam_gui.schedule import Schedule, save_schedule
from clam_gui.background import BackgroundService, Lease, enabled, heartbeat, put_preference, watched_plans, request_media, preference


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'data')
        self.run = Mock()
        self.service = BackgroundService(self.store, self.root/'units', self.run)

    def test_lease_excludes_second_owner_and_can_transfer(self):
        first, second = Lease(self.store), Lease(self.store)
        self.addCleanup(first.close); self.addCleanup(second.close)
        self.assertTrue(first.acquire())
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.close()
        self.assertTrue(second.acquire())

    def test_service_enable_disable_and_scope(self):
        self.service.apply(True)
        self.assertTrue(enabled(self.store))
        text = self.service.path.read_text()
        self.assertIn('PartOf=graphical-session.target', text)
        self.assertIn('WantedBy=graphical-session.target', text)
        self.assertIn('--data-dir', text)
        self.assertNotIn('sudo', text)
        self.run.assert_any_call('enable', '--now', self.service.name)
        self.service.apply(False)
        self.assertFalse(enabled(self.store))
        self.run.assert_any_call('disable', '--now', self.service.name)

    def test_failure_restores_preference_and_owned_file(self):
        self.run.side_effect = lambda *args: (_ for _ in ()).throw(RuntimeError('failed')) if args[0]=='enable' else None
        with self.assertRaises(RuntimeError):
            self.service.apply(True)
        self.assertFalse(enabled(self.store))
        self.assertFalse(self.service.path.exists())

    def test_foreign_file_and_symlink_are_untouched(self):
        self.service.unit_dir.mkdir()
        self.service.path.write_text('human configuration')
        with self.assertRaises(ValueError):self.service.apply(True)
        self.assertEqual(self.service.path.read_text(), 'human configuration')
        self.service.path.unlink()
        target=self.root/'outside';target.write_text('keep')
        try:
            self.service.path.symlink_to(target)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable")
            raise
        with self.assertRaises(ValueError):self.service.apply(False)
        self.assertEqual(target.read_text(),'keep')
        self.run.assert_not_called()

    def test_names_separate_data_directories_and_quote_paths(self):
        other=BackgroundService(Store(self.root/'data with % and $'),self.root/'units',self.run)
        self.assertNotEqual(other.name,self.service.name)
        self.assertIn('%% and $$',other.unit())

    def test_heartbeat_expiry_and_request_does_not_spawn_process(self):
        self.assertFalse(heartbeat(self.store))
        put_preference(self.store,'background-state',dict(updated=time.time(),statuses={'p':'running'}))
        self.assertEqual(heartbeat(self.store)['statuses']['p'],'running')
        put_preference(self.store,'background-state',dict(updated=time.time()-30))
        self.assertFalse(heartbeat(self.store))
        token=request_media(self.store)
        self.assertEqual(preference(self.store,'background-command')['token'],token)

    def test_watches_use_only_enabled_unprivileged_plans_and_exclude_internal_files(self):
        plan=Plan('watch',[str(self.root)],[],kind='watch')
        key=self.store.save_profile(plan)
        self.assertFalse(watched_plans(self.store))
        save_schedule(self.store,key,Schedule(enabled=True))
        watches=watched_plans(self.store)
        self.assertEqual(watches[0][0],key)
        self.assertIn(str(self.store.path.parent),watches[0][1].excluded_dirs)


try:
    from gi.repository import GLib
    from clam_gui.monitor import Monitor, Checkpoints
    from clam_gui.automation import Automation
    HAS_GIO=True
except ImportError:
    HAS_GIO=False


@unittest.skipUnless(HAS_GIO,'GIO is not installed')
class CheckpointTests(unittest.TestCase):
    setUp = BackgroundTests.setUp
    def test_first_baseline_then_changes_while_stopped_and_scanned_checkpoint(self):
        folder=self.root/'folder';folder.mkdir()
        file=folder/'file';file.write_text('first')
        key=self.store.save_profile(Plan('watch',[str(folder)],[],kind='watch'))
        plans=self.store.profiles()
        engine=Automation(self.store,lambda callback:callback(),lambda *args:None)
        engine.configure(plans,False)
        checkpoints=Checkpoints(self.store,engine)
        checkpoints.configure(plans)
        checkpoints.inventory(key,str(file))
        self.assertFalse(engine.queue.pending)
        checkpoints.complete_baseline({(key,str(folder)):None})
        file.write_text('changed while logged out')
        later=Checkpoints(self.store,engine)
        later.configure(plans);later.inventory(key,str(file))
        self.assertIn((key,str(file)),engine.queue.pending)
        from clam_gui.automation import signature
        engine.queue.completed[key,str(file)]=signature(file)
        later.scanned();engine.queue.pending.clear()
        later.inventory(key,str(file));self.assertFalse(engine.queue.pending)

    def test_settings_change_creates_new_baseline(self):
        from dataclasses import replace
        folder=self.root/'folder';folder.mkdir()
        file=folder/'file';file.touch()
        engine=Automation(self.store,lambda cb:cb(),lambda *args:None)
        plan=Plan('watch',[str(folder)],[],kind='watch')
        engine.configure([('p',plan)],False)
        cp=Checkpoints(self.store,engine);cp.configure([('p',plan)])
        cp.inventory('p',str(file));cp.complete_baseline({('p',str(folder)):None})
        cp.configure([('p',replace(plan,watch_recursive=False))])
        self.assertNotIn('p',cp.ready)

    def test_callback_failure_stops_and_exposes_service_failure(self):
        class Source:
            def __init__(self,engine): pass
            def close(self): pass
        monitor=Monitor(self.store,observers=Source,notifier=lambda *args:None)
        monitor.tick=Mock(side_effect=OSError('synthetic failure'))
        with patch('clam_gui.monitor.GLib.timeout_add'):
            self.assertFalse(monitor.checked_tick())
        self.assertTrue(monitor.stopping)
        self.assertIsInstance(monitor.failure,OSError)
        self.assertFalse(monitor.wait_for_stop())

    def test_resume_uses_wall_clock_and_rechecks_connected_media(self):
        class Source:
            def __init__(self,engine): self.tasks=[];self.monitors={};self.plans={};self.count=0
            def configure(self,plans): self.plans=dict(plans)
            def scan_connected(self): self.count+=1;return 1
            def close(self): pass
        monitor=Monitor(self.store,observers=Source,notifier=lambda *args:None)
        self.addCleanup(monitor.lease.close)
        put_preference(self.store,'background-enabled',True)
        values=self.store.automation_settings();values['media_enabled']=True
        self.store.set_automation_settings(values)
        monitor.tick()
        self.assertEqual(monitor.observers.count,1)
        monitor.last_tick-=3600
        monitor.tick()
        self.assertEqual(monitor.observers.count,2)

    def test_checkpoint_limit_is_bounded_and_reports_incomplete_coverage(self):
        from unittest.mock import Mock
        folder=self.root/'folder';folder.mkdir()
        one=folder/'one';one.touch()
        two=folder/'two';two.touch()
        engine=Automation(self.store,lambda cb:cb(),lambda *args:None)
        plan=Plan('watch',[str(folder)],[],kind='watch');engine.configure([('p',plan)],False)
        engine.warn=Mock()
        cp=Checkpoints(self.store,engine);cp.LIMIT=1;cp.configure([('p',plan)])
        cp.inventory('p',str(one));cp.inventory('p',str(two))
        engine.warn.assert_called_once()
        cp.flush()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM monitor_files').fetchone()[0],1)

    def test_monitor_consumes_commands_and_yields_when_another_owner_exists(self):
        class Source:
            def __init__(self,engine): self.tasks=[];self.monitors={};self.plans={};self.count=0
            def configure(self,plans): self.plans=dict(plans)
            def scan_connected(self): self.count+=1;return 2
            def close(self): pass
        monitor=Monitor(self.store,observers=Source,notifier=lambda *args:None)
        self.addCleanup(monitor.lease.close)
        put_preference(self.store,'background-enabled',True)
        lease=Lease(self.store);self.addCleanup(lease.close);lease.acquire()
        self.assertTrue(monitor.tick());self.assertFalse(heartbeat(self.store))
        lease.close()
        token=request_media(self.store)
        monitor.tick();monitor.tick()
        self.assertEqual(monitor.observers.count,1)
        self.assertEqual(preference(self.store,'background-command-result')['token'],token)
        self.assertTrue(heartbeat(self.store))
        monitor.stop();self.assertFalse(monitor.tick())
