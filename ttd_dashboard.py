# ttd_dashboard.py
"""
Streamlit dashboard for Tirumala Tirupati Devasthanam pilgrim statistics.

Reads the uploaded JSON at /mnt/data/darshan_data.json (adjust path if needed)
and provides interactive charts to explore pilgrim_count by day / week / month / year.

Usage:
    pip install streamlit pandas plotly numpy
    streamlit run ttd_dashboard.py
"""

from pathlib import Path
import json
import re
from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px

# ---------------------------
# Configuration / constants
# ---------------------------
JSON_PATH = Path("full_run_1/full_run_1/darshan_data.json")  # adjust if you moved the file

st.set_page_config(page_title="TTD Pilgrim Dashboard", layout="wide")

# ---------------------------
# Utilities: parsing helpers
# ---------------------------
def safe_int(x):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return np.nan

def parse_hundi_val(val):
    """
    Parse hundi_kanukalu-like strings into numeric rupees.
    Examples:
        "Rs.4.02 Cr" -> 4.02 * 1e7
        "4.40"        -> treat as crores (if ambiguous), or if digits only and large treat as rupees
        "43600000"    -> 43600000
    Heuristics:
      - if 'Cr' or 'crore' present -> multiply by 1e7
      - if 'Lac' or 'Lakh' present -> multiply by 1e5
      - if value is an integer string > 1e6 treat as rupees
      - otherwise return NaN
    """
    if val is None:
        return np.nan
    if isinstance(val, (int, float, np.integer, np.floating)):
        return float(val)
    s = str(val).strip()
    # remove commas and ₹/Rs/. etc
    s = s.replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    s_lower = s.lower()
    # find numeric part
    m = re.search(r"([-+]?\d*\.?\d+)", s_lower)
    if not m:
        return np.nan
    num = float(m.group(1))
    if "cr" in s_lower or "crore" in s_lower or "cr." in s_lower:
        return num * 1e7
    if "lakh" in s_lower or "lac" in s_lower or "lacs" in s_lower:
        return num * 1e5
    # some cleaned values like "4.40" in many entries could mean crores; we'll apply small heuristic:
    # if original string includes non-digit chars (like 'Rs' removed above) but no unit, assume crores if num < 100
    if num <= 100 and any(c.isalpha() for c in s):
        # treat as crores
        return num * 1e7
    # if numeric and large (>1e6) treat as rupees already
    if num >= 1e6:
        return num
    # otherwise ambiguous: return num * 1e7 if value within 0-100 and likely crores (but be conservative)
    return np.nan

def safe_get_other_metric(metrics, key_candidates):
    """
    Look for a key in other_metrics where keys may vary in capitalization/format.
    key_candidates is list of possible names like ['tonsures'] or ['hundi_kanukalu','hundi']
    """
    if not isinstance(metrics, dict):
        return None
    lower_map = {k.lower().replace(" ", "_"): v for k, v in metrics.items()}
    for cand in key_candidates:
        k = cand.lower().replace(" ", "_")
        if k in lower_map:
            return lower_map[k]
    return None

