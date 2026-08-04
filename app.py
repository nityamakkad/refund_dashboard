"""
Refund & Retention Review — Streamlit app
Reads learner-level data directly from Google Sheets (one worksheet per
course: Agentic, FDE, Switch-Up, LevelUp) and rebuilds every table live.

SETUP: see README.md in this folder for the one-time Google Sheets +
service account steps. Once secrets are configured, just run:
    streamlit run app.py
"""
import json
import re
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# CONFIG — edit these to match your actual Google Sheet
# ============================================================
SHEET_URL_OR_KEY = st.secrets.get("SHEET_URL_OR_KEY", "")  # full URL or the sheet key
# Worksheet tab names — match whatever your actual tabs are named (any case/spacing is fine,
# this is just used to find the tab; data column matching below is separately tolerant too).
WORKSHEET_NAMES = {
    "Agentic": "agentic",
    "FDE": "fde",
    "Switch-Up": "flagship",
    "LevelUp": "levelup",
}
CACHE_TTL_SECONDS = 300  # re-read Sheets at most every 5 minutes

PAYMENT_MODE_MAP = {
    'ACH_TRANSFER': 'ACH', 'CLIMBCREDIT': 'Climb', 'STRIPE_CARD': 'Stripe',
    'STRIPE_MANUAL_PAYMENT': 'Stripe', 'PAYPAL': 'PayPal', 'KLARNA': 'Klarna',
    '0_PERCENT_APR_FLEXIPAY': 'Flexipay', '0% APR FLEXIPAY': 'Flexipay', 'Flexipay': 'Flexipay',
    'BRAINTREE': 'Braintree', 'AFFIRM_VIA_STRIPE': 'Affirm', 'BANK_TRANSFER': 'Bank Transfer',
    'RAZORPAY_MANUAL_PAYMENT': 'Razorpay', 'Invalid': 'Unknown/Invalid', 'Not Updated': 'Unknown/Invalid',
}

# Canonical column name -> acceptable header variants in the raw sheet (case/whitespace-insensitive)
COLUMN_ALIASES = {
    'hubspot_id': ['hubspot_id', 'hubspot id'],
    'cohort_name': ['cohort_name', 'cohort name'],
    'cohort_start_date': ['cohort_start_date', 'cohort start date'],
    'Payment\nDeadline': ['payment deadline', 'payment_deadline'],
    'Refund date': ['refund date', 'refund_date'],
    'Status': ['status'],
    'Payment Mode': ['payment mode', 'payment_mode'],
    'Refunded': ['refunded'],
    'Trial Window': ['trial window', 'trial_window'],
    'Engagement\n Level': ['engagement level', 'engagement_level'],
    'High-Risk Flag': ['high-risk flag', 'high risk flag', 'highrisk flag'],
    'Refund Category': ['refund category', 'refund_category'],
    'Refund Reason': ['refund reason', 'refund_reason'],
    'Refund \nJourney Stage': ['refund journey stage', 'refund_journey_stage'],
    'Current Payment Status': ['current payment status', 'current_payment_status'],
    'Agreement Signed Date': ['agreement signed date'],
    'Month': ['month'],
    'Retained/Move': ['retained/move', 'retained / move', 'retained/ move', 'retained /move', 'retained_move', 'retained move'],
    'Starting with AI/IP': ['starting with ai/ip', 'starting with ai / ip', 'starting with ai/ ip', 'starting with ai /ip', 'starting with ai and ip'],
    'Deal': ['deal'],
}

NAVY, GREEN, AMBER, RED = "#0B2A4A", "#1E8E5A", "#D6971F", "#C63C3C"
COURSES = ["Agentic", "FDE", "Switch-Up", "LevelUp"]

st.set_page_config(page_title="Refund & Retention Review", layout="wide")

st.markdown("""
<style>
.kpi-row { display:flex; gap:14px; flex-wrap:wrap; margin: 4px 0 20px; }
.kpi-card { flex:1; min-width:180px; border-radius:14px; padding:18px 20px 16px;
  border:1px solid #E1E4E9; box-shadow:0 2px 10px rgba(15,42,74,0.08); position:relative; overflow:hidden; }
.kpi-card:before { content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:var(--accent,#0B2A4A); }
.kpi-icon { position:absolute; right:14px; top:12px; font-size:26px; opacity:0.18; }
.kpi-label { font-size:11px; letter-spacing:.5px; text-transform:uppercase; color:#6B7684; font-weight:700; }
.kpi-value { font-size:34px; font-weight:800; margin-top:6px; font-variant-numeric:tabular-nums; line-height:1.1; }
.kpi-sub { font-size:12px; color:#8A93A0; margin-top:4px; }
</style>
""", unsafe_allow_html=True)


