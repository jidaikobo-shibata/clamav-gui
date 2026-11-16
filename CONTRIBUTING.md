# 開発の分担

## 構成

- clam_gui/core.py：対象・設定・履歴の保存と検査実行
- clam_gui/options.py：許可するClamAVオプションと対応状況の確認
- clam_gui/__main__.py：GTK 4 GUI
- clam_gui/schedule.py：Ubuntuのユーザーsystemd連携
- clam_gui/notifications.py：通知文の生成
- scripts/run_scheduled.py：GTKを起動しない定期検査CLI

## macOS対応で確認する箇所

- Python/GTK 4/PyGObjectの導入とアプリの配布方法
- ClamAVの実行ファイルと定義ファイルの検出。Homebrew等の設置先に対応
- systemdの代わりにlaunchdを利用するスケジュール実装
- notify-sendの代わりにmacOSの通知を利用
- XDGデータ領域、固定/usr/bin/python3、GUI分離テスト等のLinux前提の置換
- 共有ロックの配置と権限、スリープ復帰時の動作
- Linux向けテストをスキップする条件とmacOS向けの検証追加

core.pyにもfcntlとUnixパスの前提が残っています。Windows対応では排他処理も分離します。
OSごとの処理を切り分け、Ubuntuで動いている実装を保ちながら移植してください。
ClamAVのシステム設定や他アプリの設定を自動で上書きしないでください。

## コミットへ含めないもの

実データ、メール、検査ログ、SQLite、個人用絶対パス、トークン、鍵、個別環境の運用記録。
テストは一時領域と架空のデータを利用します。

Ubuntuの導入・削除はclam_gui/packages.pyへ分離しています。macOSではこの実装を流用せず、導入元に合わせた管理処理を追加してください。
