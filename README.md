📌 リアルタイム Tick 取得 & 秒足生成アプリ（asyncio + Tkinter）
本ツールは GMO Coin FX API（WebSocket） からリアルタイムでティックを取得し、
秒足（1秒OHLC）を自動生成・保存 する Python アプリです。

Tkinter による軽量UI、asyncio + websockets による高速非同期処理、
さらに 未確定秒足のリアルタイム更新（replace_last_line） に対応しています。

🚀 主な特徴
WebSocket（asyncio）によるリアルタイムTick取得

1秒OHLCを自動生成し、CSVに保存

未確定足は replace_last_line() による上書き更新

複数通貨ペアを同時監視（USD/JPY, EUR/USD など）

Tickレート（1秒あたりのTick数）をリアルタイム表示

Tkinter UIで動作状況を可視化

自動再接続・バックプレッシャー対策

日付ローテーション（05:00/06:00切替）に対応

履歴CSVとのマージ機能（秒足補完）



🧠 技術的ポイント
asyncio + websockets による非同期処理

Tkinter UI と非同期ループの安全な共存

replace_last_line による未確定足の正確な更新
→ 行長が変化するローソク足データに対して唯一安定する方式

ファイルローテーション（前日分の切り出し）

pandas を使った履歴マージ

スレッドセーフなバッファ管理

📁 生成されるファイル例
コード
USDJPY_sec.csv
EURUSD_sec.csv
AUDJPY_sec.csv
...
内容（例）：

コード
USD_JPY,2026-05-17 12:34:56,156.123
USD_JPY,2026-05-17 12:34:57,156.125
...
📌 秒足 → マルチタイムフレーム ローソク足生成器（5S / 10S / 15S / 30S / 1M / 3M / 5M / 10M）
このツールは、上記の秒足CSVを監視しながら
複数のタイムフレームのローソク足をリアルタイム生成 するアプリです。

GUIで秒足CSVを選ぶだけで、
5秒足〜10分足まで自動生成 され、
未確定足は replace_last_line によりリアルタイム更新されます。

🚀 主な特徴
秒足CSVを監視し、複数TFのローソク足をリアルタイム生成

対応タイムフレーム

5S / 10S / 15S / 30S / 1M

3M / 5M / 10M（1分足から再集計）

未確定足は replace_last_line による上書き更新

確定足は append のみ（高速・安全）

Gap補完（欠損足を自動生成）

初回フル生成は高速化済み（ファイル開きっぱなし）

FloatingTrend（トレンド表示ウィンドウ）を同梱

ALMA

ATR

BB位置

トレンド方向アイコン（^^, ^, →, v, vv）



🧠 技術的ポイント
replace_last_line の完全対応版（CRLF/可変長行対応）

append_candle のバイナリ化によるダブり完全排除

1分足から上位足を再集計（高速・高精度）

TIMEFRAME_CONFIG による柔軟なTF追加

ファイル監視スレッド + Tkinter UI の安全な連携

初回フル生成は最適化済み（最速構造）

📁 生成されるファイル例
コード
USDJPY_5S.csv
USDJPY_10S.csv
USDJPY_15S.csv
USDJPY_30S.csv
USDJPY_1M.csv
USDJPY_3M.csv
USDJPY_5M.csv
USDJPY_10M.csv
📌 FloatingTrend（トレンド可視化ウィンドウ）
ALMAトレンド

ATR

BB位置

トレンド方向アイコン

全TFを1画面で表示

ドラッグで自由に移動可能

（スクショを貼ると強い）

🛠 動作環境
Python 3.10+

pandas / numpy

websockets

Tkinter（標準）

Windows（推奨）

📄 ライセンス
自由に利用・改変可能です。

🎯 まとめ
この2つのツールは、

リアルタイムTick処理

秒足生成

マルチTFローソク足生成

未確定足の正確な更新

UI構築

非同期処理

ファイル監視

上位足再集計