# ---------------------------
# Load & normalize JSON -> DataFrame
# ---------------------------
@st.cache_data(ttl=3600)
def load_and_prepare(json_path: Path):
    if not json_path.exists():
        st.error(f"JSON file not found at {json_path}. Upload or fix path.")
        return pd.DataFrame()
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    rows = []
    for date_key, obj in raw.items():
        # date_key sometimes malformed (e.g., '2017-02-31' in file). We'll try to parse,
        # otherwise set pd.NaT and keep original string.
        row = {}
        row["date_key"] = str(date_key)
        # attempt to parse date in common formats (YYYY-MM-DD)
        try:
            dt = pd.to_datetime(date_key, format="%Y-%m-%d", errors="coerce")
            # try also some other format attempts if needed
            if pd.isna(dt):
                dt = pd.to_datetime(date_key, errors="coerce")
            row["date"] = dt
        except Exception:
            row["date"] = pd.NaT
        # extract pilgrim_count if present
        pilgrim_count = None
        try:
            pilgrim_count = obj.get("data", {}).get("pilgrim_count", None)
        except Exception:
            pilgrim_count = None
        row["pilgrim_count"] = safe_int(pilgrim_count)
        # extract other_metrics keys: tonsures, hundi_kanukalu (donations), waiting_compartments etc.
        other = obj.get("data", {}).get("other_metrics", {}) if isinstance(obj.get("data", {}), dict) else {}
        row["tonsures"] = safe_int(safe_get_other_metric(other, ["tonsures", "tonsure"]))
        hundi_raw = safe_get_other_metric(other, ["hundi_kanukalu", "hundi", "hundi_kanukalu "])
        row["hundi_raw"] = hundi_raw
        row["hundi_rs"] = parse_hundi_val(hundi_raw)
        row["waiting_compartments"] = safe_get_other_metric(other, ["waiting_compartments", "waiting_compartments "])
        # store title/post/article_id if available for linking/backreference
        row["title"] = obj.get("title") if isinstance(obj.get("title"), str) else None
        row["post"] = obj.get("post")
        rows.append(row)
    df = pd.DataFrame(rows)
    # create useful columns for grouping
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    # keep a 'date_label' string for rows where date parsing failed
    df["date_label"] = df.apply(lambda r: r["date"].date().isoformat() if pd.notna(r["date"]) else r["date_key"], axis=1)
    # add year, month, week
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.to_period("M").astype(str)  # e.g., '2022-03'
    df["week"] = df["date"].dt.to_period("W").astype(str)  # e.g., '2022-03/2022-03-06' style, but string is ok
    # sort by date when possible, keeping NaT at end
    df = df.sort_values(by=["date", "date_key"], na_position="last").reset_index(drop=True)
    return df

df = load_and_prepare(JSON_PATH)

# Quick note + citation
st.title("Tirumala Tirupathi Pilgrim Dashboard")
st.caption("Data loaded from uploaded JSON (pilgrim_count, tonsures, hundi_kanukalu etc.). See source file preview. :contentReference[oaicite:1]{index=1}")

if df.empty:
    st.stop()

# ---------------------------
# Sidebar controls
# ---------------------------
st.sidebar.header("Controls")
group_by = st.sidebar.selectbox("Group / View by", ["Day", "Week", "Month", "Year"])
# date range selector: pick min/max from available parsed dates
min_date = df["date"].min()
max_date = df["date"].max()
if pd.notna(min_date) and pd.notna(max_date):
    date_range = st.sidebar.date_input("Date range (for time series)", value=(min_date.date(), max_date.date()))
else:
    # fallback: use first/last labels if dates not parsed
    date_range = None

# aggregation function
agg_func = st.sidebar.selectbox("Aggregation for grouped charts", ["sum", "mean", "median"])

# moving average toggle
show_ma = st.sidebar.checkbox("Show moving average (7-day)", value=True)

# optional filters: allow user to filter by year/month/week selections
unique_years = sorted(df["year"].dropna().astype(int).unique().tolist())
selected_years = st.sidebar.multiselect("Filter years (optional)", options=unique_years, default=unique_years)

# ---------------------------
# Filtering data
# ---------------------------
df_filtered = df.copy()
if selected_years:
    df_filtered = df_filtered[df_filtered["year"].isin(selected_years)]
# if date_range provided, filter
if date_range and len(date_range) == 2:
    start, end = pd.to_datetime(date_range[0]), pd.to_datetime(date_range[1])
    df_filtered = df_filtered[(df_filtered["date"] >= start) & (df_filtered["date"] <= end)]

