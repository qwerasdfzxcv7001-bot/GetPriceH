# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score
from numpy.lib.stride_tricks import sliding_window_view
import warnings
import tkinter as tk
from tkinter import filedialog
import os

warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
warnings.filterwarnings("ignore", message="X does not have valid feature names")

# ============================================================
#  LABEL FUNCTIONS
# ============================================================

def lbl_make_break_initial_label(df, threshold, horizon_sec):
    prices = df["price"].values.astype(np.float32)
    w = horizon_sec + 1
    if len(prices) < w: return pd.DataFrame()

    win = sliding_window_view(prices, w)
    cur = win[:, 0]

    diff_up = (win - cur[:, None]).max(axis=1)
    diff_dn = (cur[:, None] - win).max(axis=1)

    label = np.full(len(diff_up), np.nan, np.float32)
    label[diff_up > threshold] = 1
    label[(diff_up <= threshold) & (diff_dn > threshold)] = 0

    df2 = df.iloc[:len(label)].copy()
    df2["label"] = label
    return df2.dropna(subset=["label"])


def lbl_make_labels_volatility(df, threshold_pips=0.005, horizon_sec=30):
    prices = df["price"].values.astype(np.float32)
    w = horizon_sec + 1
    if len(prices) < w: return pd.DataFrame()

    win = sliding_window_view(prices, w)
    cur = win[:, 0]
    diff_max = np.abs(win - cur[:, None]).max(axis=1)

    df2 = df.iloc[:len(diff_max)].copy()
    df2["label"] = (diff_max > threshold_pips).astype(np.int8)
    return df2

# ============================================================
#  NumPy高速 rolling utilities
# ============================================================

def np_rolling_window(a, w):
    if len(a) < w:
        return np.empty((0, w), dtype=a.dtype)
    return sliding_window_view(a, w)

def np_rolling_mean(a, w):
    win = np_rolling_window(a, w)
    return np.concatenate([np.full(w-1, np.nan), win.mean(axis=1)])

def np_rolling_std(a, w):
    win = np_rolling_window(a, w)
    return np.concatenate([np.full(w-1, np.nan), win.std(axis=1)])

def np_rolling_max(a, w):
    win = np_rolling_window(a, w)
    return np.concatenate([np.full(w-1, np.nan), win.max(axis=1)])

def np_rolling_min(a, w):
    win = np_rolling_window(a, w)
    return np.concatenate([np.full(w-1, np.nan), win.min(axis=1)])

# ============================================================
#  ALMA
# ============================================================

def feat_calc_alma_fast(series, window=20, offset=0.85, sigma=6):
    a = series.values.astype(np.float32)
    m = offset * (window - 1)
    s = window / sigma
    w = np.exp(-((np.arange(window) - m) ** 2) / (2 * s * s))
    w /= w.sum()

    res = np.convolve(a, w[::-1], mode="valid")
    return np.concatenate([np.full(window - 1, np.nan), res])

# ============================================================
#  Candle Runs
# ============================================================

def feat_add_candle_runs_fast(df, windows=[5, 15, 30, 60, 180, 300]):
    price = df["price"].values.astype(np.float32)
    out = {}

    for w in windows:
        o = np.concatenate([np.full(w-1, np.nan), price[:-w+1]])
        c = price
        dir_candle = np.sign(c - o)
        dir_candle[np.isnan(dir_candle)] = 0

        bull = (dir_candle > 0).astype(np.int32)
        change = np.where(bull != np.roll(bull, 1), 1, 0)
        change[0] = 1
        grp = np.cumsum(change)
        bull_run = np.where(bull == 1, np.arange(len(bull)) - np.concatenate([[0], np.where(change[1:] == 1)[0] + 1])[grp-1] + 1, 0)

        bear = (dir_candle < 0).astype(np.int32)
        change2 = np.where(bear != np.roll(bear, 1), 1, 0)
        change2[0] = 1
        grp2 = np.cumsum(change2)
        bear_run = np.where(bear == 1, np.arange(len(bear)) - np.concatenate([[0], np.where(change2[1:] == 1)[0] + 1])[grp2-1] + 1, 0)

        out[f"bull_run_{w}"] = bull_run
        out[f"bear_run_{w}"] = bear_run

    return pd.DataFrame(out, index=df.index)

# ============================================================
#  全特徴量
# ============================================================