def norm_key(s: str) -> str:
    return re.sub(r'[\s_]+', ' ', str(s)).strip().lower()


ALIAS_TO_CANON = {norm_key(a): canon for canon, aliases in COLUMN_ALIASES.items() for a in aliases}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for c in df.columns:
        key = norm_key(c)
        if key in ALIAS_TO_CANON and ALIAS_TO_CANON[key] != c:
            rename_map[c] = ALIAS_TO_CANON[key]
    return df.rename(columns=rename_map)


def kpi_card(label, value, color=NAVY, sub="", icon="●"):
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    bg = f"linear-gradient(135deg, {color}14 0%, #ffffff 65%)"
    return (
        f'<div class="kpi-card" style="--accent:{color}; background:{bg}">'
        f'<div class="kpi-icon">{icon}</div>'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-value" style="color:{color}">{value}</div>'
        f'{sub_html}</div>'
    )


def ret_color(p):
    if p is None:
        return "#9AA3AE"
    if p >= 90:
        return GREEN
    if p >= 75:
        return AMBER
    return RED


# ============================================================
# DATA LOADING — Google Sheets -> clean DataFrame
# ============================================================
@st.cache_resource
def get_gspread_client():
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return gspread.authorize(creds)


def dedupe_columns(cols):
    seen = {}
    out = []
    for c in cols:
        c = str(c).strip() if str(c).strip() else "Unnamed"
        if c in seen:
            seen[c] += 1
            out.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Reading latest data from Google Sheets...")
def load_raw_from_sheets():
    client = get_gspread_client()
    sh = client.open_by_url(SHEET_URL_OR_KEY) if SHEET_URL_OR_KEY.startswith("http") \
        else client.open_by_key(SHEET_URL_OR_KEY)
    frames = []
    load_report = []
    for course, ws_name in WORKSHEET_NAMES.items():
        try:
            ws = sh.worksheet(ws_name)
        except gspread.exceptions.WorksheetNotFound:
            load_report.append({"course": course, "tab": ws_name, "rows_found": 0, "note": "TAB NOT FOUND — check exact tab name (case/spacing matters)"})
            continue
        try:
            values = ws.get_all_values()  # raw grid — avoids gspread's duplicate-header crash
        except Exception as e:
            load_report.append({"course": course, "tab": ws_name, "rows_found": 0, "note": f"ERROR reading tab: {e}"})
            continue
        if len(values) < 2:
            load_report.append({"course": course, "tab": ws_name, "rows_found": 0, "note": "Tab found but has no data rows"})
            continue
        headers = dedupe_columns(values[0])
        body = values[1:]
        # pad/truncate rows to header length so ragged rows don't crash the DataFrame build
        body = [r + [""] * (len(headers) - len(r)) if len(r) < len(headers) else r[:len(headers)] for r in body]
        try:
            df = pd.DataFrame(body, columns=headers)
        except Exception as e:
            load_report.append({"course": course, "tab": ws_name, "rows_found": 0, "note": f"ERROR building table: {e}"})
            continue
        df = df[~(df.apply(lambda r: all(str(v).strip() == "" for v in r), axis=1))]  # drop fully-blank rows
        df = normalize_columns(df)
        if 'Retained/drop\n/move' in df.columns:
            df.rename(columns={'Retained/drop\n/move': 'Retained'}, inplace=True)
        df['course_group'] = course
        frames.append(df)
        load_report.append({"course": course, "tab": ws_name, "rows_found": len(df), "note": "OK"})
    combined = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    return combined, load_report


def extract_period(cohort_name: str) -> str:
    if not isinstance(cohort_name, str):
        return "Unknown"
    m = re.search(r'(2nd\s*mid|end|mid|early)', cohort_name, re.IGNORECASE)
    if not m:
        return "Unknown"
    tag = re.sub(r'\s+', ' ', m.group(1).lower())
    return {'2nd mid': '2nd Mid', 'end': 'End', 'mid': 'Mid', 'early': 'Early'}[tag]


