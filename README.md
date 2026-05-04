# TTD Darshan Data Analysis

This project processes archival TTD news articles to extract daily visitor/pilgrim statistics for time-series analysis and forecasting.

## Installation

1. Ensure Python 3.10+ is installed.
2. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Set up your `.env` file with your OpenAI API key:
   ```bash
   echo "OPENAI_API_KEY=your_api_key_here" > .env
   ```

## Usage

Run the article processing pipeline on a directory containing CSV files:

```bash
python process_articles.py <path_to_csv_directory>
```

Example:
```bash
python process_articles.py test_data/some_darshan
```

This will:
- Load all CSV files in the directory
- Classify articles in batches using OpenAI Batch API
- Extract pilgrim counts and metrics in batches
- Aggregate data by date
- Save a consolidated `darshan_data.json` file with metadata
- Save `failed_records_*.csv` for any problematic articles

### Retry Failed Records

To retry processing previously failed records:

```bash
python process_articles.py <path_to_csv_directory> --retry-failed <failed_records.csv>
```

## Logs

Logs are saved in the `logs/` directory with timestamped filenames.

## Testing and Validation

- Test with smaller CSV datasets to verify classification and extraction accuracy.
- Check `darshan_data.json` for output correctness.
- Monitor `failed_records_*.csv` for any errors and retry if needed.

## Notes

- Batch API reduces cost and latency compared to individual calls.
- Processing large datasets may take time due to polling batch completion.
- The project currently supports only the OpenAI provider.
- Prompts are tuned for strict classification and extraction of visitor counts.

## Project Structure

- `process_articles.py`: Main batch processing engine
- `providers/openai_provider.py`: OpenAI batch API provider
- `prompt_templates.py`: Prompts for classification and extraction
- `README.md`: This file

## License

Licensed under the LICENSE file provided.
