"""
Fast-forwards the wait for a demo: moves a lead's "last email sent" back in time so the next
"Check replies" (or the 5-minute auto-check) sees the follow-up wait as over and sends the next
follow-up. Only changes a stored timestamp; sends nothing itself.

    python scripts/demo_age_lead.py lead@example.com          # back 4 days
    python scripts/demo_age_lead.py lead@example.com --days 10
"""
import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("email")
    parser.add_argument("--days", type=float, default=4.0)
    args = parser.parse_args()
    if not settings.firestore_collection_prefix:
        print("Refusing to run: FIRESTORE_COLLECTION_PREFIX is empty (this is for test data only).")
        return 2

    from app.services import firestore_stores as fs
    from app.services import storage_service
    from app.services.firebase_app import get_firestore_client

    store = fs.FirestoreLeadStore(get_firestore_client())
    storage_service._store = store
    lead = store.find_by_email(args.email)
    if lead is None:
        print(f"No lead with email {args.email}.")
        return 1
    if lead.last_sent_at is None:
        print(f"{lead.full_name} hasn't been sent anything yet, so there is no wait to fast-forward.")
        return 1
    lead.last_sent_at -= timedelta(days=args.days)
    store.save(lead)  # recomputes the stored follow-up due time
    print(f"{lead.full_name}: last email now {args.days:g} days ago. Press 'Check replies' (or wait up to 5 min) to send the follow-up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