# ---------------------------
# Aggregations for plotting
# ---------------------------
def aggregate_df(df_in, freq):
    """
    freq: 'D','W','M','Y' for Day/Week/Month/Year
    returns DataFrame with index as period and aggregated pilgrim_count and mean etc.
    """
    d = df_in.copy()
    if freq == "D":
        d2 = d.groupby("date").agg(
            pilgrims_sum=("pilgrim_count", "sum"),
            pilgrims_mean=("pilgrim_count", "mean"),
            tonsures_sum=("tonsures", "sum"),
            hundi_sum=("hundi_rs", "sum"),
            count_days=("pilgrim_count", "count"),
        ).reset_index()
        d2["period_label"] = d2["date"].dt.date.astype(str)
        d2 = d2.sort_values("date")
        return d2
    if freq == "W":
        d["week_label"] = d["date"].dt.to_period("W").astype(str)
        d2 = d.groupby("week_label").agg(
            pilgrims_sum=("pilgrim_count", "sum"),
            pilgrims_mean=("pilgrim_count", "mean"),
            tonsures_sum=("tonsures", "sum"),
            hundi_sum=("hundi_rs", "sum"),
            count_days=("pilgrim_count", "count"),
        ).reset_index()
        return d2
    if freq == "M":
        d["month_label"] = d["date"].dt.to_period("M").astype(str)
        d2 = d.groupby("month_label").agg(
            pilgrims_sum=("pilgrim_count", "sum"),
            pilgrims_mean=("pilgrim_count", "mean"),
            tonsures_sum=("tonsures", "sum"),
            hundi_sum=("hundi_rs", "sum"),
            count_days=("pilgrim_count", "count"),
        ).reset_index()
        return d2
    if freq == "Y":
        d["year_label"] = d["date"].dt.year
        d2 = d.groupby("year_label").agg(
            pilgrims_sum=("pilgrim_count", "sum"),
            pilgrims_mean=("pilgrim_count", "mean"),
            tonsures_sum=("tonsures", "sum"),
            hundi_sum=("hundi_rs", "sum"),
            count_days=("pilgrim_count", "count"),
        ).reset_index()
        return d2
    return pd.DataFrame()

freq_map = {"Day": "D", "Week": "W", "Month": "M", "Year": "Y"}
agg = aggregate_df(df_filtered, freq_map[group_by])

# ---------------------------
# Main layout with plots
# ---------------------------
left_col, right_col = st.columns([3, 1])

with left_col:
    st.subheader(f"Pilgrim count time series ({group_by})")
    if group_by == "Day":
        # daily time series
        ts = agg.copy()
        if ts.empty:
            st.info("No parsed dates available in the selected range. Try a different filter.")
        else:
            # choose metric for y-axis depending on aggregation selection
            y_col = "pilgrims_sum" if agg_func == "sum" else "pilgrims_mean"
            fig_ts = px.line(ts, x="date", y=y_col, markers=True, title="Pilgrim count over time")
            if show_ma and y_col in ts.columns:
                ts["ma7"] = ts[y_col].rolling(7, min_periods=1).mean()
                fig_ts.add_scatter(x=ts["date"], y=ts["ma7"], mode="lines", name="7-day MA")
            fig_ts.update_layout(xaxis_title="Date", yaxis_title="Pilgrim count")
            st.plotly_chart(fig_ts, use_container_width=True)
    else:
        # Week/Month/Year aggregated bars
        if agg.empty:
            st.info("No data to show for selected grouping and filters.")
        else:
            if group_by == "Week":
                x = "week_label"
            elif group_by == "Month":
                x = "month_label"
            else:
                x = "year_label"
            y_col = "pilgrims_sum" if agg_func == "sum" else "pilgrims_mean"
            fig_bar = px.bar(agg, x=x, y=y_col, title=f"Pilgrim count aggregated by {group_by.lower()} ({agg_func})")
            fig_bar.update_layout(xaxis_title=group_by, yaxis_title=("Total pilgrims" if agg_func=="sum" else "Avg pilgrims"))
            st.plotly_chart(fig_bar, use_container_width=True)

    # histogram / distribution
    st.subheader("Distribution of daily pilgrim counts")
    hist_df = df_filtered.dropna(subset=["pilgrim_count"])
    if hist_df.empty:
        st.write("No daily pilgrim_count data available in the filtered range.")
    else:
        fig_hist = px.histogram(hist_df, x="pilgrim_count", nbins=40, title="Histogram of daily pilgrim count")
        st.plotly_chart(fig_hist, use_container_width=True)

    # scatter: pilgrims vs tonsures
    st.subheader("Pilgrims vs Tonsures (scatter)")
    scatter_df = df_filtered.dropna(subset=["pilgrim_count", "tonsures"])
    if scatter_df.shape[0] >= 2:
        fig_scatter = px.scatter(scatter_df, x="tonsures", y="pilgrim_count", hover_data=["date_label"], trendline=None,
                                 title="Pilgrims vs Tonsures")
        st.plotly_chart(fig_scatter, use_container_width=True)
    else:
        st.write("Not enough data for tonsures vs pilgrims scatter (need both metrics).")

    # hundi / donations trend (if present)
    st.subheader("Hundi / Donations trend (converted to rupees)")
    hundi_df = df_filtered.dropna(subset=["hundi_rs"])
    if not hundi_df.empty:
        hundi_ts = hundi_df.sort_values("date")
        fig_hundi = px.line(hundi_ts, x="date", y="hundi_rs", title="Hundi (donations) over time (in rupees)", markers=True)
        fig_hundi.update_yaxes(tickformat=",.0f")
        st.plotly_chart(fig_hundi, use_container_width=True)
    else:
        st.write("No parsed hundi / donations numeric values found in this range.")

