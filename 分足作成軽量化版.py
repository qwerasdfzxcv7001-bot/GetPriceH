# unified_candle_generator_and_trend.py (拡張性向上版)

# --- ローソク足リアルタイム生成の重要メモ ---
#
# このシステムは「未確定足もリアルタイムでファイルに反映する」方式を採用している。
# これにより、チャートや Commander が常に“今の足”を参照でき、
# リアルタイム性が非常に高いというメリットがある。
#
# ただし、未確定足の更新には replace_last_line() を使う必要があり、
# この処理はローソク足特有の「行長が tick ごとに変化する」性質と強く関係する。
#
# 【重要ポイント】
# 1. append_candle() はバイナリモード（"ab"）で書く必要がある
#    - テキストモード("a", encoding="utf-8")だと \n が \r\n に変換される
#    - replace_last_line() はバイナリで改行を探すため、改行形式がズレると
#      truncate の位置が1バイトでもズレてゴミ行が残り、ダブりが発生する
#    - バイナリモードにすることで改行形式が統一され、ズレが完全に消える
#
# 2. replace_last_line() は「1バイトずつ後ろに戻って改行位置を探す」方式が唯一安定
#    - ブロック読みや offset キャッシュなどの高速化を行うと、
#      行長変化に追従できず、必ずダブりが発生する（構造的な理由）
#    - この“遅いが正確な”方式だけが、ローソク足のような可変長行に対して
#      100% 安定して動作する
#
# 3. update_candles() のロジックはすでに最適化済み
#    - 未確定足 → replace_last_line（上書きのみ）
#    - 確定足 → append_candle（追記のみ）
#    - gap 補完 → append_candle
#    - 新しい足 → メモリ保持のみ（未確定なので書かない）
#    この構造はリアルタイム性・安定性・整合性のバランスが最良
#
# 4. 初回フル生成(build_initial_minutes)も構造的に最速
#    - ファイル開きっぱなし
#    - パースのインライン化
#    - floor_dt の軽量化
#    - gap 補完なし
#    - 書き込みは append のみ
#    これ以上の高速化は Python の for ループ限界のため誤差レベル
#
# 【結論】
# ・未確定足を書き込む方式はリアルタイム性が圧倒的に高い
# ・replace_last_line は高速化すると必ず壊れる（行長変化 × truncate 問題）
# ・append_candle をバイナリにしたことで、ダブり・抜けは完全に解消
# ・現在の構成が速度・安定性・リアルタイム性の最適解
#
# 将来的にさらに高速化したい場合は、
# 「未確定足をファイルに書かず、確定足だけ書く方式」
# または「非同期書き込み方式」に切り替える必要がある。

import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import time
import os
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

# --- 設定辞書 (タイムフレームの追加・削除が容易に) ---
# キー: タイムフレーム名 (例: "15S", "1M")
# 値: そのタイムフレームの秒数 (例: 15, 60)
TIMEFRAME_CONFIG = {
    "5S": 5,
    "10S": 10,
    "15S": 15,
    "30S": 30,
    "1M": 60,   # 秒足から生成するのはここまで
    # 上位足は 1M から再集計するため、ここには入れない
}
HIGHER_TIMEFRAMES = {
    "3M": 3,
    "5M": 5,
    "10M": 10,
    # "15M": 15,
}

bairitsu = 1000

DEFAULT_DATA_DIR = "./分足ローソク" # デフォルトのデータディレクトリ名

# --- グローバル状態 ---
# CandleGeneratorApp が管理する状態
running = False
file_path = None
symbol = None # CandleGeneratorApp が秒足ファイルから抽出するシンボル

# 足バッファ（すべて独立）
# TIMEFRAME_CONFIG のキーに応じて動的に管理されるようになる
timeframe_keys = {} # {tf_name: key_datetime}
timeframe_buffers = {} # {tf_name: {"open": ..., "high": ..., ...}}

# 出力パス
output_paths = {} # {tf_name: file_path}

# ファイル監視スレッド
watch_thread = None

# --- ログ出力 ---
def _log_append(log_widget, msg):
    log_widget.insert(tk.END, msg + "\n")
    log_widget.see(tk.END)

# --- CSVパース ---
def parse_line(line):
    parts = line.strip().split(",")
    if len(parts) != 3:
        return None
    _symbol, dt_str, price_str = parts
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        price = float(price_str)
    except:
        return None
    return _symbol, dt, price

