# app.py — Streamlit × OR-Tools 研修医シフト作成（完成・整理版）

import io
import json
import os
import atexit
import datetime as dt
from collections import defaultdict

import numpy as np
import pandas as pd
import streamlit as st
from dateutil.rrule import rrule, DAILY
from ortools.sat.python import cp_model

# 祝日自動判定（任意）
try:
    import jpholiday  # noqa: F401
    HAS_JPHOLIDAY = True
except Exception:
    HAS_JPHOLIDAY = False

# -------------------------
# ページ設定 / 定数
# -------------------------
st.set_page_config(page_title="研修医シフト作成", page_icon="🗓️", layout="wide")

st.markdown(
    """
    <div style="text-align:center; line-height:1.4;">
        <h2 style="margin-bottom:0.2em;">日立総合病院</h2>
        <h3 style="margin-top:0;">救急科研修医シフト作成アプリ</h3>
        <p style="font-size:0.9em; color:gray; margin-top:0.3em;">
            Hitachi General Hospital Emergency & Critical Care Residency Scheduler
        </p>
    </div>
    """,
    unsafe_allow_html=True
)

from datetime import datetime
import sys, platform, os


WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]
SHIFTS = ["ER_Early", "ER_Day1", "ER_Day2", "ER_Day3", "ER_Late", "ICU", "VAC"]
ER_BASE = ["ER_Early", "ER_Day1", "ER_Late"]   
SHIFT_LABEL = {
    "ER_Early": "早番",
    "ER_Day1": "日勤1",
    "ER_Day2": "日勤2",
    "ER_Day3": "日勤3",
    "ER_Late": "遅番",
    "ICU": "ICU",
    "VAC": "年休",
}

WEEKDAY_MAP = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}

# -------------------------
# 年/月・日付ユーティリティ
# -------------------------
this_year = dt.date.today().year
# 初期値（ロード後に上書きされる可能性あり）
default_year = this_year
default_month = dt.date.today().month

# ————— サイドバーの土台（年/月などの前にスナップショット関数を定義するため一旦保留） ————


# -------------------------
# スナップショット関連（先に定義）※ UIから呼ばれても未定義にならないように
# -------------------------
def _serialize_for_json(obj):
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, set):
        return list(obj)
    return obj

def _current_settings_as_dict():
    """現UI状態を辞書化（後でUI構築後に上書きされる値は globals() / st.session_state から読む）"""
    ss = st.session_state

    # 年/月・カレンダー関連（なければデフォルトで安全化）
    year = globals().get("year", default_year)
    month = globals().get("month", default_month)
    holidays = globals().get("holidays", [])
    closed_days = globals().get("closed_days", [])
    per_person_total = int(globals().get("per_person_total", 22))

    # 詳細設定
    max_consecutive = int(globals().get("max_consecutive", 5))
    allow_day3 = bool(globals().get("allow_day3", False))
    allow_weekend_icu = bool(globals().get("allow_weekend_icu", False))
    max_weekend_icu_total = int(globals().get("max_weekend_icu_total", 0))
    max_weekend_icu_per_person = int(globals().get("max_weekend_icu_per_person", 0))
    enable_fatigue = bool(globals().get("enable_fatigue", True))
    weight_fatigue = float(globals().get("weight_fatigue", 6.0))
    strict_mode = bool(globals().get("strict_mode", True))
    fix_repro = bool(globals().get("fix_repro", True))
    seed_val = int(globals().get("seed_val", 42)) if fix_repro else None

    weight_day2_weekday = float(globals().get("weight_day2_weekday", 2.0))
    weight_day2_wed_bonus = float(globals().get("weight_day2_wed_bonus", 8.0))
    weight_day3_weekday = float(globals().get("weight_day3_weekday", 1.0))
    weight_day3_wed_bonus = float(globals().get("weight_day3_wed_bonus", 6.0))
    weight_icu_ratio = float(globals().get("weight_icu_ratio", 3.0))
    weight_pref_B = float(globals().get("weight_pref_B", 10.0))
    weight_pref_C = float(globals().get("weight_pref_C", 5.0))

    return {
        "period": {"year": year, "month": month},
        "holidays": list(holidays),
        "closed_days": list(closed_days),
        "per_person_total": per_person_total,
        "max_consecutive": max_consecutive,
        "allow_day3": allow_day3,
        "allow_weekend_icu": allow_weekend_icu,
        "max_weekend_icu_total": max_weekend_icu_total,
        "max_weekend_icu_per_person": max_weekend_icu_per_person,
        "enable_fatigue": enable_fatigue,
        "weight_fatigue": weight_fatigue,
        "strict_mode": strict_mode,
        "fix_repro": fix_repro,
        "seed_val": seed_val,
        "weights": {
            "day2_weekday": weight_day2_weekday,
            "day2_wed_bonus": weight_day2_wed_bonus,
            "day3_weekday": weight_day3_weekday,
            "day3_wed_bonus": weight_day3_wed_bonus,
            "icu_ratio": weight_icu_ratio,
            "pref_B": weight_pref_B,
            "pref_C": weight_pref_C,
        },
        "special_er": st.session_state.get(
            "special_er", pd.DataFrame({"date": [], "drop_shift": []})
        ).to_dict(orient="records"),
        "staff": st.session_state.get(
            "staff_df", pd.DataFrame(columns=["name", "grade", "desired_icu_ratio"])
        ).to_dict(orient="records"),
        "prefs": st.session_state.get(
            "prefs", pd.DataFrame(columns=["date", "name", "kind", "priority"])
        ).to_dict(orient="records"),
        "pins": st.session_state.get(
            "pins", pd.DataFrame(columns=["date", "name", "shift"])
        ).to_dict(orient="records"),
    }

def _apply_snapshot_dict(snap: dict):
    """辞書→UIのグローバル/セッションに反映（存在チェックしつつ上書き）"""
    # 期間
    globals()["year"] = int(snap["period"]["year"])
    globals()["month"] = int(snap["period"]["month"])

    # 祝日/休診日
    def _to_date_list(lst):
        out = []
        for x in lst or []:
            try:
                if isinstance(x, str):
                    out.append(dt.date.fromisoformat(x))
                elif isinstance(x, dt.date):
                    out.append(x)
            except Exception:
                pass
        return out

    globals()["holidays"] = _to_date_list(snap.get("holidays", []))
    globals()["closed_days"] = _to_date_list(snap.get("closed_days", []))

    # 数値/フラグ
    for k in [
        "per_person_total",
        "max_consecutive",
        "allow_day3",
        "allow_weekend_icu",
        "max_weekend_icu_total",
        "max_weekend_icu_per_person",
        "enable_fatigue",
        "weight_fatigue",
        "strict_mode",
        "fix_repro",
        "seed_val",
    ]:
        if k in snap:
            globals()[k] = snap[k]

    weights = snap.get("weights", {})
    for g, k in [
        ("weight_day2_weekday", "day2_weekday"),
        ("weight_day2_wed_bonus", "day2_wed_bonus"),
        ("weight_day3_weekday", "day3_weekday"),
        ("weight_day3_wed_bonus", "day3_wed_bonus"),
        ("weight_icu_ratio", "icu_ratio"),
        ("weight_pref_B", "pref_B"),
        ("weight_pref_C", "pref_C"),
    ]:
        if k in weights:
            globals()[g] = weights[k]

    ss = st.session_state

    # special_er
    sp = pd.DataFrame(snap.get("special_er", []))
    if not sp.empty and set(sp.columns) >= {"date", "drop_shift"}:
        try:
            sp["date"] = pd.to_datetime(sp["date"]).dt.date
        except Exception:
            pass
        ss.special_er = sp[["date", "drop_shift"]]

    # staff -> editor raw
    staff = pd.DataFrame(snap.get("staff", []))
    if not staff.empty and set(staff.columns) >= {"name", "grade", "desired_icu_ratio"}:
        ss.staff_df = staff[["name", "grade", "desired_icu_ratio"]].copy()
        if "_staff_rid_seq" not in ss:
            ss._staff_rid_seq = 1
        raw = ss.staff_df.copy()
        raw.insert(0, "_rid", range(1, len(raw) + 1))
        raw["icu_ratio_label"] = (
            (raw["desired_icu_ratio"] * 100).round().astype(int).astype(str) + "%"
        )
        raw["delete"] = False
        ss._staff_rid_seq = len(raw) + 1
        ss.staff_raw = raw[["_rid", "name", "grade", "icu_ratio_label", "delete"]]

    # prefs
    prefs_df = pd.DataFrame(snap.get("prefs", []))
    if not prefs_df.empty and set(prefs_df.columns) >= {"date", "name", "kind", "priority"}:
        try:
            prefs_df["date"] = pd.to_datetime(prefs_df["date"]).dt.date
        except Exception:
            pass
        ss.prefs = prefs_df[["date", "name", "kind", "priority"]].copy()
        ss.prefs_draft = ss.prefs.copy()
        ss.prefs_editor_ver = ss.get("prefs_editor_ver", 0) + 1

    pins_df = pd.DataFrame(snap.get("pins", []))
    if not pins_df.empty and set(pins_df.columns) >= {"date", "name", "shift"}:
        try:
            pins_df["date"] = pd.to_datetime(pins_df["date"]).dt.date
        except Exception:
            pass
        ss.pins = pins_df[["date", "name", "shift"]].copy()

