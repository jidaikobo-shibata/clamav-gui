"""Verify the Windows engine after installation."""
import subprocess

from ..engine import executable, startup_error


def prepare_install():
    for name in ('clamscan', 'freshclam'):
        binary = executable(name)
        if not binary:
            raise ValueError(f'{name}.exe が見つかりません。')
        result = subprocess.run([binary, '--help'], capture_output=True,
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or not result.stdout:
            raise ValueError(startup_error(result.returncode) or f'{name}.exe の起動確認に失敗しました。')


if __name__ == '__main__' and (executable() or executable('freshclam')):
    prepare_install()
