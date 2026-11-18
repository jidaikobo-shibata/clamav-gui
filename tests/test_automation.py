from dataclasses import replace
import datetime
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from clam_gui.core import Store, Plan, scan, ScanBusyError
from clam_gui.automation import WatchQueue, Automation, Media, allowed_file, MEDIA_ID, media_plan
from clam_gui.schedule import Schedule, Scheduler, save_schedule, read_schedule
from clam_gui.options import DEFAULTS, LIMITS


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'data')
        self.folder = self.root/'watched'; self.folder.mkdir()
        self.plan = Plan('watch', [str(self.folder)], [], kind='watch')
        self.now = 0
        self.queue = WatchQueue(lambda: self.now)

    def file(self, name='file.txt', content='test'):
        path = self.folder/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_watch_kind_persists_and_old_scheduled_default_remains(self):
        key = self.store.save_profile(self.plan)
        self.assertEqual(dict(self.store.profiles())[key].kind, 'watch')
        ordinary = self.store.save_profile(Plan('ordinary', [str(self.folder)], []))
        self.assertEqual(dict(self.store.profiles())[ordinary].kind, 'scheduled')
        save_schedule(self.store, key, Schedule(enabled=True))
        clone = self.store.duplicate_profile(key)
        self.assertEqual(dict(self.store.profiles())[clone].kind, 'watch')
        self.assertFalse(read_schedule(self.store, clone).enabled)
        with self.assertRaises(ValueError): self.store.save_profile(media_plan())

    def test_watch_schedule_never_registers_a_timer_and_disables_existing_one(self):
        key = self.store.save_profile(replace(self.plan, kind='scheduled'))
        calls = []
        scheduler = Scheduler(self.store, self.root/'units', lambda *args: calls.append(args))
        scheduler.apply(key, Schedule(enabled=True))
        calls.clear()
        self.store.save_profile(self.plan, key)
        scheduler.apply(key, Schedule(enabled=True))
        self.assertTrue(read_schedule(self.store, key).enabled)
        self.assertEqual([call[0] for call in calls], ['disable'])
        calls.clear()
        fresh = self.store.save_profile(replace(self.plan, label='fresh'))
        scheduler.apply(fresh, Schedule(enabled=True))
        self.assertEqual(calls, [])
        self.assertFalse(scheduler.paths(fresh)[1].exists())

    def test_scheduled_runner_ignores_watch_profiles(self):
        from scripts.run_scheduled import main
        key = self.store.save_profile(self.plan)
        save_schedule(self.store, key, Schedule(enabled=True))
        with patch('sys.argv', ['runner','--data-dir',str(self.store.path.parent),'--profile',key]), patch('scripts.run_scheduled.scan') as scanner:
            self.assertEqual(main(), 0)
            scanner.assert_not_called()

    def test_quiet_time_waits_for_size_changes_and_deduplicates(self):
        path = self.file()
        self.queue.put('watch', self.plan, path)
        self.now = 2
        self.assertEqual(self.queue.take({'watch':self.plan})[1], [])
        path.write_text('longer content')
        self.now = 3
        self.assertEqual(self.queue.take({'watch':self.plan})[1], [])
        self.now = 6
        key, batch = self.queue.take({'watch':self.plan})
        self.assertEqual(key, 'watch'); self.assertEqual(len(batch), 1)
        self.queue.completed[key, str(path)] = batch[0].signature
        self.queue.put(key, self.plan, path)
        self.assertFalse(self.queue.pending)

    def test_partial_names_are_skipped_but_final_rename_is_scanned(self):
        path = self.file('browser.crdownload')
        self.queue.put('watch', self.plan, path)
        self.assertFalse(self.queue.pending)
        destination = path.with_name('finished.txt'); path.rename(destination)
        self.queue.put('watch', self.plan, destination)
        self.now = 4
        self.assertEqual(self.queue.take({'watch':self.plan})[1][0].path, str(destination))

    def test_exclusions_symlinks_and_nonrecursive_scope(self):
        path = self.file('child/file')
        self.assertFalse(allowed_file(replace(self.plan, watch_recursive=False), path))
        self.assertFalse(allowed_file(replace(self.plan, excluded_dirs=[str(path.parent)]), path))
        self.assertFalse(allowed_file(replace(self.plan, excludes=[str(path)]), path))
        link = self.folder/'link'; link.symlink_to(path)
        self.assertFalse(allowed_file(self.plan, link))
        outside = self.root/'outside'; outside.mkdir(); (outside/'file').touch()
        (self.folder/'escape').symlink_to(outside, target_is_directory=True)
        self.assertFalse(allowed_file(self.plan, self.folder/'escape/file'))

    def test_queue_bound_batch_limit_and_deleted_file(self):
        self.queue.limit = 55
        for index in range(56):
            result = self.queue.put('watch', self.plan, self.file(str(index)))
        self.assertFalse(result)
        self.file('0').unlink()
        self.now = 4
        key, batch = self.queue.take({'watch':self.plan})
        self.assertEqual(len(batch), 50)
        self.assertEqual(len(self.queue.pending), 4)
        self.queue.retain(set()); self.assertFalse(self.queue.pending)

    def engine(self, scanner):
        self.callbacks = []
        self.finished = []
        self.ready = threading.Event()
        def dispatch(callback):
            self.callbacks.append(callback); self.ready.set()
        return Automation(self.store, dispatch, lambda *args:self.finished.append(args), clock=lambda:self.now, scanner=scanner)

    def finish(self):
        self.assertTrue(self.ready.wait(3))
        self.callbacks.pop(0)()
        self.ready.clear()

    def test_busy_scan_is_requeued_without_failure_log(self):
        engine = self.engine(Mock(side_effect=ScanBusyError('busy')))
        engine.configure([('watch',self.plan)], False)
        engine.file_event('watch', self.file())
        self.now = 4
        with patch('shutil.which',return_value='/mock/clamscan'): engine.tick()
        self.finish()
        self.assertTrue(engine.queue.pending)
        self.assertEqual(self.store.recent(), [])

    def test_disabling_does_not_cancel_current_job(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        def scanner(plan, store, cancel, **kwargs):
            gate.wait(2)
            self.assertFalse(cancel.is_set())
            return '検査完了', ''
        engine = self.engine(scanner)
        engine.configure([('watch',self.plan)],False)
        engine.file_event('watch',self.file()); self.now=4
        with patch('shutil.which',return_value='/mock/clamscan'): engine.tick()
        self.assertTrue(engine.active)
        engine.configure([],False)
        self.assertFalse(engine.cancel.is_set())
        gate.set(); self.finish()
        self.assertEqual(len(self.finished),1)

    def test_media_duplicate_unplug_and_path_reuse(self):
        info=self.folder.stat()
        medium=Media('connection',str(self.folder),'USB',(info.st_dev,info.st_ino))
        engine=self.engine(Mock())
        engine.configure([],True)
        engine.add_media(medium);engine.add_media(medium)
        self.assertEqual(engine.media_pending,['connection'])
        engine.remove_media('connection')
        self.assertFalse(medium.available());self.assertFalse(engine.media_pending)
        engine.add_media(medium)
        self.assertFalse(engine.media_pending)

    def test_core_scans_only_selected_files_and_records_the_batch(self):
        path=self.file()
        def spawn(command,**kwargs):
            self.assertEqual(command[command.index('--')+1:],[str(path)])
            self.assertIn('--follow-file-symlinks=0',command)
            kwargs['stdout'].write(b'Scanned files: 1\nInfected files: 0\n')
            return Mock(wait=Mock(return_value=0))
        with patch('clam_gui.core.shutil.which',return_value='/mock/clamscan'),patch('clam_gui.core.supported_options',return_value=set(DEFAULTS)|set(LIMITS)),patch('clam_gui.platforms.linux.LinuxBackend.spawn_scanner',side_effect=spawn):
            status,output=scan(self.plan,self.store,lock_path=self.root/'lock',profile_id='watch',files=[str(path)])
        self.assertTrue(status.startswith('検査完了'))
        self.assertIn(str(path),output)
        self.assertEqual(len(self.store.recent()),1)

    def test_media_removal_during_scan_is_incomplete(self):
        alive=[True]
        waits=[0]
        def spawn(command,**kwargs):
            self.assertIn('--cross-fs=no',command)
            def wait(timeout=None):
                waits[0]+=1
                if waits[0]==1:
                    alive[0]=False
                    raise subprocess.TimeoutExpired(command,.2)
                return -15
            return Mock(wait=wait)
        with patch('clam_gui.core.shutil.which',return_value='/mock/clamscan'),patch('clam_gui.core.supported_options',return_value=set(DEFAULTS)|set(LIMITS)),patch('clam_gui.platforms.linux.LinuxBackend.spawn_scanner',side_effect=spawn):
            status,_=scan(replace(self.plan,kind='media'),self.store,lock_path=self.root/'lock',profile_id=MEDIA_ID,available=lambda:alive[0])
        self.assertIn('取り外されました',status)
        self.assertEqual(len(self.store.history_entries(MEDIA_ID,media_plan())),1)