# --- 1分足から上位足を再集計する関数 ---
def aggregate_from_minutes(df_1m, minutes):
    """
    1分足 df から 3M / 5M / 10M などの上位足を生成する。
    秒足を読む必要がなくなるため、負荷が激減し、精度も安定する。
    """
    df = df_1m.copy()
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time")

    # pandas の 'T' は将来廃止 → 'min' を使う
    rule = f"{minutes}min"

    agg = df.resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last"
    }).dropna()

    return agg.reset_index()

# --- 書き込み処理（バイナリ版） ---
def append_candle(path, key, c):
    dt_str = key.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{dt_str},{c['open']},{c['high']},{c['low']},{c['close']}\n"
    try:
        with open(path, "ab") as f:  # ← バイナリモード
            f.write(line.encode("utf-8"))
    except IOError as e:
        raise e

# --- CRLF完全対応 replace_last_line ---
# NOTE:
# この replace_last_line は「1バイトずつ後ろに戻って改行位置を正確に探す」方式。
# ローソク足のように tick ごとに行の長さが変わるデータでは、
# truncate の位置が 1 バイトでもズレるとゴミ行が残り、同じ timestamp の行が増殖する。
#
# 高速化（ブロック読み、offset キャッシュ、CRLF 簡易処理など）を行うと
# 行長変化に追従できず、必ずダブりが発生するため、
# この「遅いが正確な」実装が唯一安定して動作する。
#
# つまり：
# ・高速化すると必ずダブる（構造的な理由）
# ・この実装だけが行長変化に完全対応できる
# ・未確定足をファイルに書く限り、この方式が最も安全
#
# 将来的に高速化したい場合は、
# 「未確定足をファイルに書かない方式」に切り替える必要がある。
def replace_last_line(path, key, c):
    dt_str = key.strftime("%Y-%m-%d %H:%M:%S")
    new_line = f"{dt_str},{c['open']},{c['high']},{c['low']},{c['close']}\n"
    data = new_line.encode("utf-8")

    if not os.path.exists(path):
        try:
            with open(path, "wb") as f:
                f.write(data)
        except IOError as e:
            raise e
        return

    with open(path, "rb+") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()

        if pos == 0:
            try:
                f.write(data)
            except IOError as e:
                raise e
            return

        search_pos = pos - 1
        while search_pos >= 0:
            f.seek(search_pos)
            if f.read(1) in (b"\n", b"\r"):
                search_pos -= 1
            else:
                break

        while search_pos >= 0:
            f.seek(search_pos)
            if f.read(1) == b"\n":
                search_pos += 1
                break
            search_pos -= 1

        if search_pos < 0:
            search_pos = 0

        try:
            f.seek(search_pos)
            f.write(data)
            f.truncate()
        except IOError as e:
            raise e

from datetime import datetime
import os

