# コードの責務とOS移植の入口

2026-11-18のリファクタリング後の構成です。画面の操作、保存先、プランID、
サービス名は維持しています。OS連携の実装があるのはUbuntu/Linuxだけです。

## 画面とアプリケーション

| ファイル | 責務 |
|---|---|
| `clam_gui/__main__.py` | Application、メイン画面、選択・実行状態、画面間の接続と起動/終了 |
| `clam_gui/ui/plans.py` | プラン編集・複製・削除と予定の編集 |
| `clam_gui/ui/preferences.py` | 検査設定、監視・履歴、常駐監視の設定画面 |
| `clam_gui/ui/packages.py` | ClamAVの導入・削除・定義更新と自動更新設定の画面 |
| `clam_gui/ui/history.py` | 選択したプランの履歴・ログと外部メディア履歴 |

画面はApplicationへ組み合わせる小さなクラスに分割しました。
各クラスはApplicationの状態とコールバックを使うため、単独の別アプリではありません。
画面間の状態の所有者は引き続きApplicationです。画面の全面書換えは行っていません。

## OS連携

`clam_gui/platforms.get_backend()` がOS連携の入口です。
GUIは `self.backend` を使い、個別のOSサービスを直接構築しません。
LinuxBackendは既存のAPT・FreshClam・systemd管理を呼び出します。
Windows/macOSの担当者は、対応する実装と選択処理を追加してください。
未実装のOSは明示的なエラーを返し、Linuxの管理コマンドへフォールバックしません。

| メソッド | 戻り値・役割 |
|---|---|
| `scheduler(store)` | `apply(key, schedule)` / `remove_profile(key)` を持つ定期実行管理 |
| `packages()` | `package_names` / `status()` / `preview(action)` / `execute(plan)` を持つ導入管理 |
| `can_update(installed)` | 導入状態から定義更新の準備があるか判断 |
| `updater()` | 定義更新処理。既存の中断・進捗・復元の引数を維持 |
| `auto_update()` | 定義自動更新の `status()` / `set_enabled()` |
| `background(store)` | `path` / `supported()` / `apply(enable)` を持つ常駐起動管理 |
| `observers(engine)` | ファイル・メディアのイベント取得。既存のconfigure/close等の意味を維持 |
| `lock(path)` | `acquire()` が即時にboolを返すプロセス間ロック、`close()` で解放。`file` は取得中かの確認に利用 |
| `process_identity(pid)` | 生存個体の識別子、死亡確認済みなら空文字、確認不能ならNone |
| `notify(title, body, urgency)` | ウィンドウを持たない処理からの通知配送。失敗時は例外 |
| `definition_label()` / `definition_status()` | 各OSの定義保存先に対応する日時・鮮度の表示 |
| `install_stop_handlers(callback)` | 常駐プロセスの停止要求をメインループへ接続 |

`fcntl` はLinuxのロック実装で必要になったときだけ読み込みます。
`/proc` のプロセス識別とnotify-sendもLinux側へ移しました。
これはOS移植の入口をそろえる変更です。Windows/macOSの保存先・引数・配布・通知等を
実装済みにするものではありません。OSごとの実機確認は引き続き必要です。
GTKアプリからの通知はGio.Notificationを利用し、内容の判断だけを常駐側と共有します。

## 検査結果

`clam_gui/results.py` に `ResultKind`、`Assessment`、`ScanStatus` を置いています。
区分はclean / detected / warning / incomplete / cancelledで、検出の有無は別属性です。
検出を伴う不完了・中止も表現できます。

- 新しい検査結果はClamAVの出力と終了コードから区分を作り、表示文で再判定しません。
- `assess(status, output)` から区分・検出・正常・完了を確認します。
- `ScanStatus` は既存の `(status, output)` APIを保つための文字列互換アダプターです。説明の追記でメタデータを維持します。判断前にf-stringやstr変換で単なる文字列にしないでください。
- SQLiteへは表示文とは別に `result_kind` と既存の `detected` を保存します。
- 古い行の `result_kind` はNULLのままです。既存の行を一括書換え・削除せず、互換処理で日本語の旧表示から判定します。
- `core.summarize` / `core.has_detection` は従来の呼び出し元のため再公開しています。

通知・履歴整理・自動検査の重複抑制には、共通の判定を使用します。
不完了の検査は、検出があっても完了済みキャッシュへ登録しません。

## 監視・失敗処理・設定

- `automation.py` は待機キューと自動検査の実行、`monitor.py` は画面なしのメインループと再開確認を担当します。
- `notifications.AutomaticNotices` が、正常結果の20秒集約と異常の即時通知の方針を共通化します。配送はGUI/常駐/OSのアダプターに任せます。
- `jobs.execute_scan` は、検査と失敗報告を同じ結果形式へまとめます。自動処理で再試行する競合例外は呼び出し元へ返します。
- 自動検査のスレッド起動失敗では実行中状態を解除して履歴・完了通知へ進みます。
- `settings.Preferences` にJSON設定の読み書きを集約し、複数設定と履歴整理は呼び出し元のSQLiteトランザクションで確定できます。
- 画面の状態更新では、予定・監視設定・常駐状態を更新1回につきまとめて取得し、プランごとの再読込を抑えています。

## 検証

既存の共通・OS実装のテストを維持し、`tests/test_refactoring.py` で
表示文と判定の独立、旧結果の互換、保存と履歴整理、通知集約、OS境界、設定の原子性、失敗復帰を検証します。
GUIの操作は分離Wayland、常駐は一時データと模擬検査の独立プロセスで確認します。
実端末への導入・削除、サービス/予定の再登録、実ファイル検査はリファクタリングの検証では実行しません。
