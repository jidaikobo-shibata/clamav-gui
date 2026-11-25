"""Allow-listed ClamAV scan switches; never accepts arbitrary command text."""
from dataclasses import dataclass
import re
import os
import subprocess
from .engine import startup_error


@dataclass(frozen=True)
class Option:
    key: str
    group: str
    label: str
    default: bool
    description: str


OPTIONS = (
    Option('scan-archive', 'ファイル形式', '圧縮ファイル', True, 'ZIPなどを展開して内容を調べます。'),
    Option('scan-pdf', 'ファイル形式', 'PDF', True, 'PDF内部の構造と埋め込み内容を解析します。'),
    Option('scan-ole2', 'ファイル形式', 'Office文書（OLE2）', True, '従来形式のWord・Excelなどを解析します。'),
    Option('scan-xmldocs', 'ファイル形式', 'XML形式の文書', True, 'XML形式の文書を解析します。'),
    Option('scan-onenote', 'ファイル形式', 'OneNote', True, 'OneNote文書を解析します。'),
    Option('scan-html', 'ファイル形式', 'HTML', True, 'HTMLを解析します。'),
    Option('scan-image', 'ファイル形式', '画像', True, '対応する画像形式を解析します。'),
    Option('scan-image-fuzzy-hash', 'ファイル形式', '画像の類似ハッシュ検査', True, '画像の類似性を使った検出を行います。'),
    Option('scan-pe', 'ファイル形式', 'Windows実行ファイル', True, 'PE形式を解析します。'),
    Option('scan-elf', 'ファイル形式', 'Linux実行ファイル', True, 'ELF形式を解析します。'),
    Option('scan-mail', 'ファイル形式', 'メール形式', True, 'メール形式の内容を解析します。'),
    Option('scan-swf', 'ファイル形式', 'Flash（SWF）', True, 'SWF形式を解析します。'),
    Option('scan-hwp3', 'ファイル形式', 'HWP3文書', True, 'HWP3形式を解析します。'),
    Option('phishing-sigs', '検出方法', 'メールのフィッシング検査', False, 'メールのフィッシング検出用定義を使います。'),
    Option('phishing-scan-urls', '検出方法', 'URLのフィッシング検査', False, 'URLのフィッシング検出用定義を使います。'),
    Option('detect-pua', '検出方法', '不審な可能性があるアプリ（PUA）', False, '正当な用途があるツールも検出する場合があります。'),
    Option('alert-encrypted', '警告', '暗号化された内容を報告', True, '内容を調べられない圧縮ファイル・文書を報告します。'),
    Option('alert-macros', '警告', 'マクロを含む文書を報告', False, 'VBAマクロを含むOLE2文書を報告します。'),
    Option('alert-broken', '警告', '破損した実行ファイルを報告', False, '破損したPE・ELFを報告します。'),
    Option('alert-broken-media', '警告', '破損した画像を報告', False, '対応する画像形式の破損を報告します。'),
)
DEFAULTS = {option.key: option.default for option in OPTIONS}


def help_text(binary):
    result = subprocess.run([binary, '--help'], capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'LC_ALL': 'C'}, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError(startup_error(result.returncode) or 'ClamAVの対応オプションを確認できません。')
    return result.stdout


def supported_options(binary):
    return set(re.findall(r'--([a-z][a-z0-9-]+)', help_text(binary)))


def standard_defaults(binary):
    text = help_text(binary)
    values = {}
    for option in OPTIONS:
        match = re.search(r'--' + re.escape(option.key) +
                          r'\[=(yes|no)(\(\*\))?/(yes|no)(\(\*\))?\]', text)
        if match and bool(match[2]) != bool(match[4]):
            values[option.key] = (match[1] if match[2] else match[3]) == 'yes'
    return values


def option_arguments(settings, supported):
    arguments = []
    for option in OPTIONS:
        if option.key in supported:
            arguments.append('--' + option.key + '=' + ('yes' if settings[option.key] else 'no'))
        elif settings[option.key] != option.default:
            raise ValueError(f'このClamAVは「{option.label}」の設定に対応していません。環境設定を確認してください。')
    return arguments


def describe_options(settings, supported):
    return '\n'.join(f'{option.label}: ' + ('有効' if settings[option.key] else '無効')
                     if option.key in supported else f'{option.label}: 非対応（指定なし）'
                     for option in OPTIONS)


LIMITS = {
    'max-filesize': ('1ファイルの上限', '1つのファイルを検査するサイズの上限です。'),
    'max-scansize': ('展開後の合計上限', '圧縮ファイルなど1つのコンテナーを展開して調べる合計の上限です。'),
}


APP_LIMITS = {'max-filesize': 256, 'max-scansize': 256}

def validate_limits(limits):
    if set(limits) != set(LIMITS):
        raise ValueError('サイズ上限の項目が不正です。')
    for key, value in limits.items():
        if value is not None and (type(value) is not int or not 1 <= value <= 2047):
            raise ValueError(LIMITS[key][0] + 'は1〜2047 MiBで指定してください。')
    if all(value is not None for value in limits.values()) and limits['max-scansize'] < limits['max-filesize']:
        raise ValueError('展開後の合計上限は、1ファイルの上限以上にしてください。')


def limit_arguments(limits, supported):
    validate_limits(limits)
    result = []
    for key, value in limits.items():
        if value is not None:
            if key not in supported:
                raise ValueError('このClamAVは「' + LIMITS[key][0] + '」の指定に対応していません。')
            result.append(f'--{key}={value}M')
    return result


def describe_limits(limits):
    return '\n'.join(label + ': ' + ((f'{limits[key]} MiB' + ('（アプリ標準）' if limits[key] == APP_LIMITS[key] else '')) if limits[key] is not None else 'ClamAV標準（引数省略）')
                     for key, (label, _) in LIMITS.items())
