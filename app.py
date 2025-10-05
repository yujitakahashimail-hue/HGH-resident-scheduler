# app.py — Streamlit × OR-Tools 研修医シフト作成（完成版）
# -------------------------------------------------------------
# 変更点（要旨）
# - A希望（off/early/late/day1/day2）をハード制約で必ず反映。不可日は自動でBへ降格してソフト扱い
# - 出力とCSVに ★（A希望） と A休 列を反映
# - 一括登録UIを希望エディタの前へ移動。追記方式（重複は抑止）、直後に rerun で反映
# - ICU希望比率は 0–100%（10%刻み）プルダウン。J1は保存時に自動で 0% に固定
# - スタッフ表の空行は自動除去＋🧹ボタンで明示削除
# - 冗長部の整理・return位置/目的関数の重複などのバグ要因を排除

import io
import json
import datetime as dt
from dateutil.rrule import rrule, DAILY
from collections import defaultdict

import pandas as pd
import numpy as np
import streamlit as st
from ortools.sat.python import cp_model

# 祝日の自動提案（任意）
try:
    import jpholiday  # noqa: F401
    HAS_JPHOLIDAY = True
except Exception:
    HAS_JPHOLIDAY = False

st.set_page_config(page_title="研修医シフト作成", page_icon="🗓️", layout="wide")
st.title("日立総合病院　救急科研修医シフト作成アプリ")

WEEKDAY_JA = ["月","火","水","木","金","土","日"]

# -------------------------
# サイドバー：基本入力と月別設定（統合版）
# -------------------------
st.sidebar.title("🛠️ 設定")

# 1) 必須情報入力
st.sidebar.header("📌 必須情報")
this_year = dt.date.today().year
year = st.sidebar.number_input("作成年", min_value=this_year-2, max_value=this_year+2, value=this_year, step=1)
month = st.sidebar.selectbox("作成月", list(range(1,13)), index=dt.date.today().month-1)

start_date = dt.date(year, month, 1)
end_date = (dt.date(year + (month==12), (month % 12) + 1, 1) - dt.timedelta(days=1))
all_days = [d.date() for d in rrule(DAILY, dtstart=start_date, until=end_date)]
D = len(all_days)

# 日付ラベルユーティリティ（このブロックで定義）
def date_label(d: dt.date) -> str:
    return f"{d}({WEEKDAY_JA[d.weekday()]})"
DATE_OPTIONS = [date_label(d) for d in all_days]
LABEL_TO_DATE = {date_label(d): d for d in all_days}
DATE_TO_LABEL = {d: date_label(d) for d in all_days}

# 祝日/休診日（当月）— サイドバーで一元管理
auto_holidays = []
if 'HAS_JPHOLIDAY' in globals() and HAS_JPHOLIDAY:
    auto_holidays = [d for d in all_days if jpholiday.is_holiday(d)]
holidays = st.sidebar.multiselect("祝日（当月）", options=all_days, default=auto_holidays,
                                  format_func=lambda d: f"{d}({WEEKDAY_JA[d.weekday()]})")
closed_days = st.sidebar.multiselect("病院休診日（ER日勤2/3を禁止）", options=all_days,
                                     format_func=lambda d: f"{d}({WEEKDAY_JA[d.weekday()]})")

st.sidebar.divider()
per_person_total = st.sidebar.number_input("👥 1人あたりの総勤務回数（厳密・全員共通）", min_value=0, value=22, step=1)

# 2) 月ごとの個別設定
st.sidebar.header("🗓️ 月ごとの設定")
max_consecutive = st.sidebar.slider("最大連勤日数", 3, 7, 5, help="例: 5 にすると 6 日連勤は禁止")
enable_fatigue = st.sidebar.checkbox("遅番→翌日早番を避ける", value=True)
weight_fatigue = st.sidebar.slider("疲労ペナルティの重み", 0.0, 30.0, 6.0, 1.0, disabled=not enable_fatigue)

allow_day3 = st.sidebar.checkbox("ER日勤3を許可（平日のみ）", value=False)
allow_weekend_icu = st.sidebar.checkbox("週末ICUを許可（平日優先・通常はOFF）", value=False)
max_weekend_icu_total = st.sidebar.number_input("週末ICUの総上限（許可時のみ）", min_value=0, value=0, step=1, disabled=not allow_weekend_icu)
max_weekend_icu_per_person = st.sidebar.number_input("1人あたり週末ICU上限", min_value=0, value=0, step=1, disabled=not allow_weekend_icu)

# 3) 最適化の動作
st.sidebar.header("🧩 最適化の動作")
strict_mode = st.sidebar.checkbox(
    "厳しく最適化する（B/C・見栄えも強く尊重）",
    value=True,
    help="ON: J1休日ばらつき±1 / Day2・Day3ボーナス=通常。OFF: ±2 / ボーナス弱め。A希望・総勤務回数などのハード制約は常に厳守。"
)
fix_repro = st.sidebar.checkbox("再現性を固定（同じ結果を再現）", value=True)
seed_val = st.sidebar.number_input("乱数シード", min_value=0, max_value=1_000_000, value=42, step=1, disabled=not fix_repro)

with st.sidebar.expander("⚙️ 詳細ウェイト設定", expanded=False):
    weight_day2_weekday = st.slider("平日のER日勤2を入れる優先度", 0.0, 10.0, 2.0, 0.5)
    weight_day2_wed_bonus = st.slider("水曜ボーナス（ER日勤2）", 0.0, 30.0, 8.0, 0.5)
    weight_day3_weekday = st.slider("平日のER日勤3を入れる優先度", 0.0, 10.0, 1.0, 0.5, disabled=not allow_day3)
    weight_day3_wed_bonus = st.slider("水曜ボーナス（ER日勤3）", 0.0, 30.0, 6.0, 0.5, disabled=not allow_day3)
    weight_icu_ratio = st.slider("J2のICU希望比率の遵守 重み", 0.0, 10.0, 3.0, 0.5)
    weight_pref_B = st.slider("希望B未充足ペナルティ", 0.0, 50.0, 10.0, 1.0)
    weight_pref_C = st.slider("希望C未充足ペナルティ", 0.0, 50.0, 5.0, 1.0)

# 補助集合（後続で使用）
H = set(d for d in all_days if d.weekday() >= 5) | set(holidays)

# -------------------------
# セッション状態の初期化（安全網）
# -------------------------
def _init_state():
    ss = st.session_state
    # スタッフ
    if "staff_df" not in ss:
        ss.staff_df = pd.DataFrame([{"name":"", "grade":"J1", "desired_icu_ratio":0.0}])

    # 希望（正本 / 下書き / バックアップ等）
    if "prefs" not in ss:
        ss.prefs = pd.DataFrame(columns=["date","name","kind","priority"])
    if "prefs_draft" not in ss:
        # ※ ここでは df_date_to_label は呼ばない（時点依存を避ける）
        ss.prefs_draft = ss.prefs.copy()
    if "prefs_editor_ver" not in ss:
        ss.prefs_editor_ver = 0
    if "prefs_backup" not in ss:
        ss.prefs_backup = None
    if "last_bulk_add_rows" not in ss:
        ss.last_bulk_add_rows = []

    # プリアサイン
    if "pins" not in ss:
        ss.pins = pd.DataFrame(columns=["date","name","shift"])
    if "pins_backup" not in ss:
        ss.pins_backup = None

    # ER特例
    if "special_er" not in ss:
        ss.special_er = pd.DataFrame({"date":[], "drop_shift":[]})

_init_state()

