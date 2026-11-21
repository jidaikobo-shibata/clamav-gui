"""Read-only presentation of retained logs. No file access, remediation or inference of safety."""
from collections import Counter
from dataclasses import dataclass
import datetime
import re
from .results import assess,ResultKind,found_signature,warning_signature,output_was_truncated
from .mailboxes import MAILBOX_NOTE


@dataclass(frozen=True)
class Finding:
    path: str
    detail: str


@dataclass(frozen=True)
class Group:
    title: str
    count: int
    action: str
    examples: tuple


@dataclass(frozen=True)
class Report:
    title: str
    description: str
    action: str
    metrics: tuple
    detections: tuple
    groups: tuple
    skips: tuple
    coverage: str
    conditions: str
    cautions: tuple


PREVIEW_LIMIT=100
GROUPS={
    'size':('サイズ上限','必要な対象を別プランに分け、ファイルサイズの上限を検討してください。'),
    'scan_size':('展開後の合計サイズ上限','圧縮ファイルなどの展開後の合計サイズ上限を確認してください。'),
    'files':('展開ファイル数の上限','圧縮ファイルなどに含まれるファイル数と検査上限を確認してください。'),
    'depth':('展開の深さの上限','多重に圧縮された対象か確認し、必要に応じて個別に検査してください。'),
    'limits':('その他の検査上限','詳細ログにある上限の種類を確認してください。'),
    'encrypted':('暗号化されている対象','必要な対象か確認し、内容を検査できる状態にして再検査してください。'),
    'macros':('マクロの警告','マクロが必要な文書か、入手元と用途を確認してください。'),
    'broken':('破損などの警告','対象ファイルが正しく取得できているか確認してください。'),
    'execution':('ClamAVの実行エラー','ClamAVの導入状態・実行権限・実行環境を確認してから再検査してください。'),
    'storage':('履歴保存エラー','履歴の保存先の空き容量・書込み権限・利用状態を確認してください。検査結果を失わないよう、保持した詳細ログをコピーしてください。'),
    'engine':('ClamAV本体のエラー','エラーに記載された定義ファイル・設定・読み取り権限などを確認してから再検査してください。'),
    'permission':('権限不足','対象ファイルの読み取り権限や保存場所を確認してください。'),
    'missing':('検査中に消失した対象','一時ファイルなら更新中の可能性があります。必要な対象が残っていれば再検査してください。'),
    'directory':('ディレクトリを開けない','対象ディレクトリの参照権限・保存場所・メディアの接続状態を確認してください。'),
    'read':('読み取り失敗','ファイルやメディアが利用できるか確認してください。'),
    'error':('その他の検査エラー','ClamAVの報告内容を確認してください。この分類だけで脅威の検出とは判断できません。'),
    'warning':('その他の警告','ClamAVの報告内容と検査条件を確認してください。'),
}


def _warning_group(signature):
    limits={'MaxFileSize':'size','MaxScanSize':'scan_size','MaxFiles':'files','MaxRecursion':'depth'}
    if signature.startswith('Heuristics.Limits.Exceeded.'):
        return limits.get(signature.rpartition('.')[2],'limits')
    if signature.startswith('Heuristics.Encrypted.'):return 'encrypted'
    if signature.startswith('Heuristics.OLE2.ContainsMacros'):return 'macros'
    return 'broken'


def _error_group(reason):
    reason=reason.casefold().removesuffix(' error')
    if reason=="can't open directory.":return 'directory'
    if 'permission denied' in reason or 'access denied' in reason:return 'permission'
    if 'no such file' in reason or 'not found' in reason:return 'missing'
    if any(value in reason for value in ('read error','i/o error','cannot read',"can't open",'cannot open')):return 'read'
    return 'error'