def make_snapshot(
    year=None, month=None, holidays=None, closed_days=None, special_map=None,
    staff_df=None, prefs_df=None, pins_df=None, per_person_total=None,
    max_consecutive=None, allow_day3=None, allow_weekend_icu=None,
    max_weekend_icu_total=None, max_weekend_icu_per_person=None,
    weight_day2_weekday=None, weight_day2_wed_bonus=None,
    weight_day3_weekday=None, weight_day3_wed_bonus=None,
    weight_icu_ratio=None, weight_pref_B=None, weight_pref_C=None,
    enable_fatigue=None, weight_fatigue=None,
    strict_mode=None, fix_repro=None, seed_val=None,
    out_df=None, stat_df=None, status="UNKNOWN", objective=None
):
    """実行スナップショット（結果も含める）"""
    ss = st.session_state
    year = year if year is not None else globals().get("year", default_year)
    month = month if month is not None else globals().get("month", default_month)
    holidays = holidays if holidays is not None else globals().get("holidays", [])
    closed_days = closed_days if closed_days is not None else globals().get("closed_days", [])
    per_person_total = int(per_person_total if per_person_total is not None else globals().get("per_person_total", 22))
    max_consecutive = int(max_consecutive if max_consecutive is not None else globals().get("max_consecutive", 5))
    allow_day3 = bool(allow_day3 if allow_day3 is not None else globals().get("allow_day3", False))
    allow_weekend_icu = bool(allow_weekend_icu if allow_weekend_icu is not None else globals().get("allow_weekend_icu", False))
    max_weekend_icu_total = int(max_weekend_icu_total if max_weekend_icu_total is not None else globals().get("max_weekend_icu_total", 0))
    max_weekend_icu_per_person = int(max_weekend_icu_per_person if max_weekend_icu_per_person is not None else globals().get("max_weekend_icu_per_person", 0))

    weight_day2_weekday = float(weight_day2_weekday if weight_day2_weekday is not None else globals().get("weight_day2_weekday", 2.0))
    weight_day2_wed_bonus = float(weight_day2_wed_bonus if weight_day2_wed_bonus is not None else globals().get("weight_day2_wed_bonus", 8.0))
    weight_day3_weekday = float(weight_day3_weekday if weight_day3_weekday is not None else globals().get("weight_day3_weekday", 1.0))
    weight_day3_wed_bonus = float(weight_day3_wed_bonus if weight_day3_wed_bonus is not None else globals().get("weight_day3_wed_bonus", 6.0))
    weight_icu_ratio = float(weight_icu_ratio if weight_icu_ratio is not None else globals().get("weight_icu_ratio", 3.0))
    weight_pref_B = float(weight_pref_B if weight_pref_B is not None else globals().get("weight_pref_B", 10.0))
    weight_pref_C = float(weight_pref_C if weight_pref_C is not None else globals().get("weight_pref_C", 5.0))

    enable_fatigue = bool(enable_fatigue if enable_fatigue is not None else globals().get("enable_fatigue", True))
    weight_fatigue = float(weight_fatigue if weight_fatigue is not None else globals().get("weight_fatigue", 6.0))
    strict_mode = bool(strict_mode if strict_mode is not None else globals().get("strict_mode", True))
    fix_repro = bool(fix_repro if fix_repro is not None else globals().get("fix_repro", True))
    seed_val = int(seed_val if seed_val is not None else globals().get("seed_val", 42)) if fix_repro else None

    if special_map is None:
        spdf = ss.get("special_er", pd.DataFrame({"date": [], "drop_shift": []}))
        special_map = {r["date"]: r["drop_shift"] for _, r in spdf.iterrows() if pd.notna(r.get("date"))}

    staff_df = staff_df if staff_df is not None else ss.get("staff_df", pd.DataFrame(columns=["name", "grade", "desired_icu_ratio"]))
    prefs_df = prefs_df if prefs_df is not None else ss.get("prefs", pd.DataFrame(columns=["date", "name", "kind", "priority"]))
    pins_df = pins_df if pins_df is not None else ss.get("pins", pd.DataFrame(columns=["date", "name", "shift"]))

    return {
        "run": {
            "timestamp": dt.datetime.now().isoformat(),
            "status": status,
            "objective": objective,
            "seed": int(seed_val) if fix_repro else None,
            "repro": bool(fix_repro),
        },
        "period": {"year": int(year), "month": int(month)},
        "settings": {
            "per_person_total": int(per_person_total),
            "max_consecutive": int(max_consecutive),
            "allow_day3": bool(allow_day3),
            "allow_weekend_icu": bool(allow_weekend_icu),
            "max_weekend_icu_total": int(max_weekend_icu_total),
            "max_weekend_icu_per_person": int(max_weekend_icu_per_person),
            "strict_mode": bool(strict_mode),
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
        "special_er": [{"date": str(k), "drop_shift": v} for k, v in special_map.items()],
        "staff": staff_df.to_dict(orient="records"),
        "prefs": [
            {**r, "date": (str(r["date"]) if r.get("date") is not None else None)}
            for r in prefs_df.to_dict(orient="records")
        ],
        "pins": [
            {**r, "date": (str(r["date"]) if r.get("date") is not None else None)}
            for r in pins_df.to_dict(orient="records")
        ],
        "result_table": (out_df.to_dict(orient="records") if out_df is not None else []),
        "person_stats": (stat_df.to_dict(orient="records") if stat_df is not None else []),
    }

def apply_snapshot(js: dict):
    """JSON直読み（ファイル/テキストから）→ セッションに反映して rerun"""
    try:
        per = js.get("period", {})
        if "year" in per and "month" in per:
            st.session_state["_restore_year"] = int(per["year"])
            st.session_state["_restore_month"] = int(per["month"])

        def _parse_dates(lst):
            out = []
            for s in lst or []:
                try:
                    out.append(dt.date.fromisoformat(s))
                except Exception:
                    pass
            return out

        st.session_state["_restore_holidays"] = _parse_dates(js.get("holidays", []))
        st.session_state["_restore_closed_days"] = _parse_dates(js.get("closed_days", []))

        sp = js.get("special_er", [])
        try:
            st.session_state.special_er = pd.DataFrame(
                [
                    {"date": dt.date.fromisoformat(r["date"]), "drop_shift": r["drop_shift"]}
                    for r in sp
                    if r.get("date") and r.get("drop_shift")
                ]
            )
        except Exception:
            st.session_state.special_er = pd.DataFrame({"date": [], "drop_shift": []})

        # staff
        staff = js.get("staff", [])
        st.session_state.staff_df = (
            pd.DataFrame(staff)[["name", "grade", "desired_icu_ratio"]]
            if staff
            else pd.DataFrame(columns=["name", "grade", "desired_icu_ratio"])
        )
        raw = st.session_state.staff_df.copy()
        raw.insert(0, "_rid", range(1, len(raw) + 1))
        raw["icu_ratio_label"] = (
            (raw["desired_icu_ratio"] * 100).round().astype(int).astype(str) + "%"
        )
        raw["delete"] = False
        st.session_state.staff_raw = raw[
            ["_rid", "name", "grade", "icu_ratio_label", "delete"]
        ]

        # prefs
        def parse_prefs(lst):
            rows = []
            for r in lst or []:
                try:
                    dd = dt.date.fromisoformat(r["date"]) if r.get("date") else None
                    if dd is None:
                        continue
                    rows.append(
                        {
                            "date": dd,
                            "name": r.get("name", ""),
                            "kind": r.get("kind", ""),
                            "priority": r.get("priority", ""),
                        }
                    )
                except Exception:
                    pass
            return pd.DataFrame(rows)

        st.session_state.prefs = parse_prefs(js.get("prefs", []))
        st.session_state.prefs_draft = st.session_state.prefs.copy()
        st.session_state.prefs_editor_ver = st.session_state.get("prefs_editor_ver", 0) + 1

        # pins
        def parse_pins(lst):
            rows = []
            for r in lst or []:
                try:
                    dd = dt.date.fromisoformat(r["date"]) if r.get("date") else None
                    if dd is None:
                        continue
                    rows.append(
                        {"date": dd, "name": r.get("name", ""), "shift": r.get("shift", "")}
                    )
                except Exception:
                    pass
            return pd.DataFrame(rows)

        st.session_state.pins = parse_pins(js.get("pins", []))

        st.success("スナップショットを読み込みました。年/月・祝日等を反映するため再描画します。")
        st.rerun()
    except Exception as e:
        st.error(f"スナップショット適用に失敗しました: {e}")


# -------------------------
# セッション初期化
# -------------------------
def _init_state():
    ss = st.session_state
    if "staff_df" not in ss:
        ss.staff_df = pd.DataFrame([{"name": "", "grade": "J1", "desired_icu_ratio": 0.0}])

    if "prefs" not in ss:
        ss.prefs = pd.DataFrame(columns=["date", "name", "kind", "priority"])
    if "prefs_draft" not in ss:
        ss.prefs_draft = ss.prefs.copy()
    if "prefs_editor_ver" not in ss:
        ss.prefs_editor_ver = 0
    if "prefs_backup" not in ss:
        ss.prefs_backup = None
    if "last_bulk_add_rows" not in ss:
        ss.last_bulk_add_rows = []

    if "pins" not in ss:
        ss.pins = pd.DataFrame(columns=["date", "name", "shift"])
    if "pins_backup" not in ss:
        ss.pins_backup = None

    if "special_er" not in ss:
        ss.special_er = pd.DataFrame({"date": [], "drop_shift": []})

    if "snapshots" not in ss:
        ss.snapshots = {}
    if "snap_counter" not in ss:
        ss.snap_counter = 1

_init_state()

# -------------------------
# サイドバー：基本入力（翌月をデフォルト）
# -------------------------
st.sidebar.header("📌 必須情報")

# 翌月を計算
today = dt.date.today()
if today.month == 12:
    next_year, next_month = today.year + 1, 1
else:
    next_year, next_month = today.year, today.month + 1

# 1) 復元フラグがあれば最優先で state に入れる（ウィジェット生成前）
if "_restore_year" in st.session_state:
    st.session_state["year_input"] = int(st.session_state.pop("_restore_year"))
else:
    # 初回は翌月の年をデフォルトに
    st.session_state.setdefault("year_input", next_year)

if "_restore_month" in st.session_state:
    st.session_state["month_input"] = int(st.session_state.pop("_restore_month"))
else:
    # 初回は翌月をデフォルトに
    st.session_state.setdefault("month_input", next_month)

# 2) ウィジェット作成（value/indexは渡さず key で制御）
year = st.sidebar.number_input(
    "作成年",
    min_value=this_year - 2,
    max_value=this_year + 2,
    step=1,
    key="year_input",   # ← ここが唯一のソース
)
month = st.sidebar.selectbox(
    "作成月",
    list(range(1, 13)),
    key="month_input",  # ← ここが唯一のソース（stateに入っている数値 1-12）
)

# 3) 以降は変数をそのまま使えばOK
start_date = dt.date(year, month, 1)
end_date = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
all_days = [d.date() for d in rrule(DAILY, dtstart=start_date, until=end_date)]
D = len(all_days)

def date_label(d: dt.date) -> str:
    return f"{d}({WEEKDAY_JA[d.weekday()]})"

DATE_OPTIONS = [date_label(d) for d in all_days]
LABEL_TO_DATE = {date_label(d): d for d in all_days}
DATE_TO_LABEL = {d: date_label(d) for d in all_days}

# --- placeholders for static checker (will be overwritten by UI) ---
holidays: list[dt.date] = []
closed_days: list[dt.date] = []

# --- 祝日：自動取得ヘルパー & UI（改良版） ---
def _jp_holidays_for(year: int, month: int) -> list[dt.date]:
    """当月の日本の祝日リスト（jpholiday が無い/失敗なら空）"""
    try:
        import jpholiday
        from calendar import monthrange
        last_day = monthrange(year, month)[1]
        start = dt.date(year, month, 1)
        end   = dt.date(year, month, last_day)
        days = [d.date() for d in rrule(DAILY, dtstart=start, until=end)]
        return [d for d in days if jpholiday.is_holiday(d)]
    except Exception:
        return []

# --- state 初期化 / 再取得制御（ウィジェット生成前に済ませる） ---
_restore = st.session_state.pop("_restore_holidays", None)
if _restore is not None:
    initial_holidays = [d for d in _restore if d in all_days]
else:
    initial_holidays = [d for d in _jp_holidays_for(year, month) if d in all_days]

if st.session_state.pop("_refresh_holidays", False):
    st.session_state["holidays_ms"] = [d for d in _jp_holidays_for(year, month) if d in all_days]

if "holidays_ms" not in st.session_state:
    st.session_state["holidays_ms"] = initial_holidays
else:
    st.session_state["holidays_ms"] = [d for d in st.session_state["holidays_ms"] if d in all_days]

# ---- UI部分（←ここを差し替え）----
holbox = st.sidebar.container()     # まずコンテナを作る
with holbox:
    head_l, head_r = st.columns([1, 0.22])
    with head_l:
        st.markdown("#### 祝日（当月）")
        # jpholiday の有無バッジ
        try:
            import jpholiday  # noqa: F401
            _hol_ok = True
        except Exception:
            _hol_ok = False
        st.caption("✅ 自動取得ON" if _hol_ok else "❌ 自動取得OFF（`pip install jpholiday`）")

    with head_r:
        if st.button("🔄", key="btn_refresh_holidays", help="祝日を再取得"):
            st.session_state["_refresh_holidays"] = True
            st.rerun()

    # マルチセレクト本体（ラベルは畳む）
    holidays = st.multiselect(
        "",
        options=all_days,
        format_func=lambda d: f"{d}({WEEKDAY_JA[d.weekday()]})",
        key="holidays_ms",
        label_visibility="collapsed",
    )

# 実体として使用
holidays = st.session_state["holidays_ms"]

# === 病院休診日（複数選択可） ===
# 復元値があれば最優先
_restore_closed = st.session_state.pop("_restore_closed_days", None)

# state 初期化/トリム（ウィジェット作成前に済ませる）
if "closed_ms" not in st.session_state:
    st.session_state["closed_ms"] = [d for d in (_restore_closed or []) if d in all_days]
else:
    # 月をまたいだ後のゴミを除去
    st.session_state["closed_ms"] = [d for d in st.session_state["closed_ms"] if d in all_days]

# UI（ヘッダー + 右側にクリアボタン）
closed_box = st.sidebar.container()
with closed_box:
    head_l, head_r = st.columns([1, 0.22])
    with head_l:
        st.markdown("#### 🛑 休診日の設定（複数選択可）")
        st.caption("※ 休診日は ER 日勤2, 日勤3 を配置しません")
    with head_r:
        if st.button("🧹", key="btn_clear_closed", help="選択をすべて解除"):
            st.session_state["closed_ms"] = []
            st.rerun()

    # マルチセレクト本体（defaultは渡さず key だけ）
    closed_days = st.multiselect(
        "",
        options=all_days,
        format_func=lambda d: f"{d}({WEEKDAY_JA[d.weekday()]})",
        key="closed_ms",
        label_visibility="collapsed",
    )

# 以降で使う実体
closed_days = st.session_state["closed_ms"]

st.sidebar.divider()
per_person_total = st.sidebar.number_input(
    "👥 総勤務回数", min_value=0, value=22, step=1
)
st.sidebar.caption("病院の年間休日カレンダーに記載の所定勤務日数に合わせてください")

st.sidebar.header("🗓️ 月ごとの設定")
max_consecutive = st.sidebar.slider("最大連勤日数", 3, 7, 5)
enable_fatigue = st.sidebar.checkbox("遅番→翌日早番を避ける", value=True)
weight_fatigue = st.sidebar.slider(
    "疲労ペナルティの重み", 0.0, 30.0, 6.0, 1.0, disabled=not enable_fatigue
)

allow_day3 = st.sidebar.checkbox("ER日勤3を許可", value=False, help="ON: チェックするとローテーターが多い時に日勤3が入れられるようになります（平日のみ）")
allow_weekend_icu = st.sidebar.checkbox("週末ICUを許可", value=False, help="ON: チェックすると、土日祝にJ2のICUローテが入るようになります")
max_weekend_icu_total = st.sidebar.number_input(
    "週末ICUの総上限（許可時のみ）", min_value=0, value=0, step=1, disabled=not allow_weekend_icu
)
max_weekend_icu_per_person = st.sidebar.number_input(
    "1人あたり週末ICU上限", min_value=0, value=0, step=1, disabled=not allow_weekend_icu
)

st.sidebar.header("🧩 最適化の動作")
strict_mode = st.sidebar.checkbox(
    "バランスの最適化",
    value=True,
    help="ON: J1休日ばらつき±1 / Day2・Day3ボーナス=通常。OFF: ±2 / ボーナス弱め。A希望・総勤務回数などのハード制約は常に厳守。",
)
fix_repro = st.sidebar.checkbox("再現性を固定", value=True, help="ON: 乱数シードの数値を維持することで同じ結果を再現しやすくなります",)
seed_val = st.sidebar.number_input(
    "乱数シード", min_value=0, max_value=1_000_000, value=42, step=1, disabled=not fix_repro
)

with st.sidebar.expander("⚙️ 詳細ウェイト設定", expanded=False):
    weight_day2_weekday = st.slider("平日のER日勤2を入れる優先度", 0.0, 10.0, 2.0, 0.5)
    weight_day2_wed_bonus = st.slider("水曜ボーナス（ER日勤2）", 0.0, 30.0, 8.0, 0.5)
    weight_day3_weekday = st.slider(
        "平日のER日勤3を入れる優先度", 0.0, 10.0, 1.0, 0.5, disabled=not allow_day3
    )
    weight_day3_wed_bonus = st.slider(
        "水曜ボーナス（ER日勤3）", 0.0, 30.0, 6.0, 0.5, disabled=not allow_day3
    )
    weight_icu_ratio = st.slider("J2のICU希望比率の遵守 重み", 0.0, 10.0, 3.0, 0.5)
    weight_pref_B = st.slider("希望B未充足ペナルティ", 0.0, 50.0, 10.0, 1.0)
    weight_pref_C = st.slider("希望C未充足ペナルティ", 0.0, 50.0, 5.0, 1.0)

# ===== 自動再開 用：最後の状態をディスクに保存／読込するヘルパー =====
import json, os

# ===== ディスク保存 / 復元（絶対に1か所だけ置く）=============================

# ファイルは app.py と同じフォルダに固定保存（タブを変えても同じファイルを参照できる）
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_SNAPSHOT_FILE = os.path.join(APP_DIR, ".streamlit_last_snapshot.json")

def _json_default(o):
    import datetime as _dt
    if isinstance(o, (_dt.date, _dt.datetime)):
        return o.isoformat()
    if isinstance(o, set):
        return list(o)
    return str(o)

def save_last_snapshot_to_disk():
    """現在のUI状態を app.py と同じディレクトリに保存"""
    try:
        payload = _current_settings_as_dict()
        with open(LAST_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)
        return True, None
    except Exception as e:
        return False, str(e)

def load_last_snapshot_from_disk():
    """スナップショットdictを返す（ここでは適用しない）"""
    try:
        if os.path.exists(LAST_SNAPSHOT_FILE):
            with open(LAST_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return None
    except Exception as e:
        st.sidebar.warning(f"読み込みに失敗: {e}")
        return None

# --- サイドバーUI（重複させず1回だけ） ---
st.sidebar.divider()
st.sidebar.subheader("🧷 前回状態（ディスク）")

# 状態表示（ファイルの有無/更新時刻/サイズ）
if os.path.exists(LAST_SNAPSHOT_FILE):
    try:
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(LAST_SNAPSHOT_FILE)).strftime("%Y-%m-%d %H:%M:%S")
        size_kb = os.path.getsize(LAST_SNAPSHOT_FILE) / 1024.0
        st.sidebar.caption(f"📄 保存あり: {mtime}（{size_kb:.1f} KB）\nパス: {LAST_SNAPSHOT_FILE}")
    except Exception:
        st.sidebar.caption("📄 保存あり（情報取得に失敗）")
else:
    st.sidebar.caption("（保存ファイルはまだありません）")

c_a, c_b = st.sidebar.columns(2)

# v1.41以降は width を使う（use_container_width は警告の原因）
if c_a.button("💾 保存", key="btn_save_to_disk", width="stretch"):
    ok, err = save_last_snapshot_to_disk()
    if ok:
        st.sidebar.success("保存しました。")
    else:
        st.sidebar.error(f"保存に失敗: {err}")

if c_b.button("📥 復元", key="btn_restore_from_disk", width="stretch"):
    snap = load_last_snapshot_from_disk()
    if snap:
        _apply_snapshot_dict(snap)    # ここでUIに反映
        st.sidebar.success("前回保存した設定を反映しました。再描画します。")
        st.rerun()
    else:
        st.sidebar.info("前回ファイルがありません。")



# -------------------------
# 💾 シナリオ保存 / 復元（統合・単一）
# -------------------------
st.sidebar.divider()


# --- 手動セーブ/ロード（ディスク） ---
st.sidebar.divider()

# --- ここまで ---



# -------------------------
# 休日集合
# -------------------------
H = set(d for d in all_days if d.weekday() >= 5) | set(holidays)

# -------------------------
# ER特例（画面では編集せずセッションから辞書化）
# -------------------------
_special_df = st.session_state.special_er.copy() if "special_er" in st.session_state else pd.DataFrame(columns=["date", "drop_shift"])
if not _special_df.empty:
    _special_df = _special_df.dropna()
    if "date" in _special_df.columns:
        _special_df = _special_df[_special_df["date"].isin(all_days)]
    _special_df = _special_df.drop_duplicates(subset=["date"], keep="last")
special_map = {row["date"]: row["drop_shift"] for _, row in _special_df.iterrows()}

# -------------------------
# スタッフ入力
# -------------------------
st.header("🧑‍⚕️ スタッフ入力")

if "_staff_rid_seq" not in st.session_state:
    st.session_state._staff_rid_seq = 1

def _new_staff_rid():
    rid = st.session_state._staff_rid_seq
    st.session_state._staff_rid_seq += 1
    return rid

if "staff_raw" not in st.session_state:
    st.session_state.staff_raw = pd.DataFrame(
        [{"_rid": _new_staff_rid(), "name": "", "grade": "J1", "icu_ratio_label": "0%", "delete": False}]
    )
if "delete" not in st.session_state.staff_raw.columns:
    st.session_state.staff_raw["delete"] = False

with st.form("staff_form", clear_on_submit=False):
    st.caption("入力完了後に必ず保存ボタンを押してください。そうでないと、変更が反映されません。")
    staff_in = st.session_state.staff_raw.copy()
    staff_out = st.data_editor(
        staff_in,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_order=["delete", "name", "grade", "icu_ratio_label"],
        column_config={
            "delete": st.column_config.CheckboxColumn("削除", help="削除したい行にチェック"),
            "name": st.column_config.TextColumn("名前", help="例：田中、田中一など"),
            "grade": st.column_config.SelectboxColumn("区分", options=["J1", "J2"], help="J1はICU不可（自動で0%固定）"),
            "icu_ratio_label": st.column_config.SelectboxColumn("ICU希望比率", options=[f"{i}%" for i in range(0, 101, 10)]),
            "_rid": st.column_config.NumberColumn("rid", disabled=True),
        },
        key="staff_editor",
    )
    c1, c2 = st.columns([1, 1])
    save_staff = c1.form_submit_button("💾 保存（変更したら必ず押す）", type="primary", use_container_width=True)
    del_staff = c2.form_submit_button("🗑️ チェックした行を削除", use_container_width=True)

    if save_staff or del_staff:
        df = staff_out.copy()
        if "_rid" not in df.columns:
            df["_rid"] = pd.Series(dtype="Int64")
        df["_rid"] = pd.to_numeric(df["_rid"], errors="coerce")
        mask_new = df["_rid"].isna()
        if mask_new.any():
            df.loc[mask_new, "_rid"] = [_new_staff_rid() for _ in range(mask_new.sum())]
        df["_rid"] = df["_rid"].astype(int)

        if "delete" not in df.columns:
            df["delete"] = False
        df["delete"] = df["delete"].fillna(False).apply(lambda v: bool(v) if pd.notna(v) else False)
        if del_staff:
            df = df[~df["delete"]].copy()

        df["name"] = df["name"].astype(str).str.strip()
        df["grade"] = (
            df["grade"].astype(str).str.upper().where(df["grade"].astype(str).str.upper().isin(["J1", "J2"]), "J1")
        )
        df["icu_ratio_label"] = df["icu_ratio_label"].astype(str).str.strip()
        df = df[df["name"] != ""].copy()

        def lbl_to_ratio(s):
            try:
                return float(str(s).replace("%", "")) / 100.0
            except Exception:
                return 0.0

        df["desired_icu_ratio"] = df["icu_ratio_label"].map(lbl_to_ratio)
        df.loc[df["grade"] == "J1", "desired_icu_ratio"] = 0.0

        if df["name"].duplicated().any():
            st.error("同じ名前が重複しています。重複を解消してから保存してください。")
        else:
            st.session_state.staff_raw = df.reset_index(drop=True)
            staff_df = df[["name", "grade", "desired_icu_ratio"]].reset_index(drop=True)
            st.session_state.staff_df = staff_df
            st.success("スタッフを保存しました。")
            st.rerun()

staff_df = st.session_state.get("staff_df", pd.DataFrame(columns=["name", "grade", "desired_icu_ratio"])).copy()
if staff_df.empty:
    st.warning("少なくとも1名入力してください。")
    st.stop()

names = staff_df["name"].tolist()
N = len(names)
name_to_idx = {n: i for i, n in enumerate(names)}
J1_idx = [i for i in range(N) if staff_df.iloc[i]["grade"] == "J1"]
J2_idx = [i for i in range(N) if staff_df.iloc[i]["grade"] == "J2"]

# -------------------------
# 一括登録（B/C）
# -------------------------
st.subheader("🧰 一括登録設定")

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

with st.form("bulk_prefs_form", clear_on_submit=False):
    scope = st.selectbox("対象日", ["毎週指定曜日", "全休日", "全平日", "祝日のみ"], index=0)
    sel_wd_label = st.selectbox("曜日", list(WEEKDAY_MAP.keys()), index=2, disabled=(scope != "毎週指定曜日"))
    target_mode = st.selectbox("対象者", ["全員", "J1のみ", "J2のみ", "個別選択"], index=2 if len(J2_idx) > 0 else 1)
    bulk_kind = st.selectbox("希望種別", ["off", "early", "day", "late", "icu"], index=0)
    bulk_prio = st.selectbox("優先度", ["B", "C"], index=0)

    selected_names = names
    if target_mode == "J1のみ":
        selected_names = [names[i] for i in J1_idx]
    elif target_mode == "J2のみ":
        selected_names = [names[i] for i in J2_idx]
    elif target_mode == "個別選択":
        selected_names = st.multiselect(
            "個別に選択", options=names, default=[names[i] for i in J2_idx] if len(J2_idx) > 0 else names
        )

    submitted = st.form_submit_button("＋ 一括追加（B/Cのみ）", type="primary", use_container_width=True)

if submitted:
    H_set = set(d for d in all_days if d.weekday() >= 5) | set(holidays)
    if scope == "毎週指定曜日":
        sel_wd = WEEKDAY_MAP.get(sel_wd_label, 2)
        target_days = [d for d in all_days if d.weekday() == sel_wd]
    elif scope == "全休日":
        target_days = [d for d in all_days if d in H_set]
    elif scope == "全平日":
        target_days = [d for d in all_days if d.weekday() < 5]
    else:
        target_days = list(set(holidays))

    if bulk_prio not in ("B", "C"):
        st.warning("Aは一括登録の対象外です。個別に追加してください。")
    else:
        existing = st.session_state.prefs.copy()
        add_rows = []
        skipped_j1_icu = 0
        for d in target_days:
            for nm in selected_names:
                if bulk_kind == "icu":
                    if staff_df.loc[staff_df["name"] == nm, "grade"].iloc[0] == "J1":
                        skipped_j1_icu += 1
                        continue
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
                    add_rows.append({"date": d, "name": nm, "kind": bulk_kind, "priority": bulk_prio})

        if add_rows:
            st.session_state.prefs_backup = existing.copy(deep=True)
            st.session_state.prefs = pd.concat([existing, pd.DataFrame(add_rows)], ignore_index=True)
            st.session_state.last_bulk_add_rows = add_rows
            tmp = st.session_state.prefs.copy()
            tmp["date"] = pd.to_datetime(tmp.get("date"), errors="coerce").dt.date
            st.session_state.prefs_draft = tmp
            st.session_state.prefs_editor_ver += 1

            msg = f"{len(add_rows)} 件を追加しました。"
            if skipped_j1_icu > 0:
                msg += f"（J1→ICUの希望 {skipped_j1_icu} 件は無視しました）"
            st.success(msg)
            st.rerun()
        else:
            info = "追加対象がありません（既に登録済みです）。"
            if skipped_j1_icu > 0:
                info += f" J1→ICUの希望 {skipped_j1_icu} 件は無視しました。"
            st.info(info)

# -------------------------
# 希望（A/B/C）
# -------------------------
st.subheader("📝 希望")
st.caption("※ A=絶対;冠婚葬祭など / B=強く希望;旅行予定など / C=できれば;その他の用事など")
st.caption("入力完了後に必ず保存ボタンを押してください。そうでないと、変更が反映されません。")

draft = st.session_state.prefs_draft.copy()
if "date" in draft.columns:
    draft["date"] = pd.to_datetime(draft["date"], errors="coerce").dt.date
else:
    draft["date"] = pd.Series(dtype="object")

prefs_widget_key = f"prefs_editor_{st.session_state.prefs_editor_ver}"
edited = st.data_editor(
    draft,
    key=prefs_widget_key,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "date": st.column_config.DateColumn(
            "日付", min_value=start_date, max_value=end_date, format="YYYY-MM-DD", help="当該月のみ選択できます"
        ),
        "name": st.column_config.SelectboxColumn("名前", options=names),
        "kind": st.column_config.SelectboxColumn(
    "種別",
    options=["off", "early", "late", "day", "day1", "day2", "icu", "vacation"],
    help="Aは off/early/late/（必要なら day1/day2/vacation）。day/icu のAは自動でBへ降格",
),
        "priority": st.column_config.SelectboxColumn("優先度", options=["A", "B", "C"]),
    },
)