def extract_track(course_group: str, cohort_name: str) -> str:
    if course_group == 'Agentic':
        if 'for SWEs' in cohort_name:
            return 'SWE'
        if "for EM's" in cohort_name or 'for EMs' in cohort_name:
            return 'EM'
        return 'Agentic (General)'
    if course_group == 'LevelUp':
        return re.sub(r'\s*-\s*(2nd\s*[Mm]id|[Ee]nd|[Mm]id|[Ee]arly).*$', '', cohort_name).strip()
    if course_group == 'Switch-Up':
        if 'Machine Learning Program' in cohort_name:
            return 'Flagship ML'
        if 'Advanced' in cohort_name:
            return 'Advanced ML'
        return cohort_name
    return course_group


def risk_bucket(flag: str) -> str:
    if not isinstance(flag, str):
        return 'Unknown'
    m = re.search(r'\((.*)\)', flag)
    return m.group(1) if m else flag


def clean(df: pd.DataFrame, reference_date: pd.Timestamp) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()

    def col(name, default=""):
        return df[name] if name in df.columns else pd.Series([default] * len(df))

    df['cohort_start_date'] = pd.to_datetime(col('cohort_start_date'), errors='coerce')
    df['payment_deadline'] = pd.to_datetime(col('Payment\nDeadline'), errors='coerce')
    refund_raw = col('Refund date')
    refund_date = pd.to_datetime(refund_raw, errors='coerce', format='%m/%d/%Y')
    still_na = refund_date.isna() & refund_raw.astype(str).str.strip().ne('')
    refund_date.loc[still_na] = pd.to_datetime(refund_raw[still_na], errors='coerce')
    df['refund_date'] = refund_date

    df['payment_category'] = col('Status', 'Unknown').replace('', 'Unknown').fillna('Unknown')
    df['payment_method_clean'] = col('Payment Mode').astype(str).str.strip().map(PAYMENT_MODE_MAP).fillna('Other')
    df['is_refunded'] = col('Refunded', 'No').astype(str).str.strip() == 'Yes'
    df['trial_window'] = col('Trial Window', 'Not Marked').replace('', 'Not Marked').fillna('Not Marked')
    df['engagement_level'] = col('Engagement\n Level', 'Unknown Engagement').replace('', 'Unknown Engagement').fillna('Unknown Engagement')
    df['risk_bucket'] = col('High-Risk Flag').apply(risk_bucket)
    df['refund_category'] = col('Refund Category')
    df['refund_reason'] = col('Refund Reason')
    df['refund_journey_stage'] = col('Refund \nJourney Stage')
    df['current_payment_status'] = col('Current Payment Status')
    df['hubspot_id'] = col('hubspot_id')
    df['cohort_name'] = col('cohort_name')
    df['cohort_month'] = col('Month', 'Unknown').replace('', 'Unknown').fillna('Unknown')

    retained_raw = col('Retained/Move', '').fillna('').astype(str).str.strip()
    df['retained_raw'] = retained_raw.replace('', 'Not Marked')
    df['is_retained'] = df['retained_raw'] == 'Retained'
    df['is_moved'] = df['retained_raw'] == 'Move'

    starting_track = col('Starting with AI/IP', '').fillna('').astype(str).str.strip()
    deal_raw = col('Deal', '').fillna('').astype(str).str.strip()
    df['starting_track'] = starting_track
    df['program_segment'] = np.where(deal_raw == '', 'Standalone', 'Edgeup (AI+IP)')

    df['period_tag'] = df['cohort_name'].apply(extract_period)
    df['track'] = df.apply(lambda r: extract_track(r['course_group'], r['cohort_name']), axis=1)

    df['cohort_status'] = np.where(
        df['payment_deadline'].isna(), 'Unknown',
        np.where(df['payment_deadline'] < reference_date, 'Closed', 'Open')
    )
    return df


# ============================================================
# LOAD + CLEAN
# ============================================================
today = pd.Timestamp(datetime.now().date())
raw, load_report = load_raw_from_sheets()

if raw.empty:
    st.error(
        "No data loaded from Google Sheets. Check `SHEET_URL_OR_KEY` in secrets, "
        "the worksheet names in `WORKSHEET_NAMES`, and that the sheet is shared "
        "with the service account email. See README.md."
    )
    st.dataframe(pd.DataFrame(load_report))
    st.stop()

df_all = clean(raw, today)

