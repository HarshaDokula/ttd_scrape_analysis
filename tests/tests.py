import os
import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd

# Import the globals and function from your process_articles module
from process_articles import finalize_output, _DARSHAN_ROWS, FAILED_RECORDS

class TestFinalizeOutput(unittest.TestCase):
    def setUp(self):
        # Clear existing global data before each test
        _DARSHAN_ROWS.clear()
        FAILED_RECORDS.clear()
        
        # Set up some test records in _DARSHAN_ROWS and FAILED_RECORDS
        _DARSHAN_ROWS["2023-10-31"] = {
            "article_id": "1",
            "title": "Test Article",
            "post": "http://example.com",
            "data": {
                "pilgrim_count": 23423,
                "other_metrics": {"key": "value"}
            }
        }
        FAILED_RECORDS.append({
            "article_id": "2",
            "title": "Failed Record",
            "content": "error details"
        })

    def test_finalize_output(self):
        # Create a temporary directory for output files
        with tempfile.TemporaryDirectory() as tmpdirname:
            tmp_dir = Path(tmpdirname)
            json_path = tmp_dir / "darshan_data.json"
            
            # Change current working directory to the temp directory so that
            # the CSV file is written there as well.
            original_cwd = os.getcwd()
            os.chdir(tmp_dir)
            try:
                # Call finalize_output with our temporary JSON path.
                finalize_output(out_path=json_path)
                
                # Check that the JSON output file exists and contains the expected content.
                self.assertTrue(json_path.exists(), "JSON file was not created.")
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Compare with our global _DARSHAN_ROWS.
                self.assertEqual(data, _DARSHAN_ROWS)
                
                # Check that the CSV file "failed_records.csv" exists and has the right data.
                csv_path = tmp_dir / "failed_records.csv"
                self.assertTrue(csv_path.exists(), "CSV file was not created.")
                df = pd.read_csv(csv_path)
                print(df.dtypes)
                print(df)
                self.assertEqual(len(df), len(FAILED_RECORDS))
                # Optionally, check that a column exists and data matches.
                self.assertIn("article_id", df.columns)
                self.assertEqual(df.loc[0, "article_id"].astype(str), FAILED_RECORDS[0]["article_id"])
            finally:
                os.chdir(original_cwd)

if __name__ == '__main__':
    unittest.main()