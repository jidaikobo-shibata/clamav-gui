"""Whole-app audit: retry isolation, resolved scope and monitoring alerts."""
from dataclasses import asdict, replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from clam_gui.core import Store, Plan
from clam_gui.automation import Automation, allowed_path
from clam_gui.background import watched_plans
from clam_gui.schedule import Schedule, save_schedule
from clam_gui.watch_scope import safe_watch_plan


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.folder=self.root/'watched';self.folder.mkdir()
        self.plan=Plan('watch',[str(self.folder)],[],kind='watch')
        self.store=Store(self.root/'data')
        self.now=0

    def observers(self, plans):
        from clam_gui.gio_watch import Observers
        volumes=Mock();volumes.get_mounts.return_value=[]
        self.notices=[]
        engine=Automation(self.store,lambda cb:cb(),lambda *args:self.notices.append(args),clock=lambda:self.now)
        engine.configure(plans,False)
        source=Observers(engine,volumes,clock=lambda:self.now)
        self.addCleanup(source.close)
        return engine,source

    def drain(self, source):
        for _ in range(100):
            if not source.drain():return
        self.fail('discovery did not finish')

    def test_missing_root_retries_without_cancelling_healthy_monitors(self):
        missing=self.root/'missing'
        plans=[('good',self.plan),('bad',replace(self.plan,targets=[str(missing)]))]
        engine,source=self.observers(plans)
        with patch('clam_gui.gio_watch.GLib.idle_add',return_value=1), \
             patch('clam_gui.gio_watch.Gio.File.new_for_path') as gio:
            good_monitor=Mock();gio.return_value.monitor_directory.return_value=good_monitor
            source.configure(plans);self.drain(source)
            self.assertIn('bad',engine.errors)
            for self.now in (2,4,10,29):
                source.configure(plans)
                self.assertFalse(source.tasks)
            good_monitor.cancel.assert_not_called()
            for self.now, due in ((30,90),(90,210),(210,450),(450,750)):
                source.configure(plans);self.drain(source)
                self.assertEqual(source.retry['bad'][0],due)
                good_monitor.cancel.assert_not_called()
            missing.mkdir();self.now=750
            source.configure(plans);self.drain(source)
            self.assertIn(('bad',str(missing)),source.monitors)
            self.assertNotIn('bad',engine.errors)
            self.assertNotIn('bad',source.retry)
            good_monitor.cancel.assert_not_called()

    def test_in_progress_discovery_and_unrelated_configuration_are_preserved(self):
        for index in range(300):(self.folder/str(index)).touch()
        missing=self.root/'missing'
        plans=[('good',self.plan),('bad',replace(self.plan,targets=[str(missing)]))]
        engine,source=self.observers(plans)
        with patch('clam_gui.gio_watch.GLib.idle_add',return_value=1):
            source.configure(plans);source.drain()
            self.assertIsNotNone(source.current)
            current=source.current
            self.now=2;source.configure(plans)
            self.assertIs(source.current,current)
            changed=[plans[0],('bad',replace(plans[1][1],watch_recursive=False))]
            engine.configure(changed,False);source.configure(changed)
            self.assertIs(source.current,current)
            self.assertIn(('good',str(self.folder)),source.monitors)
            source.configure([plans[0]])
            self.assertIs(source.current,current)
            self.assertNotIn('bad',source.retry)
            self.drain(source)
            source.configure([])
            self.assertFalse(source.monitors);self.assertFalse(source.tasks)
            source.idle=None

    def alias(self, destination):
        alias=self.root/'alias'
        try:alias.symlink_to(destination,target_is_directory=True)
        except OSError as error:self.skipTest('symlink unavailable: '+str(error))
        return alias

    def test_alias_of_private_directory_is_rejected_on_save_and_legacy_load(self):
        alias=self.alias(self.root)
        unsafe=replace(self.plan,targets=[str(alias/'data')])
        with self.assertRaises(ValueError):self.store.save_profile(unsafe)
        data=asdict(unsafe);label=data.pop('label')
        with self.store.connect() as db:
            db.execute('INSERT INTO profiles VALUES (?,?,?)',('legacy',label,json.dumps(data)))
        save_schedule(self.store,'legacy',Schedule(enabled=True))
        self.assertEqual(watched_plans(self.store),[])

    def test_private_descendant_exclusion_is_projected_through_alias(self):
        alias=self.alias(self.root)
        store=Store(self.folder/'private')
        (self.folder/'ordinary').touch()
        plan=replace(self.plan,targets=[str(alias/'watched')])
        key=store.save_profile(plan);save_schedule(store,key,Schedule(enabled=True))
        effective=dict(watched_plans(store))[key]
        self.assertIn(str(alias/'watched'/'private'),effective.excluded_dirs)
        self.assertFalse(allowed_path(effective,alias/'watched'/'private'/'history.sqlite3'))
        self.assertTrue(allowed_path(effective,alias/'watched'/'ordinary'))
        self.assertEqual(dict(store.profiles())[key].excluded_dirs,[])

    def test_lock_directory_alias_and_resolved_data_alias_are_protected(self):
        locks=self.root/'.clamdlog';locks.mkdir()
        alias=self.alias(self.root)
        with patch('pathlib.Path.home',return_value=self.root):
            with self.assertRaises(ValueError):
                safe_watch_plan(replace(self.plan,targets=[str(alias/'.clamdlog')]),self.store.path.parent)
            effective=safe_watch_plan(replace(self.plan,targets=[str(alias)]),self.store.path.parent)
            self.assertIn(str(alias/'.clamdlog'),effective.excluded_dirs)
        # Also protect a data path spelled through the alias when the root is physical.
        effective=safe_watch_plan(replace(self.plan,targets=[str(self.root)]),alias/'data')
        self.assertIn(str(self.root/'data'),effective.excluded_dirs)

    def test_monitor_warning_uses_durable_critical_notification_and_throttles(self):
        from clam_gui.monitor import Monitor
        class Source:
            def __init__(self,engine):self.tasks=[];self.monitors={};self.plans={}
            def close(self):pass
        deliver=Mock();monitor=Monitor(self.store,observers=Source,notifier=deliver)
        self.addCleanup(monitor.outbox.close);self.addCleanup(monitor.lease.close)
        monitor.engine.clock=lambda:self.now
        monitor.engine.configure([('p',self.plan)],False)
        monitor.engine.warn('p',self.plan,'synthetic coverage failure')
        for _ in range(5):monitor.engine.warn('p',self.plan,'synthetic coverage failure')
        self.assertEqual(len(self.store.recent()),1)
        self.assertEqual(monitor.outbox.state()['pending'],1)
        self.assertTrue(monitor.outbox.deliver_one())
        self.assertEqual(deliver.call_args.args[2],'critical')
        self.assertIn('synthetic coverage failure',deliver.call_args.args[1])
        self.now=30;monitor.engine.warn('p',self.plan,'synthetic coverage failure')
        self.assertEqual(monitor.outbox.state()['pending'],1)
        monitor.stop()

    def test_warning_is_not_lost_when_history_cannot_be_saved(self):
        finished=Mock()
        engine=Automation(self.store,lambda cb:cb(),finished,clock=lambda:self.now)
        with patch.object(self.store,'save',side_effect=OSError('synthetic storage failure')):
            engine.warn('p',self.plan,'synthetic monitor failure')
        finished.assert_called_once()
        self.assertIn('履歴も保存できませんでした',finished.call_args.args[1])
        self.assertEqual(finished.call_args.args[1].assessment.kind.value,'incomplete')
