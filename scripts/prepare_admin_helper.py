"""Prepare reviewable helper/policy files and install commands; never elevate."""
from pathlib import Path
import shlex
import tempfile


def prepare():
    project = Path(__file__).resolve().parents[1]
    stage = Path(tempfile.mkdtemp(prefix='clam-gui-admin-install-'))
    broker = stage/'clamav-gui-admin-scan'
    policy = stage/'com.jidaikobo.ClamAVGui.admin-scan.policy'
    broker.write_bytes((project/'privileged/admin_scan.py').read_bytes())
    policy.write_bytes((project/'privileged'/policy.name).read_bytes())
    command = ('#!/bin/bash\nset -eu\n'
        'sudo /usr/bin/install -o root -g root -m 0755 \\\n  ' + shlex.quote(str(broker)) + ' \\\n  /usr/libexec/clamav-gui-admin-scan\n'
        'sudo /usr/bin/install -o root -g root -m 0644 \\\n  ' + shlex.quote(str(policy)) + ' \\\n  /usr/share/polkit-1/actions/' + policy.name + '\n')
    script = stage/'install.sh'
    script.write_text(command)
    return stage


if __name__ == '__main__':
    print(prepare())