# Agentic and LevelUp share "Edgeup" learners who took both AI (Agentic) and IP (LevelUp).
# Each such learner appears in both course sheets — keep them only under the course they
# actually STARTED with, so totals aren't double-counted across the two courses.
_drop_mask = (
    ((df_all['course_group'] == 'Agentic') & (df_all['starting_track'] == 'IP')) |
    ((df_all['course_group'] == 'LevelUp') & (df_all['starting_track'] == 'AI'))
)
_crossover_removed = int(_drop_mask.sum())
df_all = df_all[~_drop_mask].copy()

# ============================================================
# SIDEBAR CONTROLS
# ============================================================
st.sidebar.title("Refund & Retention Review")
st.sidebar.caption(f"Reference date: {today.date()}  ·  {len(df_all)} learner rows loaded")

with st.sidebar.expander("Data health check"):
    st.dataframe(pd.DataFrame(load_report), hide_index=True, use_container_width=True)
    st.caption("If a course shows 0 rows or 'not found', its tab name or headers don't match — fix the tab name/header text in the Sheet rather than editing code.")
    if _crossover_removed:
        st.caption(f"{_crossover_removed} Edgeup (AI+IP) row(s) excluded from Agentic/LevelUp counts to avoid double-counting learners who took both — kept only under the course they started with.")

mode = st.sidebar.radio("View", ["Weekly", "Monthly"], horizontal=True)

if mode == "Weekly":
    days_since_tue_today = (today.dayofweek - 1) % 7
    default_start = (today - pd.Timedelta(days=int(days_since_tue_today) + 7)).date()
    default_end = default_start + timedelta(days=6)
    c1, c2 = st.sidebar.columns(2)
    start_input = c1.date_input("Start date", value=default_start)
    end_input = c2.date_input("End date", value=default_end)
    range_start = pd.Timestamp(start_input)
    range_end = pd.Timestamp(end_input)
else:
    months = sorted(df_all['cohort_start_date'].dt.to_period('M').dropna().unique().astype(str))
    month_sel = st.sidebar.selectbox("Month", months, index=len(months) - 1 if months else 0)
    period = pd.Period(month_sel)
    range_start = period.start_time
    range_end = period.end_time

course_sel = st.sidebar.selectbox("Course", ["All"] + sorted(df_all['course_group'].unique().tolist()))
st.sidebar.caption(f"Window: {range_start.date()} – {range_end.date()}")

if st.sidebar.button("🔄 Refresh from Google Sheets"):
    st.cache_data.clear()
    st.rerun()

use_ai = st.sidebar.toggle("Enable AI insights (OpenAI)", value=bool(st.secrets.get("OPENAI_API_KEY")))

# ============================================================
# FILTER — "all data in this timespan" = cohorts that started in-window
#          OR learners refunded in-window (even if they enrolled earlier)
# ============================================================
def in_course(d):
    return d if course_sel == "All" else d[d['course_group'] == course_sel]

base = in_course(df_all)
in_cohort_window = (base['cohort_start_date'] >= range_start) & (base['cohort_start_date'] <= range_end)
in_refund_window = base['is_refunded'] & base['refund_date'].notna() & (base['refund_date'] >= range_start) & (base['refund_date'] <= range_end)
scoped = base[in_cohort_window | in_refund_window]

refund_scoped = base[base['is_refunded'] & base['refund_date'].notna() & (base['refund_date'] >= range_start) & (base['refund_date'] <= range_end)]

# ============================================================
# HELPERS: aggregation
# ============================================================
def pct(n, d):
    return round(100 * n / d, 1) if d else None


def month_summary(rows):
    out = []
    for (course, month), g in rows.groupby(['course_group', 'cohort_month']):
        total = len(g)
        out.append({
            'Course': course, 'Month': month, 'Total': total,
            'Retained': int(g['is_retained'].sum()), 'Retained %': pct(int(g['is_retained'].sum()), total),
            'Refunds': int(g['is_refunded'].sum()), 'Refund %': pct(int(g['is_refunded'].sum()), total),
        })
    return pd.DataFrame(out).sort_values(['Course', 'Month']) if out else pd.DataFrame()