with st.form("prefs_save_form", clear_on_submit=False):
    save = st.form_submit_button("💾 希望を保存（必ず押してください）", type="primary", use_container_width=True)
    if save:
        df = edited.copy().fillna({"kind": "off", "priority": "C"})
        df["name"] = df["name"].astype(str).str.strip()
        df = df[df["name"] != ""]
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df = df[df["date"].notna()]
        df["kind"] = df["kind"].astype(str).str.strip().str.lower()
        df["priority"] = df["priority"].astype(str).str.strip().str.upper()
        bad_mask = (df["priority"] == "A") & (df["kind"].isin(["day", "icu"]))
        df.loc[bad_mask, "priority"] = "B"
        df = df[df["kind"].isin(["off", "early", "late", "day", "day1", "day2", "icu"])]
        df = df[df["name"].isin(names)]
        df = df.drop_duplicates(subset=["date", "name", "kind", "priority"], keep="last").reset_index(drop=True)

        st.session_state.prefs_backup = st.session_state.prefs.copy(deep=True)
        st.session_state.prefs = df
        st.session_state.prefs_draft = df.copy()
        st.session_state.prefs_editor_ver += 1
        st.success("希望を保存しました。")
        st.rerun()

# -------------------------
# プリアサイン
# -------------------------
st.subheader("📌 事前のアサイン（固定割当）")
st.caption("入力完了後に必ず保存ボタンを押してください。そうでないと、変更が反映されません。")

