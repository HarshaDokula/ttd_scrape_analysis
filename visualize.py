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


def render_output(df: pd.DataFrame, html_file: Path, data_file: Path) -> None:
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
    .month-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-top: 1rem; }
    .month-chart { border: 1px solid #ddd; border-radius: 4px; padding: 0.5rem; background: white; }
    .month-chart h3 { margin: 0 0 0.5rem 0; font-size: 1rem; text-align: center; }
  </style>
</head>
<body>
  <section>
    <h1>TTD Darshan Visualization</h1>
    <p>This page shows pilgrim counts from the selected data source.</p>
    <p>Data file: <strong>%DATA_FILE%</strong></p>
    <div id=\"tab-bar\">
      <button id=\"tab-month\" class=\"tab-button active\">Month</button>
      <button id=\"tab-year\" class=\"tab-button\">Year</button>
    </div>
    <div id=\"month-controls\" class=\"controls-section\">
      <label for=\"month-year-select\">Year</label>
      <select id=\"month-year-select\"></select>
      <label for=\"month-month-select\">Month</label>
      <select id=\"month-month-select\"></select>
    </div>
    <div id=\"year-controls\" class=\"controls-section hidden\">
      <label for=\"year-year-select\">Year</label>
      <select id=\"year-year-select\"></select>
    </div>
  </section>
  <section id=\"chart-section\">
    <div id=\"chart\"></div>
  </section>
  <section id=\"year-charts-section\" class=\"hidden\">
    <div id=\"year-charts\" class=\"month-grid\"></div>
  </section>
  <script>
    const rows = %ROWS%;
    const years = %YEARS%;
    const yearMonths = %MONTHS%;

    const tabMonth = document.getElementById('tab-month');
    const tabYear = document.getElementById('tab-year');
    const monthControls = document.getElementById('month-controls');
    const yearControls = document.getElementById('year-controls');
    const monthYearSelect = document.getElementById('month-year-select');
    const monthMonthSelect = document.getElementById('month-month-select');
    const yearYearSelect = document.getElementById('year-year-select');
    const chartSection = document.getElementById('chart-section');
    const yearChartsSection = document.getElementById('year-charts-section');
    const yearChartsDiv = document.getElementById('year-charts');

    function formatMonth(value) {
      const date = new Date(value + '-01');
      return date.toLocaleString('default', { month: 'short' }) + ' ' + date.getFullYear();
    }

    function populateMonthYearOptions() {
      // Only show years that have month data
      const yearsWithData = [...new Set(yearMonths.map(ym => ym.split('-')[0]))];
      monthYearSelect.innerHTML = yearsWithData.map(year => `<option value=\"${year}\">${year}</option>`).join('');
      if (yearsWithData.length > 0) {
        monthYearSelect.value = yearsWithData[0];
        populateMonthMonthOptions(yearsWithData[0]);
      }
    }

    function populateMonthMonthOptions(selectedYear) {
      const filtered = yearMonths.filter(value => value.startsWith(`${selectedYear}-`));
      monthMonthSelect.innerHTML = filtered.map(value => `<option value=\"${value}\">${formatMonth(value)}</option>`).join('');
      if (filtered.length > 0) {
        monthMonthSelect.value = filtered[0];
      }
    }

    function populateYearYearOptions() {
      yearYearSelect.innerHTML = years.map(year => `<option value=\"${year}\">${year}</option>`).join('');
      yearYearSelect.value = years[0];
    }

    function getMonthData(yearMonth) {
      return rows.filter(row => row.year_month === yearMonth);
    }

    function getYearData(year) {
      return rows.filter(row => row.year.toString() === year);
    }

    function buildMonthChart() {
      const selectedMonth = monthMonthSelect.value;
      const data = getMonthData(selectedMonth);
      
      if (data.length === 0) {
        Plotly.react('chart', [], { title: 'No data available' }, { responsive: true });
        return;
      }

      const maxCount = Math.max(...data.map(d => d.pilgrim_count));
      const x = data.map(row => row.date);
      const y = data.map(row => row.pilgrim_count);
      const colors = data.map(row => row.pilgrim_count === maxCount ? '#ff6b6b' : '#007bff');

      Plotly.react('chart', [{
        x,
        y,
        type: 'bar',
        marker: { color: colors },
        hovertemplate: '%{x}: %{y:,} pilgrims<extra></extra>',
        name: 'Pilgrim Count',
      }], {
        title: `Pilgrim counts for ${formatMonth(selectedMonth)} (Highest: ${maxCount.toLocaleString()})`,
        xaxis: { title: 'Date', type: 'date' },
        yaxis: { title: 'Pilgrim count' },
        template: 'plotly_white',
        margin: { t: 80 },
      }, { responsive: true });
    }

    function buildYearCharts() {
      const selectedYear = String(yearYearSelect.value);  // Ensure it's a string
      
      // Generate all 12 months for the selected year
      const allMonths = [];
      for (let month = 1; month <= 12; month++) {
        const monthStr = String(month).padStart(2, '0');
        allMonths.push(`${selectedYear}-${monthStr}`);
      }
      
      yearChartsDiv.innerHTML = '';
      
      allMonths.forEach(month => {
        const monthData = rows.filter(row => row.year_month === month);
        
        const chartDiv = document.createElement('div');
        chartDiv.className = 'month-chart';
        chartDiv.innerHTML = `<h3>${formatMonth(month)}</h3><div id=\"chart-${month.replace('-', '')}\"></div>`;
        yearChartsDiv.appendChild(chartDiv);
        
        if (monthData.length === 0) {
          // Show empty chart for months with no data
          Plotly.newPlot(`chart-${month.replace('-', '')}`, [], {
            title: 'No data',
            xaxis: { title: 'Date', type: 'date', tickformat: '%d' },
            yaxis: { title: 'Pilgrims' },
            template: 'plotly_white',
            margin: { t: 40, b: 40, l: 40, r: 20 },
            height: 250,
          }, { responsive: true });
          return;
        }
        
        const maxCount = Math.max(...monthData.map(d => d.pilgrim_count));
        const x = monthData.map(row => row.date);
        const y = monthData.map(row => row.pilgrim_count);
        const colors = monthData.map(row => row.pilgrim_count === maxCount ? '#ff6b6b' : '#007bff');

        Plotly.newPlot(`chart-${month.replace('-', '')}`, [{
          x,
          y,
          type: 'bar',
          marker: { color: colors },
          hovertemplate: '%{x}: %{y:,} pilgrims<extra></extra>',
          name: 'Pilgrim Count',
        }], {
          title: `Max: ${maxCount.toLocaleString()}`,
          xaxis: { title: 'Date', type: 'date', tickformat: '%d' },
          yaxis: { title: 'Pilgrims' },
          template: 'plotly_white',
          margin: { t: 40, b: 40, l: 40, r: 20 },
          height: 250,
        }, { responsive: true });
      });
    }

    function showMonthTab() {
      tabMonth.classList.add('active');
      tabYear.classList.remove('active');
      monthControls.classList.remove('hidden');
      yearControls.classList.add('hidden');
      chartSection.classList.remove('hidden');
      yearChartsSection.classList.add('hidden');
      buildMonthChart();
    }

    function showYearTab() {
      tabYear.classList.add('active');
      tabMonth.classList.remove('active');
      yearControls.classList.remove('hidden');
      monthControls.classList.add('hidden');
      yearChartsSection.classList.remove('hidden');
      chartSection.classList.add('hidden');
      buildYearCharts();
    }

    monthYearSelect.addEventListener('change', () => {
      populateMonthMonthOptions(monthYearSelect.value);
      buildMonthChart();
    });

    monthMonthSelect.addEventListener('change', buildMonthChart);
    yearYearSelect.addEventListener('change', buildYearCharts);

    tabMonth.addEventListener('click', showMonthTab);
    tabYear.addEventListener('click', showYearTab);

    window.addEventListener('DOMContentLoaded', () => {
      populateMonthYearOptions();
      populateYearYearOptions();
      showMonthTab();
    });
  </script>
</body>
</html>
"""

    html = template.replace('%DATA_FILE%', str(data_file))
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