def month_category_wide(rows):
    out = []
    for (course, month), g in rows.groupby(['course_group', 'cohort_month']):
        row = {'Course': course, 'Month': month}
        for cat in ['Upfront', 'Flexipay', 'Non-Upfront']:
            c = g[g['payment_category'] == cat]
            total = len(c)
            retained = int(c['is_retained'].sum())
            row[f'{cat} Total'] = total
            row[f'{cat} Retained'] = retained
            row[f'{cat} Retained %'] = pct(retained, total)
            row[f'{cat} Refunded'] = int(c['is_refunded'].sum())
        out.append(row)
    return pd.DataFrame(out).sort_values(['Course', 'Month']) if out else pd.DataFrame()


def program_segment_table(rows):
    sub = rows[rows['course_group'].isin(['Agentic', 'LevelUp'])].copy()
    if sub.empty:
        return pd.DataFrame()

    def label(r):
        track = 'AI' if r['course_group'] == 'Agentic' else 'IP'
        return f'Standalone {track}' if r['program_segment'] == 'Standalone' else f'Edgeup (Started {track})'

    sub['segment_label'] = sub.apply(label, axis=1)
    out = []
    for (course, segment), g in sub.groupby(['course_group', 'segment_label']):
        total = len(g)
        out.append({'Course': course, 'Segment': segment, 'Total': total,
                    'Retained': int(g['is_retained'].sum()), 'Retained %': pct(int(g['is_retained'].sum()), total),
                    'Refunded': int(g['is_refunded'].sum())})
    order = ['Standalone AI', 'Edgeup (Started AI)', 'Standalone IP', 'Edgeup (Started IP)']
    df = pd.DataFrame(out)
    df['_o'] = df['Segment'].apply(lambda s: order.index(s) if s in order else 99)
    return df.sort_values(['Course', '_o']).drop(columns='_o') if not df.empty else df


def payment_method_table(rows):
    out = []
    for (course, method), g in rows.groupby(['course_group', 'payment_method_clean']):
        total = len(g)
        out.append({'Course': course, 'Method': method, 'Total': total, 'Retained': int(g['is_retained'].sum()),
                    'Retained %': pct(int(g['is_retained'].sum()), total), 'Refunded': int(g['is_refunded'].sum())})
    return pd.DataFrame(out).sort_values(['Course', 'Total'], ascending=[True, False]) if out else pd.DataFrame()


def heatmap_data_by_course(rows, course):
    order = ["All 3", "Orientation & F", "O & F", "O & O", "First Class", "Orientation", "Onboarding", "None", "Unknown"]
    sub = rows[rows['course_group'] == course]
    if sub.empty:
        return pd.DataFrame(), [], order
    month_order = sub.groupby('cohort_month')['cohort_start_date'].min().sort_values().index.tolist()
    out = []
    for (month, bucket), g in sub.groupby(['cohort_month', 'risk_bucket']):
        total = len(g)
        out.append({'month': month, 'bucket': bucket, 'n': total,
                    'retained_pct': pct(int(g['is_retained'].sum()), total), 'refunded': int(g['is_refunded'].sum())})
    return pd.DataFrame(out), month_order, order


def subcat_counts(rows):
    if rows.empty:
        return pd.DataFrame(columns=['Course', 'Sub-category', 'Count'])
    r = rows.copy()
    r['refund_category'] = r['refund_category'].fillna('Uncategorized')
    vc = r.groupby(['course_group', 'refund_category']).size().reset_index(name='Count')
    vc.columns = ['Course', 'Sub-category', 'Count']
    return vc.sort_values(['Course', 'Count'], ascending=[True, False])


def refund_method_reason_table(rows):
    ref = rows[rows['is_refunded']].copy()
    if ref.empty:
        return pd.DataFrame()
    ref['refund_category'] = ref['refund_category'].fillna('Uncategorized')
    vc = ref.groupby(['course_group', 'payment_method_clean', 'refund_category']).size().reset_index(name='Refunds')
    vc.columns = ['Course', 'Payment Method', 'Refund Sub-category', 'Refunds']
    return vc.sort_values(['Course', 'Payment Method', 'Refunds'], ascending=[True, True, False])


def stage_refund_table(rows):
    ref = rows[rows['is_refunded']].copy()
    if ref.empty:
        return pd.DataFrame()
    ref['refund_journey_stage'] = ref['refund_journey_stage'].fillna('Unknown')
    out = []
    for (course, stage), g in ref.groupby(['course_group', 'refund_journey_stage']):
        out.append({'Course': course, 'Stage': stage, 'Refunds': len(g)})
    df = pd.DataFrame(out)
    if df.empty:
        return df
    totals = df.groupby('Course')['Refunds'].transform('sum')
    df['% of course refunds'] = (100 * df['Refunds'] / totals).round(1)
    return df.sort_values(['Course', 'Refunds'], ascending=[True, False])


