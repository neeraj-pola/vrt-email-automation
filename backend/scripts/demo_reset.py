"""
Wipes the test collections so a demo starts clean: leads, company research, Modify draft changes,
the follow-up schedule and the Gmail cursor. Only touches collections with FIRESTORE_COLLECTION_PREFIX.

    cd backend && source venv/bin/activate && python scripts/demo_reset.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


def main() -> int:
    prefix = settings.firestore_collection_prefix
    if not prefix:
        print("Refusing to run: FIRESTORE_COLLECTION_PREFIX is empty, so this would touch REAL data.")
        return 2
    from app.services.firebase_app import get_firestore_client

    db = get_firestore_client()
    total = 0
    for name in ("leads", "pipeline_state", "company_profiles", "draft_changes", "healthcheck"):
        n = 0
        for snap in db.collection(f"{prefix}{name}").stream():
            snap.reference.delete()
            n += 1
        total += n
        print(f"  {prefix}{name}: {n} deleted")
    print(f"Done. {total} documents removed. The next dashboard load starts empty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
