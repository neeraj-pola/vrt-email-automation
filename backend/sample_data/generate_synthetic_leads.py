"""
Generates a synthetic leads Excel file matching the real source columns (§3),
for the first testing pass (§13.1) before touching any real data.

Run: python sample_data/generate_synthetic_leads.py
"""
from datetime import datetime

import pandas as pd

ROWS = [
    {"First Name": "Maria", "Last Name": "Gonzalez", "URL": "https://www.linkedin.com/in/maria-gonzalez-test",
     "Email Address": "maria.gonzalez.test@example.com", "Company": "Acme Industrial", "Position": "CEO",
     "Connected On": datetime(2026, 8, 1, 10, 0), "Employee Count": 120, "Location": "Stamford, CT"},
    {"First Name": "James", "Last Name": "Carter", "URL": "https://www.linkedin.com/in/james-carter-test",
     "Email Address": "james.carter.test@example.com", "Company": "Carter & Sons", "Position": "Owner",
     "Connected On": datetime(2026, 8, 2, 14, 30), "Employee Count": 12, "Location": "Hartford, CT"},
    {"First Name": "Priya", "Last Name": "Shah", "URL": "https://www.linkedin.com/in/priya-shah-test",
     "Email Address": "priya.shah.test@example.com", "Company": "Acme Industrial", "Position": "VP Operations",
     "Connected On": datetime(2026, 8, 3, 9, 15), "Employee Count": 120, "Location": "Stamford, CT"},
    # Duplicate row (same email, later timestamp) -- should be deduped on import.
    {"First Name": "Priya", "Last Name": "Shah", "URL": "https://www.linkedin.com/in/priya-shah-test",
     "Email Address": "priya.shah.test@example.com", "Company": "Acme Industrial", "Position": "VP Operations",
     "Connected On": datetime(2026, 8, 3, 18, 0), "Employee Count": 120, "Location": "Stamford, CT"},
    {"First Name": "Tom", "Last Name": "Reilly", "URL": "https://www.linkedin.com/in/tom-reilly-test",
     "Email Address": "tom.reilly.test@example.com", "Company": "Reilly Freight", "Position": "President",
     "Connected On": datetime(2026, 8, 4, 11, 45), "Employee Count": "51-200", "Location": "Albany, NY"},
    {"First Name": "Angela", "Last Name": "Diaz", "URL": "https://www.linkedin.com/in/angela-diaz-test",
     "Email Address": "angela.diaz.test@example.com", "Company": "Diaz Health Group", "Position": "Managing Partner",
     "Connected On": datetime(2026, 8, 5, 13, 0), "Employee Count": 33, "Location": "Boston, MA"},
]

if __name__ == "__main__":
    df = pd.DataFrame(ROWS)
    out_path = __file__.rsplit("/", 1)[0] + "/synthetic_leads.xlsx"
    df.to_excel(out_path, index=False)
    print(f"Wrote {len(df)} synthetic rows to {out_path}")