def build_report(entry, *, preview_limit=PREVIEW_LIMIT):
    """Count retained lines; preview_limit=None includes all retained details for copying."""
    counts=Counter();examples={key:[] for key in GROUPS};detections=[];detected_count=0
    skips=Counter();error_lines=0;missing_details=[]
    app_errors={'ClamAVの実行エラー：':'execution','履歴保存エラー：':'storage'}
    for line in entry.output.splitlines():
        app_key=next((key for prefix,key in app_errors.items() if line.startswith(prefix)),None)
        if app_key:
            counts[app_key]+=1
            if preview_limit is None or len(examples[app_key])<preview_limit:
                examples[app_key].append(Finding('',line.partition('：')[2].strip()))
            continue
        if line.endswith(' FOUND'):
            signature=found_signature(line);path=line[:-6].rpartition(': ')[0]
            finding=Finding(path,signature)
            if not warning_signature(line):
                detected_count+=1
                if preview_limit is None or len(detections)<preview_limit:detections.append(finding)
                continue
            key=_warning_group(signature)
        else:
            path,separator,reason=line.rpartition(': ')
            if separator and reason in ('Empty file','Symbolic link') and (path.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:[\\/]',path)):
                skips[reason]+=1
                continue
            if line.startswith('LibClamAV Error:'):
                error_lines+=1;key='engine'
                finding=Finding('',line.partition(':')[2].strip())
                counts[key]+=1
                if preview_limit is None or len(examples[key])<preview_limit:examples[key].append(finding)
                continue
            plain_path_error=(separator and reason in ('Access denied','Permission denied',"Can't open directory.")
                              and (path.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:[\\/]',path)))
            if line.endswith(' ERROR') or line.startswith('ERROR:') or plain_path_error:
                error_lines+=1;key=_error_group(reason if separator else line)
            elif line.startswith(('WARNING:','LibClamAV Warning:')):
                # App truncation warnings are explained by the coverage notice.
                if line.startswith('WARNING:') and '検出明細の保存上限' in line:
                    missing_details.append(line.removeprefix('WARNING:').strip())
                    continue
                if '出力の一部を省略' in line:continue
                key='warning'
            else:continue
            finding=Finding(path if separator and not line.startswith(('ERROR:', 'WARNING:', 'LibClamAV Warning:')) else '',reason if separator else line)
        counts[key]+=1
        if preview_limit is None or len(examples[key])<preview_limit:examples[key].append(finding)

    def number(name):
        values=re.findall(r'^'+re.escape(name)+r':\s*(\d+)\s*$',entry.output,re.M)
        return sum(int(value) for value in values if len(value) <= 18) if values and all(len(value) <= 18 for value in values) else None
    def field(name):
        values=re.findall(r'^'+re.escape(name)+r':\s*(.+)$',entry.output,re.M)
        return values[-1].strip() if values else None
    infected=number('Infected files');errors=number('Total errors');files=number('Scanned files')
    result=assess(entry.status,entry.output,kind=entry.result_kind,detected=entry.detected) if entry.result_kind else assess(entry.status,entry.output)
    uncertain=entry.is_uncertain()
    if uncertain:
        title='要確認・内訳不明'
        description=(f'ClamAVの集計に{infected:,}件の検出情報があります。' if infected else '検出情報があります。')
        description+='保持された明細が不足しているため、脅威の有無や内訳を確認できません。「検出なし」とは判断できません。'
        action='検出明細を保持できる状態で再検査してください。'
    elif entry.is_detected() or detected_count:
        title='検出あり・検査不完了' if result.kind in (ResultKind.INCOMPLETE,ResultKind.CANCELLED) or errors or counts or missing_details else '検出あり'
        description='脅威として報告された対象があります。保存ファイルの検出だけで、この端末での感染・実行が確定するわけではありません。'
        action='対象のファイル名・検出名・入手元を確認してください。自動削除・隔離は行っていません。'
        if not detections:action='検出ありとして記録されていますが、明細を抽出できません。詳細ログを確認し、必要に応じて再検査してください。'
    elif result.kind==ResultKind.CANCELLED:
        title='検査を中止しました';description='未検査の対象が残っている可能性があります。';action='必要な範囲を再検査してください。'
    elif result.clean and not counts and not errors:
        title='検査完了・検出なし';description='この検査では脅威の報告はありませんでした。';action='今回の結果に対する追加の操作はありません。定期的な検査を継続してください。'
    else:
        title='検査不完了' if result.kind==ResultKind.INCOMPLETE or errors else '確認が必要です'
        description='完全に検査できなかった対象、または確認が必要な警告があります。'
        action='理由別の一覧を開き、必要な対象を確認して再検査してください。'
        if not counts:description=entry.status or '詳しい理由が記録されていません。'

    # A storage problem must not hide an earlier failure or cancellation.
    explicit_failure=entry.status.startswith('検査不完了：') and not entry.status.startswith('検査不完了：履歴を保存できませんでした')
    app_advice=' '.join(GROUPS[key][1] for key in ('execution','storage') if counts[key])
    if app_advice:
        if explicit_failure:
            description+='\n'+entry.status
        storage_only=(not (entry.is_detected() or detected_count or uncertain or explicit_failure)
                      and result.kind!=ResultKind.CANCELLED
                      and set(counts).issubset({'execution','storage'}))
        action=app_advice if storage_only else action+' '+app_advice

    if result.kind==ResultKind.CANCELLED:
        if entry.is_detected() or detected_count:
            title='検出あり・検査中止'
        if title!='検査を中止しました':
            description+=' 検査は中止されており、未検査の対象が残っている可能性があります。'
            action+=' 未検査の範囲を再検査してください。'
    if missing_details:
        description+=' 検出明細の保存上限に達し、特定できない検出対象が残っています。'
        action+=' 検出明細を保持できるよう、検査範囲を分けて再検査してください。'

    metrics=[('完了日時',entry.date_label()),('検査ファイル数',f'{files:,}件' if files is not None else '不明（記録なし）')]
    seconds=None
    value=field('Time')
    if value:
        match=re.match(r'(\d+(?:\.\d+)?)\s+sec',value)
        if match and len(match[1]) <= 18:seconds=int(float(match[1]))
    if seconds is None:
        try:
            seconds=max(0,int((datetime.datetime.fromisoformat(entry.completion_timestamp())-datetime.datetime.fromisoformat(entry.started)).total_seconds()))
        except (TypeError,ValueError,OverflowError):pass
    if seconds is not None:
        hours,rest=divmod(seconds,3600);minutes,seconds=divmod(rest,60)
        metrics.append(('所要時間',f'{hours}時間{minutes}分{seconds}秒'))
    else:metrics.append(('所要時間','不明（記録なし）'))
    if field('Data read'):metrics.append(('読み取り量',field('Data read').split(' (',1)[0]))
    if infected is not None:metrics.append(('ClamAVの検出集計',f'{infected:,}件（警告を含む場合があります）'))
    if errors is not None:metrics.append(('ClamAVのエラー集計',f'{errors:,}件'))

    groups=tuple(Group(title,counts[key],advice,tuple(examples[key])) for key,(title,advice) in GROUPS.items() if counts[key])
    if detections:
        detections_group=(Group('脅威として報告された対象',detected_count,action,tuple(detections)),)
    else:detections_group=()
    skip_labels={'Empty file':'空ファイルのため検査省略','Symbolic link':'リンクを追跡しない設定のため検査省略'}
    for reason,label in skip_labels.items():
        saved=re.findall(r'^'+re.escape(label)+r'：(\d+)件\s*$',entry.output,re.M)
        if saved and all(len(value) <= 18 for value in saved):skips[reason]=sum(map(int,saved))
    skip_rows=tuple((skip_labels[key],f'{value:,}件') for key,value in skips.items() if value)
    truncated=output_was_truncated(entry.output)
    coverage='分類の件数は、保持したログから確認できた明細の件数です。ClamAVの集計とは数え方が異なり、重複する場合があります。'
    if truncated:coverage='この記録はログの一部が省略されています。以下の明細・分類は全体の内訳ではありません。'+coverage
    if errors is not None:coverage+=f' エラー集計は{errors:,}件、抽出できたエラー明細は{error_lines:,}件です。'
    conditions=entry.target
    marker=re.search(r'^-{2,}\s*この検査で使用した設定\s*-{2,}\s*$',entry.output,re.M)
    if marker:conditions+='\n\n'+entry.output[marker.end():].strip()
    cautions=list(dict.fromkeys(missing_details))
    if MAILBOX_NOTE in entry.status or MAILBOX_NOTE in entry.output:
        cautions.append('メール保存ファイルは複数のメールを含む場合があります。保存ファイル自体を削除せず、メールアプリで確認してください。')
    return Report(title,description,action,tuple(metrics),detections_group,groups,skip_rows,coverage,conditions,tuple(cautions))
