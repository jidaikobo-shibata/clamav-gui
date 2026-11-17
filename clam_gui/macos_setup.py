"""Opt-in macOS setup using Cisco's verified installer and private FreshClam data.

No administrator shell, Homebrew bootstrap, global config edits, or service changes.
The system Installer handles privilege elevation and its own cancellation UI.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .paths import find_clamav, mac_data_directory

# Fixed, reviewed official release. Never execute an unverified "latest" download.
VERSION = '1.5.4'
INSTALLER_NAME = f'clamav-{VERSION}.macos.universal.pkg'
INSTALLER_URL = ('https://github.com/Cisco-Talos/clamav/releases/download/'
                 f'clamav-{VERSION}/{INSTALLER_NAME}')
INSTALLER_SHA256 = 'df7fa753e2f9f67f3bc99b2a40a3be7ef559088c68ad6bdf66b4b5764e965bd6'
INSTALLER_SIZE = 166772489
CONFIG_MARKER = '# Managed by clamav-gui: private FreshClam configuration v1'
DATABASES = ('main', 'daily', 'bytecode')


class SetupError(Exception):
    """A message suitable for the person using the application."""


class SetupCancelled(SetupError):
    pass


@dataclass(frozen=True)
class SetupState:
    engine: bool
    configuration: bool
    definitions: bool
    fresh: bool

    @property
    def ready(self):
        return self.engine and self.configuration and self.definitions and self.fresh

    def items(self):
        return [('検査に使うソフトウェア（ClamAV）', self.engine),
                ('このアプリ用の初期設定', self.configuration),
                ('検査用データ（ウイルス定義ファイル）', self.definitions),
                ('検査用データの更新確認', self.fresh)]


class MacSetup:
    def __init__(self, root=None, run=None, locate=None, opener=None, clock=None):
        self.root = Path(root) if root is not None else mac_data_directory()
        self.database = self.root / 'definitions'
        self.config = self.root / 'freshclam.conf'
        self.run = run or subprocess.run
        self.locate = locate or find_clamav
        self.opener = opener or urlopen
        self.clock = clock or time.monotonic
        self.last_output = ''

    def inspect(self):
        # Inspection must not create files, start processes, or contact the network.
        engine = bool(self.locate('clamscan') and self.locate('freshclam'))
        configuration = False
        if self.config.is_file() and not self.config.is_symlink():
            configuration = self.config.read_text() == self.configuration_text()
        files = []
        for name in DATABASES:
            candidates = [self.database / (name + suffix) for suffix in ('.cvd', '.cld')]
            files.append(next((p for p in candidates if p.is_file() and not p.is_symlink()
                               and p.stat().st_size > 0), None))
        definitions = all(files)
        checked = self.root / 'last-update-check'
        last_check = checked.stat().st_mtime if checked.is_file() and not checked.is_symlink() else 0
        daily = files[1]
        last_update = daily.stat().st_mtime if daily else 0
        fresh = bool(definitions and time.time() - max(last_check, last_update) < 3 * 86400)
        return SetupState(engine, configuration, definitions, fresh)

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or self.root.stat().st_uid != os.getuid():
            raise SetupError('準備用フォルダーの所有者を確認できません。保存先を確認してください。')
        descriptor = os.open(self.root / 'setup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SetupError('別の画面で準備・更新が進行中です。完了してから再試行してください。') from None
            yield

    @staticmethod
    def check_cancel(cancel):
        if cancel.is_set():
            raise SetupCancelled('準備を中止しました。完了した手順は残り、次回は続きから再開できます。')

    def download(self, cancel, report):
        cache = self.root / 'downloads'
        cache.mkdir(exist_ok=True, mode=0o700)
        if cache.is_symlink():
            raise SetupError('ダウンロード先を確認できません。保存先を確認してください。')
        destination = cache / INSTALLER_NAME
        if destination.is_file() and not destination.is_symlink():
            with destination.open('rb') as stream:
                if self.checksum(stream) == INSTALLER_SHA256:
                    return destination
        # A private temporary file and exact size/hash checks keep partial files inert.
        descriptor, filename = tempfile.mkstemp(prefix='installer-', suffix='.partial', dir=cache)
        temporary = Path(filename)
        try:
            request = Request(INSTALLER_URL, headers={'User-Agent': 'ClamAV-GUI/0.1'})
            certificates = Path('/etc/ssl/cert.pem')
            context = ssl.create_default_context(cafile=str(certificates) if certificates.is_file() else None)
            with os.fdopen(descriptor, 'wb') as output, self.opener(request, timeout=30, context=context) as response:
                if not response.geturl().startswith('https://'):
                    raise SetupError('安全なダウンロード先を確認できませんでした。')
                digest, size = hashlib.sha256(), 0
                while block := response.read(256 * 1024):
                    self.check_cancel(cancel)
                    size += len(block)
                    if size > INSTALLER_SIZE:
                        raise SetupError('ダウンロード内容が予定と異なります。実行せず停止しました。')
                    output.write(block)
                    digest.update(block)
                    report('ClamAVをダウンロードしています…', size / INSTALLER_SIZE)
            if size != INSTALLER_SIZE or digest.hexdigest() != INSTALLER_SHA256:
                raise SetupError('ダウンロード内容の検証に失敗しました。「再試行」で取得し直してください。')
            temporary.replace(destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def install_engine(self, cancel, report):
        package = self.download(cancel, report)
        self.check_cancel(cancel)
        report('ClamAVの提供元と署名を確認しています…', None)
        result = self.run(['/usr/sbin/pkgutil', '--check-signature', str(package)],
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, 'LC_ALL': 'C'})
        self.last_output = result.stdout + result.stderr
        if result.returncode or 'Developer ID Installer: Cisco (DE8Y96K9QP)' not in result.stdout:
            raise SetupError('公式提供元の署名を確認できませんでした。インストールせず停止しました。')
        self.check_cancel(cancel)
        report('macOSのインストール画面で「続ける」を押して進めてください。終了後、この画面に戻ります。', None)
        # Do not kill the OS Installer, even when cancellation is requested here.
        result = self.run(['/usr/bin/open', '-W', '-n', str(package)],
                          capture_output=True, text=True, timeout=3600)
        self.last_output += '\n' + result.stdout + result.stderr
        self.check_cancel(cancel)
        if result.returncode or not (self.locate('clamscan') and self.locate('freshclam')):
            raise SetupError('ClamAVのインストール完了を確認できませんでした。'
                             'macOSの画面で取り消した場合は、「再試行」でやり直せます。')

    def configure(self):
        for path in (self.root, self.database):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink() or path.stat().st_uid != os.getuid():
                raise SetupError('設定の保存先を確認できません。既存ファイルは変更していません。')
        if self.config.exists() or self.config.is_symlink():
            if not self.config.is_symlink() and self.config.read_text() == self.configuration_text():
                return
            raise SetupError('既存の設定を保護するため準備を停止しました。設定ファイルを確認してください。')
        content = self.configuration_text()
        descriptor = os.open(self.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)

    def configuration_text(self):
        directory = str(self.database)
        if any(char in directory for char in ('\n', '\r', '"')):
            raise SetupError('保存先の名前に、この準備機能では扱えない文字が含まれています。')
        return (CONFIG_MARKER + '\nDatabaseDirectory "' + directory + '"\n'
                'DatabaseMirror database.clamav.net\n'
                'DNSDatabaseInfo current.cvd.clamav.net\n'
                'Bytecode yes\nChecks 12\n')

    def update_definitions(self, cancel, report, timeout=1800):
        self.check_cancel(cancel)
        freshclam = self.locate('freshclam')
        if not freshclam:
            raise SetupError('検査用データを取得するソフトウェアが見つかりません。準備を再試行してください。')
        running = self.run(['/usr/bin/pgrep', '-x', 'freshclam'],
                           capture_output=True, text=True, timeout=10)
        if running.returncode == 0:
            raise SetupError('別の検査用データ更新が進行中です。完了を待ってから再試行してください。')
        if running.returncode != 1:
            raise SetupError('検査用データの更新状態を確認できませんでした。再試行してください。')
        report('検査用データを取得しています。初回は数分以上かかることがあります…', None)
        self.check_cancel(cancel)
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen([freshclam, '--config-file=' + str(self.config),
                                        '--datadir=' + str(self.database), '--stdout'],
                                       stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, 'LC_ALL': 'C'})
            deadline = self.clock() + timeout
            try:
                while True:
                    self.check_cancel(cancel)
                    if self.clock() > deadline:
                        raise SetupError('検査用データの取得に時間がかかっています。通信を確認し、後で再試行してください。')
                    try:
                        code = process.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                size = log.seek(0, 2)
                log.seek(max(0, size - 2 * 1024 * 1024))
                self.last_output = log.read().decode('utf-8', errors='replace')
        if code:
            if any(word in self.last_output.lower() for word in ('429', '403', 'cool-down')):
                raise SetupError('配信元から更新を待つよう案内されています。時間をおいて再試行してください。')
            raise SetupError('検査用データを取得できませんでした。インターネット接続を確認して再試行してください。')
        if not self.inspect().definitions:
            raise SetupError('必要な検査用データがまだ揃っていません。「再試行」で続きから取得できます。')
        checked = self.root / 'last-update-check'
        descriptor = os.open(checked, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(str(time.time()) + '\n')

    @staticmethod
    def checksum(stream):
        digest = hashlib.sha256()
        while block := stream.read(256 * 1024):
            digest.update(block)
        return digest.hexdigest()

    def prepare(self, cancel=None, report=lambda message, fraction: None, force_update=False):
        cancel = cancel or threading.Event()
        self.last_output = ''
        try:
            with self.locked():
                self.check_cancel(cancel)
                if not self.inspect().engine:
                    self.install_engine(cancel, report)
                self.check_cancel(cancel)
                report('このアプリ用の初期設定を準備しています…', None)
                self.configure()
                if force_update or not self.inspect().ready:
                    self.update_definitions(cancel, report)
                self.check_cancel(cancel)
                state = self.inspect()
                if not state.ready:
                    raise SetupError('準備の完了を確認できませんでした。表示された項目を確認してください。')
                report('検査の準備ができました。', 1.0)
                return state
        except (URLError, socket.timeout, TimeoutError) as error:
            raise SetupError('ダウンロード先に接続できませんでした。インターネット接続を確認して再試行してください。') from error
        except subprocess.TimeoutExpired as error:
            raise SetupError('処理の完了を確認できませんでした。インストール画面や通信状態を確認してください。') from error
        except OSError as error:
            raise SetupError('準備に必要なファイルを読み書きできませんでした。空き容量やアクセス権を確認してください。') from error