def feat_make_all_features_fast(df):
    print(">>> NumPy高速特徴量エンジン稼働中...")

    feats = df[["time", "price"]].copy()
    price = df["price"].values.astype(np.float32)

    # 1. 収益率・ボラ・レンジ
    for w in [1, 5, 10, 15, 20, 30, 60, 120, 180, 300, 600]:
        ret = np.concatenate([np.full(w, np.nan), (price[w:] - price[:-w]) / (price[:-w] + 1e-9)])
        feats[f"ret_{w}"] = ret

        if w >= 5:
            feats[f"vol_{w}"] = np_rolling_std(price, w)
            feats[f"range_{w}"] = np_rolling_max(price, w) - np_rolling_min(price, w)

    # 2. CCI / CMO / BBPos
    for w in [5, 10, 15, 20, 30, 60, 120, 180]:
        ma = np_rolling_mean(price, w)
        md = np_rolling_mean(np.abs(price - ma), w)
        feats[f"cci_{w}"] = (price - ma) / (0.015 * md + 1e-9)

        delta = np.diff(price, prepend=price[0])
        up = np_rolling_window(np.clip(delta, 0, None), w).sum(axis=1)
        up = np.concatenate([np.full(w-1, np.nan), up])
        dn = np_rolling_window(np.clip(-delta, 0, None), w).sum(axis=1)
        dn = np.concatenate([np.full(w-1, np.nan), dn])
        feats[f"cmo_{w}"] = (up - dn) / (up + dn + 1e-9)

        std = np_rolling_std(price, w)
        feats[f"bbpos_{w}"] = (price - ma) / (2 * std + 1e-9)

    # 3. ALMA
    for w in [5, 10, 15, 20, 30, 60, 120, 180, 300]:
        alma = feat_calc_alma_fast(df["price"], w)
        feats[f"alma_{w}"] = alma
        feats[f"alma_slope_{w}"] = np.concatenate([[0], np.diff(alma)])

    # 4. Candle Runs（NumPy版）
    feats = pd.concat([feats, feat_add_candle_runs_fast(df)], axis=1)

    # 5. 後処理
    feats = feats.replace([np.inf, -np.inf], np.nan).fillna(0)
    print(f">>> 完了。特徴量数: {len(feats.columns) - 2}")
    return feats

# ============================================================
#  MAIN
# ============================================================

def main():
    root = tk.Tk(); root.withdraw()
    file_path = filedialog.askopenfilename(title="秒足CSV選択", filetypes=[("CSV", "*.csv")])
    if not file_path: return

    print(f"読み込み中: {os.path.basename(file_path)}")
    df = pd.read_csv(file_path, header=None, names=["symbol", "time", "price"])

    all_feats = feat_make_all_features_fast(df)

    print("\n[ LINE 1: 上昇予測モデル ]")
    thresholds = [0.003, 0.005, 0.007]
    best_th = thresholds[0]

    labels_up = lbl_make_break_initial_label(df, best_th, 30)
    df_up = all_feats.join(labels_up[['label']], how='inner')

    features = [c for c in df_up.columns if c not in ["symbol", "time", "label"]]
    X = df_up[features]
    y = df_up["label"]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, shuffle=False)
    model = lgb.LGBMClassifier(n_estimators=500, learning_rate=0.03, num_leaves=63, max_depth=6, verbosity=-1)
    model.fit(X_train, y_train)
    auc = roc_auc_score(y_test, model.predict_proba(X_test)[:, 1])
    print(f">>> 上昇モデル AUC: {auc:.4f}")
    model.booster_.save_model("model_up_numpy.txt")

    print("\n[ LINE 2: ボラティリティ予測モデル ]")
    labels_vol = lbl_make_labels_volatility(df, 0.005, 30)
    df_vol = all_feats.join(labels_vol[['label']], how='inner')

    Xv = df_vol[features]
    yv = df_vol["label"]
    Xv_train, Xv_test, yv_train, yv_test = train_test_split(Xv, yv, test_size=0.2, shuffle=False)

    model_vol = lgb.LGBMClassifier(n_estimators=500, learning_rate=0.03, verbosity=-1)
    model_vol.fit(Xv_train, yv_train)
    auc_v = roc_auc_score(yv_test, model_vol.predict_proba(Xv_test)[:, 1])
    print(f">>> ボラモデル AUC: {auc_v:.4f}")
    model_vol.booster_.save_model("model_vol_numpy.txt")

    print("\n=== 重要特徴量 TOP10 ===")
    importances = pd.DataFrame({'feat': features, 'imp': model.feature_importances_})
    print(importances.sort_values('imp', ascending=False).head(10))

    print("\n>>> 全ての工程が完了しました。")

if __name__ == "__main__":
    main()
