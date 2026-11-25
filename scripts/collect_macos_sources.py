"""Collect verified corresponding sources and assemble paired macOS archives."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tarfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def fetch(url, destination, checksum=None):
    if urlsplit(url).scheme != 'https':
        raise ValueError('Source downloads must use HTTPS')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and checksum and digest(destination) == checksum:
        return destination
    temporary = destination.with_name(destination.name + '.partial')
    certificates = Path('/etc/ssl/cert.pem')
    context = ssl.create_default_context(cafile=str(certificates) if certificates.is_file() else None)
    try:
        request = Request(url, headers={'User-Agent': 'ClamAV-GUI-release/1.0'})
        with urlopen(request, timeout=60, context=context) as response, temporary.open('wb') as stream:
            if not response.geturl().startswith('https://'):
                raise ValueError('Insecure source redirect')
            size = 0
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > 1024 ** 3:
                    raise ValueError('Unexpectedly large source archive')
                stream.write(block)
        if checksum and digest(temporary) != checksum:
            raise ValueError('Source hash mismatch: ' + destination.name)
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def components(output):
    result = {}
    for table in (output / 'work/ClamAV-GUI').glob('Analysis-*.toc'):
        for section in ast.literal_eval(table.read_text()):
            if not isinstance(section, list):
                continue
            for entry in section:
                if not isinstance(entry, tuple) or len(entry) != 3:
                    continue
                path = Path(entry[1]).resolve()
                if 'Cellar' in path.parts:
                    i = path.parts.index('Cellar')
                    result[path.parts[i + 1]] = Path(*path.parts[:i + 3])
    return result


def recipe_source(name, keg):
    recipe = keg / '.brew' / (name + '.rb')
    text = recipe.read_text()
    url = re.search(r'^\s*url "([^"\n]+)"', text, re.M)
    checksum = re.search(r'^\s*sha256 "([0-9a-f]{64})"', text, re.M)
    if not url or not checksum or '#{' in url[1]:
        raise ValueError('Cannot determine verified source for ' + name)
    return recipe, url[1], checksum[1]


def cargo_sources(archive, cache, sources, notices):
    """Provide locked Rust sources/notices when an upstream tarball omits vendor/."""
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        if any('/vendor/' in member.name for member in members):
            return []
        lock = next((member for member in members if member.name.endswith('/Cargo.lock')
                     and len(Path(member.name).parts) == 2), None)
        if lock is None:
            return []
        with stream.extractfile(lock) as file:
            packages = tomllib.loads(file.read().decode())['package']
    packages = [package for package in packages if package.get('source')]
    destination = sources / 'RustCrates'
    destination.mkdir(exist_ok=True)
    def collect(package):
        name, version = package['name'], package['version']
        if (not re.fullmatch(r'[A-Za-z0-9_-]+', name) or not re.fullmatch(r'[A-Za-z0-9.+-]+', version)
                or package['source'] != 'registry+https://github.com/rust-lang/crates.io-index'):
            raise ValueError('Unsupported locked Rust source: ' + name)
        checksum = package['checksum']
        filename = name + '-' + version + '.crate'
        url = f'https://static.crates.io/crates/{name}/{filename}'
        file = fetch(url, cache / ('rust-' + filename), checksum)
        shutil.copy2(file, destination / filename)
        folder = notices / 'Rust' / (name + '-' + version)
        folder.mkdir(parents=True, exist_ok=True)
        with tarfile.open(file) as stream:
            for member in stream.getmembers():
                if (member.isfile() and len(Path(member.name).parts) <= 3 and member.size < 2 * 1024 ** 2
                        and re.match(r'(?i)^(license|copying|notice|copyright)', Path(member.name).name)):
                    with stream.extractfile(member) as content:
                        target = folder / (hashlib.sha256(member.name.encode()).hexdigest()[:8] + '-' + Path(member.name).name)
                        target.write_bytes(content.read())
        return {'name': 'Rust/' + name, 'version': version, 'source_url': url,
                'source_sha256': checksum, 'source_file': 'RustCrates/' + filename}
    with ThreadPoolExecutor(max_workers=4) as executor:
        records = list(executor.map(collect, packages))
    print('Verified locked Rust sources:', len(records), flush=True)
    return records


def collect(output):
    output = Path(output).resolve()
    app = output / 'bundle/ClamAV GUI.app'
    metadata = json.loads((app / 'Contents/Resources/BUILD_INFO.json').read_text())
    arch = metadata['architecture']
    label = 'AppleSilicon' if arch == 'arm64' else 'Intel'
    release = output / ('ClamAV-GUI-' + label)
    release.mkdir(exist_ok=False)
    sources = output / ('ClamAV-GUI-Sources-' + label)
    sources.mkdir()
    cache = ROOT / 'dist/source-cache'
    records = []

    def add(name, version, url, checksum, recipe=None):
        filename = name.replace('@', '-') + '-' + Path(urlsplit(url).path).name
        archive = fetch(url, cache / filename, checksum)
        folder = sources / 'Sources'
        folder.mkdir(exist_ok=True)
        shutil.copy2(archive, folder / filename)
        notices = app / 'Contents/Resources/Licenses' / name
        notices.mkdir(exist_ok=True)
        if recipe:
            destination = sources / 'BuildRecipes'
            destination.mkdir(exist_ok=True)
            shutil.copy2(recipe, destination / recipe.name)
        # Read selected license files from the verified archive without extracting paths.
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive) as stream:
                for member in stream.getmembers():
                    if (member.isfile() and len(Path(member.name).parts) <= 3 and member.size <= 2 * 1024 ** 2
                            and re.match(r'(?i)^(license|copying|notice|copyright)', Path(member.name).name)):
                        with stream.extractfile(member) as text:
                            dest = notices / (hashlib.sha256(member.name.encode()).hexdigest()[:8] + '-' + Path(member.name).name)
                            dest.write_bytes(text.read())
        records.append({'name': name, 'version': version, 'source_url': url,
                        'source_sha256': checksum, 'source_file': 'Sources/' + filename})
        if name == 'librsvg':
            records.extend(cargo_sources(archive, cache, sources, notices))
        print('Verified corresponding source:', name, version, flush=True)

    for name, keg in sorted(components(output).items()):
        recipe, url, checksum = recipe_source(name, keg)
        add(name, keg.name, url, checksum, recipe)
        # Homebrew may apply extra patches outside the upstream release archive.
        for number, block in enumerate(re.findall(r'\bpatch do\n(.*?)\n\s*end', recipe.read_text(), re.S)):
            patch_url = re.search(r'\burl "([^"]+)"', block)
            patch_hash = re.search(r'\bsha256 "([0-9a-f]{64})"', block)
            if patch_url and patch_hash:
                filename = name.replace('@', '-') + '-' + str(number) + '.patch'
                file = fetch(patch_url[1], cache / filename, patch_hash[1])
                destination = sources / 'Patches'
                destination.mkdir(exist_ok=True)
                shutil.copy2(file, destination / filename)
                records.append({'name': name + ' patch ' + str(number), 'source_url': patch_url[1],
                                'source_sha256': patch_hash[1], 'source_file': 'Patches/' + filename})
    for package in metadata['components']:
        name, version = package['name'], package['version']
        if name not in ('PyInstaller', 'pyinstaller-hooks-contrib', 'altgraph', 'macholib', 'packaging', 'pycairo'):
            continue
        info = fetch(f'https://pypi.org/pypi/{name}/{version}/json', cache / (name + '-' + version + '.json'))
        source = next(item for item in json.loads(info.read_text())['urls'] if item['packagetype'] == 'sdist')
        add(name, version, source['url'], source['digests']['sha256'])

    project = sources / 'Sources/clamav-gui'
    project.mkdir()
    for name in ('LICENSE', 'README.md', 'CONTRIBUTING.md'):
        shutil.copy2(ROOT / name, project / name)
    for name in ('clam_gui', 'scripts', 'tests', 'assets'):
        shutil.copytree(ROOT / name, project / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log'))
    (sources / 'SOURCE_MANIFEST.json').write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n')
    (sources / 'BUILDING.txt').write_text('''対応ソースの構成

SOURCE_MANIFEST.jsonに同梱部品のバージョン・取得元・SHA-256を記録しています。
Sources/clamav-guiがアプリのソース、BuildRecipesが同じライブラリのビルドレシピ、
Patchesがレシピで指定された追加パッチです。ビルド方法はプロジェクトのREADMEを参照してください。
Sources内のライブラリは各レシピと同じ設定・依存関係でビルドしてください。
AppleのSDKとコンパイラーは別途必要です。

librsvgの上流アーカイブにvendorが含まれない場合、Cargo.lockに記録されたRustの
registry依存をRustCratesへ収録しています。各crateはlockのチェックサムと照合済みです。
必要なバージョンをCargoのローカルキャッシュまたはvendorへ展開して利用できます。

同梱ライブラリの変更と再リンク

アプリは動的ライブラリをContents/Frameworksに収録しています。各ライブラリのABIと
CPU形式を維持した互換ライブラリへ差し替えるか、変更したライブラリ環境で
scripts/build_macos.pyを実行してアプリを再生成できます。
変更後はコード署名を再作成してください。ローカルのアドホック署名を使う場合の例:

codesign --force --deep --sign - "ClamAV GUI.app"
codesign --verify --deep --strict "ClamAV GUI.app"

ライブラリの変更・調査を妨げる独自の制限は設けていません。
それぞれのライセンス本文はLicensesに収録しています。
''')
    shutil.copytree(app / 'Contents/Resources/Licenses', sources / 'Licenses')
    license_text = ('ClamAV GUI 1.0.0\n\nGUI本体: WTFPL version 2\n'
                    '同梱Python/GTK等: 各Licensesフォルダーを参照してください。\n'
                    'ClamAV本体・定義は同梱せず、初回準備で公式配布元から取得します。\n'
                    '対応ソース・ビルド用レシピは同じ配布場所のSources ZIPに収録しています。\n'
                    'PyInstallerには生成物の配布を認めるブートローダー例外があります。\n')
    (app / 'Contents/Resources/LICENSES_README.txt').write_text(license_text)
    (release / 'LICENSES_README.txt').write_text(license_text)
    (sources / 'LICENSES_README.txt').write_text(license_text)
    (release / 'START_HERE.html').write_text('''<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ClamAV GUIを使い始める</title>
<body style="max-width:45em;margin:3em auto;padding:1em;font:18px/1.8 system-ui">
<h1>ClamAV GUIを使い始める</h1><p>このフォルダーの「ClamAV GUI.app」をApplicationsへ移して開いてください。
定期実行の登録後はアプリの場所を変えないでください。</p>
<ol><li>準備画面で「必要なものをインストール」を押します。</li>
<li>macOSのインストール画面で「続ける」を押し、必要な認証を受けます。</li>
<li>検査用データを取得できたら、自動更新と検査完了の通知を設定します。</li>
<li>左上の＋から検査するフォルダーを追加します。</li></ol>
<p>検出されたファイルを勝手に削除・隔離することはありません。</p>
<p>この配布物は開発機での検証用です。Appleの公証は未実施です。別のMacでダウンロード後に
開けない場合は「システム設定」の「プライバシーとセキュリティ」で配布元を確認して開いてください。</p>
<p>同梱ソフトウェアのライセンスはLICENSES_README.txt、対応ソースは別のSources ZIPを参照してください。</p></body></html>''')
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(app)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    shutil.copytree(app, release / app.name, symlinks=True)
    for directory in (release, sources):
        archive = output / (directory.name + '.zip')
        subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(directory), str(archive)], check=True)
        archive.with_suffix('.zip.sha256').write_text(digest(archive) + '  ' + archive.name + '\n')
        print('Created', archive.name, round(archive.stat().st_size / 1024 ** 2, 2), 'MiB', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    collect(parser.parse_args().output)
