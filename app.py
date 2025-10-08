# =========================
# app.py — Part 1 / 4
# （このファイルは Part 1→4 を順に連結してください）
# =========================

# ---------- Imports ----------
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


# ---------- ページ設定 / 定数 ----------
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
import sys, platform, os  # noqa: E402

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

# ---------- 年/月・日付ユーティリティ ----------
this_year = dt.date.today().year
default_year = this_year
default_month = dt.date.today().month


# ---------- スナップショット関連（先に定義） ----------
def _serialize_for_json(obj):
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, set):
        return list(obj)
    return obj

def _current_settings_as_dict():
    """現UI状態を辞書化（後でUI構築後に上書きされる値は globals() / st.session_state から読む）"""
    ss = st.session_state

    # 年/月・カレンダー関連
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

    # ウェイト
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
        "memo": ss.get("memo_text", ""),  # ← 作成者メモを保存

    }

def _apply_snapshot_dict(snap: dict):
    """辞書→UIのグローバル/セッションに反映（存在チェックしつつ上書き）"""
    # 期間
    globals()["year"] = int(snap["period"]["year"])
    globals()["month"] = int(snap["period"]["month"])

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

    # pins
    pins_df = pd.DataFrame(snap.get("pins", []))
    if not pins_df.empty and set(pins_df.columns) >= {"date", "name", "shift"}:
        try:
            pins_df["date"] = pd.to_datetime(pins_df["date"]).dt.date
        except Exception:
            pass
        ss.pins = pins_df[["date", "name", "shift"]].copy()
    ss.memo_text = snap.get("memo", ss.get("memo_text", ""))

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
    out_df=None, stat_df=None, status="UNKNOWN", objective=None,
    fair_star=None, fair_slack_val=None,       
    memo_text=None
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

    # ★ 公平性（スターと実スラック）も保存
    if fair_star is None:
        fair_star = int(st.session_state.get("star_fairness", 2))
    if fair_slack_val is None:
        fair_slack_val = int(STAR_TO_FAIR_SLACK.get(fair_star, 2))

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
            "fair_star": int(fair_star),          # ← 追加
            "fair_slack": int(fair_slack_val),    # ← 追加
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
        "memo": (memo_text if memo_text is not None else ss.get("memo_text", "")),  
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

        st.session_state.memo_text = js.get("memo", st.session_state.get("memo_text", ""))  

        st.success("スナップショットを読み込みました。年/月・祝日等を反映するため再描画します。")
        st.rerun()
    except Exception as e:
        st.error(f"スナップショット適用に失敗しました: {e}")


# ---------- セッション初期化 ----------
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


# ---------- サイドバー：基本入力（翌月をデフォルト） ----------
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
    st.session_state.setdefault("year_input", next_year)

if "_restore_month" in st.session_state:
    st.session_state["month_input"] = int(st.session_state.pop("_restore_month"))
else:
    st.session_state.setdefault("month_input", next_month)

# 2) ウィジェット作成（value/indexは渡さず key で制御）
year = st.sidebar.number_input(
    "作成年",
    min_value=this_year - 2,
    max_value=this_year + 2,
    step=1,
    key="year_input",
)
month = st.sidebar.selectbox(
    "作成月",
    list(range(1, 13)),
    key="month_input",
)

# 3) 日付リスト等
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

# --- 祝日：自動取得ヘルパー & UI ---
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

# ---- UI（祝日）----
holbox = st.sidebar.container()
with holbox:
    head_l, head_r = st.columns([1, 0.22])
    with head_l:
        st.markdown("#### 祝日（当月）")
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

    holidays = st.multiselect(
        "",
        options=all_days,
        format_func=lambda d: f"{d}({WEEKDAY_JA[d.weekday()]})",
        key="holidays_ms",
        label_visibility="collapsed",
    )

# 実体
holidays = st.session_state["holidays_ms"]

# === 病院休診日 ===
_restore_closed = st.session_state.pop("_restore_closed_days", None)
if "closed_ms" not in st.session_state:
    st.session_state["closed_ms"] = [d for d in (_restore_closed or []) if d in all_days]
else:
    st.session_state["closed_ms"] = [d for d in st.session_state["closed_ms"] if d in all_days]

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

    closed_days = st.multiselect(
        "",
        options=all_days,
        format_func=lambda d: f"{d}({WEEKDAY_JA[d.weekday()]})",
        key="closed_ms",
        label_visibility="collapsed",
    )

closed_days = st.session_state["closed_ms"]

st.sidebar.divider()
per_person_total = st.sidebar.number_input(
    "👥 総勤務回数", min_value=0, value=22, step=1
)
st.sidebar.caption("病院の年間休日カレンダーに記載の所定勤務日数に合わせてください")

st.sidebar.header("🗓️ 月ごとの設定")
max_consecutive = st.sidebar.slider("最大連勤日数", 3, 7, 5)

allow_day3 = st.sidebar.checkbox(
    "ER日勤3を許可", value=False,
    help="ON: チェックするとローテーターが多い時に日勤3が入れられるようになります（平日のみ）"
)
allow_weekend_icu = st.sidebar.checkbox(
    "週末ICUを許可", value=False,
    help="ON: チェックすると、土日祝にJ2のICUローテが入るようになります"
)
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


# ===== 星型UIコントロール定義（先に定義 / 1回だけ） =====
def star_control(label, key, disabled=False, help=None, default=2):
    # ★1〜3 の三段階に固定（★0は使わない）
    options = [1, 2, 3]
    fmt = lambda v: "★"*int(v) + "☆"*(3-int(v))

    # 初回のみ初期値セット（以後は触らない）
    if key not in st.session_state or st.session_state.get(key) is None:
        d = int(default)
        d = max(1, min(3, d))
        st.session_state[key] = d

    # value= は渡さない（session_stateの値をそのまま使わせる）
    if hasattr(st, "segmented_control"):
        val = st.segmented_control(
            label=label, options=options, format_func=fmt,
            key=key, disabled=disabled, help=help
        )
    else:
        val = st.select_slider(
            label, options=options, format_func=fmt,
            key=key, disabled=disabled, help=help
        )

    return int(val if val is not None else st.session_state[key])