if "_pin_rid_seq" not in st.session_state:
    st.session_state._pin_rid_seq = 1

def _new_pin_rid():
    rid = st.session_state._pin_rid_seq
    st.session_state._pin_rid_seq += 1
    return rid

if "pins_raw" not in st.session_state:
    st.session_state.pins_raw = pd.DataFrame(
        [{"_rid": _new_pin_rid(), "date_label": "", "name": "", "shift": "ER_Early"}]
    )

with st.form("pins_form", clear_on_submit=False):
    pins_in = st.session_state.pins_raw.copy()
    pins_out = st.data_editor(
        pins_in,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_order=["date_label", "name", "shift"],
        column_config={
            "date_label": st.column_config.SelectboxColumn("日付", options=DATE_OPTIONS),
            "name": st.column_config.SelectboxColumn("名前", options=names),
            "shift": st.column_config.SelectboxColumn(
                "シフト", options=["ER_Early", "ER_Day1", "ER_Day2", "ER_Day3", "ER_Late", "ICU"]
            ),
            "_rid": st.column_config.NumberColumn("rid", disabled=True),
        },
        key="pins_editor",
    )
    c1, c2 = st.columns([1, 1])
    save_pins = c1.form_submit_button("💾 保存（変更したら必ず押す）", type="primary", use_container_width=True)
    prune_pins = c2.form_submit_button("🧹 空行を削除して保存", use_container_width=True)

    if save_pins or prune_pins:
        tmp = pins_out.copy()
        if "_rid" not in tmp.columns:
            tmp["_rid"] = pd.Series(dtype="Int64")
        tmp["_rid"] = pd.to_numeric(tmp["_rid"], errors="coerce")
        mask_new = tmp["_rid"].isna()
        if mask_new.any():
            tmp.loc[mask_new, "_rid"] = [_new_pin_rid() for _ in range(mask_new.sum())]
        tmp["_rid"] = tmp["_rid"].astype(int)

        tmp["date_label"] = tmp["date_label"].astype(str).str.strip()
        tmp["name"] = tmp["name"].astype(str).str.strip()
        tmp["shift"] = tmp["shift"].astype(str).str.strip()

        if prune_pins:
            tmp = tmp[(tmp["date_label"] != "") | (tmp["name"] != "")]

        pins = tmp[(tmp["name"] != "") & (tmp["date_label"].isin(DATE_OPTIONS))].copy()
        pins["date"] = pins["date_label"].map(LABEL_TO_DATE)

        if not pins.empty:
            j1_names = set(staff_df.loc[staff_df["grade"] == "J1", "name"].tolist())
            bad = (pins["shift"] == "ICU") & (pins["name"].isin(j1_names))
            if bad.any():
                bad_rows = pins[bad][["date", "name"]].to_records(index=False).tolist()
                pins = pins[~bad]
                st.error(
                    "J1 に ICU のプリアサインは無効化しました:\n"
                    + "\n".join([f"- {d} {n}" for d, n in bad_rows])
                )

        pins = pins[["date", "name", "shift"]]
        st.session_state.pins_raw = tmp.reset_index(drop=True)
        st.session_state.pins = pins.reset_index(drop=True)
        st.success("プリアサインを保存しました。")
        st.rerun()