def df_date_to_label(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if col in df.columns and not df.empty:
        df = df.copy()
        df[col] = df[col].apply(lambda x: DATE_TO_LABEL.get(x, x) if isinstance(x, dt.date) else x)
    return df

def df_label_to_date(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if col in df.columns and not df.empty:
        df = df.copy()
        df[col] = df[col].map(LABEL_TO_DATE)
        df = df[df[col].notna()]
    return df

# -------------------------
# 🔗 ER特例マップの構築（サイドバーで編集しない前提）
# ※ メイン画面では編集せず、セッションの special_er から辞書を作るだけ
# -------------------------
# special_er は _init_state() で空DataFrameとして初期化済み（列: date, drop_shift）
_special_df = st.session_state.special_er.copy() if "special_er" in st.session_state else pd.DataFrame(columns=["date","drop_shift"])

# クリーニング：空行除去＆日付重複は後勝ち
if not _special_df.empty:
    _special_df = _special_df.dropna()
    if "date" in _special_df.columns:
        _special_df = _special_df[_special_df["date"].isin(all_days)]
    _special_df = _special_df.drop_duplicates(subset=["date"], keep="last")

# ソルバー＆スナップショットで参照する dict
special_map = {row["date"]: row["drop_shift"] for _, row in _special_df.iterrows()}

# -------------------------
# 🧑‍⚕️ スタッフ入力（J1/J2・ICU希望比率）
# -------------------------
st.header("🧑‍⚕️ スタッフ入力（J1/J2・ICU希望比率）")

if "_staff_rid_seq" not in st.session_state:
    st.session_state._staff_rid_seq = 1
def _new_staff_rid():
    rid = st.session_state._staff_rid_seq
    st.session_state._staff_rid_seq += 1
    return rid

# 初期データ
if "staff_raw" not in st.session_state:
    st.session_state.staff_raw = pd.DataFrame([
        {"_rid": _new_staff_rid(), "name":"", "grade":"J1", "icu_ratio_label":"0%", "delete": False}
    ])
# 👉 delete列を常に持たせる（なければ追加）
if "delete" not in st.session_state.staff_raw.columns:
    st.session_state.staff_raw["delete"] = False

with st.form("staff_form", clear_on_submit=False):
    st.caption("入力を確定する（変更したら必ず押してください）")

    staff_in = st.session_state.staff_raw.copy()
    staff_out = st.data_editor(
        staff_in,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_order=["delete","name","grade","icu_ratio_label"],
        column_config={
            "delete": st.column_config.CheckboxColumn("削除", help="削除したい行にチェック"),
            "name": st.column_config.TextColumn("名前", help="例：田中、田中一など"),
            "grade": st.column_config.SelectboxColumn("区分", options=["J1","J2"], help="J1はICU不可（自動で0%固定）"),
            "icu_ratio_label": st.column_config.SelectboxColumn("ICU希望比率", options=[f"{i}%" for i in range(0,101,10)]),
            "_rid": st.column_config.NumberColumn("rid", disabled=True),
        },
        key="staff_editor",
    )

    c1, c2 = st.columns([1,1])
    save_staff  = c1.form_submit_button("💾 保存（変更したら必ず押す）", type="primary", use_container_width=True)
    del_staff   = c2.form_submit_button("🗑️ チェックした行を削除", use_container_width=True)

    if save_staff or del_staff:
        df = staff_out.copy()

        # rid 補完
        if "_rid" not in df.columns: 
            df["_rid"] = pd.Series(dtype="Int64")
        df["_rid"] = pd.to_numeric(df["_rid"], errors="coerce")
        mask_new = df["_rid"].isna()
        if mask_new.any():
            df.loc[mask_new, "_rid"] = [_new_staff_rid() for _ in range(mask_new.sum())]
        df["_rid"] = df["_rid"].astype(int)

        # 🔧 delete列の型を厳密化（NaN→False, bool化）
        if "delete" not in df.columns:
            df["delete"] = False
        df["delete"] = df["delete"].fillna(False)
        # data_editorの都合で object/str になることがある → bool化
        df["delete"] = df["delete"].apply(lambda v: bool(v) if pd.notna(v) else False)

        # 削除実行（チェックされた行を落とす）
        if del_staff:
            df = df[~df["delete"]].copy()

        # 正規化
        df["name"]  = df["name"].astype(str).str.strip()
        df["grade"] = df["grade"].astype(str).str.upper().where(
            df["grade"].astype(str).str.upper().isin(["J1","J2"]), "J1"
        )
        df["icu_ratio_label"] = df["icu_ratio_label"].astype(str).str.strip()

        # 空行削除（名前未入力は捨てる）
        df = df[df["name"]!=""].copy()

        # 表示ラベル→数値比率
        def lbl_to_ratio(s):
            try:
                return float(str(s).replace("%",""))/100.0
            except Exception:
                return 0.0
        df["desired_icu_ratio"] = df["icu_ratio_label"].map(lbl_to_ratio)

        # J1 は ICU比率 0 固定
        df.loc[df["grade"]=="J1", "desired_icu_ratio"] = 0.0

        # 重複名チェック
        if df["name"].duplicated().any():
            st.error("同じ名前が重複しています。重複を解消してから保存してください。")
        else:
            # ✅ staff_raw には delete列を残す（次回もチェックボックスを維持）
            st.session_state.staff_raw = df.reset_index(drop=True)

            # 下流用の staff_df は必要列だけ
            staff_df = df[["name","grade","desired_icu_ratio"]].reset_index(drop=True)
            st.session_state.staff_df = staff_df

            st.success("スタッフを保存しました。")
            st.rerun()

# 以降の計算用（この部分は既存のままでOK）
if "staff_df" in st.session_state:
    staff_df = st.session_state.staff_df.copy()
else:
    staff_df = pd.DataFrame(columns=["name","grade","desired_icu_ratio"])

if staff_df.empty:
    st.warning("少なくとも1名入力してください。")
    st.stop()

names = staff_df["name"].tolist()
N = len(names)
name_to_idx = {n:i for i,n in enumerate(names)}
J1_idx = [i for i in range(N) if staff_df.iloc[i]["grade"]=="J1"]
J2_idx = [i for i in range(N) if staff_df.iloc[i]["grade"]=="J2"]


# -------------------------
# 🧰 一括登録（B/C希望のみ）— フォーム保存方式
# -------------------------
st.subheader("🧰 一括登録（B/C希望のみ）")
st.caption("入力を確定する（変更したら必ず押してください）")

# ---- 希望テーブルの正本と下書きの健全化（未定義なら初期化）----
if "prefs" not in st.session_state:
    st.session_state.prefs = pd.DataFrame(columns=["date", "name", "kind", "priority"])
if "prefs_draft" not in st.session_state:
    tmp = st.session_state.prefs.copy()
    tmp["date"] = pd.to_datetime(tmp.get("date"), errors="coerce").dt.date
    st.session_state.prefs_draft = tmp
if "prefs_editor_ver" not in st.session_state:
    st.session_state.prefs_editor_ver = 0
if "last_bulk_add_rows" not in st.session_state:
    st.session_state.last_bulk_add_rows = []
if "prefs_backup" not in st.session_state:
    st.session_state.prefs_backup = None

weekday_map = {"月":0, "火":1, "水":2, "木":3, "金":4, "土":5, "日":6}

with st.form("bulk_prefs_form", clear_on_submit=False):
    scope = st.selectbox("対象日", ["毎週指定曜日","全休日","全平日","祝日のみ"], index=0)
    sel_wd_label = st.selectbox("曜日", list(weekday_map.keys()), index=2, disabled=(scope!="毎週指定曜日"))
    target_mode = st.selectbox("対象者", ["全員","J1のみ","J2のみ","個別選択"], index=2 if len(J2_idx)>0 else 1)
    bulk_kind = st.selectbox("希望種別", ["off","early","day","late","icu"], index=0)
    bulk_prio = st.selectbox("優先度", ["B","C"], index=0)

    # 対象者リストの確定
    selected_names = names
    if target_mode == "J1のみ":
        selected_names = [names[i] for i in J1_idx]
    elif target_mode == "J2のみ":
        selected_names = [names[i] for i in J2_idx]
    elif target_mode == "個別選択":
        selected_names = st.multiselect(
            "個別に選択", options=names,
            default=[names[i] for i in J2_idx] if len(J2_idx)>0 else names
        )

    submitted = st.form_submit_button("＋ 一括追加（B/Cのみ）", type="primary", use_container_width=True)

# ---- 追加ボタン押下後の処理 ----
if submitted:
    # 休日集合 H: 土日 + 祝日
    H_set = set(d for d in all_days if d.weekday() >= 5) | set(holidays)

    # 対象日リスト
    if scope == "毎週指定曜日":
        sel_wd = weekday_map.get(sel_wd_label, 2)
        target_days = [d for d in all_days if d.weekday() == sel_wd]
    elif scope == "全休日":
        target_days = [d for d in all_days if d in H_set]
    elif scope == "全平日":
        target_days = [d for d in all_days if d.weekday() < 5]
    else:  # 祝日のみ
        target_days = list(set(holidays))

    if bulk_prio not in ("B","C"):
        st.warning("Aは一括登録の対象外です。個別に追加してください。")
    else:
        existing = st.session_state.prefs.copy()
        add_rows = []
        skipped_j1_icu = 0

        for d in target_days:
            for nm in selected_names:
                # J1 に対する ICU 希望は無視（割当不可のため）
                if bulk_kind == "icu":
                    # nm は names 由来なので必ず 1 行にヒットする前提
                    if staff_df.loc[staff_df["name"] == nm, "grade"].iloc[0] == "J1":
                        skipped_j1_icu += 1
                        continue

                # 既存重複チェック
                dup = (
                    existing[
                        (existing["date"] == d)
                        & (existing["name"] == nm)
                        & (existing["kind"] == bulk_kind)
                        & (existing["priority"] == bulk_prio)
                    ].shape[0]
                    > 0
                )
                if not dup:
                    add_rows.append(
                        {"date": d, "name": nm, "kind": bulk_kind, "priority": bulk_prio}
                    )

        if add_rows:
            # バックアップ（Undo 用）
            st.session_state.prefs_backup = existing.copy(deep=True)

            # ① 正本（prefs）へ追加
            st.session_state.prefs = pd.concat(
                [existing, pd.DataFrame(add_rows)], ignore_index=True
            )
            st.session_state.last_bulk_add_rows = add_rows

            # ② 個別エディタが読む“下書き”も同期（date→ラベル化が重要）
            tmp = st.session_state.prefs.copy()
            tmp["date"] = pd.to_datetime(tmp.get("date"), errors="coerce").dt.date
            st.session_state.prefs_draft = tmp

            # ③ Data Editor に再描画を促す
            st.session_state.prefs_editor_ver += 1

            msg = f"{len(add_rows)} 件を追加しました。"
            if skipped_j1_icu > 0:
                msg += f"（J1→ICUの希望 {skipped_j1_icu} 件は無視しました）"
            st.success(msg)

            # 画面更新で個別エディタにも即反映
            st.rerun()
        else:
            info = "追加対象がありません（既に登録済みです）。"
            if skipped_j1_icu > 0:
                info += f" J1→ICUの希望 {skipped_j1_icu} 件は無視しました。"
            st.info(info)

# -------------------------
# 📝 希望（A=絶対 / B,C=希望）— フォーム保存方式（カレンダー選択）
# -------------------------
st.subheader("📝 希望（A=絶対 / B,C=希望）")
st.caption("入力を確定する（変更したら必ず押してください）")

# 一括登録側から同期された下書きを編集対象にする
draft = st.session_state.prefs_draft.copy()

# 念のため date 型に揃える（ここで文字列→dateを吸収）
if "date" in draft.columns:
    draft["date"] = pd.to_datetime(draft["date"], errors="coerce").dt.date
else:
    draft["date"] = pd.Series(dtype="object")  # 空列でもOK

# Data Editor 強制再描画キー（bulk追加後すぐ反映）
prefs_widget_key = f"prefs_editor_{st.session_state.prefs_editor_ver}"

edited = st.data_editor(
    draft,
    key=prefs_widget_key,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "date": st.column_config.DateColumn(
            "日付",
            min_value=start_date,
            max_value=end_date,
            format="YYYY-MM-DD",
            help="当該月のみ選択できます"
        ),
        "name": st.column_config.SelectboxColumn("名前", options=names),
        "kind": st.column_config.SelectboxColumn(
            "種別",
            options=["off","early","late","day","day1","day2","icu"],
            help="Aは off/early/late/（必要なら day1/day2）。day/icu のAは自動でBへ降格"
        ),
        "priority": st.column_config.SelectboxColumn("優先度", options=["A","B","C"]),
    },
)

with st.form("prefs_save_form", clear_on_submit=False):
    save = st.form_submit_button("💾 希望を保存（必ず押してください）", type="primary", use_container_width=True)

    if save:
        df = edited.copy()

        # --- 正規化＆バリデーション ---
        df = df.fillna({"kind":"off","priority":"C"})

        # 名前空白の行を除外
        df["name"] = df["name"].astype(str).str.strip()
        df = df[df["name"]!=""]

        # 日付を date 型に（保険）
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df = df[df["date"].notna()]

        # 文字正規化
        df["kind"] = df["kind"].astype(str).str.strip().str.lower()
        df["priority"] = df["priority"].astype(str).str.strip().str.upper()

        # A-day / A-icu は仕様外 → Bへ降格（保険）
        bad_mask = (df["priority"]=="A") & (df["kind"].isin(["day","icu"]))
        df.loc[bad_mask, "priority"] = "B"

        # 許容 kind / 既知の名前 のみ
        df = df[df["kind"].isin(["off","early","late","day","day1","day2","icu"])]
        df = df[df["name"].isin(names)]

        # 完全重複排除
        df = df.drop_duplicates(subset=["date","name","kind","priority"], keep="last").reset_index(drop=True)

        # --- 保存（正本を差し替え）---
        st.session_state.prefs_backup = st.session_state.prefs.copy(deep=True)
        st.session_state.prefs = df

        # 下書きも同期（date型のまま）
        st.session_state.prefs_draft = df.copy()

        # エディタ再描画トリガ
        st.session_state.prefs_editor_ver += 1

        st.success("希望を保存しました。")
        st.rerun()

# -------------------------
# 📌 プリアサイン（フォーム保存方式）
# -------------------------
st.subheader("📌 プリアサイン（固定割当）")
st.caption("入力を確定する（変更したら必ず押してください）")

if "_pin_rid_seq" not in st.session_state:
    st.session_state._pin_rid_seq = 1
def _new_pin_rid():
    rid = st.session_state._pin_rid_seq
    st.session_state._pin_rid_seq += 1
    return rid

if "pins_raw" not in st.session_state:
    st.session_state.pins_raw = pd.DataFrame([
        {"_rid": _new_pin_rid(), "date_label":"", "name":"", "shift": "ER_Early"}
    ])

with st.form("pins_form", clear_on_submit=False):
    pins_in = st.session_state.pins_raw.copy()
    pins_out = st.data_editor(
        pins_in,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_order=["date_label","name","shift"],
        column_config={
            "date_label": st.column_config.SelectboxColumn("日付", options=DATE_OPTIONS),
            "name":       st.column_config.SelectboxColumn("名前", options=names),
            "shift":      st.column_config.SelectboxColumn("シフト", options=["ER_Early","ER_Day1","ER_Day2","ER_Day3","ER_Late","ICU"]),
            "_rid":       st.column_config.NumberColumn("rid", disabled=True),
        },
        key="pins_editor",
    )

    c1, c2 = st.columns([1,1])
    save_pins  = c1.form_submit_button("💾 保存（変更したら必ず押す）", type="primary", use_container_width=True)
    prune_pins = c2.form_submit_button("🧹 空行削除して保存", use_container_width=True)

    if save_pins or prune_pins:
        tmp = pins_out.copy()

        # rid 補完
        if "_rid" not in tmp.columns:
            tmp["_rid"] = pd.Series(dtype="Int64")
        tmp["_rid"] = pd.to_numeric(tmp["_rid"], errors="coerce")
        mask_new = tmp["_rid"].isna()
        if mask_new.any():
            tmp.loc[mask_new, "_rid"] = [ _new_pin_rid() for _ in range(mask_new.sum()) ]
        tmp["_rid"] = tmp["_rid"].astype(int)

        # 正規化
        tmp["date_label"] = tmp["date_label"].astype(str).str.strip()
        tmp["name"]       = tmp["name"].astype(str).str.strip()
        tmp["shift"]      = tmp["shift"].astype(str).str.strip()

        if prune_pins:
            tmp = tmp[(tmp["date_label"]!="") | (tmp["name"]!="")]

        # UI→内部（J1→ICU をブロック）
        pins = tmp[(tmp["name"]!="") & (tmp["date_label"].isin(DATE_OPTIONS))].copy()
        pins["date"] = pins["date_label"].map(LABEL_TO_DATE)
        # J1→ICU を除去
        if not pins.empty:
            j1_names = set(staff_df.loc[staff_df["grade"]=="J1","name"].tolist())
            bad = (pins["shift"]=="ICU") & (pins["name"].isin(j1_names))
            if bad.any():
                bad_rows = pins[bad][["date","name"]].to_records(index=False).tolist()
                pins = pins[~bad]
                st.error("J1 に ICU のプリアサインは無効化しました:\n" + "\n".join([f"- {d} {n}" for d,n in bad_rows]))

        pins = pins[["date","name","shift"]]

        st.session_state.pins_raw = tmp.reset_index(drop=True)
        st.session_state.pins     = pins.reset_index(drop=True)

        st.success("プリアサインを保存しました。")
        st.rerun()

# -------------------------
# 設置可否カレンダー（Day2/Day3/ICU）
# -------------------------
DAY2_FORBID = set([d for d in all_days if d.weekday()>=5]) | set(holidays) | set(closed_days)
WEEKDAYS = set([d for d in all_days if d.weekday()<5])
ICU_ALLOWED_DAYS = set(all_days) if allow_weekend_icu else WEEKDAYS

cal_rows = []
for d in all_days:
    cal_rows.append({
        "Date": str(d),
        "Weekday": WEEKDAY_JA[d.weekday()],
        "D2": "🟢可" if (d.weekday()<5 and d not in DAY2_FORBID) else "🔴不可",
        "D3": ("🟢可" if (allow_day3 and d.weekday()<5 and d not in DAY2_FORBID) else ("—" if not allow_day3 else "🔴不可")),
        "ICU": "可" if (d in ICU_ALLOWED_DAYS) else "不可",
        "Holiday/Closed": ("休" if d in H else "") + (" 休診" if d in set(closed_days) else "")
    })
cal_df = pd.DataFrame(cal_rows)
with st.expander("🗓️ Day2/Day3/ICU の設置可否カレンダー"):
    st.dataframe(cal_df, use_container_width=True, hide_index=True)

# -------------------------
# 前処理バリデーション
# -------------------------
R2 = len([d for d in all_days if (d.weekday()<5 and d not in DAY2_FORBID)])
R3 = len([d for d in all_days if (allow_day3 and d.weekday()<5 and d not in DAY2_FORBID)])
W = len([d for d in all_days if d in ICU_ALLOWED_DAYS])

sum_target = int(per_person_total) * N
min_required = 3*D
max_possible_info = 3*D + R2 + R3 + W

colv1, colv2, colv3 = st.columns(3)
with colv1: st.metric("当月日数 D", D)
with colv2: st.metric("ER最低必要 3×D", 3*D)
with colv3: st.metric("Σ target_total", sum_target)

if sum_target < min_required:
    st.error(f"総勤務回数の合計が不足（{sum_target} < {min_required}）。ERの基本3枠/日を満たせません。")
    st.stop()

if sum_target > max_possible_info:
    st.warning(f"参考：Σtarget（{sum_target}）が理論上限（{max_possible_info}）を超えています。ICU任意/Day2・Day3の可否により吸収できない可能性があります。")

max_ER_slots_info = 3*D + R2 + R3
sum_J1 = int(per_person_total) * len(J1_idx)
if sum_J1 > max_ER_slots_info:
    st.warning(f"参考：J1合計（{sum_J1}）がERで吸収可能な理論値（{max_ER_slots_info}）を上回る恐れがあります。")

# -------------------------
# A希望の事前検証（技術的に不可能なAを弾く）
# -------------------------
def validate_A_requests(prefs_df, DAY):
    """
    そもそも物理的に成立しない A を列挙して返す。
    戻り値: list[str] （人が読めるエラーメッセージ）
    """
    issues = []

    # A-off と A-シフト の同日両立不可
    a_off = set()
    for _, r in prefs_df[(prefs_df["priority"]=="A") & (prefs_df["kind"]=="off")].iterrows():
        if r["date"] in all_days and r["name"] in name_to_idx:
            a_off.add((r["date"], r["name"]))

    for _, r in prefs_df[prefs_df["priority"]=="A"].iterrows():
        d = r["date"]; nm = r["name"]; k = r["kind"].lower()
        if d not in all_days or nm not in name_to_idx:
            continue
        if (d, nm) in a_off and k != "off":
            issues.append(f"{d} {nm}: A-休み と A-{k} は同日に共存できません")

    # J1 の A-ICU は不可
    j1_names = set(staff_df.loc[staff_df["grade"]=="J1","name"].tolist())
    for _, r in prefs_df[(prefs_df["priority"]=="A") & (prefs_df["kind"].str.lower()=="icu")].iterrows():
        if r["name"] in j1_names:
            issues.append(f"{r['date']} {r['name']}: J1 に A-ICU は割当不可能です")

    # その日の枠が立たないケース（特例で停止、D2不可 など）
    for _, r in prefs_df[prefs_df["priority"]=="A"].iterrows():
        d = r["date"]; nm = r["name"]; k = r["kind"].lower()
        if d not in all_days or nm not in name_to_idx:
            continue
        di = all_days.index(d)
        if k == "early" and DAY[di]["req"]["ER_Early"] == 0:
            issues.append(f"{d} {nm}: 特例で早番が停止中のため A-early は不可能です")
        if k == "late"  and DAY[di]["req"]["ER_Late"] == 0:
            issues.append(f"{d} {nm}: 特例で遅番が停止中のため A-late は不可能です")
        if k == "day1"  and DAY[di]["req"]["ER_Day1"] == 0:
            issues.append(f"{d} {nm}: 特例で日勤1が停止中のため A-day1 は不可能です")
        if k == "day2"  and not DAY[di]["allow_d2"]:
            issues.append(f"{d} {nm}: その日は日勤2が立たないため A-day2 は不可能です")
        if k == "icu"   and not DAY[di]["allow_icu"]:
            issues.append(f"{d} {nm}: その日はICU不可のため A-ICU は不可能です")

    # 同一日・同一枠に A を複数人要求していないか（明白な衝突）
    # 例：同じ日に A-early が2人以上
    a_counts = {}
    for _, r in prefs_df[prefs_df["priority"]=="A"].iterrows():
        d = r["date"]; k = r["kind"].lower()
        if d in all_days:
            di = all_days.index(d)
            key = None
            if k == "early" and DAY[di]["req"]["ER_Early"]==1: key=("ER_Early", di)
            if k == "late"  and DAY[di]["req"]["ER_Late"]==1:  key=("ER_Late", di)
            if k == "day1"  and DAY[di]["req"]["ER_Day1"]==1:  key=("ER_Day1", di)
            if k == "day2"  and DAY[di]["allow_d2"]:            key=("ER_Day2", di)
            if k == "icu"   and DAY[di]["allow_icu"]:           key=("ICU", di)
            if key:
                a_counts.setdefault(key, 0)
                a_counts[key] += 1
    for (shift_name, di), cnt in a_counts.items():
        cap = 1 if shift_name in ["ER_Early","ER_Day1","ER_Late","ER_Day2","ICU"] else 0
        if shift_name == "ER_Day3":
            cap = 1  # 一応
        if cnt > cap:
            issues.append(f"{all_days[di]} {shift_name}: A希望が{cnt}件あり、定員{cap}を超えています")

    return issues

# -------------------------
# ソルバー構築
# -------------------------
SHIFTS = ["ER_Early","ER_Day1","ER_Day2","ER_Day3","ER_Late","ICU"]
ER_BASE = ["ER_Early","ER_Day1","ER_Late"]
SHIFT_LABEL = {"ER_Early":"早番","ER_Day1":"日勤1","ER_Day2":"日勤2","ER_Day3":"日勤3","ER_Late":"遅番","ICU":"ICU"}

def build_and_solve(fair_slack:int, disabled_pref_ids:set, weaken_day2_bonus:bool=False, repro_fix:bool=True):
    model = cp_model.CpModel()

    # 変数 x[d,s,i]
    x = {(d,s,i): model.NewBoolVar(f"x_d{d}_s{s}_i{i}")
         for d in range(D) for s in range(len(SHIFTS)) for i in range(N)}

    # 同日同一人物1枠
    for d in range(D):
        for i in range(N):
            model.Add(sum(x[(d,s,i)] for s in range(len(SHIFTS))) <= 1)

    ICU_IDX = SHIFTS.index("ICU")
    # J1はICU不可
    for d in range(D):
        for i in J1_idx:
            model.Add(x[(d, ICU_IDX, i)] == 0)

    # 連勤（最大）
    for i in range(N):
        y = [model.NewBoolVar(f"y_d{d}_i{i}") for d in range(D)]
        for d in range(D):
            model.Add(y[d] == sum(x[(d,s,i)] for s in range(len(SHIFTS))))
        window = max_consecutive + 1
        if D >= window:
            for start in range(0, D - window + 1):
                model.Add(sum(y[start+k] for k in range(window)) <= max_consecutive)

    # target_total 厳密（全員共通）
    for i in range(N):
        ti = model.NewIntVar(0, 5*D, f"total_i{i}")
        model.Add(ti == sum(x[(d,s,i)] for d in range(D) for s in range(len(SHIFTS))))
        model.Add(ti == int(per_person_total))

    # 日別の可否・要求
    DAY = {d: {"req": {"ER_Early":1, "ER_Day1":1, "ER_Late":1},
               "allow_d2": False, "allow_d3": False, "allow_icu": False,
               "drop": None} for d in range(D)}

    DAY2_FORBID_LOCAL = set([d for d in all_days if d.weekday()>=5]) | set(holidays) | set(closed_days)
    ICU_ALLOWED_DAYS_LOCAL = set(all_days) if allow_weekend_icu else set([d for d in all_days if d.weekday()<5])

    for d, day in enumerate(all_days):
        # 特例（基本枠を0に）
        drop = special_map.get(day)
        if drop in ER_BASE:
            DAY[d]["req"][drop] = 0
            DAY[d]["drop"] = drop
        # Day2/Day3 可否
        if day.weekday()<5 and day not in DAY2_FORBID_LOCAL:
            DAY[d]["allow_d2"] = True
            DAY[d]["allow_d3"] = bool(allow_day3)
        # ICU 可否
        if day in ICU_ALLOWED_DAYS_LOCAL:
            DAY[d]["allow_icu"] = True

    # ER基本枠（ハード）
    for d in range(D):
        for base in ER_BASE:
            sidx = SHIFTS.index(base)
            model.Add(sum(x[(d,sidx,i)] for i in range(N)) == DAY[d]["req"][base])

    # Day2/Day3（可なら 0..1、不可なら 0固定）
    D2_IDX = SHIFTS.index("ER_Day2")
    D3_IDX = SHIFTS.index("ER_Day3")
    for d in range(D):
        model.Add(sum(x[(d,D2_IDX,i)] for i in range(N)) <= (1 if DAY[d]["allow_d2"] else 0))
        model.Add(sum(x[(d,D3_IDX,i)] for i in range(N)) <= (1 if DAY[d]["allow_d3"] else 0))

    # ICU 任意（I=1/日 or 0）
    for d in range(D):
        model.Add(sum(x[(d, ICU_IDX, i)] for i in range(N)) <= (1 if DAY[d]["allow_icu"] else 0))

    # 週末ICUの上限（許可時のみ）
    if allow_weekend_icu:
        weekend_days = [d for d,day in enumerate(all_days) if day.weekday()>=5]
        model.Add(sum(x[(d, ICU_IDX, i)] for d in weekend_days for i in range(N)) <= int(max_weekend_icu_total))
        for i in range(N):
            model.Add(sum(x[(d, ICU_IDX, i)] for d in weekend_days) <= int(max_weekend_icu_per_person))

    # プリアサイン固定（pins が未設定でも安全に動く）
    pins_df = st.session_state.get("pins", pd.DataFrame(columns=["date","name","shift"]))
    for _, row in pins_df.iterrows():
        d = all_days.index(row["date"]) if row["date"] in all_days else None
        if d is None:
            continue
        sname = row.get("shift")
        if sname not in SHIFTS:
            continue
        sidx = SHIFTS.index(sname)
        i = name_to_idx.get(row.get("name"))
        if i is None:
            continue
        model.Add(x[(d, sidx, i)] == 1)

    # ---- 希望（A=ハード、B/C=ソフト）----
    prefs_eff = st.session_state.prefs.copy()
    prefs_eff["kind"] = prefs_eff["kind"].astype(str).str.strip().str.lower()
    prefs_eff["priority"] = prefs_eff["priority"].astype(str).str.strip().str.upper()

    pref_soft = []  # (rid, d, i, kind, pr)
    A_star = set()  # {(d, shift_name, name)} … 出力時に★を付けるため
    A_off = defaultdict(list)  # {d: [name,...]}

    for rid, row in prefs_eff.reset_index().iterrows():
        if row["date"] not in all_days or row["name"] not in name_to_idx:
            continue
        d = all_days.index(row["date"]); i = name_to_idx[row["name"]]
        kind = row["kind"]; pr = row["priority"]

        # A-day/icu はBへ降格（UI保険）
        if pr == "A" and kind in ("day","icu"):
            pr = "B"

        if pr == "A":
            if kind == "off":
                model.Add(sum(x[(d,s,i)] for s in range(len(SHIFTS))) == 0)
                A_off[d].append(row["name"])

            elif kind == "early":
                if DAY[d]["req"]["ER_Early"] == 1:
                    model.Add(x[(d, SHIFTS.index("ER_Early"), i)] == 1)
                    A_star.add((d, "ER_Early", row["name"]))
                else:
                    pref_soft.append((rid, d, i, "early", "B"))

            elif kind == "late":
                if DAY[d]["req"]["ER_Late"] == 1:
                    model.Add(x[(d, SHIFTS.index("ER_Late"), i)] == 1)
                    A_star.add((d, "ER_Late", row["name"]))
                else:
                    pref_soft.append((rid, d, i, "late", "B"))

            elif kind == "day1":
                if DAY[d]["req"]["ER_Day1"] == 1:
                    model.Add(x[(d, SHIFTS.index("ER_Day1"), i)] == 1)
                    A_star.add((d, "ER_Day1", row["name"]))
                else:
                    pref_soft.append((rid, d, i, "day1", "B"))

            elif kind == "day2":
                if DAY[d]["allow_d2"]:
                    model.Add(x[(d, SHIFTS.index("ER_Day2"), i)] == 1)
                    A_star.add((d, "ER_Day2", row["name"]))
                else:
                    pref_soft.append((rid, d, i, "day2", "B"))

            else:
                # 想定外はソフトへ
                pref_soft.append((rid, d, i, kind, "B"))
        else:
            # B/C … 自動緩和対象（disabled_pref_ids）に含まれていればスキップ
            if rid in disabled_pref_ids:
                continue
            pref_soft.append((rid, d, i, kind, pr))

    # 休日公平（当該月のみ／J1間の差を抑制）
    hol = []
    Hd = [idx for idx,day in enumerate(all_days) if (day.weekday()>=5 or day in holidays)]
    for i in range(N):
        hi = model.NewIntVar(0, 5*D, f"hol_i{i}")
        model.Add(hi == sum(x[(d,s,i)] for d in Hd for s in range(len(SHIFTS))))
        hol.append(hi)

    for a in J1_idx:
        for b in J1_idx:
            if a>=b: continue
            diff = model.NewIntVar(-5*D, 5*D, f"j1diff_{a}_{b}")
            model.Add(diff == hol[a] - hol[b])
            model.Add(diff <= fair_slack)
            model.Add(-diff <= fair_slack)

    # J2 の休日は J1 最大以下
    if len(J1_idx)>0 and len(J2_idx)>0:
        j1max = model.NewIntVar(0, 5*D, "j1max_hol")
        for a in J1_idx: model.Add(j1max >= hol[a])
        for j in J2_idx: model.Add(hol[j] <= j1max)

    # ⭐ J1のシフト偏り（早番・遅番・日勤(1+2)）を ±2
    E_IDX = SHIFTS.index("ER_Early"); L_IDX = SHIFTS.index("ER_Late")
    D1_IDX = SHIFTS.index("ER_Day1"); D2X_IDX = SHIFTS.index("ER_Day2")
    early_cnt = []; late_cnt = []; day12_cnt = []
    for i in range(N):
        ei = model.NewIntVar(0, D, f"early_i{i}"); model.Add(ei == sum(x[(d,E_IDX,i)] for d in range(D))); early_cnt.append(ei)
        li = model.NewIntVar(0, D, f"late_i{i}");  model.Add(li == sum(x[(d,L_IDX,i)] for d in range(D)));  late_cnt.append(li)
        di = model.NewIntVar(0, 2*D, f"day12_i{i}"); model.Add(di == sum(x[(d,D1_IDX,i)] + x[(d,D2X_IDX,i)] for d in range(D))); day12_cnt.append(di)
    for a in J1_idx:
        for b in J1_idx:
            if a>=b: continue
            for arr in (early_cnt, late_cnt, day12_cnt):
                df = model.NewIntVar(-2*D, 2*D, "tmp")
                model.Add(df == arr[a] - arr[b])
                model.Add(df <= 2); model.Add(-df <= 2)

    # 目的関数
    terms = []

    # (1) B/C 希望 未充足ペナルティ
    for rid, d, i, kind, pr in pref_soft:
        w = weight_pref_B if pr == "B" else weight_pref_C
        if w <= 0: continue
        assigned_any = model.NewBoolVar(f"assign_any_d{d}_i{i}")
        model.Add(assigned_any == sum(x[(d, s, i)] for s in range(len(SHIFTS))))
        if kind == "off":
            terms.append(int(100*w) * assigned_any)
        elif kind == "early" and DAY[d]["req"]["ER_Early"] == 1:
            correct = x[(d, SHIFTS.index("ER_Early"), i)]
            miss = model.NewBoolVar(f"pref_early_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100*w) * miss)
        elif kind == "late" and DAY[d]["req"]["ER_Late"] == 1:
            correct = x[(d, SHIFTS.index("ER_Late"), i)]
            miss = model.NewBoolVar(f"pref_late_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100*w) * miss)
        elif kind == "day1" and DAY[d]["req"]["ER_Day1"] == 1:
            correct = x[(d, SHIFTS.index("ER_Day1"), i)]
            miss = model.NewBoolVar(f"pref_day1_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100*w) * miss)
        elif kind == "day2" and DAY[d]["allow_d2"]:
            correct = x[(d, SHIFTS.index("ER_Day2"), i)]
            miss = model.NewBoolVar(f"pref_day2_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100*w) * miss)
        elif kind == "day":
            day1_ok = (DAY[d]["req"]["ER_Day1"] == 1); day2_ok = DAY[d]["allow_d2"]
            if day1_ok or day2_ok:
                cands = []
                if day1_ok: cands.append(x[(d, SHIFTS.index("ER_Day1"), i)])
                if day2_ok: cands.append(x[(d, SHIFTS.index("ER_Day2"), i)])
                correct = model.NewBoolVar(f"pref_day_any_ok_d{d}_i{i}")
                model.AddMaxEquality(correct, cands)
                miss = model.NewBoolVar(f"pref_day_miss_d{d}_i{i}")
                model.Add(miss + correct == 1)
                terms.append(int(100*w) * miss)
        elif kind == "icu" and (i in J2_idx) and DAY[d]["allow_icu"]:
            correct = x[(d, SHIFTS.index("ICU"), i)]
            miss = model.NewBoolVar(f"pref_icu_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100*w) * miss)

    # (2) Day2/Day3 未配置ペナルティ + 水曜ボーナス
    for d, day in enumerate(all_days):
        if DAY[d]["allow_d2"]:
            placed = model.NewBoolVar(f"d2_placed_{d}")
            model.Add(placed == sum(x[(d,SHIFTS.index("ER_Day2"),i)] for i in range(N)))
            w = weight_day2_weekday + (weight_day2_wed_bonus if day.weekday()==2 else 0.0)
            if weaken_day2_bonus: w = max(0.0, w*0.5)
            if w>0: terms.append(int(100*w) * (1 - placed))
        if DAY[d]["allow_d3"]:
            placed3 = model.NewBoolVar(f"d3_placed_{d}")
            model.Add(placed3 == sum(x[(d,SHIFTS.index("ER_Day3"),i)] for i in range(N)))
            w3 = weight_day3_weekday + (weight_day3_wed_bonus if day.weekday()==2 else 0.0)
            if weaken_day2_bonus: w3 = max(0.0, w3*0.5)
            if w3>0: terms.append(int(100*w3) * (1 - placed3))

    # (3) J2 ICU 比率（弱め）
    if weight_icu_ratio>0 and len(J2_idx)>0:
        scale = 100
        for j in J2_idx:
            ICU_j = model.NewIntVar(0, 5*D, f"ICU_j{j}")
            model.Add(ICU_j == sum(x[(d, SHIFTS.index("ICU"), j)] for d in range(D)))
            target_scaled = model.NewIntVar(0, scale*5*D, f"icu_target_j{j}")
            desired = float(staff_df.iloc[j]["desired_icu_ratio"])
            # total_i == per_person_total なので、target_scaled = desired * total_i * scale
            model.Add(target_scaled == int(round(desired*scale)) * int(per_person_total))
            ICU_scaled = model.NewIntVar(0, scale*5*D, f"icu_scaled_j{j}")
            model.Add(ICU_scaled == scale * ICU_j)
            diff = model.NewIntVar(-scale*5*D, scale*5*D, f"icu_diff_j{j}")
            model.Add(diff == ICU_scaled - target_scaled)
            dev = model.NewIntVar(0, scale*5*D, f"icu_dev_j{j}")
            model.AddAbsEquality(dev, diff)
            terms.append(int(weight_icu_ratio) * dev)

    # (4) 疲労（遅番→翌日早番）
    if enable_fatigue and weight_fatigue>0:
        L_IDX = SHIFTS.index("ER_Late"); E_IDX = SHIFTS.index("ER_Early")
        for i in range(N):
            for d in range(D-1):
                f = model.NewBoolVar(f"fatigue_d{d}_i{i}")
                model.Add(f >= x[(d, L_IDX, i)] + x[(d+1, E_IDX, i)] - 1)
                model.Add(f <= x[(d, L_IDX, i)])
                model.Add(f <= x[(d+1, E_IDX, i)])
                terms.append(int(100*weight_fatigue) * f)

    model.Minimize(sum(terms))

    # 求解
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20.0
    solver.parameters.num_search_workers = 1 if (fix_repro and repro_fix) else 8
    if fix_repro and repro_fix:
        try: solver.parameters.random_seed = int(seed_val)
        except Exception: pass

    status = solver.Solve(model)
    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }

    artifacts = {"x": x, "DAY": DAY, "A_star": A_star, "A_off": A_off}
    return status_map.get(status, "UNKNOWN"), solver, artifacts

# -------------------------
# infeasible 時のブロッキングA特定（単体除外テスト）
# -------------------------
def find_blocking_A_once(fair_slack_base, weaken_base):
    """
    A希望はハードのまま。Aレコードを1件ずつ“なかったことに”して解けるか検査。
    戻り値: list[(rid, row_dict)] … 除外すると解けたAの候補
    """
    # 行番号基準で扱えるように drop=True の reset_index を使う
    prefs_base = st.session_state.prefs.reset_index(drop=True)
    A_only = prefs_base[prefs_base["priority"] == "A"].copy()
    blockers = []

    for rid, row in A_only.iterrows():
        # prefs のコピーを作り、この A だけ priority を "Z" に一時変更
        tmp = prefs_base.copy()
        tmp.loc[rid, "priority"] = "Z"

        # build_and_solve は st.session_state.prefs を参照するので、差し替えて試行
        bak = st.session_state.prefs
        st.session_state.prefs = tmp
        s, sol, art = build_and_solve(
            fair_slack=fair_slack_base,
            disabled_pref_ids=set(),
            weaken_day2_bonus=weaken_base
        )
        st.session_state.prefs = bak

        if s in ("OPTIMAL", "FEASIBLE"):
            blockers.append((rid, row.to_dict()))
    return blockers


# -------------------------
# 実行ボタン & 事前検証 & 自動リトライ（B/C自動無効化を含む）
# -------------------------
run = st.button("🚀 生成する（最適化）")
relax_log = []

def find_blocking_A_once(fair_slack_base, weaken_base):
    """
    A希望はハードのまま固定。Aレコードを1件ずつ除外して解けるか検査。
    戻り値: list[(idx, row_dict)]  … 除外すると解けたAの候補（=犯人）
    """
    prefs = st.session_state.prefs.reset_index()  # index が rid になる
    A_only = prefs[prefs["priority"]=="A"].copy()
    blockers = []

    for rid, row in A_only.iterrows():
        disabled = set()
        tmp = st.session_state.prefs.copy()
        tmp.loc[tmp.reset_index().index==rid, "priority"] = "Z"  # 一時的にAを無効化
        bak = st.session_state.prefs
        st.session_state.prefs = tmp
        s, sol, a = build_and_solve(fair_slack=fair_slack_base,
                                    disabled_pref_ids=disabled,
                                    weaken_day2_bonus=weaken_base)
        st.session_state.prefs = bak
        if s in ("OPTIMAL","FEASIBLE"):
            blockers.append((rid, row.to_dict()))
    return blockers

if run:
    # ---- その月の DAY マップを先に組み立て（validate 用）----
    DAY_tmp = {d: {"req": {"ER_Early":1, "ER_Day1":1, "ER_Late":1},
                   "allow_d2": False, "allow_d3": False, "allow_icu": False,
                   "drop": None} for d in range(D)}

    DAY2_FORBID_LOCAL = set([d for d in all_days if d.weekday()>=5]) | set(holidays) | set(closed_days)
    ICU_ALLOWED_DAYS_LOCAL = set(all_days) if allow_weekend_icu else set([d for d in all_days if d.weekday()<5])

    for d, day in enumerate(all_days):
        drop = special_map.get(day)
        if drop in ER_BASE:
            DAY_tmp[d]["req"][drop] = 0
            DAY_tmp[d]["drop"] = drop
        if day.weekday()<5 and day not in DAY2_FORBID_LOCAL:
            DAY_tmp[d]["allow_d2"] = True
            DAY_tmp[d]["allow_d3"] = bool(allow_day3)
        if day in ICU_ALLOWED_DAYS_LOCAL:
            DAY_tmp[d]["allow_icu"] = True

    # ---- A希望の事前検証（技術的に不可能なAを先に報告して停止）----
    issues = validate_A_requests(st.session_state.prefs.copy(), DAY_tmp)
    if issues:
        st.error("A希望に物理的に不可能な指定が含まれています。以下を修正してください：\n- " + "\n- ".join(issues))
        st.stop()

    # ---- ここから解探索（Aは常にハード）----
    disabled_pref_ids = set()     # 自動無効化した B/C のIDを溜める
    disabled_log_rows = []        # 表示/JSON用に行辞書も保存

    # ① 初回（モード依存）
    status, solver, art = build_and_solve(
        fair_slack=(1 if strict_mode else 2),
        disabled_pref_ids=disabled_pref_ids,
        weaken_day2_bonus=(not strict_mode),
    )

    # ② 不可 → 公平±2
    if status in ("INFEASIBLE","UNKNOWN"):
        relax_log.append(("fairness","J1休日ばらつきを ±1→±2 に緩和"))
        status, solver, art = build_and_solve(fair_slack=2, disabled_pref_ids=disabled_pref_ids, weaken_day2_bonus=False)

    # ③ なお不可 → Day2/3ボーナス弱め
    if status in ("INFEASIBLE","UNKNOWN"):
        relax_log.append(("bonus","Day2/Day3の平日・水曜ボーナスを一段弱め"))
        status, solver, art = build_and_solve(fair_slack=2, disabled_pref_ids=disabled_pref_ids, weaken_day2_bonus=True)

    # ④ なお不可 → C希望を順次 無効化 → ダメならB
    def iteratively_disable(level:str, disabled_ids:set):
        """
        level: 'C' or 'B'
        反復的に1件ずつ level の希望を disabled に加え、可行解が出るまで試す。
        戻り値: (status, solver, art, disabled_ids, log_rows)
        """
        log_rows = []
        prefs_all = st.session_state.prefs.reset_index()  # rid = index
        target = prefs_all[prefs_all["priority"]==level]

        for rid, row in target.iterrows():
            if rid in disabled_ids:  # 既に無効化済みはスキップ
                continue
            disabled_ids2 = set(disabled_ids); disabled_ids2.add(rid)
            s, sol, a = build_and_solve(fair_slack=2, disabled_pref_ids=disabled_ids2, weaken_day2_bonus=True)
            if s in ("OPTIMAL","FEASIBLE"):
                # 採用
                log_rows.append(row.to_dict())
                return s, sol, a, disabled_ids2, log_rows
        return None, None, None, disabled_ids, log_rows

    # 反復：まずCを何件か外す→ダメならB
    while status in ("INFEASIBLE","UNKNOWN"):
        s, sol, a, disabled_pref_ids, logs = iteratively_disable("C", disabled_pref_ids)
        if s is None:
            break
        disabled_log_rows.extend(logs)
        status, solver, art = s, sol, a

    while status in ("INFEASIBLE","UNKNOWN"):
        s, sol, a, disabled_pref_ids, logs = iteratively_disable("B", disabled_pref_ids)
        if s is None:
            break
        disabled_log_rows.extend(logs)
        status, solver, art = s, sol, a

    # ⑤ それでも不可 → ブロッキングA候補を提示して停止
    if status not in ("OPTIMAL","FEASIBLE"):
        st.error("A希望をすべて厳守すると可行解が見つかりませんでした。以下の A を外すと解ける可能性があります：")
        blockers = find_blocking_A_once(fair_slack_base=2, weaken_base=True)
        if blockers:
            for rid, row in blockers:
                st.write(f"- {row['date']} {row['name']} A-{row['kind']}")
            st.info("対応案：該当日の ER 基本枠を特例で停止 / A をBへ変更 などをご検討ください。")
        else:
            st.info("単体除外では特定できませんでした（複数Aの組合せが原因の可能性）。")
        st.stop()

    # -------------------------
    # 出力テーブル（★と A休 / B休 / C休 を反映）
    # -------------------------
    A_star = art["A_star"]; A_off = art["A_off"]
    x = art["x"]

    # B/Cのoff希望（付与済み/未付与判定のため）
    prefs_now = st.session_state.prefs.copy()
    prefs_now["kind"] = prefs_now["kind"].astype(str).str.lower()
    prefs_now["priority"] = prefs_now["priority"].astype(str).str.upper()
    B_off_want = defaultdict(set)
    C_off_want = defaultdict(set)
    for _, r in prefs_now.iterrows():
        if r.get("date") in all_days and r.get("kind")=="off" and r.get("name") in name_to_idx:
            d = all_days.index(r["date"])
            if r["priority"]=="B":
                B_off_want[d].add(r["name"])
            elif r["priority"]=="C":
                C_off_want[d].add(r["name"])

    # 日別：誰がどこにも割り当たっていないか（OFF実現）
    assigned_set_by_day = [set() for _ in range(D)]
    for d in range(D):
        for sidx in range(len(SHIFTS)):
            for i in range(N):
                if solver.Value(x[(d,sidx,i)]) == 1:
                    assigned_set_by_day[d].add(names[i])

    B_off_granted = {d: sorted([nm for nm in B_off_want.get(d, set()) if nm not in assigned_set_by_day[d]]) for d in range(D)}
    C_off_granted = {d: sorted([nm for nm in C_off_want.get(d, set()) if nm not in assigned_set_by_day[d]]) for d in range(D)}

    rows = []
    for d, day in enumerate(all_days):
        row = {"日付":str(day), "曜日": WEEKDAY_JA[day.weekday()]}
        for sname in SHIFTS:
            sidx = SHIFTS.index(sname)
            assigned = [names[i] for i in range(N) if solver.Value(x[(d,sidx,i)])==1]
            starset = set(nm for (dd, ss, nm) in A_star if (dd==d and ss==sname))
            labeled = [ (nm + "★") if (nm in starset) else nm for nm in assigned ]
            row[SHIFT_LABEL[sname]] = ",".join(labeled)
        # 休み実現記録
        aoff_names = A_off.get(d, [])
        row["A休"] = ",".join(sorted(aoff_names)) if aoff_names else ""
        row["B休"] = ",".join(B_off_granted.get(d, [])) if B_off_granted.get(d) else ""
        row["C休"] = ",".join(C_off_granted.get(d, [])) if C_off_granted.get(d) else ""
        rows.append(row)
    out_df = pd.DataFrame(rows)

    # A-off 違反チェック（画面表示のみ）
    viol = []
    for d, a_names in A_off.items():
        for nm in a_names:
            assigned_any = any(
                isinstance(out_df.loc[d, lbl], str) and nm in [x.strip("★") for x in out_df.loc[d, lbl].split(",") if x]
                for lbl in ["早番","日勤1","日勤2","日勤3","遅番","ICU"]
            )
            if assigned_any:
                viol.append((all_days[d], nm))
    if viol:
        st.error("A-休みの違反が検出されました（設定の矛盾かバグの可能性）。\n" +
                 "\n".join([f"- {d} {nm}" for d, nm in viol]))

    # 画面表示
    st.subheader("📋 生成スケジュール（★=A希望反映）")
    st.dataframe(out_df, use_container_width=True, hide_index=True)

    # 個人別集計 + 休日数 + 疲労回数
    person_stats = []
    hol_days_idx = [idx for idx,day in enumerate(all_days) if (day.weekday()>=5 or day in holidays)]
    for i,nm in enumerate(names):
        cnt = {lbl: sum(1 for d in range(D) if isinstance(out_df.loc[d,lbl],str) and nm in [x.strip("★") for x in out_df.loc[d,lbl].split(",") if x]) for lbl in ["早番","日勤1","日勤2","日勤3","遅番","ICU"]}
        total = sum(cnt.values())
        hol_cnt = sum(sum(1 for lbl in ["早番","日勤1","日勤2","日勤3","遅番","ICU"] if isinstance(out_df.loc[d,lbl],str) and nm in [x.strip("★") for x in out_df.loc[d,lbl].split(",") if x]) for d in hol_days_idx)
        fatigue = 0
        for d in range(D-1):
            late = (isinstance(out_df.loc[d,"遅番"],str) and nm in [x.strip("★") for x in out_df.loc[d,"遅番"].split(",") if x])
            early_next = (isinstance(out_df.loc[d+1,"早番"],str) and nm in [x.strip("★") for x in out_df.loc[d+1,"早番"].split(",") if x])
            if late and early_next:
                fatigue += 1
        person_stats.append({"name":nm, "grade":staff_df.iloc[i]["grade"], **cnt, "Total":total, "Holiday":hol_cnt, "Fatigue":fatigue})
    stat_df = pd.DataFrame(person_stats)

    st.subheader("👥 個人別集計（Holiday=土日祝、Fatigue=遅番→翌早番）")
    st.dataframe(stat_df, use_container_width=True, hide_index=True)

    # 未充足の希望（B/C） … 実際に叶わなかったもの
    unmet = []
    for _, row in st.session_state.prefs.reset_index().iterrows():
        if row["priority"] not in ("B","C"):
            continue
        if row["date"] not in all_days or row["name"] not in name_to_idx:
            continue
        d = all_days.index(row["date"])
        nm = row["name"]; kind=row["kind"].lower()
        got = False
        if kind=="off":
            got = not any(isinstance(out_df.loc[d,lbl],str) and nm in [x.strip("★") for x in out_df.loc[d,lbl].split(",") if x] for lbl in ["早番","日勤1","日勤2","日勤3","遅番","ICU"])
        elif kind=="early":
            got = isinstance(out_df.loc[d,"早番"],str) and nm in [x.strip("★") for x in out_df.loc[d,"早番"].split(",") if x]
        elif kind=="late":
            got = isinstance(out_df.loc[d,"遅番"],str) and nm in [x.strip("★") for x in out_df.loc[d,"遅番"].split(",") if x]
        elif kind=="day":
            got = (isinstance(out_df.loc[d,"日勤1"],str) and nm in [x.strip("★") for x in out_df.loc[d,"日勤1"].split(",") if x]) or \
                  (isinstance(out_df.loc[d,"日勤2"],str) and nm in [x.strip("★") for x in out_df.loc[d,"日勤2"].split(",") if x])
        elif kind=="icu":
            got = isinstance(out_df.loc[d,"ICU"],str) and nm in [x.strip("★") for x in out_df.loc[d,"ICU"].split(",") if x]
        elif kind=="day1":
            got = isinstance(out_df.loc[d,"日勤1"],str) and nm in [x.strip("★") for x in out_df.loc[d,"日勤1"].split(",") if x]
        elif kind=="day2":
            got = isinstance(out_df.loc[d,"日勤2"],str) and nm in [x.strip("★") for x in out_df.loc[d,"日勤2"].split(",") if x]
        if not got:
            unmet.append((row["priority"], row["date"], nm, kind))

    # 自動で無効化した希望（B/C） … solverのために外したもの
    auto_disabled_rows = []
    if disabled_pref_ids:
        base = st.session_state.prefs.reset_index()  # rid= index
        hit = base[base["index"].isin(disabled_pref_ids)].copy()
        # 表示整形
        for _, r in hit.iterrows():
            auto_disabled_rows.append((r["priority"], r["date"], r["name"], r["kind"].lower()))

    # 画面表示（2つを別々に見せる）
    if unmet:
        st.subheader("🙇‍♂️ 未充足となった希望（B/C）")
        show = pd.DataFrame(unmet, columns=["priority","date","name","kind"]).sort_values(["priority","date","name"])
        st.dataframe(show, use_container_width=True, hide_index=True)

    if auto_disabled_rows:
        st.subheader("⚠️ 自動で無効化した希望（B/C）")
        show2 = pd.DataFrame(auto_disabled_rows, columns=["priority","date","name","kind"]).sort_values(["priority","date","name"])
        st.dataframe(show2, use_container_width=True, hide_index=True)

    # ダウンロード（★/A休/B休/C休・無効化/未充足 付き）
    json_snapshot = {
        "run": {"timestamp": dt.datetime.now().isoformat(), "status": status, "objective": solver.ObjectiveValue(), "seed": int(seed_val) if fix_repro else None, "repro": fix_repro},
        "period": {"year":year, "month":month},
        "settings": {
            "per_person_total": int(per_person_total),
            "max_consecutive": max_consecutive,
            "allow_day3": allow_day3,
            "allow_weekend_icu": allow_weekend_icu,
            "max_weekend_icu_total": int(max_weekend_icu_total),
            "max_weekend_icu_per_person": int(max_weekend_icu_per_person),
            "weights": {
                "day2_weekday": float(weight_day2_weekday),
                "day2_wed_bonus": float(weight_day2_wed_bonus),
                "day3_weekday": float(weight_day3_weekday),
                "day3_wed_bonus": float(weight_day3_wed_bonus),
                "icu_ratio": float(weight_icu_ratio),
                "pref_B": float(weight_pref_B),
                "pref_C": float(weight_pref_C),
                "fatigue": float(weight_fatigue if enable_fatigue else 0.0),
            },
        },
        "holidays": [str(d) for d in holidays],
        "closed_days": [str(d) for d in closed_days],
        "special_er": [{"date":str(k), "drop_shift":v} for k,v in special_map.items()],
        "staff": staff_df.to_dict(orient="records"),
        "prefs": [{**r, "date": str(r["date"]) if r.get("date") is not None else None} for r in st.session_state.prefs.to_dict(orient="records")],
        "auto_disabled_prefs": [
            {"priority":pr, "date":str(dt_), "name":nm, "kind":kd} for pr, dt_, nm, kd in auto_disabled_rows
        ],
        "unmet_prefs": [
            {"priority":pr, "date":str(dt_), "name":nm, "kind":kd} for pr, dt_, nm, kd in unmet
        ],
        "result_table": out_df.to_dict(orient="records"),
        "person_stats": stat_df.to_dict(orient="records"),
    }
    buf_json = io.StringIO(); buf_json.write(json.dumps(json_snapshot, ensure_ascii=False, indent=2))
    buf_csv = io.StringIO(); out_df.to_csv(buf_csv, index=False)

    c1, c2 = st.columns(2)
    with c1:
        st.download_button("📥 スケジュールCSVをダウンロード（★/A休/B休/C休 付き）", data=buf_csv.getvalue(), file_name="schedule.csv", mime="text/csv")
    with c2:
        st.download_button("🧾 スナップショットJSONをダウンロード", data=buf_json.getvalue(), file_name="run_snapshot.json", mime="application/json")