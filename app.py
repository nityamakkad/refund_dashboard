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
}

NAVY, GREEN, AMBER, RED = "#0B2A4A", "#1E8E5A", "#D6971F", "#C63C3C"

st.set_page_config(page_title="Refund & Retention Review", layout="wide")

st.markdown("""
<style>
.kpi-row { display:flex; gap:14px; flex-wrap:wrap; margin: 4px 0 20px; }
.kpi-card { flex:1; min-width:170px; background:#fff; border-radius:12px; padding:16px 20px;
  border:1px solid #E1E4E9; box-shadow:0 1px 4px rgba(15,42,74,0.07); position:relative; overflow:hidden; }
.kpi-card:before { content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:var(--accent,#0B2A4A); }
.kpi-label { font-size:11px; letter-spacing:.5px; text-transform:uppercase; color:#6B7684; font-weight:700; }
.kpi-value { font-size:32px; font-weight:800; margin-top:4px; font-variant-numeric:tabular-nums; line-height:1.1; }
.kpi-sub { font-size:12px; color:#8A93A0; margin-top:3px; }
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


def kpi_card(label, value, color=NAVY, sub=""):
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return f'<div class="kpi-card" style="--accent:{color}"><div class="kpi-label">{label}</div><div class="kpi-value" style="color:{color}">{value}</div>{sub_html}</div>'


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
            load_report.append({"course": course, "tab": ws_name, "rows_found": 0, "note": "TAB NOT FOUND — check exact name"})
            continue
        records = ws.get_all_records()
        if not records:
            load_report.append({"course": course, "tab": ws_name, "rows_found": 0, "note": "Tab found but empty"})
            continue
        df = normalize_columns(pd.DataFrame(records))
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

# ============================================================
# SIDEBAR CONTROLS
# ============================================================
st.sidebar.title("Refund & Retention Review")
st.sidebar.caption(f"Reference date: {today.date()}  ·  {len(df_all)} learner rows loaded")

with st.sidebar.expander("Data health check"):
    st.dataframe(pd.DataFrame(load_report), hide_index=True, use_container_width=True)
    st.caption("If a course shows 0 rows or 'not found', its tab name or headers don't match — fix the tab name/header text in the Sheet rather than editing code.")

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


def retention_matrix(rows, group_col='course_group'):
    out = []
    for g, gdf in rows.groupby(group_col):
        all_total, all_ref = 0, 0
        for cat in ['Upfront', 'Non-Upfront', 'Flexipay']:
            c = gdf[gdf['payment_category'] == cat]
            total, refunded = len(c), int(c['is_refunded'].sum())
            all_total += total; all_ref += refunded
            out.append({'Course': g, 'Category': cat, 'Total': total, 'Retained': total - refunded,
                        'Retained %': pct(total - refunded, total), 'Refunded': refunded, 'Refund %': pct(refunded, total)})
        out.append({'Course': g, 'Category': 'All categories', 'Total': all_total, 'Retained': all_total - all_ref,
                    'Retained %': pct(all_total - all_ref, all_total), 'Refunded': all_ref, 'Refund %': pct(all_ref, all_total)})
    return pd.DataFrame(out)


def cohort_summary(rows):
    out = []
    for (course, cohort), g in rows.groupby(['course_group', 'cohort_name']):
        up = g[g['payment_category'] == 'Upfront']; fp = g[g['payment_category'] == 'Flexipay']; nu = g[g['payment_category'] == 'Non-Upfront']
        out.append({
            'Course': course, 'Cohort': cohort,
            'Start': g['cohort_start_date'].iloc[0].date() if pd.notna(g['cohort_start_date'].iloc[0]) else None,
            'Deadline': g['payment_deadline'].iloc[0].date() if pd.notna(g['payment_deadline'].iloc[0]) else None,
            'Status': g['cohort_status'].iloc[0],
            'Total': len(g), 'Refunds': int(g['is_refunded'].sum()),
            'Upfront': len(up), 'Upfront Rfd': int(up['is_refunded'].sum()),
            'Flexipay': len(fp), 'Flexipay Rfd': int(fp['is_refunded'].sum()),
            'Non-Upfront': len(nu), 'Non-Upf Rfd': int(nu['is_refunded'].sum()),
        })
    return pd.DataFrame(out).sort_values('Start') if out else pd.DataFrame()


def payment_method_table(rows):
    out = []
    for (course, method), g in rows.groupby(['course_group', 'payment_method_clean']):
        refunded = int(g['is_refunded'].sum())
        out.append({'Course': course, 'Method': method, 'Total': len(g), 'Retained': len(g) - refunded,
                    'Retained %': pct(len(g) - refunded, len(g)), 'Refunded': refunded})
    return pd.DataFrame(out).sort_values(['Course', 'Total'], ascending=[True, False]) if out else pd.DataFrame()


def heatmap_data(rows):
    order = ["All 3", "Orientation & F", "O & F", "O & O", "First Class", "Orientation", "Onboarding", "None", "Unknown"]
    out = []
    for (course, bucket), g in rows.groupby(['course_group', 'risk_bucket']):
        refunded = int(g['is_refunded'].sum())
        out.append({'course': course, 'bucket': bucket, 'n': len(g), 'retained_pct': pct(len(g) - refunded, len(g)), 'refunded': refunded})
    return pd.DataFrame(out), order


def subcat_counts(rows):
    vc = rows['refund_category'].fillna('Uncategorized').value_counts()
    return pd.DataFrame({'Sub-category': vc.index, 'Count': vc.values})


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
refund_pct = pct(refunds, total)
cards = [
    kpi_card("Enrollments in window", total),
    kpi_card("Refunds in window", refunds, color=RED if (refund_pct or 0) > 15 else NAVY),
    kpi_card("Refund rate", f"{refund_pct}%" if total else "—", color=ret_color(100 - (refund_pct or 0)) if total else "#9AA3AE"),
]
st.markdown(f'<div class="kpi-row">{"".join(cards)}</div>', unsafe_allow_html=True)

tabs = st.tabs([
    "Cohort Summary", "Retention Matrix", "Cohort Calendar", "Refund Sub-categories",
    "Refunds — Detail", "Active Cases", "Payment Method", "Engagement Heatmap",
])

# ---- Cohort Summary ----
with tabs[0]:
    st.subheader("Cohort-wise enrollments & refunds")
    data = cohort_summary(scoped)
    st.dataframe(data, use_container_width=True, hide_index=True)
    if not data.empty:
        chart_df = scoped.groupby('course_group').agg(
            Total=('hubspot_id', 'count'), Refunds=('is_refunded', 'sum')
        ).reset_index().rename(columns={'course_group': 'Course'})
        chart_long = chart_df.melt(id_vars='Course', value_vars=['Total', 'Refunds'], var_name='Metric', value_name='Count')
        fig = px.bar(chart_long, x='Course', y='Count', color='Metric', barmode='group',
                     color_discrete_map={'Total': NAVY, 'Refunds': RED})
        fig.update_layout(height=340, margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
    ai_insight(data.to_dict('records') if not data.empty else [], "cohort summary", "cohort")

# ---- Retention Matrix ----
with tabs[1]:
    st.subheader("Retention matrix — Upfront / Non-Upfront / Flexipay / All")
    data = retention_matrix(scoped)
    st.dataframe(data, use_container_width=True, hide_index=True)
    if not data.empty:
        fig = px.bar(data[data['Category'] == 'All categories'], x='Course', y='Retained %',
                     color='Retained %', color_continuous_scale=[RED, AMBER, GREEN], range_color=[60, 100])
        fig.update_layout(height=340, margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
    ai_insight(data.to_dict('records') if not data.empty else [], "retention matrix", "retention")
    with st.expander(f"Show raw data ({len(scoped)} rows)"):
        st.dataframe(scoped[['hubspot_id', 'course_group', 'cohort_name', 'cohort_start_date',
                              'payment_category', 'payment_method_clean', 'is_refunded', 'engagement_level']],
                     use_container_width=True, hide_index=True)

# ---- Cohort Calendar ----
with tabs[2]:
    st.subheader("Cohort calendar — payment deadlines & trial window")
    rows_ = []
    for (course, cohort), g in scoped.groupby(['course_group', 'cohort_name']):
        rows_.append({
            'Course': course, 'Cohort': cohort,
            'Start': g['cohort_start_date'].iloc[0].date() if pd.notna(g['cohort_start_date'].iloc[0]) else None,
            'Deadline': g['payment_deadline'].iloc[0].date() if pd.notna(g['payment_deadline'].iloc[0]) else None,
            'Trial: Yes': int((g['trial_window'] == 'Yes').sum()),
            'Trial: No': int((g['trial_window'] == 'No').sum()),
            'Not Marked': int((g['trial_window'] == 'Not Marked').sum()),
        })
    cal_df = pd.DataFrame(rows_).sort_values('Deadline') if rows_ else pd.DataFrame()
    st.dataframe(cal_df, use_container_width=True, hide_index=True)

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
    st.dataframe(data, use_container_width=True, hide_index=True)
    if not data.empty:
        fig = px.bar(data, x='Method', y='Total', color='Course', barmode='group')
        fig.update_layout(height=340, margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
    ai_insight(data.to_dict('records') if not data.empty else [], "payment method table", "paymethod")

# ---- Engagement Heatmap ----
with tabs[7]:
    st.subheader("Engagement category heatmap")
    hdf, order = heatmap_data(scoped)
    if hdf.empty:
        st.caption("No data in this window.")
    else:
        courses = sorted(hdf['course'].unique())
        pivot_pct = hdf.pivot(index='bucket', columns='course', values='retained_pct').reindex(order)
        pivot_n = hdf.pivot(index='bucket', columns='course', values='n').reindex(order)
        pivot_ref = hdf.pivot(index='bucket', columns='course', values='refunded').reindex(order)

        text_matrix = []
        for bucket in pivot_pct.index:
            row_text = []
            for c in pivot_pct.columns:
                n = pivot_n.loc[bucket, c]
                p = pivot_pct.loc[bucket, c]
                r = pivot_ref.loc[bucket, c]
                if pd.isna(n):
                    row_text.append("")
                else:
                    row_text.append(f"n={int(n)}<br>{p}% retained<br>{int(r)} refunded")
            text_matrix.append(row_text)

        fig = go.Figure(data=go.Heatmap(
            z=pivot_pct.values, x=list(pivot_pct.columns), y=list(pivot_pct.index),
            colorscale=[[0, RED], [0.5, AMBER], [1, GREEN]], zmin=0, zmax=100,
            text=text_matrix, texttemplate="%{text}", textfont={"size": 12},
            hoverinfo='skip', showscale=False,
        ))
        fig.update_layout(height=480, margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Green = high retention, red = high refund risk. Each cell shows total learners (n), retained %, and refund count for that engagement category.")

st.divider()
with st.expander("Data notes / known limitations"):
    st.markdown(f"""
- Retention/drop/move column intentionally excluded — pending, will be added later.
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
