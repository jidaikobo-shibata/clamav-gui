from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from clam_gui.options import DEFAULTS
from clam_gui.core import Store, Plan, scan
from clam_gui.admin_scan import AdminProcess, trusted
from privileged.admin_scan import arguments, sandbox, BOOL_KEYS, filter_bpf, supervise


class AdminScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = dict(version=1, targets=[str(self.root)], excludes=[], excluded_dirs=[],
                            options=dict(DEFAULTS), limits={'max-filesize': 256, 'max-scansize': 512})

    def test_broker_accepts_only_fixed_read_only_scan_options(self):
        self.assertEqual(BOOL_KEYS, set(DEFAULTS))
        args = arguments(self.request)
        self.assertEqual(args[0], '/usr/bin/clamscan')
        self.assertIn('--max-scansize=512M', args)
        self.assertIn('--cross-fs=no', args)
        self.assertIn('--follow-dir-symlinks=0', args)
        self.assertNotIn('--remove', args)
        self.assertEqual(args[-2:], ['--', str(self.root)])
        for key, value in [('command', '/bin/sh'), ('version', True), ('targets', ['/dev']),
                           ('targets', ['--remove']), ('targets', ['/proc/self']),
                           ('options', dict(DEFAULTS, remove=True)),
                           ('limits', {'max-filesize': True, 'max-scansize': 512})]:
            request = deepcopy(self.request)
            request[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                arguments(request)
        alias = self.root/'alias'
        alias.symlink_to('/dev')
        request = dict(self.request, targets=[str(alias)])
        with self.assertRaises(ValueError):
            arguments(request)

    def test_sandbox_has_read_only_root_network_isolation_and_read_cap_only(self):
        args = sandbox(arguments(self.request), '/var/tmp/private', 7)
        self.assertEqual(args[:4], ['/usr/bin/bwrap', '--ro-bind', '/', '/'])
        self.assertIn('--unshare-net', args)
        self.assertIn('--unshare-pid', args)
        self.assertIn('--die-with-parent', args)
        self.assertIn('CAP_DAC_READ_SEARCH', args)
        self.assertNotIn('--dev-bind', args)
        self.assertIn('--exclude-dir=^/var/tmp/private(/|$)', args)
        self.assertEqual(args[args.index('--bind')+1:args.index('--bind')+3], ['/var/tmp/private']*2)

    def test_seccomp_filter_can_be_built_without_elevation(self):
        with tempfile.TemporaryFile() as file:
            filter_bpf(file)
            self.assertTrue(file.read())

    def test_seccomp_blocks_socket_in_an_unprivileged_child(self):
        code = """import ctypes, socket, tempfile
from privileged.admin_scan import filter_bpf
class Instruction(ctypes.Structure):
    _fields_ = [('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]
class Program(ctypes.Structure):
    _fields_ = [('len',ctypes.c_ushort),('filter',ctypes.POINTER(Instruction))]
with tempfile.TemporaryFile() as f:
    filter_bpf(f)
    raw=f.read()
array=(Instruction*(len(raw)//ctypes.sizeof(Instruction))).from_buffer_copy(raw)
program=Program(len(array),array)
libc=ctypes.CDLL(None,use_errno=True)
assert libc.prctl(38,1,0,0,0)==0
assert libc.prctl(22,2,ctypes.byref(program),0,0)==0
try:
    socket.socket()
except PermissionError:
    print('blocked')
else:
    raise AssertionError('socket was allowed')
"""
        result = subprocess.run(['/usr/bin/python3', '-c', code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'blocked')

    def test_prepared_installation_matches_reviewed_sources(self):
        from scripts.prepare_admin_helper import prepare
        stage = self.root/'stage'
        stage.mkdir()
        with patch('scripts.prepare_admin_helper.tempfile.mkdtemp', return_value=str(stage)):
            self.assertEqual(prepare(), stage)
        self.assertEqual((stage/'clamav-gui-admin-scan').read_bytes(), Path('privileged/admin_scan.py').read_bytes())
        command = (stage/'install.sh').read_text()
        self.assertEqual(command.count('sudo /usr/bin/install'), 2)
        result = subprocess.run(['/bin/bash', '-n', str(stage/'install.sh')], capture_output=True)
        self.assertEqual(result.returncode, 0)
        for line in command.splitlines():
            if line.endswith(chr(92)):
                self.assertFalse(line.endswith(chr(92)*2))

    def test_gui_pipe_eof_stops_the_scan(self):
        read, write = os.pipe()
        try:
            process = subprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(60)'])
            os.close(write)
            self.assertEqual(supervise(process, read), 130)
            self.assertIsNotNone(process.poll())
        finally:
            os.close(read)
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_broker_refuses_unprivileged_direct_execution(self):
        result = subprocess.run(['/usr/bin/python3', '-I', 'privileged/admin_scan.py'],
                                input=b'', capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)

    def test_untrusted_or_missing_installation_is_not_executed(self):
        self.assertFalse(trusted(self.root/'missing'))
        with patch('clam_gui.admin_scan.available', return_value=False), patch('clam_gui.admin_scan.subprocess.Popen') as spawn:
            with self.assertRaises(ValueError):
                AdminProcess(self.request)
            spawn.assert_not_called()

    def test_policy_is_specific_to_broker_and_does_not_cache_auth(self):
        import xml.etree.ElementTree as ET
        policy = ET.parse('privileged/com.jidaikobo.ClamAVGui.admin-scan.policy')
        action = policy.find('action')
        self.assertEqual(action.find('defaults/allow_active').text, 'auth_admin')
        self.assertEqual(action.find('defaults/allow_inactive').text, 'no')
        self.assertEqual(action.find('annotate').text, '/usr/libexec/clamav-gui-admin-scan')

    def test_oversized_request_is_not_launched(self):
        request = dict(self.request, targets=['/' + 'x'*5000])
        with patch('clam_gui.admin_scan.available', return_value=True), patch('clam_gui.admin_scan.subprocess.Popen') as spawn:
            with self.assertRaises(ValueError):
                AdminProcess(request)
        spawn.assert_not_called()

    def test_sandbox_failure_never_claims_scan_success(self):
        store = Store(self.root/'data')
        plan = Plan('test', [str(self.root)], [])
        process = Mock()
        process.wait.return_value = 0
        with patch('clam_gui.admin_scan.spawn', return_value=process), patch('clam_gui.core.shutil.which', return_value='/usr/bin/clamscan'), patch('clam_gui.core.supported_options', return_value=set(DEFAULTS)|{'max-filesize','max-scansize'}):
            status, output = scan(plan, store, lock_path=self.root/'lock', administrator=True)
        self.assertIn('集計結果が返りません', output)
        self.assertIn('検査不完了', status)

    def test_client_sends_bounded_json_to_fixed_helper_without_shell(self):
        real_popen = subprocess.Popen
        commands = []
        def fake_auth(command, **kwargs):
            commands.append(command)
            return real_popen(['/usr/bin/python3', '-c', 'import sys; print(sys.stdin.readline(), end="")'], **kwargs)
        with patch('clam_gui.admin_scan.available', return_value=True), patch('clam_gui.admin_scan.subprocess.Popen', side_effect=fake_auth):
            process = AdminProcess(self.request, stdout=subprocess.PIPE)
            self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(commands, [['/usr/bin/pkexec', '/usr/libexec/clamav-gui-admin-scan']])
        self.assertEqual(json.loads(process.process.stdout.read()), self.request)
        process.process.stdout.close()

    def test_cancel_closes_pipe_even_when_root_process_cannot_be_signalled(self):
        process = Mock()
        process.stdin = io.BytesIO()
        process.terminate.side_effect = PermissionError()
        instance = AdminProcess.__new__(AdminProcess)
        instance.process = process
        instance.terminate()
        self.assertTrue(process.stdin.closed)

    def test_privileged_scan_records_mode_and_cancellation_without_root(self):
        store = Store(self.root/'data')
        plan = Plan('protected', [str(self.root)], [], requires_admin=True)
        class Fake:
            def wait(self, timeout=None): return 126
            def poll(self): return 126
        with patch('clam_gui.admin_scan.spawn', return_value=Fake()) as spawn, patch('clam_gui.core.shutil.which', return_value='/usr/bin/clamscan'), patch('clam_gui.core.supported_options', return_value=set(DEFAULTS)|{'max-filesize','max-scansize'}):
            status, output = scan(plan, store, lock_path=self.root/'lock', administrator=True)
        self.assertIn('認証がキャンセル', output)
        self.assertIn('管理者権限による手動検査', store.recent()[0][1])
        self.assertIn('管理者権限による手動検査', store.recent()[0][3])
        self.assertNotIn('通常ユーザーで検査します', store.recent()[0][1])
        self.assertTrue(plan.requires_admin)
        spawn.assert_called_once()
