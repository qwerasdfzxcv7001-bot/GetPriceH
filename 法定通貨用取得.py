import subprocess, sys

# --- ライブラリ自動インストール ---
def ensure(pkg, pip_name=None):
    try:
        __import__(pkg)
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", pip_name or pkg], shell=True)

# --- 必要なライブラリを先に確保 ---
ensure("websockets", "websockets")
ensure("pandas", "pandas")

# --- ここから通常の import ---
import os
import json
import tkinter as tk
import tkinter.messagebox as msg
import threading
import datetime
import time
import ssl
import asyncio
from tkinter import filedialog
import pandas as pd
import glob
from collections import deque
import logging
import websockets

koshin = 8
kofun = 0

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- バックプレッシャー関連の定数（asyncio版ではキューは使わず、その場処理に変更） ---
MAX_QUEUE_SIZE = 2000  # 予備（必要なら asyncio.Queue に戻せるように残しておく）

# --- 主要な定数定義 ---
ALL_SYMBOLS = ["USD_JPY", "EUR_JPY", "EUR_USD", "GBP_JPY", "AUD_JPY", "AUD_USD"]
SERVER_OFFSET_HOURS = 9
JST_TZ = datetime.timezone(datetime.timedelta(hours=SERVER_OFFSET_HOURS))

# --- グローバル状態の初期化 ---
def _initialize_global_state():
    global symbols, connections, ui_buffer, sec_buffer, buffer_lock
    global candle_history, sec_state, last_tick_sec, last_tick_date_jst
    global last_written_sec, tick_counter, tick_rate
    global symbol_window, symbol_vars, timeframe_mode
    global BASE_PATH, BASE_PATH_LOCAL, hozon
    global labels, open_prices, last_prices, history_labels
    global SAVE_TICKS, async_loop

    symbols = []                      # 有効な通貨
    connections = {}                  # sym -> asyncio.Task
    ui_buffer = {sym: None for sym in ALL_SYMBOLS}
    sec_buffer = {sym: {} for sym in ALL_SYMBOLS}
    buffer_lock = threading.Lock()

    candle_history = {sym: deque(maxlen=10) for sym in ALL_SYMBOLS}
    sec_state = {sym: {"sec": None, "vals": []} for sym in ALL_SYMBOLS}
    last_tick_sec = {sym: None for sym in ALL_SYMBOLS}
    last_tick_date_jst = {sym: None for sym in ALL_SYMBOLS}
    last_written_sec = {sym: None for sym in ALL_SYMBOLS}

    tick_counter = {sym: 0 for sym in ALL_SYMBOLS}
    tick_rate = {sym: 0 for sym in ALL_SYMBOLS}

    symbol_window = None
    symbol_vars = {}
    timeframe_mode = "15s"

    hozon = r"D:\\tick"
    BASE_PATH_LOCAL = os.path.dirname(os.path.abspath(__file__))
    BASE_PATH = BASE_PATH_LOCAL

    labels = {}
    open_prices = {}
    last_prices = {}
    history_labels = {}

    SAVE_TICKS = False  # 将来用
    async_loop = None   # asyncio イベントループ（別スレッドで動かす）

_initialize_global_state()

# --- MTパス自動探索 ---
def find_mt_paths():
    base = os.path.expanduser(r"~\\AppData\\Roaming\\MetaQuotes\\Terminal")
    paths = {"MT4": [], "MT5": []}
    if os.path.exists(base):
        for folder in glob.glob(os.path.join(base, "*")):
            mql4 = os.path.join(folder, "MQL4", "Files")
            mql5 = os.path.join(folder, "MQL5", "Files")
            if os.path.isdir(mql4):
                paths["MT4"].append(mql4)
            if os.path.isdir(mql5):
                paths["MT5"].append(mql5)
    return paths

mt_paths = find_mt_paths()

# --- Tk root ---
root = tk.Tk()
root.title("最新ティック表示 (秒足保存＋自動再接続・asyncio版)")
root.geometry("530x80")

