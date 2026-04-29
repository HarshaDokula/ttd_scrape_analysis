import argparse
import json
from pathlib import Path
from datetime import datetime

import pandas as pd


DEFAULT_DATA_FILES = [
    Path("full_run_1/darshan_data_full_run.json"),
    Path("darshan_data.json"),
]
OUTPUT_FILE = Path("darshan_visualization.html")


def find_default_data_file() -> Path:
    for path in DEFAULT_DATA_FILES:
        if path.exists():
            return path
    raise FileNotFoundError(
        "No darshan data file was found. Create darshan_data.json or use full_run_1/darshan_data_full_run.json."
    )


def load_darshan_data(data_path: Path) -> pd.DataFrame:
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    for date_key, record in data.items():
        try:
            date = datetime.strptime(date_key, "%Y-%m-%d")
        except ValueError:
            continue

        pilgrim_count = record.get("data", {}).get("pilgrim_count")
        try:
            pilgrim_count = int(pilgrim_count)
        except (TypeError, ValueError):
            pilgrim_count = None

        rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "year": date.year,
                "month": date.month,
                "year_month": date.strftime("%Y-%m"),
                "pilgrim_count": pilgrim_count,
                "article_id": record.get("article_id"),
                "title": record.get("title"),
                "post": record.get("post"),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"No valid darshan records found in {data_path}")
    return df.sort_values("date")


def build_highlights(df: pd.DataFrame) -> str:
    monthly_best = (
        df.loc[df.groupby("year_month")["pilgrim_count"].idxmax()]
        .sort_values(["year", "month"])
    )
    best_yearly_month = (
        monthly_best.loc[monthly_best.groupby("year")["pilgrim_count"].idxmax()]
        .sort_values("year")
    )

    month_rows = []
    for _, row in monthly_best.iterrows():
        month_rows.append(
            f"{row.year_month}: {row.pilgrim_count:,} pilgrims (best day: {row.date})"
        )

    year_rows = []
    for _, row in best_yearly_month.iterrows():
        year_rows.append(
            f"{row.year}: {row.year_month} with {row.pilgrim_count:,} pilgrims"
        )

    return "\n".join([
        "<h2>Highlights</h2>",
        "<h3>Highest-day record for each month</h3>",
        "<ul>",
        "".join(f'<li>{item}</li>' for item in month_rows),
        "</ul>",
        "<h3>Highest month in each year</h3>",
        "<ul>",
        "".join(f'<li>{item}</li>' for item in year_rows),
        "</ul>",
    ])