with right_col:
    st.subheader("Summary & Top days")
    total_pilgrims = int(df_filtered["pilgrim_count"].sum(min_count=1) or 0)
    avg_pilgrims = int(df_filtered["pilgrim_count"].mean()) if df_filtered["pilgrim_count"].count() else 0
    st.metric("Total pilgrims (filtered range)", f"{total_pilgrims:,}")
    st.metric("Average pilgrims (per entry)", f"{avg_pilgrims:,}")

    st.markdown("**Top 10 days by pilgrim_count**")
    top10 = df_filtered.dropna(subset=["pilgrim_count"]).sort_values("pilgrim_count", ascending=False).head(10)[
        ["date_label", "pilgrim_count", "tonsures", "hundi_raw", "post"]
    ]
    st.dataframe(top10.reset_index(drop=True).rename(columns={
        "date_label": "date",
        "pilgrim_count": "pilgrims",
        "tonsures": "tonsures",
        "hundi_raw": "hundi_raw",
        "post": "source_post"
    }))

# ---------------------------
# Data export & helpful info
# ---------------------------
st.markdown("---")
st.subheader("Download / Data & Notes")
st.write("You can download the filtered table shown below as CSV for further analysis.")
download_df = df_filtered.copy()
# Show a readable small table
st.dataframe(download_df[["date_label", "date", "pilgrim_count", "tonsures", "hundi_raw", "hundi_rs"]].rename(
    columns={"date_label": "date_str", "pilgrim_count": "pilgrims", "hundi_raw": "hundi_raw", "hundi_rs": "hundi_rupees"}
).reset_index(drop=True), height=250)

@st.cache_data
def convert_df_to_csv(dfi):
    return dfi.to_csv(index=False).encode('utf-8')

csv_bytes = convert_df_to_csv(download_df)
st.download_button("Download filtered data as CSV", data=csv_bytes, file_name="ttd_filtered.csv", mime="text/csv")

st.markdown("""
### Notes & caveats
- The JSON contains many date keys; some keys are not strictly valid calendar dates (e.g., `2017-02-31`) and are parsed conservatively. Rows with unparseable dates keep the original `date_key` in the `date_label` column.
- `hundi_kanukalu` values are parsed heuristically (supporting "Cr", "crore", "Lac/Lakh", plain numbers). If a value is ambiguous it may be left NaN — you can inspect `hundi_raw` for the raw string.
- The file you uploaded is the dashboard data source. Sample fields and entries from that file were used for parsing logic. :contentReference[oaicite:2]{index=2}
""")