# 通貨ごとの小数点桁数設定
decimal_places = {
    "USD_JPY": 3,
    "EUR_JPY": 3,
    "GBP_JPY": 3,
    "AUD_JPY": 3,
    "EUR_USD": 5,
    "AUD_USD": 5,
}

# --- UI 初期化（通貨ごとの表示行） ---
for sym in ALL_SYMBOLS:
    frame = tk.Frame(root)
    frame.pack(anchor="w", pady=5)

    name_lbl = tk.Label(frame, text=f"{sym}:", font=("Arial", 14), width=10, anchor="w")
    name_lbl.pack(side="left")

    hist_frame = tk.Frame(frame)
    hist_frame.pack(side="left", padx=10)
    history_labels[sym] = []
    for i in range(10):
        h = tk.Label(hist_frame, text=" ", width=2, height=1, bg="lightgray")
        h.pack(side="left")
        history_labels[sym].append(h)

    price_lbl = tk.Label(frame, text="Waiting...", font=("Arial", 14), bg="lightgray", width=15)
    price_lbl.pack(side="left", padx=10)
    labels[sym] = price_lbl

    open_prices[sym] = None
    last_prices[sym] = None

# --- 日付・パス関連 ---
def jst_date_for_folder(sym):
    if last_tick_date_jst[sym] is None:
        now = datetime.datetime.now(JST_TZ)
        return now.strftime("%Y%m%d"), now.strftime("%H")
    return last_tick_date_jst[sym], "00"

