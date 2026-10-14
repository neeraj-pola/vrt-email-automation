# VRT Email Automation

Automated lead contact and follow-up for VRT Management Group, LLC. It takes a list of leads (an Excel
export), researches each lead's company, writes a personalized first email, sends it from a Gmail account
at a capped daily pace, watches for replies, classifies them with AI, follows up a limited number of times
when a lead goes quiet, and hands interested leads to staff with everything needed to make the call.

This README is everything needed to run, test and deploy it: what to sign up for, which settings to fill
in, and the exact commands.

**Contents**
1. [What it does](#1-what-it-does)
2. [How it is built](#2-how-it-is-built)
3. [Accounts you need](#3-accounts-you-need)
4. [Set up the services](#4-set-up-the-services) (Firebase, Gmail, OpenAI, Tavily)
5. [Run it on your computer](#5-run-it-on-your-computer)
6. [Settings reference](#6-settings-reference)
7. [Deploy](#7-deploy) (Railway backend, Vercel frontend)
8. [Using the dashboard](#8-using-the-dashboard)
9. [Testing](#9-testing)
10. [Costs](#10-costs)
11. [Operating notes and limits](#11-operating-notes-and-limits)
12. [Troubleshooting](#12-troubleshooting)
13. [Repository map](#13-repository-map)

---

## 1. What it does

| Step | What happens |
|---|---|
| Import | Upload an Excel file of leads (columns: First Name, Last Name, URL, Email Address, Company, Position, Connected On, Employee Count, Location). Columns are matched by name, you confirm the mapping, and duplicate emails are skipped. |
| Research | For each lead's company, once: read the company's website (found from the lead's email domain, or by a Tavily web search if that fails), summarize it, and note which of VRT's 27 core business challenges the site gives real evidence for. |
| Draft | Write a personalized first email from the person's position, company, size and location. Staff can edit it by hand, rewrite it with a chatbot, or change how all first emails are written from the **Modify draft** page. |
| Send | Staff approve and send. Sending is capped per day (default 30), and every email carries the business address and a working unsubscribe link. |
| Watch | Every 5 minutes the backend checks Gmail for replies and bounces. |
| Classify | AI reads each reply: interested, not interested, or unclear. Interested leads email staff a notification; not-interested leads stop; unclear ones get a gentle automatic reply. |
| Follow up | If a lead is silent after the wait (default 3 days, changeable per company and per stage), the next follow-up goes out, up to 3. Then the lead is marked "No response". |
| Hand off | Staff confirm qualification, agree a call time with the lead, and a confirmation email is sent. |

Out of scope in this version: CRM integration (Buildertrend has no public API), automatic calendar booking,
SMS.

## 2. How it is built

```
 Browser (React, Vercel)  --sign in-->  Firebase Auth
        |   ID token
        v
 API (FastAPI, Railway)  --->  Firestore    (leads, schedule, research, Modify-draft changes)
        |        |
        |        +---------->  Gmail API    (send mail, read replies)
        +------------------->  OpenAI       (drafts, classification)  and  Tavily (company search)
```

- **Frontend** `frontend/`: React + TypeScript + Vite. Staff sign in with Firebase Auth; every data request
  goes to the API with the login token.
- **Backend** `backend/`: FastAPI + LangChain/LangGraph. It is the only thing that touches Firestore, Gmail
  and OpenAI. A background loop inside it checks Gmail every 5 minutes.
- **Models:** `gpt-4o-mini` for drafting, classification and notifications; `gpt-4o` only when a staff member
  asks the chatbot for a rewrite.
- **Data:** Firestore. Direct browser access is denied by rules; only the backend (with a service account)
  reads and writes. Details: [backend/firebase/README.md](backend/firebase/README.md).

## 3. Accounts you need

| Service | Used for | Cost |
|---|---|---|
| [Firebase](https://console.firebase.google.com) (Google) | Database and staff login | Free tier is enough for this scale |
| [Google Cloud](https://console.cloud.google.com) | Gmail API access | Free |
| A Gmail account | The mailbox emails are sent from and replies are read from | Free (use the company mailbox in production) |
| [OpenAI](https://platform.openai.com) | Writing and classifying | Pay as you go, see [Costs](#10-costs) |
| [Tavily](https://tavily.com) | Finding company websites (optional) | Free tier: 1,000 searches a month |
| [Railway](https://railway.com) | Hosts the backend | About $5 a month |
| [Vercel](https://vercel.com) | Hosts the frontend | Free tier |
| [GitHub](https://github.com) | Source for both deployments | Free |

You also need **Python 3.11**, **Node.js 20.19 or newer** (22 recommended), and **git** installed to run it locally.

## 4. Set up the services

Do these once. Keep the keys and files you collect, because section 6 says where each goes.

### 4.1 Firebase (database and login)

1. In the Firebase console, **Add project** (any name; Google Analytics can be off).
2. **Build > Firestore Database > Create database.** Choose **production mode** and a US region.
3. **Build > Authentication > Get started > Sign-in method:** enable **Email/Password** only. Leave
   "Email link (passwordless sign-in)" off.
4. **Authentication > Settings > User actions:** turn **off** "Enable create (sign-up)" so nobody can
   register themselves.
5. **Authentication > Users > Add user:** add each staff member with an email and a password.
6. **Project settings (gear) > Service accounts > Generate new private key.** A `.json` file downloads.
   This is the backend's key. Store it safely and never commit it.
7. **Project settings > General > Your apps > Add app > Web (`</>`).** Register it (no Hosting). Copy the four
   values it shows: `apiKey`, `authDomain`, `projectId`, `appId`. These go in the frontend settings.
8. Deploy the access rules (they block all direct browser access). Install the Firebase CLI once with
   `npm install -g firebase-tools`, then:
   ```
   cd backend/firebase
   firebase login
   firebase deploy --only firestore --project YOUR_PROJECT_ID
   ```

No collections need creating by hand; the app creates them on first use.

### 4.2 Gmail (sending and reading)

1. In Google Cloud, create or pick a project. **APIs & Services > Library > Gmail API > Enable.**
2. **APIs & Services > OAuth consent screen:** User type **External**. Fill in the app name and your email.
   Add the scopes `.../auth/gmail.send` and `.../auth/gmail.readonly`. While the app is in **Testing**, add
   the sending Gmail address under **Test users**.
3. **APIs & Services > Credentials > Create credentials > OAuth client ID > Desktop app.** Download the JSON.
   Save it as `backend/credentials/gmail_client_secret.json`. (The `credentials/` folder is ignored by git.)
4. Create `backend/.env` (section 5) with `GMAIL_SENDER_ADDRESS` set, then, from `backend/` with the virtual
   environment active, run:
   ```
   python -m app.authorize_gmail
   ```
   A browser opens. Sign in as the **sending** account and approve. This writes
   `backend/credentials/gmail_token.json`. If Google warns the app is unverified, choose Advanced and
   continue.
5. **Tokens expire after 7 days while the OAuth app is in Testing.** For a long-running deployment, set the
   consent screen's publishing status to **In production** (you can leave it unverified for a single
   internal account), then re-run step 4. If a token ever expires, deleting it and re-running step 4 fixes it.

To send as the company's own mailbox later, repeat steps 4 with that account and change
`GMAIL_SENDER_ADDRESS`. Nothing else changes.

### 4.3 OpenAI

Create an API key under **API keys**, and add a small prepaid balance under **Billing**. Setting a monthly
usage limit there is a good idea.

### 4.4 Tavily (optional)

Sign up and copy the API key. Without it the system still researches companies whose lead has a company
email address (it reads that domain's website); it just can't search for leads on Gmail or Yahoo addresses.

## 5. Run it on your computer

```
git clone <this repository>
cd vrt-email-automation
```

**Backend** (terminal 1):
```
cd backend
python3.11 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                # then edit .env, see below
uvicorn app.main:app --port 8000
```
Check it is up: http://localhost:8000/health shows `{"status":"ok", ...}`. API docs are at
http://localhost:8000/docs.

**Frontend** (terminal 2):
```
cd frontend
npm install
cp .env.example .env.local          # then edit .env.local, see below
npm run dev
```
Open http://localhost:5173.

**Two ways to run, depending on what is in `.env`:**

- **Try it with nothing configured.** Leave `USE_MOCK_INTEGRATIONS=true` (the default) and leave the
  `VITE_FIREBASE_*` lines empty. The backend uses fake Gmail, fake storage in memory and no OpenAI; the
  frontend skips the login screen. Good for exploring the screens. Nothing is saved or sent.
- **Real services.** Fill in the real values (section 6), set `USE_MOCK_INTEGRATIONS=false`, and set
  `FIRESTORE_COLLECTION_PREFIX=test_` so everything you do locally is stored in separate `test_` collections
  and never mixes with real data. Sign in with a user you created in Firebase.

**Sample data** (writes Excel files into `backend/sample_data/`, which git ignores):
```
cd backend && source venv/bin/activate
python sample_data/generate_synthetic_leads.py                   # six fictional leads, example.com addresses
python sample_data/generate_manual_test_leads.py                 # four fictional leads; edit the addresses to yours to send
python sample_data/generate_demo_leads.py you@gmail.com sender@gmail.com   # two leads on YOUR addresses
```
Sample addresses on `example.com` can't receive mail, so use the demo generator (or edit the manual one) with
addresses you control before sending anything. Never put other people's real addresses in a test sheet.

### Try it end to end (about 10 minutes)

1. Import a sheet with a lead on your own address. **Import Excel**, confirm the column mapping.
2. Tick the lead and click **Generate drafts**. Open the lead to see the company research and the draft.
3. Click **Approve & send**. The email arrives in your inbox. Check the logo, the address and the Unsubscribe
   link in the footer.
4. Reply from that inbox ("Sure, Thursday afternoon works"). Click **Check replies** (the backend also checks
   by itself every 5 minutes). The lead moves to **Needs review**, the reply shows under "AI classification",
   and the notification email goes to `COMPANY_NOTIFICATION_EMAIL`.
5. Open the lead: **Confirm qualified**, pick a call time, **Confirm call & notify lead**.
6. To see a follow-up without waiting 3 days, change the wait in the **Follow-up schedule** bar at the top of
   the Leads page, or run `python scripts/demo_age_lead.py lead@example.com` (moves that lead's last-sent time
   back 4 days; run it from `backend/`), then click **Check replies**.
7. Clear everything you created: `python scripts/demo_reset.py` (only touches the `test_` collections).

## 6. Settings reference

Settings are environment variables. Locally they live in `backend/.env` and `frontend/.env.local` (both are
ignored by git). In production you enter them in Railway and Vercel (section 7).
`backend/.env.example` and `frontend/.env.example` list every one.

### Backend

| Variable | Required | Default | What it is |
|---|---|---|---|
| `USE_MOCK_INTEGRATIONS` | yes | `true` | `false` = real Gmail, Firestore and OpenAI. `true` = fakes, nothing is sent or saved. **Must be `false` in production.** |
| `ENVIRONMENT` | prod | `development` | Set to `production` when deployed. Production refuses to start if anything below is missing. |
| `OPENAI_API_KEY` | live | | OpenAI key (section 4.3). |
| `FIREBASE_PROJECT_ID` | live | | The Firebase project id. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | live (prod) | | The **entire contents** of the service-account key file (section 4.1 step 6), pasted as one value. Use this in production. |
| `FIREBASE_SERVICE_ACCOUNT_FILE` | live (local) | | Path to that file, instead of pasting it. Local use. |
| `STAFF_EMAIL_ALLOWLIST` | prod | | Comma-separated emails allowed into the dashboard API. A Firebase login alone is not enough. **Required in production.** |
| `FIRESTORE_COLLECTION_PREFIX` | no | empty | Prefix for collection names, e.g. `test_`. Leave empty in production. |
| `GMAIL_SENDER_ADDRESS` | live | | The Gmail address emails are sent from. Must be the account the token belongs to. |
| `GMAIL_OAUTH_CLIENT_SECRET_FILE` | local | `./credentials/gmail_client_secret.json` | Path to the OAuth client file (section 4.2). |
| `GMAIL_OAUTH_TOKEN_FILE` | local | `./credentials/gmail_token.json` | Path to the token written by `python -m app.authorize_gmail`. |
| `GMAIL_OAUTH_CLIENT_SECRET_JSON` | prod | | Contents of the client file, pasted. Production alternative to the file. |
| `GMAIL_OAUTH_TOKEN_JSON` | prod | | Contents of the token file, pasted. **Required in production** (the server has no browser to sign in with). |
| `COMPANY_NOTIFICATION_EMAIL` | recommended | | Where "a lead replied as interested" emails go. |
| `COMPANY_NAME` | no | `VRT Management Group, LLC` | Shown in the email footer and sign-off. |
| `COMPANY_ADDRESS` | no | `1 Botsford Hill Road, PO Box 150, Botsford, CT 06404` | The business address in every email footer (a legal requirement). |
| `PUBLIC_BASE_URL` | prod | `http://localhost:8000` | The backend's public `https://` address. It is put in every unsubscribe and click link, so it must be reachable from the internet. |
| `UNSUBSCRIBE_SECRET` | prod | random each start | Signs unsubscribe links. Set a fixed value, or every link already sent breaks when the server restarts. Generate one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `ALLOWED_ORIGINS` | prod | | The frontend's address, e.g. `https://your-app.vercel.app` (comma-separated for several, no trailing slash). Without it the browser blocks every request. |
| `TAVILY_API_KEY` | no | | Enables web search for companies (section 4.4). |
| `DAILY_SEND_CAP` | no | `30` | Maximum first emails and follow-ups sent per day. Raise it gradually. |
| `FOLLOW_UP_WAIT_DAYS` | no | `3` | Default wait before a follow-up. Staff can override per company and per stage in the dashboard. |
| `MAX_FOLLOW_UPS` | no | `3` | Follow-ups per lead before giving up. |
| `DRAFTING_MODEL` / `CLASSIFICATION_MODEL` | no | `gpt-4o-mini` | Models for writing and for classifying replies. |
| `CHATBOT_MODIFICATION_MODEL` | no | `gpt-4o` | Model for the staff rewrite chatbot. |
| `AUTO_TICK_INTERVAL_SECONDS` | no | `300` | How often the backend checks Gmail. `0` turns the automatic check off (the **Check replies** button still works). |

### Frontend

| Variable | Required | What it is |
|---|---|---|
| `VITE_API_URL` | yes | The backend address: `http://localhost:8000` locally, the Railway `https://` address in production. |
| `VITE_FIREBASE_API_KEY` | live | From the Firebase web app (section 4.1 step 7). |
| `VITE_FIREBASE_AUTH_DOMAIN` | live | Same. |
| `VITE_FIREBASE_PROJECT_ID` | live | Same. |
| `VITE_FIREBASE_APP_ID` | live | Same. |

Leaving the four `VITE_FIREBASE_*` values empty turns the login screen off, which is only appropriate with
the backend in mock mode. Vite reads these when it **builds**, so after changing them on Vercel, redeploy.

## 7. Deploy

Deploy the backend first (the frontend needs its address), then the frontend, then point each at the other.

**Run exactly one backend instance.** The 5-minute reply check runs inside the backend process; two copies
would both act on the same replies. Do not scale Railway above one replica.

### 7.1 Backend on Railway

1. Push this repository to GitHub.
2. In Railway: **New Project > Deploy from GitHub repo**, pick the repository (a good service name is `vrt-email-automation-api`).
3. In the service **Settings**: set **Root Directory** to `backend`. Railway detects Python from
   `requirements.txt` and `runtime.txt`, and starts the app with the `Procfile`
   (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`).
4. **Settings > Networking > Generate Domain.** Note the `https://....up.railway.app` address.
5. **Settings > Healthcheck path:** `/health`.
6. **Variables:** add these (section 6 has the details):
   ```
   ENVIRONMENT=production
   USE_MOCK_INTEGRATIONS=false
   OPENAI_API_KEY=...
   FIREBASE_PROJECT_ID=...
   FIREBASE_SERVICE_ACCOUNT_JSON=<paste the whole key file>
   STAFF_EMAIL_ALLOWLIST=you@company.com,colleague@company.com
   GMAIL_SENDER_ADDRESS=sales@company.com
   GMAIL_OAUTH_CLIENT_SECRET_JSON=<paste the whole client file>
   GMAIL_OAUTH_TOKEN_JSON=<paste the whole token file>
   COMPANY_NOTIFICATION_EMAIL=you@company.com
   PUBLIC_BASE_URL=https://<your Railway domain>
   UNSUBSCRIBE_SECRET=<generated value>
   ALLOWED_ORIGINS=https://<your Vercel domain>      (add after step 7.2; use the temporary value https://placeholder.invalid until then)
   TAVILY_API_KEY=...                                 (optional)
   ```
   Do **not** set `FIRESTORE_COLLECTION_PREFIX`.
7. Deploy. Open the **Deploy Logs**. If a setting is missing the app refuses to start and lists exactly
   what to add (for example `PUBLIC_BASE_URL points at localhost`). Fix it and redeploy.
8. Check `https://<railway domain>/health`.

### 7.2 Frontend on Vercel

1. In Vercel: **Add New > Project**, import the same repository.
2. Name the project `vrt-email-automation`. **Root Directory:** `frontend`. Framework preset: **Vite**. Build command `npm run build`, output `dist`
   (the defaults).
3. **Environment Variables:** `VITE_API_URL` = the Railway address, and the four `VITE_FIREBASE_*` values.
4. Deploy. Note the `https://....vercel.app` address.
   (`frontend/vercel.json` already makes page reloads on deep links work.)

### 7.3 Connect them

1. **Railway:** set `ALLOWED_ORIGINS` to the Vercel address (no trailing slash) and redeploy.
2. **Firebase console > Authentication > Settings > Authorized domains:** add the Vercel domain (without
   `https://`). Without this, sign-in on the deployed site fails.
3. Open the Vercel address and sign in with a Firebase user that is on `STAFF_EMAIL_ALLOWLIST`.

### 7.4 Check the deployment

- Sign in, import a one-lead sheet with your own address, generate and send a draft to yourself.
- In the received email, click **Unsubscribe**. It must open a confirmation page on your Railway address.
  (Do not click through and confirm unless you want that lead stopped.)
- Reply, click **Check replies**, and confirm the lead moves to Needs review.
- Only then import the real list. Start with a batch of 25 to 50 leads, then the full list, keeping the daily
  cap in place so a personal Gmail account does not trip spam filters.

### 7.5 Updating

Push to the repository's main branch; Railway and Vercel redeploy automatically. Changing an environment
variable needs a redeploy (Vercel) or an automatic restart (Railway).

## 8. Using the dashboard

- **Leads page.** One dense row per lead: company, position, size, location, status, stage, reply, dates.
  Search, sort (including "Recently sent" and "Recently replied"), and filter by company, stage, reply type
  and status. **Download Excel** exports exactly what is on screen. Select rows (click, drag, or shift-click)
  to generate drafts or send.
- **Follow-up schedule** (top of the Leads page): the default wait between emails, with optional rules per
  company and per follow-up stage. A company rule beats a stage rule, which beats the default.
- **Lead panel** (click a row): contact and company details, the company research (with its source link and
  a Refresh button), the lead's reply and the AI's read of it, qualification controls, staff-only fields
  (notes, CRM status, sale status), the current draft with a live preview, earlier versions, and the chatbot.
- **Modify draft** (sidebar): see the flow of the first email, add plain-word instructions aimed at the whole
  email or one stage, for all companies or chosen ones, preview a real before and after for one lead, apply,
  name a change to reuse it on other companies, and Undo or go back to the initial state. Instructions cannot
  override the safety rules (no invented facts, prices or promised results), and the sign-off, links, address
  and unsubscribe link cannot be changed.
- **Analytics:** who clicked the website and strategy-call links in the emails.

**What the knowledge behind the AI is, and where to change it:**
- `backend/app/knowledge/vrt_company.md`: what VRT offers and how it sounds. Edit this when the offer changes.
- `backend/app/knowledge/smb_challenges.md`: the 27 business challenges.
- `backend/app/services/llm_service.py`: the prompts themselves.

## 9. Testing

```
cd backend && source venv/bin/activate && python -m pytest tests -q        # 253 tests, no keys or network needed
cd frontend && npm test                                                    # 43 tests for the Modify draft logic
cd frontend && npm run build                                               # type-checks and builds
```
The tests never call a real service and do not depend on your `.env`.

**Checks against the real services** (run from `backend/` with the virtual environment active and a filled-in
`.env`; each works in its own `verify_` collections and cleans up after itself):

| Command | What it proves | Sends email? |
|---|---|---|
| `python scripts/verify_firebase.py` | Firestore, the transactions and queries the pipeline relies on, Firebase Auth | No |
| `python scripts/verify_live.py` | Real Gmail and Firestore together: sending, seeing the mail in Gmail history, the unsubscribe link | One email, to the sender's own address |
| `python scripts/verify_openai.py` | Real research and drafting, the Modify draft rules, reply classification. Prints everything. | No |

## 10. Costs

Measured on this build, with the model split above:

- Research: about $0.001 per company with OpenAI, plus a Tavily search only when the email domain doesn't work
  (free tier covers 1,000 a month). Results are cached per company, so a second lead at the same company is free.
- A personalized first email: about $0.001. A reply classification or follow-up: a fraction of a cent.
- A full 600-lead campaign: roughly **$1 to $5** of OpenAI usage.
- Firebase: free tier (50,000 reads and 20,000 writes a day). The reply check reads about one document when
  nothing happened. Opening the dashboard reads every lead once, so about 80 full loads a day fit in the free
  tier; beyond that the paid plan costs well under a dollar a month at this scale.
- Hosting: Railway about $5 a month; Vercel free.

## 11. Operating notes and limits

- **One backend instance only** (see section 7).
- **Gmail limits.** A personal Gmail account can send roughly 500 emails a day, and a new sender that suddenly
  sends a lot looks like spam. Keep `DAILY_SEND_CAP` low and raise it gradually. For the company's own domain,
  set up SPF, DKIM and DMARC for it.
- **The unsubscribe link must be reachable.** It uses `PUBLIC_BASE_URL`. An email sent while that points at
  `localhost` has an unsubscribe link that cannot work for the recipient, which breaks the law (CAN-SPAM).
  Never email real leads from a local machine.
- **An unsubscribed, bounced, or exhausted lead is never emailed again.** A reply from one is flagged for staff
  but never answered automatically.
- **Replies are never lost to a temporary failure.** The reply check saves its place in Gmail only after the
  replies were handled; if OpenAI or Gmail is briefly down, the next check handles them. After five failed
  checks in a row on the same mail, it moves on and logs that the lead needs a manual look.
- **Gmail test-mode tokens expire in 7 days.** See section 4.2 step 5.
- **Data.** Leads live in Firestore. The Excel files contain real people's contact details; do not leave them
  in shared folders or commit them (spreadsheets under `backend/sample_data/` are ignored by git).
- **Keys.** Treat the service-account file, the Gmail token and the OpenAI key as passwords. If one leaks,
  revoke it (Firebase: delete the key; Google: remove the app's access; OpenAI: delete the key) and create a new one.
- **Not included:** CRM sync, automatic calendar booking, SMS.

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The backend exits at start with `Refusing to start with an incomplete configuration` | Production mode lists what is missing. Set each item and redeploy. |
| The browser console shows a CORS error | `ALLOWED_ORIGINS` doesn't match the frontend address exactly (check `https`, no trailing slash). |
| Sign-in says "Incorrect email or password" | Reset the user's password in Firebase > Authentication > Users. |
| Sign-in works but the dashboard shows an error with 403 | The email isn't in `STAFF_EMAIL_ALLOWLIST`. Add it and restart the backend. |
| Sign-in fails only on the deployed site | The Vercel domain isn't under Firebase > Authentication > Authorized domains. |
| `invalid_grant` from Gmail | The token expired or was revoked. Re-run `python -m app.authorize_gmail` (delete the old token file first), then update `GMAIL_OAUTH_TOKEN_JSON` on Railway. |
| Gmail asks "app not verified" or blocks sign-in | Add the sending address as a **test user** on the OAuth consent screen, or choose Advanced and continue. |
| A reply isn't picked up | Click **Check replies**. Confirm `GMAIL_SENDER_ADDRESS` is the account the token belongs to. Look at the backend logs for `Tick:` lines; a failed check retries automatically. |
| Company research says "not found" | Normal when a lead's Gmail address gives no company website and the search finds nothing. The draft uses the person's role and company size instead. **Refresh research** retries. |
| Research found the wrong company | Common names match several businesses. The source link is shown next to the summary; use **Refresh research**, or give the lead a company email address. |
| Emails show a broken logo or no unsubscribe | Check `PUBLIC_BASE_URL` and that the backend is reachable at that address. |
| OpenAI errors in the logs | Check the key, the billing balance and the usage limit. Reply handling retries on its own. |
| `Could not find the model` errors | A model name in `DRAFTING_MODEL`, `CLASSIFICATION_MODEL` or `CHATBOT_MODIFICATION_MODEL` is wrong or unavailable to your account. |
| Local frontend shows a blank login or "Failed to fetch" | The backend isn't running on the address in `VITE_API_URL`. |

## 13. Repository map

```
backend/
  app/
    main.py                  API entry point and startup checks
    startup_checks.py        refuses to start a production deploy with missing settings
    config.py                every setting (section 6)
    api/                     HTTP routes: leads, drafts, draft-changes, pipeline, public (unsubscribe, click)
    pipeline/                import, draft generation, sending, the 5-minute tick, follow-up graph, Excel export
    services/                Gmail, OpenAI prompts, company research, storage (Firestore), schedule, email template
    knowledge/               what the AI knows about VRT and the 27 challenges (edit the .md files)
    models/                  the Lead and draft-change data models
  firebase/                  Firestore rules and the data-model notes
  sample_data/               generators for sample spreadsheets
  scripts/                   verify_*.py (real-service checks), demo_reset.py, demo_age_lead.py
  tests/                     the backend test suite
frontend/
  src/pages/                 Dashboard, Lead detail, Analytics, Modify draft, Login
  src/components/            lead panel, filters, schedule bar, flowchart, chat
  src/lib/                   API client, Firebase sign-in, the Modify draft logic (tested)
```
