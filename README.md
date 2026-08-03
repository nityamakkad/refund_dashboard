# Refund & Retention Review — Streamlit dashboard

This reads learner-level data straight from a Google Sheet (one worksheet
per course) and rebuilds every table/chart live — no manual CSV uploads.

## What you need before starting
- A Google account with access to Google Cloud Console (free, no billing needed for this).
- The Google Sheet that will hold the data (see "Sheet format" below).
- Python 3.9+ on your machine (or a place to deploy, like Streamlit Community Cloud).

---

## Step 1 — Set up the Google Sheet

Create one Google Sheet with **4 worksheets** (tabs at the bottom), named exactly:

```
Agentic
FDE
Switch-Up
LevelUp
```

Each worksheet's first row must be headers matching your existing raw export
columns exactly (same names you already use: `hubspot_id`, `cohort_name`,
`cohort_start_date`, `Payment Mode`, `Refunded`, `Refund date`,
`High-Risk Flag`, etc. — the same columns as the CSVs you sent me).

Each week, just **add new rows** to the relevant worksheet(s). Nothing else
needs to change — the app re-reads the sheet fresh (see Step 5).

---

## Step 2 — Create a Google Cloud service account (one-time)

This gives the app a "robot" identity that can read your Sheet without using
your personal login.

1. Go to https://console.cloud.google.com/ and create a new project (or use an existing one).
2. In the search bar, go to **APIs & Services → Library**, search for
   **"Google Sheets API"**, and click **Enable**.
3. Go to **APIs & Services → Credentials → Create Credentials → Service account**.
4. Give it any name (e.g. `refund-dashboard-reader`), click through the
   defaults, and click **Done**.
5. Click on the service account you just created → **Keys** tab → **Add Key
   → Create new key → JSON**. This downloads a `.json` file — keep it safe,
   it's a credential.
6. Open that JSON file in a text editor. You'll paste its contents into
   `secrets.toml` in Step 4.

---

## Step 3 — Share the Sheet with the service account

1. Open the downloaded JSON file and find the `client_email` field — it
   looks like `something@your-project.iam.gserviceaccount.com`.
2. Open your Google Sheet → **Share** → paste that email address → give it
   **Viewer** access → Send.

Without this step the app will fail to read the Sheet, even with valid credentials.

---

## Step 4 — Configure the app's secrets

1. In the project folder, create a folder named `.streamlit` and a file
   inside it named `secrets.toml` (copy `secrets.toml.example` as a starting
   point and rename it).
2. Fill in:
   - `SHEET_URL_OR_KEY` — paste the full URL of your Google Sheet.
   - `[gcp_service_account]` — paste in every field from the JSON key file
     from Step 2 (same field names, just reformatted as shown in the example).
   - `OPENAI_API_KEY` — optional, only if you want the "AI insights" buttons
     to work. Get one from https://platform.openai.com/api-keys.

**Important:** `secrets.toml` contains real credentials — never share it,
email it, or commit it to a public repo.

---

## Step 5 — Run it

### Option A — On your own machine
```bash
pip install -r requirements.txt
streamlit run app.py
```
This opens the dashboard in your browser at `http://localhost:8501`.
It stays running as long as your terminal/machine does.

### Option B — Deploy for free so anyone on your team can open a link
1. Push this folder to a **private** GitHub repo (don't include
   `.streamlit/secrets.toml` — add it to `.gitignore`).
2. Go to https://share.streamlit.io/, sign in, click **New app**, point it
   at your repo and `app.py`.
3. In the app's **Settings → Secrets**, paste the same contents you put in
   `secrets.toml` locally.
4. Deploy — you'll get a permanent URL to bookmark and share with your
   manager/team.

---

## Using it week to week
- Add new rows to the relevant worksheet(s) in the Google Sheet — that's the
  only "update" step.
- In the app, click **"🔄 Refresh from Google Sheets"** in the sidebar (it
  also auto-refreshes every 5 minutes on its own).
- Use the **Weekly / Monthly** toggle and the date picker to pick the
  Tue→Mon window or month you want to review.

## What's still a placeholder
- The **Active Cases** tab is intentionally empty — it needs a separate
  case-tracking data source, which isn't wired in yet.
- **Closed vs open cohort** = whether the payment deadline has passed as of
  today's date.