# -------------------------
# 可否カレンダー
# -------------------------
DAY2_FORBID = set([d for d in all_days if d.weekday() >= 5]) | set(holidays) | set(closed_days)
WEEKDAYS = set([d for d in all_days if d.weekday() < 5])
ICU_ALLOWED_DAYS = set(all_days) if allow_weekend_icu else WEEKDAYS

cal_rows = []
for d in all_days:
    cal_rows.append(
        {
            "Date": str(d),
            "Weekday": WEEKDAY_JA[d.weekday()],
            "D2": "🟢可" if (d.weekday() < 5 and d not in DAY2_FORBID) else "🔴不可",
            "D3": ("🟢可" if (allow_day3 and d.weekday() < 5 and d not in DAY2_FORBID) else ("—" if not allow_day3 else "🔴不可")),
            "ICU": "可" if (d in ICU_ALLOWED_DAYS) else "不可",
            "Holiday/Closed": ("休" if d in H else "") + (" 休診" if d in set(closed_days) else ""),
        }
    )
cal_df = pd.DataFrame(cal_rows)
with st.expander("🗓️ Day2/Day3/ICU の設置可否カレンダー"):
    st.dataframe(cal_df, use_container_width=True, hide_index=True)

# -------------------------
# 前処理バリデーション
# -------------------------
R2 = len([d for d in all_days if (d.weekday() < 5 and d not in DAY2_FORBID)])
R3 = len([d for d in all_days if (allow_day3 and d.weekday() < 5 and d not in DAY2_FORBID)])
W = len([d for d in all_days if d in ICU_ALLOWED_DAYS])

sum_target = int(per_person_total) * N
min_required = 3 * D
max_possible_info = 3 * D + R2 + R3 + W

colv1, colv2, colv3 = st.columns(3)
with colv1:
    st.metric("当月日数 D", D)
with colv2:
    st.metric("ER最低必要 3×D", 3 * D)
with colv3:
    st.metric("Σ target_total", sum_target)

if sum_target < min_required:
    st.error(f"総勤務回数の合計が不足（{sum_target} < {min_required}）。ERの基本3枠/日を満たせません。")
    st.stop()

if sum_target > max_possible_info:
    st.warning(
        f"参考：Σtarget（{sum_target}）が理論上限（{max_possible_info}）を超えています。ICU任意/Day2・Day3の可否により吸収できない可能性があります。"
    )

max_ER_slots_info = 3 * D + R2 + R3
sum_J1 = int(per_person_total) * len(J1_idx)
if sum_J1 > max_ER_slots_info:
    st.warning(f"参考：J1合計（{sum_J1}）がERで吸収可能な理論値（{max_ER_slots_info}）を上回る恐れがあります。")