# ===== サイドバー：詳細ウェイト設定 =====
with st.sidebar.expander("⚙️ 詳細ウェイト設定", expanded=False):
    st.markdown(
        """
        <div style="font-size:0.92em; line-height:1.5; padding:10px; border:1px solid #ddd; border-radius:8px;">
          <b>このセクションは「目的関数」の重みづけ」です。</b><br>
          各項目は ★1〜3 で強さを指定します（大きいほど優先）。<br>
          ※ ハード制約に反するものは、どれだけ重みを上げても実現されません。
        </div>
        """,
        unsafe_allow_html=True
    )

    # --- 余白を挿入（見た目の間隔を空ける） ---
    st.markdown("<br>", unsafe_allow_html=True)

    # --- J1の休日勤務の公平性（ばらつき抑制） ---
    s_fairness = star_control(
        "休日勤務の公平性を優先（J1）", key="star_fairness",
        help="J1の間での土日祝の勤務数の偏りを減らします。星1で±3, 星2で±2, 星3で±1までを許容とします。",
        default=2
    )

    # --- Day2 / Day3（平日優先＋水曜ボーナス） ---
    s_day2_weekday = star_control(
        "日勤2配置の優先度（平日）", key="star_day2_weekday",
        help="平日に 日勤2 を“置ける日”で、置くことをどれだけ優先するか。",
        default=2
    )
    s_day2_wed = star_control(
        "水曜ボーナス（日勤2）", key="star_day2_wed",
        help="水曜日だけ 日勤2 を特に優先する加点。",
        default=2
    )
    s_day3_weekday = star_control(
        "日勤3配置の優先度（平日）", key="star_day3_weekday",
        disabled=not allow_day3,
        help="平日に 日勤3 を置く優先度（許可している場合のみ有効）。",
        default=2
    )
    s_day3_wed = star_control(
        "水曜ボーナス（日勤3）", key="star_day3_wed",
        disabled=not allow_day3,
        help="水曜日だけ 日勤3 を特に優先する加点。",
        default=2
    )

    # --- ICU希望比率 / B・C希望ペナルティ ---
    s_icu_ratio = star_control(
        "J2のICU希望比率の遵守（強さ）", key="star_icu_ratio",
        help="J2の設定したICU希望比率に近づける重み。",
        default=3
    )
    s_pref_b = star_control(
        "希望B未充足ペナルティ（強さ）", key="star_pref_b",
        help="B希望が叶わなかったときのペナルティ。",
        default=3  # ★デフォルト3
    )
    s_pref_c = star_control(
        "希望C未充足ペナルティ（強さ）", key="star_pref_c",
        help="C希望が叶わなかったときのペナルティ。",
        default=2
    )

    # --- 疲労（遅番→翌早番の回避） ---
    enable_fatigue = st.checkbox("疲労ペナルティを有効にする", value=True)
    if enable_fatigue:
        s_fatigue = star_control(
            "疲労ペナルティの強さ", key="star_fatigue",
            help="大きいほど『遅番の翌日に早番』を強く避けます。",
            default=2  # ★デフォルト2
        )
    else:
        s_fatigue = 2  # 有効でない場合も仮に★2扱い（重みは0で後処理）

# ★→実数ウェイトの変換（1〜3のみ）
STAR_TO_WEIGHT_DAY_WEEKDAY = {1: 2.0, 2: 6.0, 3: 12.0}
STAR_TO_WEIGHT_WED_BONUS   = {1: 4.0, 2: 8.0, 3: 12.0}
STAR_TO_WEIGHT_ICU_RATIO   = {1: 2.5, 2: 6.0, 3: 10.0}
STAR_TO_WEIGHT_PREF_B      = {1: 10,  2: 25,  3: 50}
STAR_TO_WEIGHT_PREF_C      = {1: 5,   2: 12,  3: 25}
STAR_TO_WEIGHT_FATIGUE     = {1: 6.0, 2: 12.0, 3: 24.0}
STAR_TO_FAIR_SLACK         = {1: 3, 2: 2, 3: 1}

# 従来の変数名に変換
weight_day2_weekday   = STAR_TO_WEIGHT_DAY_WEEKDAY[s_day2_weekday]
weight_day2_wed_bonus = STAR_TO_WEIGHT_WED_BONUS[s_day2_wed]
weight_day3_weekday   = STAR_TO_WEIGHT_DAY_WEEKDAY[s_day3_weekday]
weight_day3_wed_bonus = STAR_TO_WEIGHT_WED_BONUS[s_day3_wed]
weight_icu_ratio      = STAR_TO_WEIGHT_ICU_RATIO[s_icu_ratio]
weight_pref_B         = STAR_TO_WEIGHT_PREF_B[s_pref_b]
weight_pref_C         = STAR_TO_WEIGHT_PREF_C[s_pref_c]
weight_fatigue        = STAR_TO_WEIGHT_FATIGUE[s_fatigue] if enable_fatigue else 0.0

# ===== ここで Part 1 / 4 終了 =====
# （続きは Part 2 へ）  # =========================
# app.py — Part 2 / 4
# =========================

# ===== ディスク保存 / 復元（前回状態の読み書き） =====

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

# --- サイドバーUI（ディスク保存/復元） ---
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

if c_a.button("💾 保存", key="btn_save_to_disk", use_container_width=True):
    ok, err = save_last_snapshot_to_disk()
    if ok:
        st.sidebar.success("保存しました。")
    else:
        st.sidebar.error(f"保存に失敗: {err}")

