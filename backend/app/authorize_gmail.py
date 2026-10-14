# authorize_gmail.py
import os
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

CLIENT_SECRET_FILE = os.getenv("GMAIL_OAUTH_CLIENT_SECRET_FILE")
TOKEN_FILE = os.getenv("GMAIL_OAUTH_TOKEN_FILE")

def authorize():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # A revoked or expired refresh token (Google test-mode apps expire them after 7 days)
            # can't be refreshed -- fall through to a fresh sign-in instead of crashing.
            print(f"The saved token is no longer valid ({e.args[0]}). Starting a new sign-in...")
            creds = None

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as token:
        token.write(creds.to_json())

    print("Authorization complete. Token saved to", TOKEN_FILE)


if __name__ == "__main__":
    authorize()