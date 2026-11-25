"""Real process/SQLite/flock coordination; the scanner only touches test fixtures."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from clam_gui.core import Store, Plan
from clam_gui.execution import FULL, EVENT, ScanAlreadyQueued, ScanQueueFull, lane_for
from clam_gui.runtime import track_scan, activities, request_stop
from clam_gui.schedule import Schedule, save_schedule

CHILD = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
from clam_gui.core import Store, scan
from clam_gui.options import DEFAULTS, LIMITS
from clam_gui.execution import ScanAlreadyQueued
root, key, mode = sys.argv[1:]
os.environ['HOME'] = root
store = Store(Path(root)/'data')
plan = dict(store.profiles())[key]
if mode=='media':
    from dataclasses import replace
    plan=replace(plan,kind='media')
with patch('clam_gui.core.executable',return_value=str(Path(root)/'scanner')), patch('clam_gui.core.supported_options',return_value=set(DEFAULTS)|set(LIMITS)), patch('clam_gui.definitions.daily_definition_age', return_value=0):
    if mode == 'scheduled':
        from scripts.run_scheduled import main
        sys.argv=['runner','--data-dir',str(Path(root)/'data'),'--profile',key]
        with patch('clam_gui.platforms.linux.LinuxBackend.notify'), patch('clam_gui.definitions.DefinitionUpdater.update', return_value=('test update', '')):
            raise SystemExit(main())
    try:
        status, output = scan(plan, store, profile_id=key, wait=True,
                              files=[str(Path(plan.targets[0])/'payload')] if mode=='event' else None)
        print(status, flush=True)
    except ScanAlreadyQueued as error:
        print('coalesced: '+str(error), flush=True)
'''


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process/lock integration')
class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('started','release','targets'):(self.root/name).mkdir()
        self.store=Store(self.root/'data')
        self.keys={}
        for name in ('a','b','c','event','media'):
            target=self.root/'targets'/name;target.mkdir();(target/'payload').touch()
            self.keys[name]=self.store.save_profile(Plan(name,[str(target)],[]))
            save_schedule(self.store,self.keys[name],Schedule(enabled=True))
        scanner=self.root/'scanner'
        scanner.write_text('#!'+sys.executable+'\n'+'''import os, sys, time
from pathlib import Path
root=Path(__file__).parent
path=Path(sys.argv[-1]);tag=path.parent.name if path.is_file() else path.name
(root/'started'/tag).write_text(str(os.getpid()))
deadline=time.monotonic()+10
while not (root/'release'/tag).exists() and time.monotonic()<deadline:time.sleep(.01)
print('Scanned files: 1\\nInfected files: 0\\nTotal errors: 0')
''')
        scanner.chmod(0o700)
        self.children=[];self.addCleanup(self.stop_children)

    def stop_children(self):
        for name in self.keys:(self.root/'release'/name).touch()
        for child in self.children:
            try:child.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                child.terminate();child.communicate(timeout=4)

    def start(self, name, mode='full'):
        child=subprocess.Popen([sys.executable,'-c',CHILD,str(self.root),self.keys[name],mode],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(child)
        return child

    def wait(self, predicate):
        deadline=time.monotonic()+5
        while not predicate() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(predicate())

    def started(self,name):return (self.root/'started'/name).exists()
    def release(self,name):(self.root/'release'/name).touch()
    def waiting(self,name):
        return any(a.profile_id==self.keys[name] and a.phase=='waiting' for a in activities(self.store))
    def result(self,child):
        output,error=child.communicate(timeout=4)
        self.assertEqual(child.returncode,0,error)
        return output

    def test_scanner_is_killed_when_runner_dies_and_next_scan_can_start(self):
        from clam_gui.runtime import process_identity
        first=self.start('a');self.wait(lambda:self.started('a'))
        scanner_pid=int((self.root/'started/a').read_text());identity=process_identity(scanner_pid)
        self.assertTrue(identity)
        first.kill();first.communicate(timeout=3)
        self.wait(lambda:process_identity(scanner_pid)!=identity)
        second=self.start('b');self.wait(lambda:self.started('b'))
        self.assertNotEqual(process_identity(scanner_pid),identity)
        self.release('b');self.result(second)

    def test_inherited_lock_lasts_until_scanner_exits(self):
        from clam_gui.platforms import get_backend
        backend=get_backend();path=self.root/'owned.lock'
        first=backend.lock(path);second=backend.lock(path)
        self.addCleanup(first.close);self.addCleanup(second.close)
        self.assertTrue(first.acquire())
        child=backend.spawn_scanner([str(self.root/'scanner'),str(self.root/'targets/a')],lock=first,
                                    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        self.children.append(child);self.wait(lambda:self.started('a'))
        first.close();self.assertFalse(second.acquire())
        self.release('a');child.communicate(timeout=3)
        self.assertEqual(child.returncode,0);self.assertTrue(second.acquire())

    def test_guard_refuses_to_execute_if_parent_already_disappeared(self):
        from clam_gui.platforms import get_backend
        import clam_gui.platforms.scanner_guard as guard
        lock=get_backend().lock(self.root/'guard.lock');self.addCleanup(lock.close);lock.acquire()
        child=subprocess.run([sys.executable,guard.__file__,'--parent',str(os.getpid()+10000000),
                              '--lock-fd',str(lock.file.fileno()),'--',str(self.root/'scanner'),str(self.root/'targets/a')],
                             pass_fds=(lock.file.fileno(),),capture_output=True,timeout=3)
        self.assertEqual(child.returncode,125)
        self.assertFalse(self.started('a'))

    def test_full_scans_are_serial_but_event_scan_runs_in_parallel(self):
        first=self.start('a');self.wait(lambda:self.started('a'))
        second=self.start('b');self.wait(lambda:self.waiting('b'))
        event=self.start('event','event');self.wait(lambda:self.started('event'))
        self.assertFalse(self.started('b'))
        self.assertEqual(len([a for a in activities(self.store) if a.phase=='running']),2)
        self.release('a');self.wait(lambda:self.started('b'))
        self.assertIsNone(event.poll())
        self.release('b');self.release('event')
        for child in (first,second,event):self.assertIn('検査完了',self.result(child))
        self.assertEqual(len(self.store.recent()),3)

    def test_waiting_requests_run_in_registration_order(self):
        first=self.start('a');self.wait(lambda:self.started('a'))
        second=self.start('b');self.wait(lambda:self.waiting('b'))
        third=self.start('c');self.wait(lambda:self.waiting('c'))
        self.release('a');self.wait(lambda:self.started('b'))
        self.assertFalse(self.started('c'))
        self.release('b');self.wait(lambda:self.started('c'));self.release('c')
        for child in (first,second,third):self.result(child)

    def test_event_lane_is_serial_independently_of_full_lane(self):
        first=self.start('event','event');self.wait(lambda:self.started('event'))
        second=self.start('media','media');self.wait(lambda:self.waiting('media'))
        self.assertFalse(self.started('media'))
        self.release('event');self.wait(lambda:self.started('media'));self.release('media')
        for child in (first,second):self.result(child)

    def test_scheduled_duplicate_is_coalesced_without_failure_history(self):
        first=self.start('a');self.wait(lambda:self.started('a'))
        duplicate=self.start('a','scheduled')
        self.assertIn('すでに',self.result(duplicate))
        self.assertFalse(self.store.recent())
        self.release('a');self.result(first)
        self.assertEqual(len(self.store.recent()),1)

    def test_waiting_request_can_be_cancelled_without_starting_scanner(self):
        first=self.start('a');self.wait(lambda:self.started('a'))
        second=self.start('b');self.wait(lambda:self.waiting('b'))
        state=next(a for a in activities(self.store) if a.profile_id==self.keys['b'])
        self.assertIn('検査待ち',state.describe())
        self.assertTrue(request_stop(self.store,state.token))
        self.assertIn('取り消し',self.result(second))
        self.assertFalse(self.started('b'))
        self.assertFalse(any(a.profile_id==self.keys['b'] for a in activities(self.store)))
        self.release('a');self.result(first)

    def test_disabling_schedule_cancels_waiting_scheduled_job(self):
        first=self.start('a');self.wait(lambda:self.started('a'))
        second=self.start('b','scheduled');self.wait(lambda:self.waiting('b'))
        save_schedule(self.store,self.keys['b'],Schedule(enabled=False))
        output,error=second.communicate(timeout=4)
        self.assertEqual(second.returncode,2,error)
        self.assertIn('予定が無効',output)
        self.assertFalse(self.started('b'))
        self.release('a');self.result(first)

    def test_registration_is_bounded_and_allows_same_plan_in_other_lane(self):
        with ExitStack() as stack:
            stack.enter_context(track_scan(self.store,'same','now',lane=FULL,deduplicate=True))
            with self.assertRaises(ScanAlreadyQueued):
                stack.enter_context(track_scan(self.store,'same','now',lane=FULL,deduplicate=True))
            stack.enter_context(track_scan(self.store,'same','now',lane=EVENT,deduplicate=True))
            for i in range(31):stack.enter_context(track_scan(self.store,str(i),'now',lane=FULL,deduplicate=True))
            with self.assertRaises(ScanQueueFull):
                stack.enter_context(track_scan(self.store,'overflow','now',lane=FULL,deduplicate=True))
        self.assertFalse(activities(self.store))

    def test_legacy_six_column_records_remain_compatible(self):
        from clam_gui.runtime import process_identity
        with self.store.connect() as db:
            db.execute('INSERT INTO scan_runs VALUES (?,?,?,?,?,0)',
                       ('legacy','old-plan',os.getpid(),process_identity(os.getpid()),'2026-11-18T00:00:00+00:00'))
        legacy=activities(self.store)[0]
        self.assertEqual((legacy.phase,legacy.lane),('running',FULL))
        with track_scan(self.store,'new-plan','now',phase='waiting',lane=FULL) as pending:
            self.assertFalse(pending.is_next())
        with self.store.connect() as db:
            db.execute('DELETE FROM scan_runs WHERE token=?',('legacy',))

    def test_manual_full_scan_of_watch_plan_uses_full_lane(self):
        plan=Plan('watch',[str(self.root/'targets/a')],[],kind='watch')
        self.assertEqual(lane_for(plan),FULL)
        self.assertEqual(lane_for(plan,['payload']),EVENT)
        self.assertEqual(lane_for(Plan('media',[str(self.root)],[],kind='media')),EVENT)
