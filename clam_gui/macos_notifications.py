"""Use an app-associated native notifier without loading GTK in scan workers."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def notifier_path():
    if getattr(sys, 'frozen', False):
        return (Path(sys.executable).parent.parent / 'Helpers/ClamAV Notifications.app/Contents/MacOS/ClamAV Notify')
    return Path(__file__).resolve().parents[1] / 'dist/native/ClamAV GUI.app/Contents/MacOS/ClamAV Notify'


def request(operation, title='', body=''):
    executable = notifier_path()
    if not executable.is_file():
        return {'authorization': None, 'delivered': False, 'error': '通知用ヘルパーが見つかりません。'}
    try:
        payload = json.dumps({'operation': operation, 'title': title[:256], 'body': body[:2000]}, ensure_ascii=False)
        if operation == 'authorize':
            # The permission request must originate from a LaunchServices application,
            # not a bare command-line subprocess. No normal window is created.
            with tempfile.TemporaryDirectory(prefix='clam-notification-') as directory:
                root = Path(directory)
                request_file, response_file = root / 'request.json', root / 'response.json'
                request_file.write_text(payload)
                result = subprocess.run(['/usr/bin/open', '-n', '-W', '-a', str(executable.parents[2]),
                                         '--stdin', str(request_file), '--stdout', str(response_file),
                                         '--stderr', str(root / 'errors.log')],
                                        capture_output=True, text=True, timeout=130)
                output = response_file.read_text() if response_file.exists() else ''
        else:
            result = subprocess.run([str(executable)], input=payload, capture_output=True, text=True, timeout=35)
            output = result.stdout
        if result.returncode:
            return {'authorization': None, 'delivered': False, 'error': 'macOSの通知状態を確認できません。'}
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError('Invalid notification response')
        if 'delivered' in data:
            data['delivered'] = data['delivered'] in (True, 1)
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'authorization': None, 'delivered': False, 'error': 'macOSの通知機能に接続できません。'}


def send(title, body):
    return request('send', title, body).get('delivered') is True


def status_label(data):
    return {0: '通知：未設定', 1: '通知：許可されていません',
            2: '通知：許可済み', 3: '通知：許可済み'}.get(data.get('authorization'), '通知：確認できません')