def render_output(df: pd.DataFrame, html_file: Path, data_file: Path) -> None:
    highlights_html = build_highlights(df)
    data_json = json.loads(json.dumps(df.to_dict(orient="records"), default=int))
    years = [int(y) for y in sorted(df["year"].unique())]
    year_months = [str(value) for value in sorted(df["year_month"].unique())]

    template = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <title>TTD Darshan Visualization</title>
  <script src=\"https://cdn.plot.ly/plotly-latest.min.js\"></script>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; }
    h1 { margin-bottom: 0.25rem; }
    section { margin-bottom: 2rem; }
    ul { margin-top: 0.5rem; }
    li { margin: 0.25rem 0; }
    label { margin-right: 0.5rem; font-weight: 600; }
    select { padding: 0.35rem 0.5rem; margin-right: 1rem; }
    #controls { margin-bottom: 1.25rem; }
    #tab-bar { margin-bottom: 1rem; }
    .tab-button { border: 1px solid #ccc; background: #f6f6f6; color: #333; padding: 0.5rem 1rem; cursor: pointer; margin-right: 0.5rem; border-radius: 4px; }
    .tab-button.active { background: #007bff; color: white; border-color: #007bff; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <section>
    <h1>TTD Darshan Visualization</h1>
    <p>This page shows pilgrim counts from the selected data source.</p>
    <p>Data file: <strong>%DATA_FILE%</strong></p>
    <div id=\"tab-bar\">
      <button id=\"tab-data\" class=\"tab-button active\">Data</button>
      <button id=\"tab-highlights\" class=\"tab-button\">Highlights</button>
    </div>
    <div id=\"controls\">
      <label for=\"year-select\">Year</label>
      <select id=\"year-select\"></select>
      <label for=\"month-select\">Month</label>
      <select id=\"month-select\"></select>
    </div>
  </section>
  <section id=\"chart-section\">
    <div id=\"chart\"></div>
  </section>
  <section id=\"highlights-section\" class=\"hidden\">
    %HIGHLIGHTS%
  </section>
  <script>
    const rows = %ROWS%;
    const years = %YEARS%;
    const yearMonths = %MONTHS%;

    const yearSelect = document.getElementById('year-select');
    const monthSelect = document.getElementById('month-select');
    const tabData = document.getElementById('tab-data');
    const tabHighlights = document.getElementById('tab-highlights');
    const chartSection = document.getElementById('chart-section');
    const highlightsSection = document.getElementById('highlights-section');

    function formatMonth(value) {
      const date = new Date(value + '-01');
      return date.toLocaleString('default', { month: 'short' }) + ' ' + date.getFullYear();
    }

    function populateYearOptions() {
      yearSelect.innerHTML = '<option value="all">All</option>' +
        years.map(year => `<option value="${year}">${year}</option>`).join('');
    }

    function populateMonthOptions(selectedYear = 'all') {
      const filtered = selectedYear === 'all'
        ? yearMonths
        : yearMonths.filter(value => value.startsWith(`${selectedYear}-`));
      monthSelect.innerHTML = ['<option value="all">All</option>']
        .concat(filtered.map(value => `<option value="${value}">${formatMonth(value)}</option>`))
        .join('');
    }

    function syncYearToMonth() {
      const selectedMonth = monthSelect.value;
      if (selectedMonth === 'all') {
        return;
      }
      const monthYear = selectedMonth.split('-')[0];
      if (yearSelect.value !== monthYear) {
        yearSelect.value = monthYear;
        populateMonthOptions(monthYear);
        monthSelect.value = selectedMonth;
      }
    }

    function filterRows() {
      const selectedYear = yearSelect.value;
      const selectedMonth = monthSelect.value;
      return rows.filter(row => {
        if (selectedYear !== 'all' && row.year.toString() !== selectedYear) return false;
        if (selectedMonth !== 'all' && row.year_month !== selectedMonth) return false;
        return true;
      });
    }

    function buildPlot() {
      const filtered = filterRows();
      const x = filtered.map(row => row.date);
      const y = filtered.map(row => row.pilgrim_count);

      const selectedYear = yearSelect.value;
      const selectedMonth = monthSelect.value;
      const titleParts = [];
      if (selectedYear !== 'all') titleParts.push(`Year ${selectedYear}`);
      if (selectedMonth !== 'all') titleParts.push(formatMonth(selectedMonth));
      const titleText = titleParts.length
        ? `Pilgrim counts for ${titleParts.join(' / ')}`
        : 'Pilgrim counts for all available dates';

      Plotly.react('chart', [{
        x,
        y,
        mode: 'lines+markers',
        marker: { size: 7 },
        hovertemplate: '%{x}: %{y:,} pilgrims<extra></extra>',
        line: { shape: 'linear' },
        name: 'Pilgrim Count',
      }], {
        title: titleText,
        xaxis: { title: 'Date', type: 'date' },
        yaxis: { title: 'Pilgrim count' },
        template: 'plotly_white',
        margin: { t: 80 },
      }, { responsive: true });
    }

    yearSelect.addEventListener('change', () => {
      populateMonthOptions(yearSelect.value);
      monthSelect.value = 'all';
      buildPlot();
    });
    monthSelect.addEventListener('change', () => {
      syncYearToMonth();
      buildPlot();
    });

    function showDataTab() {
      tabData.classList.add('active');
      tabHighlights.classList.remove('active');
      chartSection.classList.remove('hidden');
      highlightsSection.classList.add('hidden');
    }

    function showHighlightsTab() {
      tabHighlights.classList.add('active');
      tabData.classList.remove('active');
      highlightsSection.classList.remove('hidden');
      chartSection.classList.add('hidden');
    }

    tabData.addEventListener('click', showDataTab);
    tabHighlights.addEventListener('click', showHighlightsTab);

    window.addEventListener('DOMContentLoaded', () => {
      populateYearOptions();
      populateMonthOptions();
      showDataTab();
      buildPlot();
    });
  </script>
</body>
</html>
"""

    html = template.replace('%DATA_FILE%', str(data_file))
    html = html.replace('%HIGHLIGHTS%', highlights_html)
    html = html.replace('%ROWS%', json.dumps(data_json))
    html = html.replace('%YEARS%', json.dumps(years))
    html = html.replace('%MONTHS%', json.dumps(year_months))

    html_file.write_text(html, encoding="utf-8")
    print(f"Saved interactive visualization to {html_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate interactive TTD Darshan visualization.")
    parser.add_argument(
        "--data-file",
        type=Path,
        default=None,
        help="Path to the darshan JSON file. Defaults to full_run_1/darshan_data_full_run.json if available, otherwise darshan_data.json.",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=OUTPUT_FILE,
        help="Output HTML file path.",
    )
    args = parser.parse_args()

    data_file = args.data_file or find_default_data_file()
    df = load_darshan_data(data_file)
    render_output(df, args.output_file, data_file)


if __name__ == "__main__":
    main()