if c_b.button("📥 復元", key="btn_restore_from_disk", use_container_width=True):
    snap = load_last_snapshot_from_disk()
    if snap:
        _apply_snapshot_dict(snap)    # ここでUIに反映
        st.sidebar.success("前回保存した設定を反映しました。再描画します。")
        st.rerun()
    else:
        st.sidebar.info("前回ファイルがありません。")

# -------------------------
# 📂 スナップショットJSON（アップロードで即反映）
# -------------------------
st.sidebar.subheader("📂 スナップショットJSONをアップロードする")
st.sidebar.caption(
    "過去にダウンロードしたスナップショットJSONを読み込むと、"
    "画面状態を一括復元できます。"
)
up_snap = st.sidebar.file_uploader(
    "JSONを選択して『UIに反映』",
    type=["json"],
    key="sidebar_snapshot_uploader",
    label_visibility="collapsed"
)
apply_up_btn = st.sidebar.button("🧷 反映する（再描画）", use_container_width=True, key="sidebar_apply_snapshot_btn")

if up_snap is not None and apply_up_btn:
    import json as _json
    try:
        snap_dict = _json.load(up_snap)
        apply_snapshot(snap_dict)   # 既存の関数をそのまま利用（UIへ反映 & rerun）
    except Exception as e:
        st.sidebar.error(f"JSONの読み込みに失敗しました: {e}")

# -------------------------
# 休日集合（後続の表示や検証で利用）
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

# 実体のスタッフDF
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
# 希望（A/B/C）エディタ
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
        df = df[df["kind"].isin(["off", "early", "late", "day", "day1", "day2", "icu", "vacation"])]
        df = df[df["name"].isin(names)]
        df = df.drop_duplicates(subset=["date", "name", "kind", "priority"], keep="last").reset_index(drop=True)

        st.session_state.prefs_backup = st.session_state.prefs.copy(deep=True)
        st.session_state.prefs = df
        st.session_state.prefs_draft = df.copy()
        st.session_state.prefs_editor_ver += 1
        st.success("希望を保存しました。")
        st.rerun()

# -------------------------
# プリアサイン（固定割当）
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

        # J1 の ICU プリアサインは無効化（警告表示）
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

# ===== ここで Part 2 / 4 終了 =====
# （続きは Part 3 へ）

# =========================
# app.py — Part 3 / 4
# =========================

# -------------------------
# 可否カレンダー（Day2/Day3/ICU）
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
# 前処理バリデーション（ボリューム等）
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
def validate_A_requests(prefs_df: pd.DataFrame, DAY_template: dict) -> list[str]:
    """A希望の物理不可能を早期チェック"""
    issues = []
    a_off = set()

    # A-休みの集合
    for _, r in prefs_df[(prefs_df["priority"] == "A") & (prefs_df["kind"].str.lower() == "off")].iterrows():
        if r["date"] in all_days and r["name"] in name_to_idx:
            a_off.add((r["date"], r["name"]))

    # A-休みと同日の他A
    for _, r in prefs_df[prefs_df["priority"] == "A"].iterrows():
        d, nm, k = r["date"], r["name"], str(r["kind"]).lower()
        if d not in all_days or nm not in name_to_idx:
            continue
        if (d, nm) in a_off and k != "off":
            issues.append(f"{d} {nm}: A-休み と A-{k} は同日に共存できません")
        if (d, nm) in a_off and k == "vacation":
            issues.append(f"{d} {nm}: A-休み と A-vacation は同日に共存できません")

    # J1のA-ICUは不可
    j1_names = set(staff_df.loc[staff_df["grade"] == "J1", "name"].tolist())
    for _, r in prefs_df[(prefs_df["priority"] == "A") & (prefs_df["kind"].str.lower() == "icu")].iterrows():
        if r["name"] in j1_names:
            issues.append(f"{r['date']} {r['name']}: J1 に A-ICU は割当不可能です")

    # 特例や可否
    for _, r in prefs_df[prefs_df["priority"] == "A"].iterrows():
        d, nm, k = r["date"], r["name"], str(r["kind"]).lower()
        if d not in all_days or nm not in name_to_idx:
            continue
        di = all_days.index(d)
        DAY = DAY_template
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

    # 同一スロットへのA過多
    a_counts = {}
    for _, r in prefs_df[prefs_df["priority"] == "A"].iterrows():
        d, k = r["date"], str(r["kind"]).lower()
        if d in all_days:
            di = all_days.index(d)
            key = None
            if k == "early" and DAY_template[di]["req"]["ER_Early"] == 1:
                key = ("ER_Early", di)
            if k == "late" and DAY_template[di]["req"]["ER_Late"] == 1:
                key = ("ER_Late", di)
            if k == "day1" and DAY_template[di]["req"]["ER_Day1"] == 1:
                key = ("ER_Day1", di)
            if k == "day2" and DAY_template[di]["allow_d2"]:
                key = ("ER_Day2", di)
            if k == "icu" and DAY_template[di]["allow_icu"]:
                key = ("ICU", di)
            if key:
                a_counts.setdefault(key, 0)
                a_counts[key] += 1

    for (shift_name, di), cnt in a_counts.items():
        if cnt > 1:
            issues.append(f"{all_days[di]} {shift_name}: A希望が{cnt}件あり、定員1を超えています")

    return issues

