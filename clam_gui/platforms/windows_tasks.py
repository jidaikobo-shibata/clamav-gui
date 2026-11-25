"""Current-user Task Scheduler registrations scoped to a data directory."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from ..schedule import read_schedule, save_schedule
from ..core import validate_profile

ROOT = Path(__file__).resolve().parents[2]
MARK = 'Managed by ClamAV GUI / '
NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
CONTROL = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$p = $env:CLAM_GUI_TASK | ConvertFrom-Json
$s = New-Object -ComObject Schedule.Service
$s.Connect()
$f = $s.GetFolder('\\')
$t = $null
try { $t = $f.GetTask($p.name) } catch {
    if ($_.Exception.HResult -ne -2147024894) { throw }
}
if ($t -and $t.Definition.RegistrationInfo.Description -cne $p.marker) {
    throw 'Task name belongs to another application.'
}
switch ($p.action) {
    'running' { if ($t -and $t.GetInstances(0).Count) { [Console]::Write('1') } }
    'query' { if ($t) { [Console]::Write($t.Xml) } }
    'remove' { if ($t) { $f.DeleteTask($p.name, 0) } }
    'run' { $null = $t.Run($null) }
    'register' {
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $flags = if ($t) { 4 } else { 2 }
        $null = $f.RegisterTask($p.name,$p.xml,$flags,$user,$null,3)
    }
}
exit 0
"""


class Task:
    def __init__(self, store, key):
        token = hashlib.sha256((str(store.path.resolve()).casefold() + '/' + key).encode()).hexdigest()
        self.name = 'ClamAV-GUI-' + token[:24]
        self.marker = MARK + token

    def control(self, action, xml=''):
        payload = dict(action=action, xml=xml, marker=self.marker, name=self.name)
        shell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-Command', CONTROL],
            capture_output=True, encoding='utf-8', errors='replace', timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW, env={**os.environ, 'CLAM_GUI_TASK': json.dumps(payload)})
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or 'タスクを変更できません。')
        return result.stdout

    def xml(self, trigger, script, args):
        root = ET.Element('Task', version='1.2', xmlns=NS)
        reg = ET.SubElement(root, 'RegistrationInfo')
        ET.SubElement(reg, 'Description').text = self.marker
        ET.SubElement(root, 'Triggers').append(trigger)
        principal = ET.SubElement(ET.SubElement(root, 'Principals'), 'Principal', id='Author')
        ET.SubElement(principal, 'LogonType').text = 'InteractiveToken'
        ET.SubElement(principal, 'RunLevel').text = 'LeastPrivilege'
        settings = ET.SubElement(root, 'Settings')
        for name, value in [('MultipleInstancesPolicy','IgnoreNew'), ('DisallowStartIfOnBatteries','false'),
                            ('StopIfGoingOnBatteries','false'), ('StartWhenAvailable','true'), ('ExecutionTimeLimit','PT0S')]:
            ET.SubElement(settings, name).text = value
        action = ET.SubElement(ET.SubElement(root, 'Actions', Context='Author'), 'Exec')
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        ET.SubElement(action, 'Command').text = str(pythonw)
        ET.SubElement(action, 'Arguments').text = subprocess.list2cmdline([str(ROOT/'scripts'/script), *args])
        return ET.tostring(root, encoding='unicode')


def trigger_for(s):
    now = datetime.datetime.now()
    if s.frequency in ('interval', 'hourly'):
        node = ET.Element('TimeTrigger')
        start = now + datetime.timedelta(minutes=s.interval_minutes)
        repeat = ET.SubElement(node, 'Repetition')
        interval = 'PT1M'
        if s.frequency == 'hourly':
            start = now.replace(minute=s.minute, second=0)
            if start <= now:
                start += datetime.timedelta(hours=1)
            interval = 'PT1H'
        ET.SubElement(repeat, 'Interval').text = interval
        ET.SubElement(node, 'StartBoundary').text = start.isoformat(timespec='seconds')
        return node
    node = ET.Element('CalendarTrigger')
    ET.SubElement(node, 'StartBoundary').text = now.replace(hour=s.hour, minute=s.minute, second=0).isoformat(timespec='seconds')
    if s.frequency == 'daily':
        ET.SubElement(ET.SubElement(node, 'ScheduleByDay'), 'DaysInterval').text = '1'
    elif s.frequency == 'weekly':
        week = ET.SubElement(node, 'ScheduleByWeek')
        ET.SubElement(week, 'WeeksInterval').text = '1'
        days = ET.SubElement(week, 'DaysOfWeek')
        for day in s.selected_weekdays():
            ET.SubElement(days, ('Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.split())[day])
    else:
        month = ET.SubElement(node, 'ScheduleByMonth')
        ET.SubElement(ET.SubElement(month, 'DaysOfMonth'), 'Day').text = str(s.monthday)
        months = ET.SubElement(month, 'Months')
        for name in 'January February March April May June July August September October November December'.split():
            ET.SubElement(months, name)
    return node


class Scheduler:
    def __init__(self, store):
        self.store = store

    def apply(self, key, schedule):
        schedule.calendar()
        plan = dict(self.store.profiles())[key]
        if schedule.enabled:
            validate_profile(plan)
        task = Task(self.store, 'scan-' + key)
        old = read_schedule(self.store, key)
        xml = ''
        if schedule.enabled and plan.kind == 'scheduled':
            if plan.requires_admin:
                raise ValueError('管理者権限が必要な対象は定期実行できません。')
            xml = task.xml(trigger_for(schedule), 'run_scheduled.py', ['--data-dir', str(self.store.path.parent), '--profile', key])
        if xml or old.enabled:
            previous = task.control('query')
            if xml:
                task.control('register', xml)
            else:
                task.control('remove')
            try:
                save_schedule(self.store, key, schedule)
            except Exception:
                task.control('register', previous) if previous else task.control('remove')
                raise
        else:
            save_schedule(self.store, key, schedule)

    def remove_profile(self, key):
        from dataclasses import replace
        self.apply(key, replace(read_schedule(self.store, key), enabled=False))
        self.store.delete_profile(key)