# -------------------------
# A希望の事前検証
# -------------------------
def validate_A_requests(prefs_df, DAY):
    issues = []
    a_off = set()
    for _, r in prefs_df[(prefs_df["priority"] == "A") & (prefs_df["kind"] == "off")].iterrows():
        if r["date"] in all_days and r["name"] in name_to_idx:
            a_off.add((r["date"], r["name"]))
    for _, r in prefs_df[prefs_df["priority"] == "A"].iterrows():
        d = r["date"]
        nm = r["name"]
        k = str(r["kind"]).lower()
        if d not in all_days or nm not in name_to_idx:
            continue
        if (d, nm) in a_off and k != "off":
            issues.append(f"{d} {nm}: A-休み と A-{k} は同日に共存できません")
    # すでに a_off セットがある前提で
    for _, r in prefs_df[prefs_df["priority"] == "A"].iterrows():
        d, nm, k = r["date"], r["name"], str(r["kind"]).lower()
        if d not in all_days or nm not in name_to_idx:
            continue
        if (d, nm) in a_off and k == "vacation":
            issues.append(f"{d} {nm}: A-休み と A-vacation は同日に共存できません")

    j1_names = set(staff_df.loc[staff_df["grade"] == "J1", "name"].tolist())
    for _, r in prefs_df[(prefs_df["priority"] == "A") & (prefs_df["kind"].str.lower() == "icu")].iterrows():
        if r["name"] in j1_names:
            issues.append(f"{r['date']} {r['name']}: J1 に A-ICU は割当不可能です")

    for _, r in prefs_df[prefs_df["priority"] == "A"].iterrows():
        d = r["date"]
        nm = r["name"]
        k = str(r["kind"]).lower()
        if d not in all_days or nm not in name_to_idx:
            continue
        di = all_days.index(d)
        if k == "early" and DAY[di]["req"]["ER_Early"] == 0:
            issues.append(f"{d} {nm}: 特例で早番が停止中のため A-early は不可能です")
        if k == "late" and DAY[di]["req"]["ER_Late"] == 0:
            issues.append(f"{d} {nm}: 特例で遅番が停止中のため A-late は不可能です")
        if k == "day1" and DAY[di]["req"]["ER_Day1"] == 0:
            issues.append(f"{d} {nm}: 特例で日勤1が停止中のため A-day1 は不可能です")
        if k == "day2" and not DAY[di]["allow_d2"]:
            issues.append(f"{d} {nm}: その日は日勤2が立たないため A-day2 は不可能です")
        if k == "icu" and not DAY[di]["allow_icu"]:
            issues.append(f"{d} {nm}: その日はICU不可のため A-ICU は不可能です")

    a_counts = {}
    for _, r in prefs_df[prefs_df["priority"] == "A"].iterrows():
        d = r["date"]
        k = str(r["kind"]).lower()
        if d in all_days:
            di = all_days.index(d)
            key = None
            if k == "early" and DAY[di]["req"]["ER_Early"] == 1:
                key = ("ER_Early", di)
            if k == "late" and DAY[di]["req"]["ER_Late"] == 1:
                key = ("ER_Late", di)
            if k == "day1" and DAY[di]["req"]["ER_Day1"] == 1:
                key = ("ER_Day1", di)
            if k == "day2" and DAY[di]["allow_d2"]:
                key = ("ER_Day2", di)
            if k == "icu" and DAY[di]["allow_icu"]:
                key = ("ICU", di)
            if key:
                a_counts.setdefault(key, 0)
                a_counts[key] += 1
    for (shift_name, di), cnt in a_counts.items():
        cap = 1
        if cnt > cap:
            issues.append(f"{all_days[di]} {shift_name}: A希望が{cnt}件あり、定員{cap}を超えています")

    return issues

