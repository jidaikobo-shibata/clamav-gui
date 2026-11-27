"""Verify notification action cold-start in isolated Wayland and D-Bus."""
import os
from pathlib import Path
import subprocess
import tempfile

with tempfile.TemporaryDirectory(prefix='clam-cold-test-') as root:
    env = dict(os.environ)
    for key in ('DISPLAY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS'):
        env.pop(key,None)
    for key,name in [('XDG_RUNTIME_DIR','runtime'),('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),('XDG_CACHE_HOME','cache')]:
        p=Path(root)/name
        p.mkdir(mode=0o700)
        env[key]=str(p)
    env.update(GIO_USE_VFS='local',GTK_USE_PORTAL='0',GDK_BACKEND='wayland',WAYLAND_DISPLAY='clam-test',GTK_A11Y='none',GSK_RENDERER='cairo')
    code = """
from pathlib import Path
from gi.repository import Gio, GLib
from scripts.install_launcher import install
from clam_gui.core import Store, Plan
from clam_gui.results import ScanStatus, ResultKind
from clam_gui.notifications import notification_target
from clam_gui.desktop_notifications import APP_ID
install(Path.cwd())
store=Store()
folder=store.path.parent/'sample'
folder.mkdir()
plan=Plan('sample',[str(folder)],[])
key=store.save_profile(plan)
status=ScanStatus('検査完了',ResultKind.CLEAN)
store.save('2026-11-27T01:00:00+09:00',plan.describe(),status,'test log',key)
connection=Gio.bus_get_sync(Gio.BusType.SESSION,None)
path='/'+APP_ID.replace('.','/')
def call(interface,method,args):
    return connection.call_sync(APP_ID,path,interface,method,args,None,Gio.DBusCallFlags.NONE,15000,None)
call('org.freedesktop.Application','ActivateAction',GLib.Variant('(sava{sv})',('open-result',[GLib.Variant('s',notification_target(key,status))],{})))
actions=call('org.gtk.Actions','List',None).unpack()[0]
assert 'open-result' in actions and 'quit' in actions,actions
call('org.freedesktop.Application','ActivateAction',GLib.Variant('(sava{sv})',('quit',[],{})))
print('Cold D-Bus activation: OK',flush=True)
"""
    subprocess.run(['dbus-run-session','--','weston','--backend=headless','--renderer=pixman','--no-config','--socket=clam-test','--log='+str(Path(root)/'weston.log'),'--','/usr/bin/python3','-c',code],env=env,check=True,timeout=25)
