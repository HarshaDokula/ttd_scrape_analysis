import glob
import csv

total_records = 0
for file_path in glob.glob("../ttd_scrape/scraped_data/articles_*.csv"):
    with open(file_path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        total_records += sum(1 for _ in reader)

print(f"Total records: {total_records}")
