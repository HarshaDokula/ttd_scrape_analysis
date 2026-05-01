# TTD data analysis

TTD(Tirumala Tirupathi Devasthanam) archival data analysis.
This particulary focuses on the seperating the "Darshan"/Visitors data from the other archival data and infer patterns or use for forecasting.

# Supported providers
* OpenAI
* PerplexityAI (in testing)

# Supported models for each provider

- OpenAI
	* gpt-4o-mini
- Perplexity
	* sonar
	* NOTE: not tested on the remaining models.

## Visualization

A new script is available to generate interactive graphs from the processed `darshan_data.json` output.

Usage:

1. Generate the cleaned data first:
   ```bash
   python process_articles.py <csv_data_dir>
   ```
2. Generate the visualization:
   ```bash
   python visualize.py
   ```
3. Open `darshan_visualization.html` in a browser.

The generated HTML includes two tabs:

- **Month Tab**: Shows a single bar chart for the selected month/year combination. The highest daily record is highlighted in red. Use the year and month dropdowns to select different periods.

- **Year Tab**: Displays a 3x4 grid of monthly charts for the selected year. Each chart shows daily pilgrim counts for that month, with the highest record highlighted in red. Empty months (with no data) are shown as placeholder charts labeled "No data".

Features:
- Interactive dropdown selectors for year/month navigation
- Automatic highlighting of highest daily records per month
- Responsive design that works on different screen sizes
- Data sourced from `full_run_1/darshan_data_full_run.json` or `darshan_data.json`