# ============================================================
# AI INSIGHTS (server-side — key never touches the browser)
# ============================================================
def ai_insight(payload, label="this table", key_suffix=""):
    if not use_ai:
        return
    if st.button(f"✦ Generate insight — {label}", key=f"btn_{key_suffix}"):
        api_key = st.secrets.get("OPENAI_API_KEY")
        if not api_key:
            st.warning("No OPENAI_API_KEY found in secrets.")
            return
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        "You are annotating an internal refund/retention dashboard for a bootcamp "
                        "operations team. Using ONLY the JSON data below — do not invent any number, "
                        "name, or reason not present in it — write 3 short plain-text bullet observations "
                        "a manager could read in 10 seconds before a weekly review. If the data doesn't "
                        f"support a clear pattern, say so plainly.\n\nDATA:\n{json.dumps(payload, default=str)}"
                    ),
                }],
                max_tokens=300,
            )
            st.info(resp.choices[0].message.content)
        except Exception as e:
            st.error(f"Couldn't reach OpenAI: {e}")


# ============================================================
# HEADER + KPIs
# ============================================================
st.title("Refund & Retention Review")

total = len(scoped)
refunds = int(scoped['is_refunded'].sum())
retained = int(scoped['is_retained'].sum())
refund_pct = pct(refunds, total)
retained_pct_kpi = pct(retained, total)
cards = [
    kpi_card("Enrollments in window", total, icon="👥"),
    kpi_card("Retained", retained, color=ret_color(retained_pct_kpi) if total else NAVY, sub=f"{retained_pct_kpi}% of window" if total else "", icon="✅"),
    kpi_card("Refunds in window", refunds, color=RED if (refund_pct or 0) > 15 else NAVY, icon="💸"),
    kpi_card("Refund rate", f"{refund_pct}%" if total else "—", color=ret_color(100 - (refund_pct or 0)) if total else "#9AA3AE", icon="📉"),
]
st.markdown(f'<div class="kpi-row">{"".join(cards)}</div>', unsafe_allow_html=True)

tabs = st.tabs([
    "Cohort Summary", "Category Breakdown", "Trial Window (Refunds)", "Refund Sub-categories",
    "Refunds — Detail", "Active Cases", "Payment Method", "Engagement Heatmap",
])

# ---- Cohort Summary (by month) ----
with tabs[0]:
    st.subheader("Month-wise enrollments & refunds")
    data = month_summary(scoped)
    st.dataframe(data, use_container_width=True, hide_index=True)

    if not scoped.empty:
        chart_df = scoped.groupby('course_group').agg(
            Total=('hubspot_id', 'count'), Refunds=('is_refunded', 'sum'), Retained=('is_retained', 'sum')
        ).reset_index().rename(columns={'course_group': 'Course'})
        chart_df['Retained %'] = (100 * chart_df['Retained'] / chart_df['Total']).round(1)

        fig = go.Figure()
        fig.add_bar(x=chart_df['Course'], y=chart_df['Total'], name='Total', marker_color=NAVY)
        fig.add_bar(x=chart_df['Course'], y=chart_df['Refunds'], name='Refunds', marker_color=RED)
        fig.add_trace(go.Scatter(
            x=chart_df['Course'], y=chart_df['Retained %'], name='Retained %', mode='lines+markers+text',
            text=[f"{v}%" for v in chart_df['Retained %']], textposition='top center',
            yaxis='y2', line=dict(color=GREEN, width=3), marker=dict(size=9),
        ))
        fig.update_layout(
            barmode='group', height=380, margin=dict(t=30, b=10),
            yaxis=dict(title='Count'),
            yaxis2=dict(title='Retained %', overlaying='y', side='right', range=[0, 105], showgrid=False),
            legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        )
        st.plotly_chart(fig, use_container_width=True)

    ai_insight(data.to_dict('records') if not data.empty else [], "cohort summary", "cohort")

    seg = program_segment_table(scoped)
    if not seg.empty:
        st.markdown("**Program segment — Agentic & LevelUp** _(standalone vs. Edgeup learners who took both AI + IP, split by which program they started with)_")
        st.dataframe(seg, use_container_width=True, hide_index=True)