def utc_sec_to_jst_datetime(sec_utc: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(sec_utc, tz=datetime.timezone.utc).astimezone(JST_TZ)

def check_rotation():
    now = datetime.datetime.now(JST_TZ)
    if now.hour == koshin and now.minute == kofun:
        for sym in symbols:
            rotate_daily_file(sym)
    root.after(60000, check_rotation)


def check_rotation2():
    if not symbols:
        print("ローテーション対象の通貨がありません")
        return

    # --- 確認ダイアログ ---
    ok = msg.askokcancel(
        "強制実行",
        "本日分と昨日分を分離します\n実行しますか？\n（対象: {}）".format(", ".join(symbols)), icon="warning"
    )

    if not ok:
        print("ローテーションはキャンセルされました")
        return

    # --- OK の場合だけ実行 ---
    for sym in symbols:
        rotate_daily_file(sym)


def rotate_daily_file(symbol):
    clean_sym = symbol.replace("_", "")
    main_file = os.path.join(BASE_PATH, f"{clean_sym}_sec.csv")

    if not os.path.exists(main_file):
        return

    try:
        df = pd.read_csv(main_file, header=None, names=["symbol", "time", "price"])
    except Exception as e:
        print(f"{symbol} ローテーション読み込みエラー: {e}")
        return

    # ★ JST の tz-aware に変換（比較エラー防止）
    df["dt"] = pd.to_datetime(df["time"]).dt.tz_localize(JST_TZ)

    now = datetime.datetime.now(JST_TZ)
    today = now.date()
    prev_day = today - datetime.timedelta(days=1)

    # 前日 06:00
    prev_6 = datetime.datetime.combine(prev_day, datetime.time(6, 0), tzinfo=JST_TZ)

    # 当日 06:00
    today_6 = datetime.datetime.combine(today, datetime.time(6, 0), tzinfo=JST_TZ)

    # 当日 05:00
    today_5 = datetime.datetime.combine(today, datetime.time(5, 0), tzinfo=JST_TZ)

    # 1. 前日 06:00〜当日 06:00 のデータを前日ファイルへ
    df_prev = df[(df["dt"] >= prev_6) & (df["dt"] < today_6)]

    # 2. 通貨名_sec.csv に残すのは「当日 05:00 以降」
    df_keep = df[df["dt"] >= today_5]

    # 前日分保存
    if not df_prev.empty:
        # ★ secdata フォルダを初回だけ作成
        out_dir = os.path.join(BASE_PATH, "secdata")
        os.makedirs(out_dir, exist_ok=True)

        out_name = f"{clean_sym}_{prev_day.strftime('%Y%m%d')}.csv"
        out_path = os.path.join(out_dir, out_name)

        df_prev.drop(columns=["dt"]).to_csv(out_path, index=False, header=False)
        print(f"{symbol} → 前日分保存: {out_path}")

    # 通貨名_sec.csv を更新（05:00以降のみ）
    df_keep.drop(columns=["dt"]).to_csv(main_file, index=False, header=False)
    print(f"{symbol} → 通貨名_sec.csv 更新（05:00以降のみ）")




# --- UI 関連 ---
def update_history_bar(symbol):
    for i, h in enumerate(history_labels[symbol]):
        if i < len(candle_history[symbol]):
            color = candle_history[symbol][i]
            h.config(
                bg="lightblue" if color == "blue"
                else "lightcoral" if color == "red"
                else "lightgray"
            )
        else:
            h.config(bg="lightgray")

def cutoff_check():
    now = datetime.datetime.now(JST_TZ)
    sec = now.second

    cutoff = False
    if timeframe_mode == "1min" and sec == 0:
        cutoff = True
    elif timeframe_mode == "15s" and sec in [0, 15, 30, 45]:
        cutoff = True

    if cutoff:
        for sym in symbols:
            if open_prices[sym] is not None and last_prices[sym] is not None:
                if last_prices[sym] > open_prices[sym]:
                    direction = "blue"
                elif last_prices[sym] < open_prices[sym]:
                    direction = "red"
                else:
                    direction = "gray"
                candle_history[sym].append(direction)
                update_history_bar(sym)
                open_prices[sym] = last_prices[sym]

    root.after(1000, cutoff_check)

def safe_write(filepath, line, retries=3, delay=0.1):
    for attempt in range(retries):
        try:
            os.makedirs(os.path.dirname(filepath) or BASE_PATH, exist_ok=True)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except Exception as e:
            print(f"ファイル書き込み失敗 ({filepath}, 試行 {attempt+1}/{retries}): {e}")
            time.sleep(delay)
    print(f"最終的にファイル書き込み失敗: {filepath}")
    return False

def replace_last_line(filepath, new_line):
    new_bytes = new_line.encode("utf-8")
    try:
        with open(filepath, "r+b") as f:
            f.seek(0, os.SEEK_END)
            end_pos = f.tell()
            if end_pos == 0:
                f.write(new_bytes)
                return
            pos = end_pos - 1
            buffer_size = 1024
            while pos >= 0:
                read_start = max(0, pos - buffer_size + 1)
                f.seek(read_start)
                chunk = f.read(pos - read_start + 1)
                newline_idx = chunk.rfind(b"\n")
                if newline_idx != -1:
                    pos = read_start + newline_idx + 1
                    break
                pos = read_start - 1
            if pos < 0:
                pos = 0
            f.seek(pos)
            f.write(new_bytes)
            f.truncate()
    except FileNotFoundError:
        with open(filepath, "wb") as f:
            f.write(new_bytes)

def ensure_day_folder(path):
    if not os.path.exists(path):
        os.makedirs(path)

def flush_buffer():
    # SAVE_TICKS=False のため、今は何もしない構造だけ残す
    root.after(1000, flush_buffer)

def update_ui():
    for sym in symbols:
        if ui_buffer[sym] is not None:
            price, color = ui_buffer[sym]
            d = decimal_places.get(sym, 5)
            labels[sym].config(
                text=f"{price:.{d}f}     ({tick_rate[sym]})",
                bg=color
            )
    root.after(500, update_ui)

def update_tick_rate():
    global tick_counter, tick_rate
    for sym in symbols:
        tick_rate[sym] = tick_counter[sym]
        tick_counter[sym] = 0
    root.after(1000, update_tick_rate)

def set_timeframe(mode):
    global timeframe_mode
    timeframe_mode = mode
    print(f"タイムフレーム切替: {mode}")
    for sym in symbols:
        open_prices[sym] = None
        last_prices[sym] = None
        candle_history[sym] = deque(maxlen=10)
        labels[sym].config(text="Waiting...", bg="lightgray")
        update_history_bar(sym)

def toggle_ticks():
    global SAVE_TICKS
    SAVE_TICKS = not SAVE_TICKS
    state = "ON" if SAVE_TICKS else "OFF"
    print(f"ティック保存: {state}")
    root.title(f"最新ティック表示 (ティック保存 {state}＋秒足保存常時＋自動再接続・asyncio版)")

# --- asyncio + websockets 部分 ---

WS_URL = "wss://forex-api.coin.z.com/ws/public/v1"

async def handle_message(sym: str, message: str):
    global last_tick_sec, last_tick_date_jst, ui_buffer, tick_counter

    try:
        data = json.loads(message)
    except Exception as e:
        logger.error(f"WS({sym}) JSON decode error: {e}, raw: {message[:200]}...")
        return

    if "symbol" not in data or "bid" not in data or "ask" not in data:
        return

    # OFF の通貨は無視
    if sym not in symbols:
        return

    try:
        bid = float(data["bid"])
        ask = float(data["ask"])
        price = bid
        # price = (bid + ask) / 2
    except Exception as e:
        logger.error(f"WS({sym}) price parse error: {e}, data: {data}")
        return

    ts_iso = data.get("timestamp", "")
    try:
        dt_utc = datetime.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        ts_sec_utc = int(dt_utc.timestamp())
    except Exception:
        ts_sec_utc = int(time.time())

    last_tick_sec[sym] = ts_sec_utc

    dt_jst_for_file = utc_sec_to_jst_datetime(ts_sec_utc)
    last_tick_date_jst[sym] = (
        dt_jst_for_file - datetime.timedelta(days=1)
        if dt_jst_for_file.hour < koshin else dt_jst_for_file
    ).strftime("%Y%m%d")


    st = sec_state[sym]
    if st["sec"] is None:
        st["sec"] = ts_sec_utc
        st["vals"] = [price]
    elif ts_sec_utc == st["sec"]:
        st["vals"].append(price)
    else:
        if st["vals"]:
            avg = sum(st["vals"]) / len(st["vals"])
            with buffer_lock:
                sec_buffer[sym][st["sec"]] = avg
        st["sec"] = ts_sec_utc
        st["vals"] = [price]

    if open_prices[sym] is None:
        open_prices[sym] = price
    last_prices[sym] = price

    if price > open_prices[sym]:
        color = "lightblue"
    elif price < open_prices[sym]:
        color = "lightcoral"
    else:
        color = "lightgray"

    ui_buffer[sym] = (price, color)
    tick_counter[sym] += 1

async def ws_loop(sym: str):
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    while sym in symbols:
        try:
            async with websockets.connect(
                WS_URL,
                ssl=ssl_ctx,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                sub_msg = {
                    "command": "subscribe",
                    "channel": "ticker",
                    "symbol": sym
                }
                await ws.send(json.dumps(sub_msg))
                logger.info(f"WS({sym}) 購読開始: {sub_msg}")

                async for message in ws:
                    await handle_message(sym, message)

        except Exception as e:
            logger.error(f"WS({sym}) 接続エラー/切断: {e}")
            await asyncio.sleep(3)

    logger.info(f"WS({sym}) ループ終了（symbols から削除されたため）")

def start_async_loop():
    global async_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async_loop = loop
    loop.run_forever()

# --- 秒足出力（UTC秒 → JST文字列、通貨名.csv に保存） ---
def flush_sec_buffer():
    global last_written_sec

    current_sec_data_to_process = {}
    with buffer_lock:
        for sym in symbols:
            if last_tick_sec[sym] is None:
                continue

            ts_sec_key = last_tick_sec[sym]
            prev_sec = last_written_sec[sym]
            if prev_sec is None:
                prev_sec = ts_sec_key - 1

            for sec in range(prev_sec + 1, ts_sec_key + 1):
                if sec in sec_buffer[sym]:
                    price = sec_buffer[sym][sec]
                    del sec_buffer[sym][sec]
                    current_sec_data_to_process[(sym, sec)] = price
                else:
                    if last_prices[sym] is not None:
                        price = last_prices[sym]
                        current_sec_data_to_process[(sym, sec)] = price

    for (sym, sec), price in current_sec_data_to_process.items():
        dt_jst = utc_sec_to_jst_datetime(sec)
        ts_str_jst = dt_jst.strftime("%Y-%m-%d %H:%M:%S")

        clean_sym = sym.replace("_", "")
        filepath = os.path.join(BASE_PATH, f"{clean_sym}_sec.csv")

        # 保存先フォルダが無ければ作成
        if not os.path.exists(BASE_PATH):
            os.makedirs(BASE_PATH)

        d = decimal_places.get(sym, 5)
        price_str = f"{round(price, d)}"
        line = f"{sym},{ts_str_jst},{price_str}\n"

        # 同じ秒なら置換、それ以外は追記
        if last_written_sec[sym] == sec:
            replace_last_line(filepath, line)
        else:
            safe_write(filepath, line)
            last_written_sec[sym] = sec

    delay = max(1, 1000 - (datetime.datetime.now().microsecond // 1000))
    root.after(delay, flush_sec_buffer)


# --- 通貨選択サブウィンドウ関連（asyncio タスク起動もここから） ---
def start_single_ws(sym):
    if async_loop is None:
        logger.warning("async_loop がまだ初期化されていません")
        return

    def _start():
        if sym in connections:
            return
        connections[sym] = async_loop.create_task(ws_loop(sym))
    async_loop.call_soon_threadsafe(_start)

def stop_single_ws(sym):
    if async_loop is None:
        return

    def _stop():
        task = connections.pop(sym, None)
        if task is not None:
            task.cancel()
    async_loop.call_soon_threadsafe(_stop)

def toggle_symbol(sym, var):
    if var.get():
        if sym not in symbols:
            symbols.append(sym)
            symbols.sort()
            labels[sym].config(text="Waiting...", bg="lightgray")
            start_single_ws(sym)
    else:
        if sym in symbols:
            symbols.remove(sym)
            labels[sym].config(text="OFF", bg="gray")
            stop_single_ws(sym)

def open_symbol_window():
    global symbol_window

    if symbol_window is not None and symbol_window.winfo_exists():
        symbol_window.lift()
        return

    symbol_window = tk.Toplevel(root)
    symbol_window.title("通貨選択")
    symbol_window.geometry("200x200")

    def window_on_close():
        global symbol_window
        try:
            if symbol_window is not None and symbol_window.winfo_exists():
                symbol_window.destroy()
        except:
            pass
        symbol_window = None

    symbol_window.protocol("WM_DELETE_WINDOW", window_on_close)

    for sym in ALL_SYMBOLS:
        var = tk.BooleanVar(value=(sym in symbols))
        chk = tk.Checkbutton(
            symbol_window,
            text=sym,
            variable=var,
            command=lambda s=sym, v=var: toggle_symbol(s, v)
        )
        chk.pack(anchor="w")
        symbol_vars[sym] = var

# --- 履歴補完（pandas 利用部分はそのまま） ---
def merge_into_live_sec(symbol):
    def run():
        # --- 初期フォルダ選択 ---
        if mt_paths["MT5"]:
            initial_dir = mt_paths["MT5"][0]   # 最初の MT5 を使う
        else:
            initial_dir = os.path.expanduser("~")  # フォールバック

        hist_file = filedialog.askopenfilename(
            title=f"{symbol} の履歴（秒足）CSVを選択してください",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=initial_dir
        )
        if not hist_file:
            print("履歴ファイルが選択されませんでした")
            return

        clean_sym = symbol.replace("_", "")
        live_file = os.path.join(BASE_PATH, f"{clean_sym}_sec.csv")

        print("ライブ（秒足）:", live_file)
        print("履歴:", hist_file)

        # 保存先フォルダが無ければ作成
        if not os.path.exists(BASE_PATH):
            os.makedirs(BASE_PATH)

        # ライブファイルが無ければ空で作成
        if not os.path.exists(live_file):
            with open(live_file, "a", encoding="utf-8") as f:
                pass

        # 履歴読み込み
        try:
            df_hist = pd.read_csv(hist_file, header=None, names=["symbol", "time", "price"])
        except Exception as e:
            print(f"履歴読み込みエラー: {e}")
            return

        # ライブ読み込み
        try:
            df_live = pd.read_csv(live_file, header=None, names=["symbol", "time", "price"])
        except pd.errors.EmptyDataError:
            print(f"ライブ読み込み警告（{live_file} は空ファイル）")
            df_live = pd.DataFrame(columns=["symbol", "time", "price"])
        except Exception as e:
            print(f"ライブ読み込みエラー: {e}")
            df_live = pd.DataFrame(columns=["symbol", "time", "price"])

        # マージ処理
        merged = pd.concat([df_hist, df_live], ignore_index=True)
        merged["time_ts"] = pd.to_datetime(merged["time"]).astype(int) // 10**9
        merged = merged.drop_duplicates(subset=["symbol", "time_ts"], keep="last")
        merged = merged.sort_values(by="time_ts")
        merged = merged.drop(columns=["time_ts"])

        # 一時ファイルに書き出してから置き換え
        tmp_file = live_file + ".tmp"
        try:
            merged.to_csv(tmp_file, index=False, header=False)
            os.replace(tmp_file, live_file)
            print(f"{symbol} 履歴補完完了 → {live_file}（ヘッダ無し、リアルタイム優先）")
        except Exception as e:
            print(f"履歴補完書き込みエラー: {e}")
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except:
                pass

    threading.Thread(target=run, daemon=True).start()


# --- 保存先切り替え ---
def set_base_path(path, label):
    global BASE_PATH
    BASE_PATH = path
    print(f"保存先切替: {label} → {BASE_PATH}")
    root.title(f"最新ティック表示 (保存先: {label}・asyncio版)")

# --- メニュー設定 ---
menu = tk.Menu(root, tearoff=0)
menu.add_command(label="15秒足", command=lambda: set_timeframe("15s"))
menu.add_command(label="1分足", command=lambda: set_timeframe("1min"))
menu.add_separator()

save_var = tk.StringVar(value="Local")

save_menu = tk.Menu(menu, tearoff=0)
save_menu.add_radiobutton(
    label="pyと同じフォルダ", variable=save_var, value="Local",
    command=lambda: set_base_path(os.path.dirname(os.path.abspath(__file__)), "Local")
)
save_menu.add_radiobutton(
    label="D:\\tick", variable=save_var, value="D:\\tick",
    command=lambda: set_base_path(hozon, "D:\\tick")
)

for p in mt_paths["MT4"]:
    save_menu.add_radiobutton(
        label=f"MT4: {p}", variable=save_var, value=p,
        command=lambda path=p: set_base_path(path, "MT4候補")
    )
for p in mt_paths["MT5"]:
    save_menu.add_radiobutton(
        label=f"MT5: {p}", variable=save_var, value=p,
        command=lambda path=p: set_base_path(path, "MT5候補")
    )

menu.add_cascade(label="保存先", menu=save_menu)
menu.add_command(label="通貨選択", command=open_symbol_window)
menu.add_command(label="強制保存", command=check_rotation2)

root.config(menu=menu)

# --- 右クリックメニュー ---
context_menu = tk.Menu(root, tearoff=0)
for sym in ALL_SYMBOLS:
    context_menu.add_command(
        label=f"{sym} 秒足 履歴補完",
        command=lambda s=sym: merge_into_live_sec(s)
    )

def show_context_menu(event):
    context_menu.post(event.x_root, event.y_root)

root.bind("<Button-3>", show_context_menu)

# --- asyncio イベントループ開始（別スレッド） ---
async_thread = threading.Thread(target=start_async_loop, daemon=True)
async_thread.start()

# --- 定期処理開始 ---
update_ui()
update_tick_rate()
flush_buffer()
flush_sec_buffer()
cutoff_check()
check_rotation()


# Tkinterメインループ開始
root.mainloop()
