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
from datetime import date, timedelta, datetime

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
WORKSHEET_NAMES = {
    "Agentic": "Agentic",
    "FDE": "FDE",
    "Switch-Up": "Switch-Up",
    "LevelUp": "LevelUp",
}
CACHE_TTL_SECONDS = 300  # re-read Sheets at most every 5 minutes

PAYMENT_MODE_MAP = {
    'ACH_TRANSFER': 'ACH', 'CLIMBCREDIT': 'Climb', 'STRIPE_CARD': 'Stripe',
    'STRIPE_MANUAL_PAYMENT': 'Stripe', 'PAYPAL': 'PayPal', 'KLARNA': 'Klarna',
    '0_PERCENT_APR_FLEXIPAY': 'Flexipay', '0% APR FLEXIPAY': 'Flexipay', 'Flexipay': 'Flexipay',
    'BRAINTREE': 'Braintree', 'AFFIRM_VIA_STRIPE': 'Affirm', 'BANK_TRANSFER': 'Bank Transfer',
    'RAZORPAY_MANUAL_PAYMENT': 'Razorpay', 'Invalid': 'Unknown/Invalid', 'Not Updated': 'Unknown/Invalid',
}

st.set_page_config(page_title="Refund & Retention Review", layout="wide")

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
    for course, ws_name in WORKSHEET_NAMES.items():
        ws = sh.worksheet(ws_name)
        records = ws.get_all_records()
        if not records:
            continue
        df = pd.DataFrame(records)
        if 'Retained/drop\n/move' in df.columns:
            df.rename(columns={'Retained/drop\n/move': 'Retained'}, inplace=True)
        df['course_group'] = course
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


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
    # Refund date sometimes MM/DD/YYYY, sometimes ISO — try both
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


def week_start_of(d: pd.Timestamp) -> pd.Timestamp:
    days_since_tue = (d.dayofweek - 1) % 7  # Tue=0
    return d - pd.Timedelta(days=int(days_since_tue))


# ============================================================
# LOAD + CLEAN
# ============================================================
today = pd.Timestamp(datetime.now().date())
raw = load_raw_from_sheets()

if raw.empty:
    st.error(
        "No data loaded from Google Sheets. Check `SHEET_URL_OR_KEY` in secrets, "
        "the worksheet names in `WORKSHEET_NAMES`, and that the sheet is shared "
        "with the service account email. See README.md."
    )
    st.stop()

df_all = clean(raw, today)

# ============================================================
# SIDEBAR CONTROLS
# ============================================================
st.sidebar.title("Refund & Retention Review")
st.sidebar.caption(f"Reference date: {today.date()}  ·  {len(df_all)} learner rows loaded")

mode = st.sidebar.radio("View", ["Weekly", "Monthly"], horizontal=True)

min_date = df_all['cohort_start_date'].min()
max_date = df_all['cohort_start_date'].max()

if mode == "Weekly":
    days_since_tue_today = (today.dayofweek - 1) % 7
    default_week_start = (today - pd.Timedelta(days=int(days_since_tue_today) + 7)).date()
    week_start_input = st.sidebar.date_input("Week starts (Tuesday)", value=default_week_start)
    range_start = pd.Timestamp(week_start_input)
    range_end = range_start + pd.Timedelta(days=6)
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
# FILTER
# ============================================================
def in_course(d):
    return d if course_sel == "All" else d[d['course_group'] == course_sel]

scoped = in_course(df_all)
scoped = scoped[(scoped['cohort_start_date'] >= range_start) & (scoped['cohort_start_date'] <= range_end)]

refund_scoped = in_course(df_all)
refund_scoped = refund_scoped[
    refund_scoped['is_refunded'] & refund_scoped['refund_date'].notna() &
    (refund_scoped['refund_date'] >= range_start) & (refund_scoped['refund_date'] <= range_end)
]

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
k1, k2, k3 = st.columns(3)
total = len(scoped)
refunds = int(scoped['is_refunded'].sum())
k1.metric("Enrollments in window", total)
k2.metric("Refunds in window", refunds)
k3.metric("Refund rate", f"{pct(refunds, total)}%" if total else "—")

tabs = st.tabs([
    "Cohort Summary", "Retention Matrix", "Cohort Calendar", "Refund Sub-categories",
    "Refunds — Detail", "Active Cases", "Payment Method", "Engagement Heatmap",
])

# ---- Cohort Summary ----
with tabs[0]:
    st.subheader("Cohort-wise enrollments & refunds")
    data = cohort_summary(scoped)
    st.dataframe(data, use_container_width=True, hide_index=True)
    ai_insight(data.to_dict('records') if not data.empty else [], "cohort summary", "cohort")
    with st.expander(f"Show raw data ({len(scoped)} rows)"):
        st.dataframe(scoped[['hubspot_id', 'course_group', 'cohort_name', 'cohort_start_date',
                              'payment_category', 'payment_method_clean', 'is_refunded', 'engagement_level']],
                     use_container_width=True, hide_index=True)

# ---- Retention Matrix ----
with tabs[1]:
    st.subheader("Retention matrix — Upfront / Non-Upfront / Flexipay / All")
    data = retention_matrix(scoped)
    st.dataframe(data, use_container_width=True, hide_index=True)
    if not data.empty:
        fig = px.bar(data[data['Category'] == 'All categories'], x='Course', y='Retained %',
                     color='Retained %', color_continuous_scale=['#C63C3C', '#D6971F', '#1E8E5A'],
                     range_color=[60, 100])
        st.plotly_chart(fig, use_container_width=True)
    ai_insight(data.to_dict('records') if not data.empty else [], "retention matrix", "retention")

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

# ---- Refunds Detail (last week / selected window) ----
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
        pivot = hdf.pivot(index='bucket', columns='course', values='retained_pct').reindex(order)
        fig = go.Figure(data=go.Heatmap(
            z=pivot.values, x=pivot.columns, y=pivot.index,
            colorscale=[[0, '#C63C3C'], [0.5, '#D6971F'], [1, '#1E8E5A']],
            zmin=0, zmax=100, text=pivot.values, texttemplate="%{text}%",
        ))
        fig.update_layout(height=450)
        st.plotly_chart(fig, use_container_width=True)
        n_pivot = hdf.pivot(index='bucket', columns='course', values='n').reindex(order)
        st.caption("Cell counts (n):")
        st.dataframe(n_pivot, use_container_width=True)

st.divider()
with st.expander("Data notes / known limitations"):
    st.markdown("""
- Retention/drop/move column intentionally excluded — pending, will be added later.
- Course/track is derived from which worksheet a row is in plus its cohort name, since the
  sheet's own free-text Category/Course columns are only populated on refunded rows.
- **Active Cases** has no dedicated status column in this data — left empty until that source
  is connected.
- **Closed vs Open** cohort = whether the cohort's payment deadline has passed as of today
  ({today}).
""".format(today=today.date()))