# ---- Category Breakdown (month-wise) ----
with tabs[1]:
    st.subheader("Upfront / Flexipay / Non-Upfront — month-wise")
    data = month_category_wide(scoped)
    st.dataframe(data, use_container_width=True, hide_index=True)

    if not scoped.empty:
        pie_df = scoped['payment_category'].value_counts().reset_index()
        pie_df.columns = ['Category', 'Count']
        pie_df = pie_df[pie_df['Category'].isin(['Upfront', 'Flexipay', 'Non-Upfront'])]
        fig = px.pie(pie_df, names='Category', values='Count', hole=0.45,
                     color='Category', color_discrete_map={'Upfront': NAVY, 'Flexipay': AMBER, 'Non-Upfront': GREEN})
        fig.update_traces(textinfo='label+percent')
        fig.update_layout(height=360, margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)

    ai_insight(data.to_dict('records') if not data.empty else [], "category breakdown", "catwide")

    with st.expander(f"Show raw data ({len(scoped)} rows)"):
        st.dataframe(scoped[['hubspot_id', 'course_group', 'cohort_name', 'cohort_start_date',
                              'payment_category', 'payment_method_clean', 'retained_raw', 'is_refunded',
                              'program_segment', 'starting_track', 'engagement_level']],
                     use_container_width=True, hide_index=True)

# ---- Trial Window (refunded learners only) ----
with tabs[2]:
    st.subheader("Trial window — refunded learners, course & cohort-wise")
    ref_rows = scoped[scoped['is_refunded']]
    rows_ = []
    for (course, cohort), g in ref_rows.groupby(['course_group', 'cohort_name']):
        yes = int((g['trial_window'] == 'Yes').sum())
        no = int((g['trial_window'] == 'No').sum())
        denom = yes + no
        rows_.append({
            'Course': course, 'Cohort': cohort, 'Total Refunds': len(g),
            'Trial: Yes': yes, 'Yes %': pct(yes, denom),
            'Trial: No': no, 'No %': pct(no, denom),
        })
    trial_df = pd.DataFrame(rows_).sort_values(['Course', 'Cohort']) if rows_ else pd.DataFrame()
    if trial_df.empty:
        st.caption("No refunds in this window.")
    else:
        st.dataframe(trial_df, use_container_width=True, hide_index=True)
        chart_src = ref_rows[ref_rows['trial_window'].isin(['Yes', 'No'])]
        chart_agg = chart_src.groupby(['course_group', 'trial_window']).size().reset_index(name='Count')
        fig = px.bar(chart_agg, x='course_group', y='Count', color='trial_window', barmode='group',
                     color_discrete_map={'Yes': GREEN, 'No': RED}, labels={'course_group': 'Course', 'trial_window': 'Trial Window'})
        fig.update_layout(height=340, margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)

# ---- Refund Sub-categories ----
with tabs[3]:
    st.subheader("Refund sub-categories — closed vs open cohorts")
    c1, c2 = st.columns(2)
    src = refund_scoped if not refund_scoped.empty else scoped[scoped['is_refunded']]
    with c1:
        st.markdown("**Closed cohorts** _(payment deadline has passed)_")
        st.dataframe(subcat_counts(src[src['cohort_status'] == 'Closed']), use_container_width=True, hide_index=True)
    with c2:
        st.markdown("**Open cohorts** _(still in the decision window)_")
        st.dataframe(subcat_counts(src[src['cohort_status'] == 'Open']), use_container_width=True, hide_index=True)

# ---- Refunds Detail (selected window) ----
with tabs[4]:
    st.subheader(f"Refunds processed — detailed reasons  ({range_start.date()} – {range_end.date()})")
    if refund_scoped.empty:
        st.caption("No refunds with a refund date in this window.")
    else:
        detail = refund_scoped[['course_group', 'cohort_name', 'refund_date', 'refund_category',
                                 'payment_method_clean', 'refund_journey_stage', 'refund_reason']].copy()
        detail['refund_date'] = detail['refund_date'].dt.date
        detail.columns = ['Course', 'Cohort', 'Refund Date', 'Category', 'Payment Method', 'Stage', 'Reason']
        st.dataframe(detail, use_container_width=True, hide_index=True)
        ai_insight(detail.drop(columns=['Reason']).to_dict('records'), "this week's refunds", "lastweek")

    st.markdown("**Refunds by course & journey stage**")
    stage_df = stage_refund_table(refund_scoped)
    if stage_df.empty:
        st.caption("No refunds in this window.")
    else:
        st.dataframe(stage_df, use_container_width=True, hide_index=True)