# -------------------------
# ソルバー
# -------------------------
def build_and_solve(fair_slack: int, disabled_pref_ids: set, weaken_day2_bonus: bool = False, repro_fix: bool = True):
    model = cp_model.CpModel()
    x = {(d, s, i): model.NewBoolVar(f"x_d{d}_s{s}_i{i}") for d in range(D) for s in range(len(SHIFTS)) for i in range(N)}

    for d in range(D):
        for i in range(N):
            if (d, i) not in allow_vac:
                model.Add(x[(d, VAC_IDX, i)] == 0)

    for d in range(D):
        for i in range(N):
            model.Add(sum(x[(d, s, i)] for s in range(len(SHIFTS))) <= 1)

    ICU_IDX = SHIFTS.index("ICU")
    for d in range(D):
        for i in [j for j in range(N) if staff_df.iloc[j]["grade"] == "J1"]:
            model.Add(x[(d, ICU_IDX, i)] == 0)

    VAC_IDX = SHIFTS.index("VAC")

    for i in range(N):
        y = [model.NewBoolVar(f"y_d{d}_i{i}") for d in range(D)]
        for d in range(D):
            model.Add(y[d] == sum(x[(d, s, i)] for s in range(len(SHIFTS))))
        window = max_consecutive + 1
        if D >= window:
            for start in range(0, D - window + 1):
                model.Add(sum(y[start + k] for k in range(window)) <= max_consecutive)

    for i in range(N):
        ti = model.NewIntVar(0, 5 * D, f"total_i{i}")
        model.Add(ti == sum(x[(d, s, i)] for d in range(D) for s in range(len(SHIFTS))))
        model.Add(ti == int(per_person_total))

    DAY = {
        d: {"req": {"ER_Early": 1, "ER_Day1": 1, "ER_Late": 1}, "allow_d2": False, "allow_d3": False, "allow_icu": False, "drop": None}
        for d in range(D)
    }
    DAY2_FORBID_LOCAL = set([d for d in all_days if d.weekday() >= 5]) | set(holidays) | set(closed_days)
    ICU_ALLOWED_DAYS_LOCAL = set(all_days) if allow_weekend_icu else set([d for d in all_days if d.weekday() < 5])

    for d, day in enumerate(all_days):
        drop = special_map.get(day)
        if drop in ER_BASE:
            DAY[d]["req"][drop] = 0
            DAY[d]["drop"] = drop
        if day.weekday() < 5 and day not in DAY2_FORBID_LOCAL:
            DAY[d]["allow_d2"] = True
            DAY[d]["allow_d3"] = bool(allow_day3)
        if day in ICU_ALLOWED_DAYS_LOCAL:
            DAY[d]["allow_icu"] = True

    for d in range(D):
        for base in ER_BASE:
            sidx = SHIFTS.index(base)
            model.Add(sum(x[(d, sidx, i)] for i in range(N)) == DAY[d]["req"][base])

    D2_IDX = SHIFTS.index("ER_Day2")
    D3_IDX = SHIFTS.index("ER_Day3")
    for d in range(D):
        model.Add(sum(x[(d, D2_IDX, i)] for i in range(N)) <= (1 if DAY[d]["allow_d2"] else 0))
        model.Add(sum(x[(d, D3_IDX, i)] for i in range(N)) <= (1 if DAY[d]["allow_d3"] else 0))
    for d in range(D):
        model.Add(sum(x[(d, ICU_IDX, i)] for i in range(N)) <= (1 if DAY[d]["allow_icu"] else 0))

    if allow_weekend_icu:
        weekend_days = [d for d, day in enumerate(all_days) if day.weekday() >= 5]
        model.Add(sum(x[(d, ICU_IDX, i)] for d in weekend_days for i in range(N)) <= int(max_weekend_icu_total))
        for i in range(N):
            model.Add(sum(x[(d, ICU_IDX, i)] for d in weekend_days) <= int(max_weekend_icu_per_person))

    pins_df = st.session_state.get("pins", pd.DataFrame(columns=["date", "name", "shift"]))
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

    prefs_eff = st.session_state.prefs.copy()
    prefs_eff["kind"] = prefs_eff["kind"].astype(str).str.strip().str.lower()
    prefs_eff["priority"] = prefs_eff["priority"].astype(str).str.strip().str.upper()

    # vacation をリクエストした (d,i) のみ年休可
    allow_vac = set()
    for _, r in prefs_eff.iterrows():
        if r["date"] in all_days and r["name"] in name_to_idx:
            if str(r["kind"]).strip().lower() == "vacation":
                d = all_days.index(r["date"])
                i = name_to_idx[r["name"]]
                allow_vac.add((d, i))

    pref_soft = []
    A_star = set()
    A_off = defaultdict(list)

    for rid, row in prefs_eff.reset_index(drop=True).iterrows():
        if row["date"] not in all_days or row["name"] not in name_to_idx:
            continue
        d = all_days.index(row["date"])
        i = name_to_idx[row["name"]]
        kind = row["kind"]
        pr = row["priority"]

        if pr == "A" and kind in ("day", "icu"):
            pr = "B"

        if pr == "A":
            if kind == "off":
                model.Add(sum(x[(d, s, i)] for s in range(len(SHIFTS))) == 0)
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
            elif kind == "vacation":
                model.Add(x[(d, VAC_IDX, i)] == 1)
                A_star.add((d, "VAC", row["name"]))
            
            else:
                pref_soft.append((rid, d, i, kind, "B"))
        else:
            if rid in disabled_pref_ids:
                continue
            pref_soft.append((rid, d, i, kind, pr))

    hol = []
    Hd = [idx for idx, day in enumerate(all_days) if (day.weekday() >= 5 or day in holidays)]
    for i in range(N):
        hi = model.NewIntVar(0, 5 * D, f"hol_i{i}")
        model.Add(hi == sum(x[(d, s, i)] for d in Hd for s in range(len(SHIFTS))))
        hol.append(hi)

    for a in J1_idx:
        for b in J1_idx:
            if a >= b:
                continue
            diff = model.NewIntVar(-5 * D, 5 * D, f"j1diff_{a}_{b}")
            model.Add(diff == hol[a] - hol[b])
            model.Add(diff <= fair_slack)
            model.Add(-diff <= fair_slack)

    if len(J1_idx) > 0 and len(J2_idx) > 0:
        j1max = model.NewIntVar(0, 5 * D, "j1max_hol")
        for a in J1_idx:
            model.Add(j1max >= hol[a])
        for j in J2_idx:
            model.Add(hol[j] <= j1max)

    E_IDX = SHIFTS.index("ER_Early")
    L_IDX = SHIFTS.index("ER_Late")
    D1_IDX = SHIFTS.index("ER_Day1")
    D2X_IDX = SHIFTS.index("ER_Day2")
    early_cnt = []
    late_cnt = []
    day12_cnt = []
    for i in range(N):
        ei = model.NewIntVar(0, D, f"early_i{i}")
        model.Add(ei == sum(x[(d, E_IDX, i)] for d in range(D)))
        early_cnt.append(ei)
        li = model.NewIntVar(0, D, f"late_i{i}")
        model.Add(li == sum(x[(d, L_IDX, i)] for d in range(D)))
        late_cnt.append(li)
        di = model.NewIntVar(0, 2 * D, f"day12_i{i}")
        model.Add(di == sum(x[(d, D1_IDX, i)] + x[(d, D2X_IDX, i)] for d in range(D)))
        day12_cnt.append(di)
    for a in J1_idx:
        for b in J1_idx:
            if a >= b:
                continue
            for arr in (early_cnt, late_cnt, day12_cnt):
                df = model.NewIntVar(-2 * D, 2 * D, "tmp")
                model.Add(df == arr[a] - arr[b])
                model.Add(df <= 2)
                model.Add(-df <= 2)

    terms = []
    for rid, d, i, kind, pr in pref_soft:
        w = weight_pref_B if pr == "B" else weight_pref_C
        if w <= 0:
            continue
        assigned_any = model.NewBoolVar(f"assign_any_d{d}_i{i}")
        model.Add(assigned_any == sum(x[(d, s, i)] for s in range(len(SHIFTS))))
        if kind == "off":
            terms.append(int(100 * w) * assigned_any)
        elif kind == "early" and DAY[d]["req"]["ER_Early"] == 1:
            correct = x[(d, SHIFTS.index("ER_Early"), i)]
            miss = model.NewBoolVar(f"pref_early_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "late" and DAY[d]["req"]["ER_Late"] == 1:
            correct = x[(d, SHIFTS.index("ER_Late"), i)]
            miss = model.NewBoolVar(f"pref_late_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "day1" and DAY[d]["req"]["ER_Day1"] == 1:
            correct = x[(d, SHIFTS.index("ER_Day1"), i)]
            miss = model.NewBoolVar(f"pref_day1_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "day2" and DAY[d]["allow_d2"]:
            correct = x[(d, SHIFTS.index("ER_Day2"), i)]
            miss = model.NewBoolVar(f"pref_day2_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "day":
            day1_ok = (DAY[d]["req"]["ER_Day1"] == 1)
            day2_ok = DAY[d]["allow_d2"]
            if day1_ok or day2_ok:
                cands = []
                if day1_ok:
                    cands.append(x[(d, SHIFTS.index("ER_Day1"), i)])
                if day2_ok:
                    cands.append(x[(d, SHIFTS.index("ER_Day2"), i)])
                correct = model.NewBoolVar(f"pref_day_any_ok_d{d}_i{i}")
                model.AddMaxEquality(correct, cands)
                miss = model.NewBoolVar(f"pref_day_miss_d{d}_i{i}")
                model.Add(miss + correct == 1)
                terms.append(int(100 * w) * miss)
        elif kind == "icu" and (i in J2_idx) and DAY[d]["allow_icu"]:
            correct = x[(d, SHIFTS.index("ICU"), i)]
            miss = model.NewBoolVar(f"pref_icu_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "vacation":
            correct = x[(d, VAC_IDX, i)]
            miss = model.NewBoolVar(f"pref_vac_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)    

    if enable_fatigue and weight_fatigue > 0:
        L_IDX = SHIFTS.index("ER_Late")
        E_IDX = SHIFTS.index("ER_Early")
        for i in range(N):
            for d in range(D - 1):
                f = model.NewBoolVar(f"fatigue_d{d}_i{i}")
                model.Add(f >= x[(d, L_IDX, i)] + x[(d + 1, E_IDX, i)] - 1)
                model.Add(f <= x[(d, L_IDX, i)])
                model.Add(f <= x[(d + 1, E_IDX, i)])
                terms.append(int(100 * weight_fatigue) * f)

    for d, day in enumerate(all_days):
        if DAY[d]["allow_d2"]:
            placed = model.NewBoolVar(f"d2_placed_{d}")
            model.Add(placed == sum(x[(d, SHIFTS.index("ER_Day2"), i)] for i in range(N)))
            w = weight_day2_weekday + (weight_day2_wed_bonus if day.weekday() == 2 else 0.0)
            if weaken_day2_bonus:
                w = max(0.0, w * 0.5)
            if w > 0:
                terms.append(int(100 * w) * (1 - placed))
        if DAY[d]["allow_d3"]:
            placed3 = model.NewBoolVar(f"d3_placed_{d}")
            model.Add(placed3 == sum(x[(d, SHIFTS.index("ER_Day3"), i)] for i in range(N)))
            w3 = weight_day3_weekday + (weight_day3_wed_bonus if day.weekday() == 2 else 0.0)
            if weaken_day2_bonus:
                w3 = max(0.0, w3 * 0.5)
            if w3 > 0:
                terms.append(int(100 * w3) * (1 - placed3))

    if weight_icu_ratio > 0 and len(J2_idx) > 0:
        scale = 100
        for j in J2_idx:
            ICU_j = model.NewIntVar(0, 5 * D, f"ICU_j{j}")
            model.Add(ICU_j == sum(x[(d, SHIFTS.index("ICU"), j)] for d in range(D)))
            target_scaled = model.NewIntVar(0, scale * 5 * D, f"icu_target_j{j}")
            desired = float(staff_df.iloc[j]["desired_icu_ratio"])
            model.Add(target_scaled == int(round(desired * scale)) * int(per_person_total))
            ICU_scaled = model.NewIntVar(0, scale * 5 * D, f"icu_scaled_j{j}")
            model.Add(ICU_scaled == scale * ICU_j)
            diff = model.NewIntVar(-scale * 5 * D, scale * 5 * D, f"icu_diff_j{j}")
            model.Add(diff == ICU_scaled - target_scaled)
            dev = model.NewIntVar(0, scale * 5 * D, f"icu_dev_j{j}")
            model.AddAbsEquality(dev, diff)
            terms.append(int(weight_icu_ratio) * dev)

    model.Minimize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20.0
    solver.parameters.num_search_workers = 1 if (fix_repro and repro_fix) else 8
    if fix_repro and repro_fix:
        try:
            solver.parameters.random_seed = int(seed_val)
        except Exception:
            pass

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
# infeasible 時のブロッキングA特定
# -------------------------
def find_blocking_A_once(fair_slack_base: int, weaken_base: bool):
    """Aレコードを1件ずつ無効化して解けるか検査。戻り値: list[(rid, row_dict)]"""
    prefs_base = st.session_state.prefs.reset_index(drop=True)
    A_only = prefs_base[prefs_base["priority"] == "A"].copy()
    blockers = []
    for rid, row in A_only.iterrows():
        tmp = prefs_base.copy()
        tmp.loc[rid, "priority"] = "Z"
        bak = st.session_state.prefs
        st.session_state.prefs = tmp
        s, sol, a = build_and_solve(
            fair_slack=fair_slack_base, disabled_pref_ids=set(), weaken_day2_bonus=weaken_base
        )
        st.session_state.prefs = bak
        if s in ("OPTIMAL", "FEASIBLE"):
            blockers.append((rid, row.to_dict()))
    return blockers

# -------------------------
# 実行ボタン & 自動リトライ
# -------------------------
run = st.button("🚀 生成する（最適化）")
relax_log = []

if run:
    # DAYマップ（検証用）
    DAY_tmp = {
        d: {"req": {"ER_Early": 1, "ER_Day1": 1, "ER_Late": 1}, "allow_d2": False, "allow_d3": False, "allow_icu": False, "drop": None}
        for d in range(D)
    }
    DAY2_FORBID_LOCAL = set([d for d in all_days if d.weekday() >= 5]) | set(holidays) | set(closed_days)
    ICU_ALLOWED_DAYS_LOCAL = set(all_days) if allow_weekend_icu else set([d for d in all_days if d.weekday() < 5])
    for d, day in enumerate(all_days):
        drop = special_map.get(day)
        if drop in ER_BASE:
            DAY_tmp[d]["req"][drop] = 0
            DAY_tmp[d]["drop"] = drop
        if day.weekday() < 5 and day not in DAY2_FORBID_LOCAL:
            DAY_tmp[d]["allow_d2"] = True
            DAY_tmp[d]["allow_d3"] = bool(allow_day3)
        if day in ICU_ALLOWED_DAYS_LOCAL:
            DAY_tmp[d]["allow_icu"] = True

    issues = validate_A_requests(st.session_state.prefs.copy(), DAY_tmp)
    if issues:
        st.error("A希望に物理的に不可能な指定が含まれています。以下を修正してください：\n- " + "\n- ".join(issues))
        st.stop()

    disabled_pref_ids = set()
    disabled_log_rows = []

    status, solver, art = build_and_solve(
        fair_slack=(1 if strict_mode else 2),
        disabled_pref_ids=disabled_pref_ids,
        weaken_day2_bonus=(not strict_mode),
    )

    if status in ("INFEASIBLE", "UNKNOWN"):
        relax_log.append(("fairness", "J1休日ばらつきを ±1→±2 に緩和"))
        status, solver, art = build_and_solve(
            fair_slack=2, disabled_pref_ids=disabled_pref_ids, weaken_day2_bonus=False
        )

    if status in ("INFEASIBLE", "UNKNOWN"):
        relax_log.append(("bonus", "Day2/Day3の平日・水曜ボーナスを一段弱め"))
        status, solver, art = build_and_solve(
            fair_slack=2, disabled_pref_ids=disabled_pref_ids, weaken_day2_bonus=True
        )

    def iteratively_disable(level: str, disabled_ids: set):
        log_rows = []
        prefs_all = st.session_state.prefs.reset_index()  # rid=index
        target = prefs_all[prefs_all["priority"] == level]
        for rid, row in target.iterrows():
            if rid in disabled_ids:
                continue
            disabled_ids2 = set(disabled_ids)
            disabled_ids2.add(rid)
            s, sol, a = build_and_solve(
                fair_slack=2, disabled_pref_ids=disabled_ids2, weaken_day2_bonus=True
            )
            if s in ("OPTIMAL", "FEASIBLE"):
                log_rows.append(row.to_dict())
                return s, sol, a, disabled_ids2, log_rows
        return None, None, None, disabled_ids, log_rows

    while status in ("INFEASIBLE", "UNKNOWN"):
        s, sol, a, disabled_pref_ids, logs = iteratively_disable("C", disabled_pref_ids)
        if s is None:
            break
        disabled_log_rows.extend(logs)
        status, solver, art = s, sol, a

    while status in ("INFEASIBLE", "UNKNOWN"):
        s, sol, a, disabled_pref_ids, logs = iteratively_disable("B", disabled_pref_ids)
        if s is None:
            break
        disabled_log_rows.extend(logs)
        status, solver, art = s, sol, a

    if status not in ("OPTIMAL", "FEASIBLE"):
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
    # 出力テーブル
    # -------------------------
    A_star = art["A_star"]
    A_off = art["A_off"]
    x = art["x"]

    prefs_now = st.session_state.prefs.copy()
    prefs_now["kind"] = prefs_now["kind"].astype(str).str.lower()
    prefs_now["priority"] = prefs_now["priority"].astype(str).str.upper()
    B_off_want = defaultdict(set)
    C_off_want = defaultdict(set)
    for _, r in prefs_now.iterrows():
        if r.get("date") in all_days and r.get("kind") == "off" and r.get("name") in name_to_idx:
            d = all_days.index(r["date"])
            if r["priority"] == "B":
                B_off_want[d].add(r["name"])
            elif r["priority"] == "C":
                C_off_want[d].add(r["name"])

    assigned_set_by_day = [set() for _ in range(D)]
    for d in range(D):
        for sidx in range(len(SHIFTS)):
            for i in range(N):
                if solver.Value(x[(d, sidx, i)]) == 1:
                    assigned_set_by_day[d].add(names[i])

    B_off_granted = {d: sorted([nm for nm in B_off_want.get(d, set()) if nm not in assigned_set_by_day[d]]) for d in range(D)}
    C_off_granted = {d: sorted([nm for nm in C_off_want.get(d, set()) if nm not in assigned_set_by_day[d]]) for d in range(D)}

    rows = []
    for d, day in enumerate(all_days):
        row = {"日付": str(day), "曜日": WEEKDAY_JA[day.weekday()]}
        for sname in SHIFTS:
            sidx = SHIFTS.index(sname)
            assigned = [names[i] for i in range(N) if solver.Value(x[(d, sidx, i)]) == 1]
            starset = set(nm for (dd, ss, nm) in A_star if (dd == d and ss == sname))
            labeled = [(nm + "★") if (nm in starset) else nm for nm in assigned]
            row[SHIFT_LABEL[sname]] = ",".join(labeled)
        aoff_names = A_off.get(d, [])
        row["A休"] = ",".join(sorted(aoff_names)) if aoff_names else ""
        row["B休"] = ",".join(B_off_granted.get(d, [])) if B_off_granted.get(d) else ""
        row["C休"] = ",".join(C_off_granted.get(d, [])) if C_off_granted.get(d) else ""
        rows.append(row)
    out_df = pd.DataFrame(rows)

    viol = []
    for d, a_names in A_off.items():
        for nm in a_names:
            assigned_any = any(
                isinstance(out_df.loc[d, lbl], str)
                and nm in [x.strip("★") for x in out_df.loc[d, lbl].split(",") if x]
                for lbl in ["早番", "日勤1", "日勤2", "日勤3", "遅番", "ICU", "年休"]
            )
            if assigned_any:
                viol.append((all_days[d], nm))
    if viol:
        st.error(
            "A-休みの違反が検出されました（設定の矛盾かバグの可能性）。\n"
            + "\n".join([f"- {d} {nm}" for d, nm in viol])
        )

    st.subheader("📋 生成スケジュール（★=A希望反映）")
    st.dataframe(out_df, use_container_width=True, hide_index=True)

    # 個人別集計
    person_stats = []
    hol_days_idx = [idx for idx, day in enumerate(all_days) if (day.weekday() >= 5 or day in holidays)]
    for i, nm in enumerate(names):
        def _has(lbl, di):
            if not isinstance(out_df.loc[di, lbl], str):
                return False
            return nm in [x.strip("★") for x in out_df.loc[di, lbl].split(",") if x]

        cnt = {lbl: sum(1 for d in range(D) if _has(lbl, d)) for lbl in ["早番", "日勤1", "日勤2", "日勤3", "遅番", "ICU", "年休"]}
        total = sum(cnt.values())
        hol_cnt = sum(sum(1 for lbl in ["早番", "日勤1", "日勤2", "日勤3", "遅番", "ICU"] if _has(lbl, d)) for d in hol_days_idx)
        fatigue = 0
        for d in range(D - 1):
            late = _has("遅番", d)
            early_next = _has("早番", d + 1)
            if late and early_next:
                fatigue += 1
        person_stats.append({"name": nm, "grade": staff_df.iloc[i]["grade"], **cnt, "Total": total, "Holiday": hol_cnt, "Fatigue": fatigue})
    stat_df = pd.DataFrame(person_stats)

    st.subheader("👥 個人別集計（Holiday=土日祝、Fatigue=遅番→翌早番）")
    st.dataframe(stat_df, use_container_width=True, hide_index=True)

    # 未充足の希望（B/C）
    unmet = []
    for _, row in st.session_state.prefs.reset_index().iterrows():
        if row["priority"] not in ("B", "C"):
            continue
        if row["date"] not in all_days or row["name"] not in name_to_idx:
            continue
        d = all_days.index(row["date"])
        nm = row["name"]
        kind = str(row["kind"]).lower()

        def _in(lbl):
            return isinstance(out_df.loc[d, lbl], str) and nm in [x.strip("★") for x in out_df.loc[d, lbl].split(",") if x]

        got = False
        if kind == "off":
            got = not any(_in(lbl) for lbl in ["早番", "日勤1", "日勤2", "日勤3", "遅番", "ICU"])
        elif kind == "early":
            got = _in("早番")
        elif kind == "late":
            got = _in("遅番")
        elif kind == "day":
            got = _in("日勤1") or _in("日勤2")
        elif kind == "icu":
            got = _in("ICU")
        elif kind == "day1":
            got = _in("日勤1")
        elif kind == "day2":
            got = _in("日勤2")
        elif kind == "vacation":
            got = _in("年休")    
        if not got:
            unmet.append((row["priority"], row["date"], nm, kind))

    auto_disabled_rows = []
    if len(disabled_pref_ids) > 0:
        base = st.session_state.prefs.reset_index()
        hit = base[base["index"].isin(disabled_pref_ids)].copy()
        for _, r in hit.iterrows():
            auto_disabled_rows.append((r["priority"], r["date"], r["name"], str(r["kind"]).lower()))

    if unmet:
        st.subheader("🙇‍♂️ 未充足となった希望（B/C）")
        show = pd.DataFrame(unmet, columns=["priority", "date", "name", "kind"]).sort_values(["priority", "date", "name"])
        st.dataframe(show, use_container_width=True, hide_index=True)

    if auto_disabled_rows:
        st.subheader("⚠️ 自動で無効化した希望（B/C）")
        show2 = pd.DataFrame(auto_disabled_rows, columns=["priority", "date", "name", "kind"]).sort_values(["priority", "date", "name"])
        st.dataframe(show2, use_container_width=True, hide_index=True)

    # ダウンロード
    json_snapshot = make_snapshot(
        out_df=out_df, stat_df=stat_df, status=status, objective=solver.ObjectiveValue()
    )
    buf_json = io.StringIO()
    buf_json.write(json.dumps(json_snapshot, ensure_ascii=False, indent=2))
    buf_csv = io.StringIO()
    out_df.to_csv(buf_csv, index=False)

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "📥 スケジュールCSVをダウンロード（★/A休/B休/C休 付き）",
            data=buf_csv.getvalue(),
            file_name="schedule.csv",
            mime="text/csv",
        )
    with c2:
        st.download_button(
            "🧾 スナップショットJSONをダウンロード",
            data=buf_json.getvalue(),
            file_name="run_snapshot.json",
            mime="application/json",
        )


