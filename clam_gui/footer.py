"""Concise presentation, keeping package details in the footer tooltip."""
import re


def compact_engine_label(label):
    match = re.match(r'^ClamAV本体（APT） (\S+)：', label)
    if match:
        version = match[1].split(':')[-1]
        return 'ClamAV ' + re.split(r'[+\-]', version, maxsplit=1)[0]
    if label == 'ClamAV本体：APT未導入':
        return 'ClamAV：APT未導入'
    if label.startswith('ClamAV本体：'):
        return 'ClamAV：確認不可'
    return label