# ---- Active Cases (placeholder — separate data source pending) ----
with tabs[5]:
    st.subheader("Active cases")
    st.info(
        "Waiting on a dedicated case-tracking data source — this tab is intentionally empty. "
        "Once that sheet/source is shared, this can be wired in the same way as the other tabs."
    )

# ---- Payment Method ----
with tabs[6]:
    st.subheader("Payment method × retention")
    data = payment_method_table(scoped)
    if data.empty:
        st.caption("No data in this window.")
    else:
        course_tabs = st.tabs(COURSES)
        for course, ctab in zip(COURSES, course_tabs):
            with ctab:
                sub = data[data['Course'] == course].drop(columns='Course')
                if sub.empty:
                    st.caption("No data in this window.")
                else:
                    st.dataframe(sub, use_container_width=True, hide_index=True)
        fig = px.bar(data, x='Method', y='Total', color='Course', barmode='group')
        fig.update_layout(height=340, margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
    ai_insight(data.to_dict('records') if not data.empty else [], "payment method table", "paymethod")

    st.markdown("**Refunds — course × payment method × reason sub-category**")
    mr = refund_method_reason_table(scoped)
    if mr.empty:
        st.caption("No refunds in this window.")
    else:
        st.dataframe(mr, use_container_width=True, hide_index=True)

# ---- Engagement Heatmap ----
with tabs[7]:
    st.subheader("Engagement category heatmap")
    st.caption("One heatmap per course, month-wise. Each cell: total learners (n), retained %, refund count for that engagement category.")
    HEAT_SCALE = [[0, "#D9776B"], [0.5, "#E8B85E"], [1, "#4A9B87"]]

    for course in COURSES:
        hdf, month_order, order = heatmap_data_by_course(scoped, course)
        if hdf.empty or not month_order:
            continue
        pivot_pct = hdf.pivot(index='bucket', columns='month', values='retained_pct').reindex(order).reindex(columns=month_order)
        pivot_n = hdf.pivot(index='bucket', columns='month', values='n').reindex(order).reindex(columns=month_order)
        pivot_ref = hdf.pivot(index='bucket', columns='month', values='refunded').reindex(order).reindex(columns=month_order)

        text_matrix = []
        for bucket in pivot_pct.index:
            row_text = []
            for m in pivot_pct.columns:
                n = pivot_n.loc[bucket, m]
                p = pivot_pct.loc[bucket, m]
                r = pivot_ref.loc[bucket, m]
                row_text.append("" if pd.isna(n) else f"n={int(n)}<br><b>{p}% retained</b><br>{int(r)} refunded")
            text_matrix.append(row_text)

        fig = go.Figure(data=go.Heatmap(
            z=pivot_pct.values, x=list(pivot_pct.columns), y=list(pivot_pct.index),
            colorscale=HEAT_SCALE, zmin=0, zmax=100,
            text=text_matrix, texttemplate="%{text}", textfont={"size": 10, "color": "white"},
            hoverinfo='skip', showscale=False,
        ))
        fig.update_layout(height=max(220, 34 * len(pivot_pct.index)), margin=dict(t=30, b=10, l=10, r=10), title=course)
        st.plotly_chart(fig, use_container_width=True)

st.divider()
with st.expander("Data notes / known limitations"):
    st.markdown(f"""
- **Retained** now comes from the sheet's own `Retained/Move` column — "Retained" counts as retained;
  "No" and "Move" both count as not retained (a Move means the learner left that cohort, so it's treated
  the same as a drop even though it isn't a formal refund).
- **Agentic/LevelUp crossover**: learners who did both AI and IP (Edgeup) are counted only once, under
  whichever program they started with — {_crossover_removed} duplicate row(s) were excluded this load.
  See the sidebar Data health check for the current count.
- Course/track is derived from which worksheet a row is in plus its cohort name, since the
  sheet's own free-text Category/Course columns are only populated on refunded rows.
- **Active Cases** has no dedicated status column in this data — left empty until that source
  is connected.
- **Closed vs Open** cohort = whether the cohort's payment deadline has passed as of today
  ({today.date()}).
- A window's "all data" = cohorts that started in that window, **plus** any learner refunded
  in that window even if they enrolled earlier — so refund activity is never missed just
  because the enrollment happened outside the selected dates.
""")