# -------------------------
# ソルバー本体
# -------------------------
def build_and_solve(
    fair_slack: int,
    disabled_pref_ids: set,
    weaken_day2_bonus: bool = False,
    repro_fix: bool = True,
):
    model = cp_model.CpModel()

    # 便利なインデックス（SHIFTS は Part 1 で定義済み）
    E_IDX   = SHIFTS.index("ER_Early")
    D1_IDX  = SHIFTS.index("ER_Day1")
    D2_IDX  = SHIFTS.index("ER_Day2")
    D3_IDX  = SHIFTS.index("ER_Day3")
    L_IDX   = SHIFTS.index("ER_Late")
    ICU_IDX = SHIFTS.index("ICU")
    VAC_IDX = SHIFTS.index("VAC")

    # 変数: x[d, s, i] ∈ {0,1}
    x = {
        (d, s, i): model.NewBoolVar(f"x_d{d}_s{s}_i{i}")
        for d in range(D) for s in range(len(SHIFTS)) for i in range(N)
    }

    # 1日1人1枠まで
    for d in range(D):
        for i in range(N):
            model.Add(sum(x[(d, s, i)] for s in range(len(SHIFTS))) <= 1)

    # J1 は ICU 不可
    for d in range(D):
        for i in [j for j in range(N) if staff_df.iloc[j]["grade"] == "J1"]:
            model.Add(x[(d, ICU_IDX, i)] == 0)

    # 最大連勤
    for i in range(N):
        y = [model.NewBoolVar(f"y_d{d}_i{i}") for d in range(D)]
        for d in range(D):
            model.Add(y[d] == sum(x[(d, s, i)] for s in range(len(SHIFTS))))
        window = max_consecutive + 1
        if D >= window:
            for start in range(0, D - window + 1):
                model.Add(sum(y[start + k] for k in range(window)) <= max_consecutive)

    # 個々の総勤務回数（= per_person_total）
    for i in range(N):
        ti = model.NewIntVar(0, 5 * D, f"total_i{i}")
        model.Add(ti == sum(x[(d, s, i)] for d in range(D) for s in range(len(SHIFTS))))
        model.Add(ti == int(per_person_total))

    # 日ごとの枠・可否（特例と休日設定を反映）
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

    # ER 基本枠（早/日1/遅）の充足
    for d in range(D):
        for base in ER_BASE:
            sidx = SHIFTS.index(base)
            model.Add(sum(x[(d, sidx, i)] for i in range(N)) == DAY[d]["req"][base])

    # D2/D3/ICU は可の日のみ 0/1
    for d in range(D):
        model.Add(sum(x[(d, D2_IDX, i)] for i in range(N)) <= (1 if DAY[d]["allow_d2"] else 0))
        model.Add(sum(x[(d, D3_IDX, i)] for i in range(N)) <= (1 if DAY[d]["allow_d3"] else 0))
        model.Add(sum(x[(d, ICU_IDX, i)] for i in range(N)) <= (1 if DAY[d]["allow_icu"] else 0))

    # Day1 が立っている日だけ Day2/Day3 を許可（連動制約）
    for d in range(D):
        total_d1 = sum(x[(d, D1_IDX, i)] for i in range(N))
        model.Add(sum(x[(d, D2_IDX, i)] for i in range(N)) <= total_d1)
        model.Add(sum(x[(d, D3_IDX, i)] for i in range(N)) <= total_d1)

    # 週末ICUの総量/個人上限
    if allow_weekend_icu:
        weekend_days = [d for d, day in enumerate(all_days) if day.weekday() >= 5]
        model.Add(sum(x[(d, ICU_IDX, i)] for d in weekend_days for i in range(N)) <= int(max_weekend_icu_total))
        for i in range(N):
            model.Add(sum(x[(d, ICU_IDX, i)] for d in weekend_days) <= int(max_weekend_icu_per_person))

    # プリアサイン（固定）
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

    # 希望（A/B/C）
    prefs_eff = st.session_state.prefs.copy()
    prefs_eff["kind"] = prefs_eff["kind"].astype(str).str.strip().str.lower()
    prefs_eff["priority"] = prefs_eff["priority"].astype(str).str.strip().str.upper()

    # --- Vacation（年休）を許可する (d,i) の集合 ---
    allow_vac = set()
    for _, row in prefs_eff.iterrows():
        try:
            kind = str(row["kind"]).lower().strip()
            pr   = str(row["priority"]).upper().strip()
            dte  = row["date"]
            nm   = row["name"]
        except Exception:
            continue
        if kind == "vacation" and pr in ("A", "B", "C"):
            if dte in all_days and nm in name_to_idx:
                d = all_days.index(dte)
                i = name_to_idx[nm]
                allow_vac.add((d, i))

    # 許可されていない (d,i) は VAC=0
    for d in range(D):
        for i in range(N):
            if (d, i) not in allow_vac:
                model.Add(x[(d, VAC_IDX, i)] == 0)

    # Aは基本的にハード制約化、B/Cは目的関数でペナルティ
    pref_soft = []        # (rid, d, i, kind, pr)  … B/C or 落としたAの代替
    A_star = set()        # (d, shift_name, name)
    A_off  = defaultdict(list)

    for rid, row in prefs_eff.reset_index(drop=True).iterrows():
        if row["date"] not in all_days or row["name"] not in name_to_idx:
            continue
        d = all_days.index(row["date"])
        i = name_to_idx[row["name"]]
        kind = row["kind"]
        pr = row["priority"]

        # day/icu の A は B に降格（UI側でもやっているが二重防御）
        if pr == "A" and kind in ("day", "icu"):
            pr = "B"

        if pr == "A":
            if kind == "off":
                model.Add(sum(x[(d, s, i)] for s in range(len(SHIFTS))) == 0)
                A_off[d].append(row["name"])
            elif kind == "early":
                if DAY[d]["req"]["ER_Early"] == 1:
                    model.Add(x[(d, E_IDX, i)] == 1)
                    A_star.add((d, "ER_Early", row["name"]))
                else:
                    pref_soft.append((rid, d, i, "early", "B"))
            elif kind == "late":
                if DAY[d]["req"]["ER_Late"] == 1:
                    model.Add(x[(d, L_IDX, i)] == 1)
                    A_star.add((d, "ER_Late", row["name"]))
                else:
                    pref_soft.append((rid, d, i, "late", "B"))
            elif kind == "day1":
                if DAY[d]["req"]["ER_Day1"] == 1:
                    model.Add(x[(d, D1_IDX, i)] == 1)
                    A_star.add((d, "ER_Day1", row["name"]))
                else:
                    pref_soft.append((rid, d, i, "day1", "B"))
            elif kind == "day2":
                if DAY[d]["allow_d2"]:
                    model.Add(x[(d, D2_IDX, i)] == 1)
                    A_star.add((d, "ER_Day2", row["name"]))
                else:
                    pref_soft.append((rid, d, i, "day2", "B"))
            elif kind == "vacation":
                # 事前に allow_vac に入っているため、ここは単純に 1 固定でOK
                model.Add(x[(d, VAC_IDX, i)] == 1)
                A_star.add((d, "VAC", row["name"]))
            else:
                pref_soft.append((rid, d, i, kind, "B"))
        else:
            if rid in disabled_pref_ids:
                continue
            pref_soft.append((rid, d, i, kind, pr))

    # 休日回数のバランス（J1）
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

    # J1 ≧ J2 の休日（上限的に）
    if len(J1_idx) > 0 and len(J2_idx) > 0:
        j1max = model.NewIntVar(0, 5 * D, "j1max_hol")
        for a in J1_idx:
            model.Add(j1max >= hol[a])
        for j in J2_idx:
            model.Add(hol[j] <= j1max)

    # J1 内の早/遅/日勤(1+2)の偏り±2
    early_cnt, late_cnt, day12_cnt = [], [], []
    for i in range(N):
        ei = model.NewIntVar(0, D, f"early_i{i}")
        li = model.NewIntVar(0, D, f"late_i{i}")
        di = model.NewIntVar(0, 2 * D, f"day12_i{i}")
        model.Add(ei == sum(x[(d, E_IDX, i)] for d in range(D)))
        model.Add(li == sum(x[(d, L_IDX, i)] for d in range(D)))
        model.Add(di == sum(x[(d, D1_IDX, i)] + x[(d, D2_IDX, i)] for d in range(D)))
        early_cnt.append(ei); late_cnt.append(li); day12_cnt.append(di)

    for a in J1_idx:
        for b in J1_idx:
            if a >= b:
                continue
            for arr in (early_cnt, late_cnt, day12_cnt):
                df = model.NewIntVar(-2 * D, 2 * D, "tmp")
                model.Add(df == arr[a] - arr[b])
                model.Add(df <= 2)
                model.Add(-df <= 2)

    # 目的関数（未充足ペナルティ／疲労／D2・D3配置ボーナス／ICU比率）
    terms = []

    # B/C 希望ペナルティ
    for rid, d, i, kind, pr in pref_soft:
        w = weight_pref_B if pr == "B" else weight_pref_C
        if w <= 0:
            continue
        assigned_any = model.NewBoolVar(f"assign_any_d{d}_i{i}")
        model.Add(assigned_any == sum(x[(d, s, i)] for s in range(len(SHIFTS))))
        if kind == "off":
            terms.append(int(100 * w) * assigned_any)  # 出勤してしまったらペナルティ
        elif kind == "early" and DAY[d]["req"]["ER_Early"] == 1:
            correct = x[(d, E_IDX, i)]
            miss = model.NewBoolVar(f"pref_early_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "late" and DAY[d]["req"]["ER_Late"] == 1:
            correct = x[(d, L_IDX, i)]
            miss = model.NewBoolVar(f"pref_late_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "day1" and DAY[d]["req"]["ER_Day1"] == 1:
            correct = x[(d, D1_IDX, i)]
            miss = model.NewBoolVar(f"pref_day1_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "day2" and DAY[d]["allow_d2"]:
            correct = x[(d, D2_IDX, i)]
            miss = model.NewBoolVar(f"pref_day2_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "day":
            day1_ok = (DAY[d]["req"]["ER_Day1"] == 1)
            day2_ok = DAY[d]["allow_d2"]
            if day1_ok or day2_ok:
                cands = []
                if day1_ok: cands.append(x[(d, D1_IDX, i)])
                if day2_ok: cands.append(x[(d, D2_IDX, i)])
                correct = model.NewBoolVar(f"pref_day_any_ok_d{d}_i{i}")
                model.AddMaxEquality(correct, cands)
                miss = model.NewBoolVar(f"pref_day_miss_d{d}_i{i}")
                model.Add(miss + correct == 1)
                terms.append(int(100 * w) * miss)
        elif kind == "icu" and (i in J2_idx) and DAY[d]["allow_icu"]:
            correct = x[(d, ICU_IDX, i)]
            miss = model.NewBoolVar(f"pref_icu_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)
        elif kind == "vacation":
            correct = x[(d, VAC_IDX, i)]
            miss = model.NewBoolVar(f"pref_vac_miss_d{d}_i{i}")
            model.Add(miss + correct == 1)
            terms.append(int(100 * w) * miss)

    # 疲労（遅番→翌早番）
    if enable_fatigue and weight_fatigue > 0:
        for i in range(N):
            for d in range(D - 1):
                f = model.NewBoolVar(f"fatigue_d{d}_i{i}")
                model.Add(f >= x[(d, L_IDX, i)] + x[(d + 1, E_IDX, i)] - 1)
                model.Add(f <= x[(d, L_IDX, i)])
                model.Add(f <= x[(d + 1, E_IDX, i)])
                terms.append(int(100 * weight_fatigue) * f)

    # Day2/Day3 の配置ボーナス（置ける日なのに置かなかったら損）
    for d, day in enumerate(all_days):
        if DAY[d]["allow_d2"]:
            placed = model.NewBoolVar(f"d2_placed_{d}")
            model.Add(placed == sum(x[(d, D2_IDX, i)] for i in range(N)))
            w = weight_day2_weekday + (weight_day2_wed_bonus if day.weekday() == 2 else 0.0)
            if weaken_day2_bonus:
                w = max(0.0, w * 0.5)
            if w > 0:
                terms.append(int(100 * w) * (1 - placed))
        if DAY[d]["allow_d3"]:
            placed3 = model.NewBoolVar(f"d3_placed_{d}")
            model.Add(placed3 == sum(x[(d, D3_IDX, i)] for i in range(N)))
            w3 = weight_day3_weekday + (weight_day3_wed_bonus if day.weekday() == 2 else 0.0)
            if weaken_day2_bonus:
                w3 = max(0.0, w3 * 0.5)
            if w3 > 0:
                terms.append(int(100 * w3) * (1 - placed3))

    # ICU 希望比率の偏差
    if weight_icu_ratio > 0 and len(J2_idx) > 0:
        scale = 100
        for j in J2_idx:
            ICU_j = model.NewIntVar(0, 5 * D, f"ICU_j{j}")
            model.Add(ICU_j == sum(x[(d, ICU_IDX, j)] for d in range(D)))
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

    # ---- Solve ----
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
# infeasible 時のブロッキングA特定（1件ずつ）
# -------------------------
def find_blocking_A_once(fair_slack_base: int, weaken_base: bool):
    """Aレコードを1件ずつ無効化して解けるか検査。戻り値: list[(rid, row_dict)]"""
    prefs_base = st.session_state.prefs.reset_index(drop=True)
    A_only = prefs_base[prefs_base["priority"] == "A"].copy()
    blockers = []
    for rid, row in A_only.iterrows():
        tmp = prefs_base.copy()
        tmp.loc[rid, "priority"] = "Z"  # 一時的に無効化
        bak = st.session_state.prefs
        st.session_state.prefs = tmp
        s, sol, a = build_and_solve(
            fair_slack=fair_slack_base, disabled_pref_ids=set(),
            weaken_day2_bonus=weaken_base
        )
        st.session_state.prefs = bak
        if s in ("OPTIMAL", "FEASIBLE"):
            blockers.append((rid, row.to_dict()))
    return blockers

# ===== ここで Part 3 / 4 終了 =====
# （続きは Part 4 へ：実行ボタン、結果表示、ダウンロード）  

# =========================
# app.py — Part 4 / 4
# =========================

# -------------------------
# 実行セクション
# -------------------------
st.header("🧩 スケジュール自動生成")

fair_slack = STAR_TO_FAIR_SLACK.get(s_fairness, 2)
weaken_day2_bonus = False

fix_repro = st.checkbox(
    "再現性を固定",
    value=True,
    help="ONにすると、下の乱数シード値を使って同じ条件で同じ勤務表を再現できます。"
)

if fix_repro:
    seed_val = st.number_input(
        "乱数シード値",
        min_value=0, max_value=1_000_000, value=42, step=1,
        help="同じ条件で同じ勤務表を再現したい場合に利用します。"
    )
    st.caption("🔑 同じseed値であれば、同じ条件の勤務表を再現できます。")
else:
    seed_val = None
    st.caption("🎲 OFFにすると、毎回異なる乱数でスケジュールを生成します。")

# =========================
# 🚀 実行ボタン＆最適化処理（押すまで何も表示しない）
# =========================

run_btn = st.button("🚀 勤務表を作成する", type="primary", use_container_width=True, key="generate_schedule")

if run_btn:
    if fix_repro:
        st.caption("※ 乱数シードを固定中（同じ条件なら再現しやすくなります）")

    with st.spinner("最適化中... 最大20秒ほどかかることがあります"):
        status, solver, artifacts = build_and_solve(
            fair_slack=fair_slack,                     # ← ★つまみの値から自動計算済み
            disabled_pref_ids=set(),
            weaken_day2_bonus=weaken_day2_bonus,
            repro_fix=fix_repro
        )

    st.write(f"**Solver status:** {status}")

    # 可行解チェック
    if status not in ("OPTIMAL", "FEASIBLE"):
        st.error("❌ 可行解が見つかりませんでした。A希望・特例・総勤務回数の整合をご確認ください。")
        st.stop()

    # ---------- 成功時 ----------
    x        = artifacts["x"]
    DAY      = artifacts["DAY"]
    A_star   = artifacts.get("A_star", set())
    A_off    = artifacts.get("A_off", {})     # {day_index: [names,...]}

    # ===== B/C 希望の充足判定（全種別） =====
    prefs_now = st.session_state.prefs.copy()
    prefs_now["kind"]     = prefs_now["kind"].astype(str).str.lower()
    prefs_now["priority"] = prefs_now["priority"].astype(str).str.upper()
    # この月・既知の名前だけに限定
    prefs_now = prefs_now[prefs_now["date"].isin(all_days) & prefs_now["name"].isin(name_to_idx.keys())]

    # 便利なシフトindex
    E_IDX   = SHIFTS.index("ER_Early")
    D1_IDX  = SHIFTS.index("ER_Day1")
    D2_IDX  = SHIFTS.index("ER_Day2")
    L_IDX   = SHIFTS.index("ER_Late")
    ICU_IDX = SHIFTS.index("ICU")
    VAC_IDX = SHIFTS.index("VAC")

    from collections import defaultdict
    total_B = defaultdict(int); hit_B = defaultdict(int)
    total_C = defaultdict(int); hit_C = defaultdict(int)
    unmet_examples = []  # タイトルメッセージ用に、未充足の例を数件拾う

    def _sat(d: int, i: int, kind: str) -> bool:
        """(日index d, 人index i) が kind の B/C希望を満たしているか"""
        # その日の本人の割当有無
        assigned_any = sum(solver.Value(x[(d, s, i)]) for s in range(len(SHIFTS))) > 0
        if kind == "off":
            return not assigned_any

        if kind == "early":
            return (DAY[d]["req"]["ER_Early"] == 1) and (solver.Value(x[(d, E_IDX, i)]) == 1)

        if kind in ("day1", "day_1", "d1"):
            return (DAY[d]["req"]["ER_Day1"] == 1) and (solver.Value(x[(d, D1_IDX, i)]) == 1)

        if kind in ("day2", "day_2", "d2"):
            return DAY[d]["allow_d2"] and (solver.Value(x[(d, D2_IDX, i)]) == 1)

        if kind == "day":
            ok1 = (DAY[d]["req"]["ER_Day1"] == 1) and (solver.Value(x[(d, D1_IDX, i)]) == 1)
            ok2 = DAY[d]["allow_d2"] and (solver.Value(x[(d, D2_IDX, i)]) == 1)
            return ok1 or ok2

        if kind == "late":
            return (DAY[d]["req"]["ER_Late"] == 1) and (solver.Value(x[(d, L_IDX, i)]) == 1)

        if kind == "icu":
            return (i in J2_idx) and DAY[d]["allow_icu"] and (solver.Value(x[(d, ICU_IDX, i)]) == 1)

        if kind == "vacation":
            return solver.Value(x[(d, VAC_IDX, i)]) == 1

        # 未知の種類は満たせていない扱い
        return False

    # 人別・優先度別の総数/充足数をカウント
    for _, r in prefs_now.iterrows():
        d = all_days.index(r["date"])
        i = name_to_idx[r["name"]]
        k = r["kind"]
        p = r["priority"]
        if p not in ("B", "C"):
            continue
        ok = _sat(d, i, k)
        if p == "B":
            total_B[r["name"]] += 1
            hit_B[r["name"]] += int(ok)
        else:
            total_C[r["name"]] += 1
            hit_C[r["name"]] += int(ok)
        if (not ok) and (len(unmet_examples) < 5):
            unmet_examples.append(f"{r['date']} {r['name']}（{k}）")

    # ===== タイトルメッセージ（成功でも違反があれば必ず出す） =====
    total_unmet_B = sum(max(0, total_B[nm] - hit_B[nm]) for nm in names)
    total_unmet_C = sum(max(0, total_C[nm] - hit_C[nm]) for nm in names)
    bc_violations = total_unmet_B + total_unmet_C

    if bc_violations > 0:
        head = f"⚠️ B/C希望の未充足: B={total_unmet_B}件, C={total_unmet_C}件"
        if unmet_examples:
            head += " 例: " + ", ".join(unmet_examples)
        st.error(head)
    else:
        st.success("✅ 最適化に成功しました（B/C希望は全て充足）。")
        # ※ 全充足時は詳細メッセージは出さない

    # ===== J2のICU希望比率：未達アラート =====
    icu_shortfalls = []  # [(name, actual, target)]
    for j in J2_idx:
        nm = names[j]
        desired = float(staff_df.iloc[j]["desired_icu_ratio"])  # 0.0〜1.0
        target  = int(round(desired * int(per_person_total)))
        actual  = sum(int(solver.Value(x[(d, ICU_IDX, j)])) for d in range(D))
        if target > 0 and actual < target:
            icu_shortfalls.append((nm, actual, target))
    if icu_shortfalls:
        ex = ", ".join([f"{nm}({a}/{t})" for nm, a, t in icu_shortfalls[:5]])
        st.error(f"⚠️ J2のICU希望比率の未達が {len(icu_shortfalls)} 名あります。例: {ex}")

    # ===== 1) 日別スケジュール表（★=A希望反映、A休/B休/C休 表示） =====
    # B/C の「休み」が満たせた人を日別表示
    from collections import defaultdict as _dd
    B_off_want = _dd(set); C_off_want = _dd(set)
    for _, r in prefs_now.iterrows():
        if r["kind"] == "off":
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
    for d in range(D):
        row = {"日付": str(all_days[d]), "曜日": WEEKDAY_JA[all_days[d].weekday()]}
        for sname in SHIFTS:
            sidx = SHIFTS.index(sname)
            assigned = [names[i] for i in range(N) if solver.Value(x[(d, sidx, i)]) == 1]
            starset  = {nm for (dd, ss, nm) in A_star if (dd == d and ss == sname)}
            labeled  = [(nm + "★") if (nm in starset) else nm for nm in assigned]
            row[SHIFT_LABEL.get(sname, sname)] = ",".join(labeled)
        # A/B/C 休み（満たせた人の一覧）
        row["A休"] = ",".join(sorted(A_off.get(d, []))) if A_off.get(d) else ""
        row["B休"] = ",".join(B_off_granted.get(d, [])) if B_off_granted.get(d) else ""
        row["C休"] = ",".join(C_off_granted.get(d, [])) if C_off_granted.get(d) else ""
        rows.append(row)

    out_df = pd.DataFrame(rows)
    st.subheader("📋 生成スケジュール（★=A希望反映）")
    st.dataframe(out_df, use_container_width=True, hide_index=True)

    # ===== 2) 個人別集計（早/日1/日2/日3/遅番/ICU/年休、B/C分数表記、ICU希望達成、未達アラート） =====
    hol_days_idx = [idx for idx, day in enumerate(all_days) if (day.weekday() >= 5 or day in holidays)]

    def _in_cell(lbl: str, di: int, nm: str) -> bool:
        cell = out_df.loc[di, lbl]
        if not isinstance(cell, str) or not cell:
            return False
        return nm in [x.strip("★") for x in cell.split(",") if x]

    def _frac(hit: int, total: int) -> str:
        return "-" if total == 0 else f"{hit}/{total}"

    person_rows = []
    for i, nm in enumerate(names):
        cnt = {lbl: sum(1 for d in range(D) if _in_cell(lbl, d, nm))
               for lbl in ["早番", "日勤1", "日勤2", "日勤3", "遅番", "ICU", "年休"]}
        total   = sum(cnt.values())
        hol_cnt = sum(
            sum(1 for lbl in ["早番", "日勤1", "日勤2", "日勤3", "遅番", "ICU"] if _in_cell(lbl, d, nm))
            for d in hol_days_idx
        )
        fatigue = sum(1 for d in range(D - 1) if _in_cell("遅番", d, nm) and _in_cell("早番", d + 1, nm))

        # ICU希望（J2のみ目標あり）
        desired_ratio = float(staff_df.iloc[i]["desired_icu_ratio"])
        icu_target    = int(round(desired_ratio * int(per_person_total))) if staff_df.iloc[i]["grade"] == "J2" else 0
        icu_actual    = cnt["ICU"]
        icu_col       = "-" if icu_target == 0 else f"{icu_actual}/{icu_target}"

        person_rows.append({
            "name": nm,
            "grade": staff_df.iloc[i]["grade"],
            **cnt,
            "B希望充足": _frac(hit_B[nm], total_B[nm]),
            "C希望充足": _frac(hit_C[nm], total_C[nm]),
            "ICU希望達成": icu_col,
            "Total": total,
            "Holiday": hol_cnt,
            "Fatigue": fatigue,
        })

    stat_df = pd.DataFrame(person_rows)[
        ["name","grade","早番","日勤1","日勤2","日勤3","遅番","ICU","年休",
         "B希望充足","C希望充足","ICU希望達成","Total","Holiday","Fatigue"]
    ]

    # 未充足セル（B/C/ICU）を淡い赤＋赤字でマーキング
    def _alert_style(series):
        styles = []
        for v in series:
            if isinstance(v, str) and "/" in v:
                try:
                    a, b = v.split("/")
                    a = int(a) if a != "-" else 0
                    b = int(b) if b != "-" else 0
                    styles.append("background-color:#FFF1F1;color:#B10000;" if (b > 0 and a < b) else "")
                except Exception:
                    styles.append("")
            else:
                styles.append("")
        return styles

    st.subheader("👥 個人別集計（B/Cは分数表記、ICU希望達成を追加。未達セルを淡色で警告）")
    styled = (
        stat_df
        .style
        .apply(_alert_style, subset=["B希望充足","C希望充足","ICU希望達成"])
    )
    st.write(styled)

    # ===== 3) 改善ヒント（B/C違反がある時だけ） =====
    if bc_violations > 0:
        tips = []
        tips.append("・B/C未充足が出ています。希望ウェイト（⭐️）を上げると優先されやすくなります。")
        if s_fairness == 3:
            tips.append("・『休日公平性』を⭐️3→⭐️2（または⭐️1）に下げると取りやすくなる場合があります。")
        if max_consecutive <= 4:
            tips.append("・『最大連勤日数』を+1（例: 5→6）にすると探索の自由度が上がります。")
        if enable_fatigue and weight_fatigue >= STAR_TO_WEIGHT_FATIGUE.get(2, 12.0):
            tips.append("・『疲労ペナルティ（遅番→翌早番）』を⭐️1に下げると割当の自由度が増えます。")
        if (weight_day2_weekday + weight_day2_wed_bonus) > 0 or (weight_day3_weekday + weight_day3_wed_bonus) > 0:
            tips.append("・Day2/Day3のボーナス⭐️を弱めると、B/C希望を優先しやすくなります。")
        # ICU未達があるなら関連ヒントも（上のアラートに合わせて）
        if icu_shortfalls:
            if not allow_weekend_icu:
                tips.append("・ICU希望の未達があるため『週末ICUを許可』をONにすることを検討してください。")
            tips.append("・J2の『ICU希望比率』の⭐️（遵守強さ）を上げると、ICUが優先されやすくなります。")
        st.info("**詳細メッセージ（改善のヒント）**\n\n" + "\n".join(tips))

    # ===== 4) CSV/JSON ダウンロード =====
    json_snapshot = make_snapshot(
        out_df=out_df, stat_df=stat_df, status=status,
        objective=solver.ObjectiveValue(),
        fair_star=s_fairness, fair_slack_val=STAR_TO_FAIR_SLACK.get(s_fairness, 2)
    )

    import io, json as _json
    buf_json = io.StringIO(); buf_json.write(_json.dumps(json_snapshot, ensure_ascii=False, indent=2))
    buf_csv  = io.StringIO(); out_df.to_csv(buf_csv, index=False)

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "📥 スケジュールCSVをダウンロード",
            data=buf_csv.getvalue(), file_name="schedule.csv", mime="text/csv"
        )
    with c2:
        st.download_button(
            "🧾 スナップショットJSONをダウンロード",
            data=buf_json.getvalue(), file_name="run_snapshot.json", mime="application/json"
        )

    st.caption("🧾 スナップショットJSONは、条件や結果を丸ごと保存/復元できます。")

# -------------------------
# 結果のメモ欄
# -------------------------
st.divider()
st.subheader("🗒️ メモ")
st.text_area(
    "補足・コメント（任意）",
    placeholder="例: ○○さんのDay2比率が高すぎるため、次回は平日ボーナスを弱める など",
    key="memo_text",
    height=120
)

# -------------------------
# おわり
# -------------------------
st.caption("Resident Scheduler © 2025 Yuji Takahashi")

# ===== ここで Part 4 / 4 終了 =====