# --- datetime 切り捨て高速版 ---
def floor_dt(dt, sec):
    s = (dt.second // sec) * sec
    return dt.replace(second=s, microsecond=0)

# --- 初回フル生成 ---
def build_initial_minutes(sec_file, app_instance):
    global timeframe_keys, timeframe_buffers  # グローバル状態を更新

    app_instance.log("初回フル生成開始…")

    if not os.path.exists(sec_file):
        app_instance.log(f"エラー: 初回生成ファイルが見つかりません: {sec_file}")
        return None

    # --- 出力ファイルを開きっぱなしにして I/O を激減 ---
    writers = {
        tf: open(output_paths[tf], "a", encoding="utf-8")
        for tf in TIMEFRAME_CONFIG
    }

    tf_conf = TIMEFRAME_CONFIG
    tf_keys = timeframe_keys
    tf_bufs = timeframe_buffers

    last_pos = 0

    try:
        with open(sec_file, "r", encoding="utf-8") as f:
            for line in f:
                # --- parse_line() をインライン化（高速化） ---
                parts = line.strip().split(",")
                if len(parts) != 3:
                    continue

                symbol = parts[0]
                try:
                    dt = datetime.fromisoformat(parts[1])
                except:
                    continue

                try:
                    price = float(parts[2])
                except:
                    continue

                # --- 各タイムフレーム処理 ---
                for tf_name, interval_sec in tf_conf.items():
                    key_dt = floor_dt(dt, interval_sec)

                    # 初回 or 同じ足の更新
                    if tf_name not in tf_keys or tf_keys[tf_name] is None:
                        tf_keys[tf_name] = key_dt
                        tf_bufs[tf_name] = {
                            "open": price, "high": price,
                            "low": price, "close": price
                        }

                    elif key_dt == tf_keys[tf_name]:
                        buf = tf_bufs[tf_name]
                        buf["high"] = max(buf["high"], price)
                        buf["low"] = min(buf["low"], price)
                        buf["close"] = price

                    else:
                        # --- 足確定：即書き込み（メモリ節約） ---
                        buf = tf_bufs[tf_name]
                        writers[tf_name].write(
                            f"{tf_keys[tf_name]},{buf['open']},{buf['high']},{buf['low']},{buf['close']}\n"
                        )

                        # 新しい足
                        tf_keys[tf_name] = key_dt
                        tf_bufs[tf_name] = {
                            "open": price, "high": price,
                            "low": price, "close": price
                        }

            last_pos = f.tell()

        # --- 最後の未確定足を確定して書き込み ---
        for tf_name in tf_conf.keys():
            if tf_name in tf_bufs and tf_bufs[tf_name]:
                buf = tf_bufs[tf_name]
                writers[tf_name].write(
                    f"{tf_keys[tf_name]},{buf['open']},{buf['high']},{buf['low']},{buf['close']}\n"
                )

        app_instance.log("初回フル生成完了！")
        return last_pos

    except Exception as e:
        app_instance.log(f"初回生成中に予期せぬエラー: {e}")
        return None

    finally:
        # --- ファイルを閉じる ---
        for w in writers.values():
            w.close()

# --- リアルタイム更新 ---
def update_candles(dt, price, app_instance):
    global timeframe_keys, timeframe_buffers

    tf_conf = TIMEFRAME_CONFIG
    tf_keys = timeframe_keys
    tf_bufs = timeframe_buffers

    # --- datetime 切り捨て高速版 ---
    def floor_dt(dt, sec):
        s = (dt.second // sec) * sec
        return dt.replace(second=s, microsecond=0)

    for tf_name, interval_sec in tf_conf.items():

        path = output_paths[tf_name]
        last_key = tf_keys.get(tf_name)
        candle = tf_bufs.get(tf_name)

        key_dt = floor_dt(dt, interval_sec)

        # --- 初回 ---
        if last_key is None or candle is None:
            new_c = {"open": price, "high": price, "low": price, "close": price}
            append_candle(path, key_dt, new_c)
            tf_keys[tf_name] = key_dt
            tf_bufs[tf_name] = new_c
            continue

        # --- 同じ足（未確定足）→ replace_last_line のみ ---
        if key_dt == last_key:
            candle["high"] = max(candle["high"], price)
            candle["low"]  = min(candle["low"], price)
            candle["close"] = price

            # ★ append しない！未確定足は上書きのみ
            replace_last_line(path, last_key, candle)
            continue

        # --- 足が切り替わった（確定足）→ append のみ ---
        append_candle(path, last_key, candle)

        # --- Gap 補完 ---
        check_key = last_key + timedelta(seconds=interval_sec)
        while check_key < key_dt:
            c = candle["close"]
            gap_c = {"open": c, "high": c, "low": c, "close": c}
            append_candle(path, check_key, gap_c)
            check_key += timedelta(seconds=interval_sec)

        # --- 新しい足（未確定）→ append してはいけない ---
        new_c = {"open": price, "high": price, "low": price, "close": price}

        # ★ 新しい足は append しない（未確定だから）
        tf_keys[tf_name] = key_dt
        tf_bufs[tf_name] = new_c

        # ============================================================
        # ★ ここから追加：1分足が更新されたら上位足を再集計する
        # ============================================================
        if tf_name == "1M":
            try:
                # 1分足を読み込む
                df_1m = pd.read_csv(output_paths["1M"], header=None)
                df_1m.columns = ["time", "open", "high", "low", "close"]

                # 3M
                df_3m = aggregate_from_minutes(df_1m, 3)
                df_3m.to_csv(output_paths["3M"], index=False, header=False)

                # 5M
                df_5m = aggregate_from_minutes(df_1m, 5)
                df_5m.to_csv(output_paths["5M"], index=False, header=False)

                # 10M
                df_10m = aggregate_from_minutes(df_1m, 10)
                df_10m.to_csv(output_paths["10M"], index=False, header=False)

                # 15M
                # df_15m = aggregate_from_minutes(df_1m, 15)
                # df_15m.to_csv(output_paths["15M"], index=False, header=False)

            except Exception as e:
                app_instance.log(f"上位足再集計エラー: {e}")


# --- ファイル監視 ---
# TIMEFRAME_CONFIG の数に関わらず、秒足ファイルの更新を監視し、update_candles を呼び出す
def watch_file(sec_file, start_pos, app_instance):
    log_method = app_instance.log

    log_method("リアルタイム監視開始")

    if not os.path.exists(sec_file):
        log_method(f"エラー: ファイルが見つかりません: {sec_file}")
        return
        
    try:
        with open(sec_file, "r", encoding="utf-8") as f:
            f.seek(start_pos)

            while running: # グローバル変数 running を参照
                line = f.readline()
                if not line:
                    time.sleep(0.1)
                    continue

                parsed = parse_line(line)
                if parsed:
                    _symbol, dt, price = parsed
                    # GUIスレッドで安全に更新をスケジュール
                    app_instance.after(0, update_candles, dt, price, app_instance)
                    
    except FileNotFoundError:
        log_method(f"エラー: ファイルが見つかりません: {sec_file}")
    except IOError as e:
        log_method(f"ファイル監視中のI/Oエラー: {e}")
    except Exception as e:
        log_method(f"ファイル監視中に予期せぬエラー: {e}")
    finally:
        log_method("監視終了")
        if running: # もしrunningがTrueのまま監視が終了したら、stop()を呼んで状態をクリーンにする
            app_instance.stop()

# --- GUI本体 (CandleGeneratorApp クラス) ---
class CandleGeneratorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("秒足 → マルチTF生成器 (拡張版)")
        self.geometry("700x700") # GUIのサイズを調整

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # グローバル状態の初期化 (Applicationインスタンスへの参照)
        #TIMEFRAME_CONFIG を基に、動的にバッファやパスを初期化
        self.initialize_global_states()
        
        self.log_widget = self.log_box

        # FloatingTrendを起動するためのボタン　
        self.btn_launch_trend = tk.Button(self.frame_buttons, text="トレンド表示起動", command=self.launch_trend_viewer, width=12)
        self.btn_launch_trend.pack(side="left", padx=5)

    def initialize_global_states(self):
        """TIMEFRAME_CONFIG に基づいて、グローバル変数を初期化する。"""
        global timeframe_keys, timeframe_buffers, output_paths, file_path, symbol, watch_thread
        
        timeframe_keys = {}
        timeframe_buffers = {}
        output_paths = {}
        file_path = None
        symbol = None
        watch_thread = None

        # TIMEFRAME_CONFIG の各タイムフレームについて初期化
        for tf_name in TIMEFRAME_CONFIG.keys():
            timeframe_keys[tf_name] = None
            timeframe_buffers[tf_name] = None
            # output_paths は setup_output_files で決定されるので、ここでは空のまま
    
    def _build_ui(self):
        """GUI要素を構築する。"""
        main_frame = tk.Frame(self, padx=10, pady=10)
        main_frame.pack(fill="both", expand=True)

        self.frame_buttons = tk.Frame(main_frame)
        self.frame_buttons.pack(fill="x", pady=(0, 10))

        self.btn_select_file = tk.Button(self.frame_buttons, text="秒足CSV選択", command=self.select_file, width=12)
        self.btn_select_file.pack(side="left", padx=5)
        self.btn_start = tk.Button(self.frame_buttons, text="開始", command=self.start, state="normal", width=12)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop = tk.Button(self.frame_buttons, text="停止", command=self.stop, state="disabled", width=12)
        self.btn_stop.pack(side="left", padx=5)

        self.lbl_file = tk.Label(main_frame, text="秒足CSVファイルが選択されていません", anchor="w", justify="left", wraplength=650)
        self.lbl_file.pack(fill="x", pady=(0, 5))

        self.log_box = tk.Text(main_frame, width=80, height=22, wrap="word", font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, pady=(0, 10))
        scrollbar = tk.Scrollbar(self.log_box, command=self.log_box.yview)
        self.log_box.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
    
    def log(self, msg):
        """GUIイベントループに処理をスケジュールしてログを出力する。"""
        self.log_box.after(0, lambda: _log_append(self.log_widget, msg))

    def select_file(self):
        """秒足CSVファイルを選択するダイアログを開く。"""
        global file_path
        path = filedialog.askopenfilename(
            title="秒足CSVファイルを選択",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return
        file_path = path
        self.lbl_file.config(text=f"選択ファイル: {os.path.basename(file_path)}")
        self.log(f"秒足CSVファイルを選択しました: {file_path}")

    def setup_output_files(self, sec_file_path):
        """出力ディレクトリとファイルパスを設定する。TIMEFRAME_CONFIG を動的に使用。"""
        global output_paths, symbol, bairitsu

        base_dir = os.path.dirname(sec_file_path)
        filename = os.path.basename(sec_file_path)
        symbol = filename[:6] if len(filename) >= 6 else filename  # シンボル抽出

        self.current_data_dir = os.path.join(base_dir, DEFAULT_DATA_DIR)
        self.current_symbol = symbol

        os.makedirs(self.current_data_dir, exist_ok=True)

        # --- 秒足→1分足までの出力ファイルを作成 ---
        output_paths.clear()  # 既存のパスをクリア
        for tf_name in TIMEFRAME_CONFIG.keys():
            output_paths[tf_name] = os.path.join(
                self.current_data_dir, f"{self.current_symbol}_{tf_name}.csv"
            )

            if os.path.exists(output_paths[tf_name]):
                try:
                    os.remove(output_paths[tf_name])
                except OSError as e:
                    self.log(f"既存ファイル削除エラー ({output_paths[tf_name]}): {e}")

        for tf_name, minutes in HIGHER_TIMEFRAMES.items():
            path = os.path.join(self.current_data_dir, f"{self.current_symbol}_{tf_name}.csv")
            output_paths[tf_name] = path

            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as e:
                    self.log(f"既存ファイル削除エラー ({path}): {e}")
        if symbol == "ETHUSD":
            bairitsu = 10
        elif symbol == "BTCUSD":
            bairitsu = 0.1
        else :
            bairitsu = 1000
        
        # --- ログ出力 ---
        self.log(f"通貨ペア: {self.current_symbol}")
        self.log(f"出力ディレクトリ: {self.current_data_dir}")
        for tf_name, path in output_paths.items():
            self.log(f"{tf_name}足出力先: {os.path.basename(path)}")

        return True

    def start(self):
        """ローソク足生成処理を開始する。"""
        global running, watch_thread
        if running:
            self.log("既に実行中です。")
            return
        
        # --- ガード節の強化 ---
        # file_path が None の場合、警告を表示して処理を中断する
        if file_path is None:
            messagebox.showwarning("警告", "秒足CSVファイルを選択してください。")
            self.log("エラー: 秒足CSVファイルが選択されていません。") # ログにも記録
            return
        # --- ここまでが修正箇所 ---

        # setup_output_files は file_path が None でなければ呼び出されるので、
        # その返り値チェックはそのまま維持
        if not self.setup_output_files(file_path):
            self.log("出力設定に失敗したため、開始できません。")
            return
        
        running = True
        self.btn_select_file.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_launch_trend.config(state="normal")
        self.log("処理を開始します。")

        # 初回フル生成とリアルタイム監視スレッドの開始
        # build_initial_minutes は file_path が None でないことを前提とする
        last_pos = build_initial_minutes(file_path, self)

        if last_pos is not None:
            watch_thread = threading.Thread(
                target=watch_file,
                args=(file_path, last_pos, self),
                daemon=True
            )
            watch_thread.start()
            self.log("リアルタイム監視を開始しました。")
        else:
            self.log("初回生成に失敗したため、監視を開始できません。")
            self.stop() # エラー時は停止状態に戻す


    def stop(self):
        """ローソク足生成処理を停止する。"""
        global running, watch_thread
        if not running:
            return
        
        running = False
        self.log("停止処理を実行します。")

        self.btn_select_file.config(state="normal")
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_launch_trend.config(state="disabled")

        if watch_thread and watch_thread.is_alive():
            watch_thread.join(timeout=1.0)

        # グローバル状態のクリア (TIMEFRAME_CONFIG に基づいてクリア)
        self.initialize_global_states()
        
        global file_path
        file_path = None
        self.lbl_file.config(text="秒足CSVファイルが選択されていません")

        self.log("処理を停止しました。")

    def launch_trend_viewer(self):
        # まだ start() が押されていない場合
        if not hasattr(self, "current_symbol") or not hasattr(self, "current_data_dir"):
            messagebox.showwarning("警告", "先に開始ボタンを押してローソク足生成を開始してください。")
            return

        FloatingTrend(
            symbol_to_monitor=self.current_symbol,
            data_dir=self.current_data_dir,
            parent_app=self
        )
    
    def on_closing(self):
        """メインウィンドウを閉じる際の処理。"""
        if running:
            self.stop()
        if hasattr(self, 'trend_window') and self.trend_window and self.trend_window.winfo_exists():
            self.trend_window.destroy()
        self.destroy()


# --- FloatingTrend クラス (Toplevel ウィジェット) ---
# TIMEFRAME_CONFIG を参照して動的にラベルを生成
# ===== ALMA, ATR, trend_icon, icon_color, body_avg =====
# （モジュールレベルに置く）

def alma(series, period=17, offset=0.25, sigma=6.0):
    if len(series) < period:
        return None
    m = offset * (period - 1)
    s = period / sigma
    idx = np.arange(period)
    w = np.exp(-((idx - m)**2) / (2 * s * s))
    w /= w.sum()
    return np.convolve(series, w[::-1], mode="valid")

def atr(df, period=100):
    if len(df) < period + 2:
        return None
    high = df["high"]
    low = df["low"]
    close_prev = df["close"].shift(1)
    tr = np.maximum(high - low, np.maximum(abs(high - close_prev), abs(low - close_prev)))
    return tr.rolling(period).mean().iloc[-1]

def body_avg(df, n=20):
    if len(df) < n:
        return None
    body = (df["close"] - df["open"]).abs()
    return body.tail(n).mean()

def trend_icon(slope, atr_value):
    if atr_value is None or np.isnan(atr_value):
        return "-"
    th_high = atr_value * 0.50
    th_low  = atr_value * 0.08
    if slope > th_high: return "^^"
    if slope > th_low:  return "^"
    if slope < -th_high: return "vv"
    if slope < -th_low:  return "v"
    return "→"

def icon_color(icon):
    return {
        "^^": "#0040FF",
        "^":  "#4DA6FF",
        "→":  "#BBBBBB",
        "v":  "#FF6666",
        "vv": "#CC0000"
    }.get(icon, "#FFFFFF")

def bb_position_alma(df, alma_period=17):
    close = df["close"].values
    if len(close) < alma_period:
        return None

    # --- ALMA 中心線 ---
    alma_series = alma(close, period=alma_period)
    if alma_series is None or len(alma_series) == 0:
        return None

    alma_center = alma_series[-1]

    # --- ALMA からの残差で標準偏差を計算 ---
    # alma_series は valid なので、末尾 alma_period 分だけ対応
    residual = close[-len(alma_series):] - alma_series
    std = residual.std()

    upper = alma_center + 2 * std
    lower = alma_center - 2 * std
    last_close = close[-1]

    if upper == lower:
        return None

    pos = (last_close - lower) / (upper - lower) * 200 - 100
    return pos

def bb_position(df, period=14):
    if len(df) < period:
        return None

    close = df["close"].values
    ma = close[-period:].mean()
    std = close[-period:].std()

    upper = ma + 2 * std
    lower = ma - 2 * std
    last_close = close[-1]

    if upper == lower:
        return None

    pos = (last_close - lower) / (upper - lower) * 200 - 100
    return pos

def set_bairitsu(bai):
    global bairitsu
    bairitsu = bai

# ===== 完全統合版 FloatingTrend =====
class FloatingTrend(tk.Toplevel):
    def __init__(self, symbol_to_monitor, data_dir, parent_app=None):
        super().__init__(parent_app)

        self.symbol = symbol_to_monitor
        self.data_dir = data_dir
        self.parent_app = parent_app
        self.all_tf = list(TIMEFRAME_CONFIG.keys()) + list(HIGHER_TIMEFRAMES.keys())
        
        self.overrideredirect(True)  # 枠なし
        self.attributes("-topmost", True)
        self.config(bg="#222222")

        self.offset_x = 0
        self.offset_y = 0

        self.labels = {}
        self.labels_bb = {}
        self.build_ui()
        self.update_loop()

    def start_move(self, event):
        self.offset_x = event.x
        self.offset_y = event.y

    def do_move(self, event):
        x = self.winfo_pointerx() - self.offset_x
        y = self.winfo_pointery() - self.offset_y
        self.geometry(f"+{x}+{y}")

    def build_ui(self):
        row = 0
        for tf_name in self.all_tf:
            # 元のラベル（そのまま）
            lbl = tk.Label(
                self,
                text=f"{tf_name}  -",
                font=("Consolas", 14, "bold"),
                fg="white",
                bg="#222222"
            )
            lbl.grid(row=row, column=0, padx=5, pady=1, sticky="w")

            lbl.bind("<Button-1>", self.start_move)
            lbl.bind("<B1-Motion>", self.do_move)
            lbl.bind("<Button-3>", lambda e: self.destroy())

            self.labels[tf_name] = lbl

            # ★ ここに BB 専用ラベルを追加 ★
            lbl_bb = tk.Label(
                self,
                text="BB:-",
                font=("Consolas", 14, "bold"),
                fg="#FFCC00",   # BB の色（自由に変更）
                bg="#222222"
            )
            lbl_bb.grid(row=row, column=1, padx=5, pady=1, sticky="w")

            lbl_bb.bind("<Button-1>", self.start_move)
            lbl_bb.bind("<B1-Motion>", self.do_move)
            lbl_bb.bind("<Button-3>", lambda e: self.destroy())

            # BB ラベル辞書に登録
            self.labels_bb[tf_name] = lbl_bb

            row += 1
        
    def update_loop(self):
        for tf_name in self.all_tf:
            path = os.path.join(self.data_dir, f"{self.symbol}_{tf_name}.csv")

            if not os.path.exists(path):
                self.labels[tf_name].config(text=f"{tf_name}  ×", fg="white")
                self.labels_bb[tf_name].config(text="BB:-", fg="white")
                continue

            try:
                df = pd.read_csv(path, header=None)
                df.columns = ["time", "open", "high", "low", "close"]
            except:
                self.labels[tf_name].config(text=f"{tf_name}  E", fg="white")
                self.labels_bb[tf_name].config(text="BB:-", fg="white")
                continue

            alma_series = alma(df["close"].values)
            atr_value = atr(df)

            if alma_series is None or len(alma_series) < 6:
                icon = "-"
            else:
                slope = alma_series[-1] - alma_series[-6]
                icon = trend_icon(slope, atr_value)

            color = icon_color(icon)
            avg_body = body_avg(df, 20)

            # --- BB位置％（現在値）
            bb_pos = bb_position_alma(df, alma_period=17)
            bb_text = "-" if bb_pos is None else f"{bb_pos:.1f}"

            # --- 直近3本の BB位置％の平均 ---
            bb_list = []
            for i in range(3):
                sub_df = df.iloc[:len(df)-i] if i > 0 else df
                v = bb_position_alma(sub_df, alma_period=17)
                if v is not None:
                    bb_list.append(v)

            bb_last3_avg = sum(bb_list) / 3 if len(bb_list) == 3 else None

            # --- BB の勢いで色決定（±3以内なら黄色） ---
            if bb_last3_avg is None or bb_pos is None:
                bb_color = "white"
            else:
                diff = bb_pos - bb_last3_avg

                if abs(diff) <= 3:
                    bb_color = "yellow"
                else:
                    bb_color = "cyan" if diff > 0 else "red"

            # --- body 表示 ---
            body_text = "-" if avg_body is None else f"{avg_body * bairitsu:.1f}"

            # ★ メイン部分（TF / icon / body）
            self.labels[tf_name].config(
                text=f"{tf_name:<3} {icon:<2} {body_text:<5}",
                fg=color
            )

            # ★ BB 部分だけ独立色（勢いで色分け）
            self.labels_bb[tf_name].config(
                text=f"BB:{bb_text:<6}",
                fg=bb_color
            )

        self.after(1000, self.update_loop)


# --- Main application entry point ---
if __name__ == "__main__":
    app = CandleGeneratorApp()
    app.mainloop()
