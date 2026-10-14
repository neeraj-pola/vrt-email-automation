"""
Generates a small lead sheet for manually testing the whole pipeline (send, reply, bounce, unsubscribe,
qualify, confirm). The people and companies are fictional and the addresses are placeholders on
example.com, which can never receive mail.

TO ACTUALLY SEND AND REPLY, put addresses you control in the "Email Address" column (your own inboxes),
then import the sheet. Never use real people's addresses you don't own.

Columns match the connections export the company works from:
First Name, Last Name, URL, Email Address, Company, Position, Connected On, Employee Count, Location.
Two leads share a company on purpose, so the dashboard's company filter has something to group.

Run: python sample_data/generate_manual_test_leads.py   (writes sample_data/manual_test_leads.xlsx)
"""
from datetime import datetime

import pandas as pd

ROWS = [
    {"First Name": "Alex", "Last Name": "Rivera", "URL": "https://www.linkedin.com/in/sample-alex",
     "Email Address": "alex.rivera@example.com", "Company": "Northwind Advisory", "Position": "Founder & CEO",
     "Connected On": datetime(2026, 9, 1, 9, 0), "Employee Count": 45, "Location": "Stamford, CT"},
    {"First Name": "Sam", "Last Name": "Patel", "URL": "https://www.linkedin.com/in/sample-sam",
     "Email Address": "sam.patel@example.com", "Company": "Northwind Advisory", "Position": "Chief Operating Officer",
     "Connected On": datetime(2026, 9, 1, 10, 30), "Employee Count": 45, "Location": "Stamford, CT"},
    {"First Name": "Jordan", "Last Name": "Lee", "URL": "https://www.linkedin.com/in/sample-jordan",
     "Email Address": "jordan.lee@example.com", "Company": "Bluepeak Logistics", "Position": "Managing Director",
     "Connected On": datetime(2026, 9, 1, 12, 0), "Employee Count": "51-200", "Location": "Austin, TX"},
    {"First Name": "Casey", "Last Name": "Nguyen", "URL": "https://www.linkedin.com/in/sample-casey",
     "Email Address": "casey.nguyen@example.com", "Company": "Kiran Digital", "Position": "Owner",
     "Connected On": datetime(2026, 9, 1, 14, 15), "Employee Count": 8, "Location": "Hartford, CT"},
]

if __name__ == "__main__":
    df = pd.DataFrame(ROWS)
    out_path = __file__.rsplit("/", 1)[0] + "/manual_test_leads.xlsx"
    df.to_excel(out_path, index=False)
    print(f"Wrote {len(df)} rows to {out_path}")
