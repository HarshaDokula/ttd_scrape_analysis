#!/usr/bin/env python3
import os
import glob
import csv
import argparse
import re
from pathlib import Path

def process_record(record):
    """
    Placeholder function to process a single record (row) from the CSV.
    Replace this with your actual processing logic later.
    """
    print(f"Processing Article ID: {record['article_id']} - Title: {record['title']}")

def get_csv_files(data_dir, year=None, month=None):
    """
    Get list of CSV files in the directory, optionally filtered by year/month.
    """
    pattern = os.path.join(data_dir, "articles_*.csv")
    files = glob.glob(pattern)

    # Filter by year/month if provided
    filtered_files = []
    for f in files:
        match = re.search(r"articles_(\d{4})_(\d{2})\.csv$", os.path.basename(f))
        if match:
            file_year, file_month = match.groups()
            if (year is None or file_year == year) and (month is None or file_month == month):
                filtered_files.append(f)

    return sorted(filtered_files)

def main():
    parser = argparse.ArgumentParser(description="Process TTD scraped article CSV files.")
    parser.add_argument("data_dir", type=str, help="Path to directory containing article CSV files.")
    parser.add_argument("--year", type=str, help="Filter files by year (YYYY).")
    parser.add_argument("--month", type=str, help="Filter files by month (MM, zero-padded).")

    args = parser.parse_args()

    # Validate directory
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"Error: {data_dir} is not a valid directory.")
        return

    # Get files
    csv_files = get_csv_files(data_dir, year=args.year, month=args.month)
    if not csv_files:
        print("No matching CSV files found.")
        return

    for file_path in csv_files:
        print(f"\nReading file: {os.path.basename(file_path)}")
        with open(file_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                process_record(row)

if __name__ == "__main__":
    main()
