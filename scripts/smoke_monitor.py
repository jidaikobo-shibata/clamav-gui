"""Test a headless monitor subprocess using temporary data and a synthetic scanner."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clam_gui.core import Store, Plan
from clam_gui.schedule import Schedule, save_schedule
from clam_gui.background import put_preference, heartbeat

CODE = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from clam_gui.core import Store
from clam_gui.monitor import Monitor
from clam_gui.gio_watch import Observers
from unittest.mock import patch
class Volumes:
    def get_mounts(self):return []
    def connect(self,*args):return len(args)
    def disconnect(self,*args):pass
store=Store(sys.argv[2])
monitor=Monitor(store, observers=lambda engine:Observers(engine, Volumes()), notifier=lambda *args:None)
monitor.engine.queue.quiet=.05
def scanner(plan, store, cancel, **kwargs):
    import datetime
    assert all(str(Path(sys.argv[3])) in file for file in kwargs['files'])
    store.save(datetime.datetime.now().astimezone().isoformat(),plan.describe(),'検査完了','synthetic scan',kwargs['profile_id'])
    return '検査完了','synthetic scan'
monitor.engine.scanner=scanner
with patch('shutil.which', return_value='/mock/clamscan'):
    monitor.run()
'''


def wait(predicate, process):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise RuntimeError('headless monitor exited early: '+str(process.returncode))
        if predicate():return
        time.sleep(.025)
    raise AssertionError('headless monitor timed out')


with tempfile.TemporaryDirectory(prefix='clam-monitor-smoke-') as temporary:
    root=Path(temporary)
    folder=root/'watched';folder.mkdir()
    original=folder/'original';original.write_text('baseline')
    store=Store(root/'data')
    key=store.save_profile(Plan('Synthetic watch',[str(folder)],[],kind='watch'))
    save_schedule(store,key,Schedule(enabled=True))
    put_preference(store,'background-enabled',True)
    env=dict(os.environ)
    for name in ('DISPLAY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS'):
        env.pop(name,None)
    for name in ('XDG_DATA_HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_RUNTIME_DIR'):
        path=root/name;path.mkdir(mode=0o700);env[name]=str(path)
    def start():
        return subprocess.Popen([sys.executable,'-c',CODE,str(Path(__file__).resolve().parents[1]),str(root/'data'),str(folder)],env=env)
    process=start()
    try:
        wait(lambda:bool(heartbeat(store)),process)
        def ready():
            with store.connect() as db:
                return db.execute('SELECT count(*) FROM monitor_roots').fetchone()[0]>0
        wait(ready,process)
        assert not store.recent()
        (folder/'new').write_text('created without GUI')
        wait(lambda:len(store.recent())==1,process)
    finally:
        process.terminate();process.wait(timeout=8)
    assert process.returncode==0
    original.write_text('changed while monitor stopped')
    process=start()
    try:
        wait(lambda:len(store.recent())==2,process)
    finally:
        process.terminate();process.wait(timeout=8)
    assert process.returncode==0
    print('Headless monitoring, graceful stop, and downtime recovery: OK')
