"""
A tiny demo sheet: YOUR OWN email addresses on real, distinctive company names, so a live demo shows real
company research and a personalized first email, and everything sent lands in your own inbox.
Company sizes and positions are illustrative demo values.

    python sample_data/generate_demo_leads.py you@gmail.com sender@gmail.com

The first address becomes a lead that can reply to the emails it receives. The second should be the
mailbox the system sends from (GMAIL_SENDER_ADDRESS): it can receive mail but cannot reply to itself.
Writes sample_data/demo_leads.xlsx.
"""
import sys
from datetime import datetime

import pandas as pd


def rows(first: str, second: str) -> list[dict]:
    return [
        {"First Name": "Demo", "Last Name": "Replier", "URL": "https://www.linkedin.com/in/demo-replier",
         "Email Address": first, "Company": "Doist", "Position": "Founder & CEO",
         "Connected On": datetime(2026, 9, 20, 9, 0), "Employee Count": 100, "Location": "Lisbon, Portugal"},
        {"First Name": "Demo", "Last Name": "Sender", "URL": "https://www.linkedin.com/in/demo-sender",
         "Email Address": second, "Company": "Wistia", "Position": "Chief Operating Officer",
         "Connected On": datetime(2026, 9, 21, 10, 30), "Employee Count": 110, "Location": "Cambridge, MA"},
    ]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python sample_data/generate_demo_leads.py <your address> <sender address>")
    out = __file__.rsplit("/", 1)[0] + "/demo_leads.xlsx"
    data = rows(sys.argv[1], sys.argv[2])
    pd.DataFrame(data).to_excel(out, index=False)
    print(f"Wrote {len(data)} demo rows to {out}")
