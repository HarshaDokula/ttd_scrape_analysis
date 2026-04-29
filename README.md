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

The generated HTML includes a dropdown selector for month/year and highlights for the highest daily records per month and the highest month per year.

