# Firebase setup and data model

Firestore holds the data; Firebase Auth handles staff login. The backend (FastAPI on
Railway) is the only thing that talks to Firestore, using the Firebase Admin SDK.

## One-time setup

1. Create a Firebase project. Add **Firestore** (production mode, a US region) and
   **Authentication > Sign-in method > Email/Password**.
2. Authentication > Settings > User actions: turn **off** "Enable create (sign-up)" so
   nobody can register themselves. Add each staff member under Authentication > Users.
3. Project settings > Service accounts > **Generate new private key**. Put the file's
   contents in `FIREBASE_SERVICE_ACCOUNT_JSON` (Railway) or point
   `FIREBASE_SERVICE_ACCOUNT_FILE` at it (local).
4. Set `STAFF_EMAIL_ALLOWLIST` to the staff emails. Outside development the API refuses
   every request without it, because a valid login alone is not proof of staff status.
5. Project settings > General > Your apps > add a **Web app**; copy its config into the
   frontend's `VITE_FIREBASE_*` variables.
6. Deploy the rules (they deny all direct client access): from this folder,
   `firebase deploy --only firestore:rules,firestore:indexes`.

No composite indexes are needed: every query filters on a single field, which Firestore
indexes automatically.

## Collections

| Collection / document | What it holds |
|---|---|
| `leads/{lead id}` | One document per lead (below) |
| `pipeline_state/gmail_sync` | `{gmail_history_id}` -- the Gmail reply-detection cursor |
| `pipeline_state/follow_up_schedule` | `{default_days, by_stage, by_company: [{company, days}]}` |
| `company_profiles/{sha1(company key)}` | `{key, profile}` -- one researched profile per company |
| `draft_changes/state` | `{version, changes[], saved[]}` -- the Modify draft page |

`FIRESTORE_COLLECTION_PREFIX` (e.g. `test_`) is prepended to every collection name.

## How the original SQL design maps (nothing is lost)

The system was first designed around a SQL database; this table records how each part of that design is
represented in Firestore.

**`leads` table -> `leads` collection.** Every column is a field of the same name.

| Column(s) | In Firestore |
|---|---|
| `id uuid` | the document id (still a UUID, still `Lead.id`) |
| text / int / boolean columns | the same fields |
| `timestamptz` columns (`created_time`, `last_sent_at`, `scheduled_call_time`, `last_reply_at`, `website_clicked_at`, `get_started_clicked_at`) | native Firestore Timestamps; read back as naive UTC, as the pipeline expects |
| `email_status`, `ai_response_classification`, `qualified_status`, `qualified_status_source` (with `check (... in ...)`) | strings; the allowed values are enforced by the `Lead` model's enums on every read and write |
| `draft_history jsonb` | an array of maps (each version's `created_at` is an ISO string) |
| `inserted_at`, `updated_at` | Firestore keeps `create_time` / `update_time` on every document itself |
| unique index on `lower(email)` | `email_lower` field, checked before every insert (`upsert_many`). Unlike the SQL index this is not atomic across two simultaneous imports; imports run from one backend instance, so that window is not reachable in practice. |
| indexes on `thread_id`, `email_status`, `qualified_status` | single-field indexes, automatic |
| row-level security policies | `firestore.rules` denies all client access; the backend checks the staff login |
| `staff_profiles` + a separate login service | Firebase Auth users, plus `STAFF_EMAIL_ALLOWLIST` |

**Two derived fields are written on every save**, purely so queries cost little:

- `email_lower` -- find a lead by email (dedupe, and matching a reply sent from a mail client that
  broke threading) without scanning.
- `follow_up_due_at` -- when a lead that is waiting on a reply is next due (absent otherwise). A tick
  asks for `follow_up_due_at <= now` and reads only those leads. It is recomputed for waiting leads
  whenever the follow-up schedule is saved.

**`pipeline_state`**: the single state row became a document; the cursor's compare-and-swap is a
Firestore transaction. Company names in the schedule are stored as a list of `{company, days}`
instead of map keys, because a name can contain characters that are illegal in field names.

**`company_profiles`** document ids are a SHA-1 of the company key, since a name can contain `/`.

## Read budget (Firestore free tier: 50,000 reads and 20,000 writes a day)

- An idle reply/follow-up tick reads about one document: it fetches new Gmail messages, looks up the
  lead for each (one read per message), and queries only the leads whose follow-up is due.
- "Sent today" is a count query, not a read of every lead.
- Importing a sheet checks existing emails 30 at a time, then writes in batches of 400.
- Opening the dashboard (or Analytics, or Modify draft) reads every lead once. 600 leads means
  about 80 full page loads a day on the free tier. Beyond that, the Blaze plan bills $0.06 per
  100,000 reads, which is well under a dollar a month at this scale.
