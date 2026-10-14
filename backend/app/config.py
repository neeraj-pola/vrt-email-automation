"""
Central app configuration, loaded from environment variables (.env).

Per CLAUDE.md §11: OpenAI key, Gmail OAuth creds, Firebase creds are all
expected as env vars, distinct per environment (local vs. Railway prod).
"""
import secrets

from pydantic import PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OpenAI
    openai_api_key: str = ""

    # Gmail
    # File-based: local dev, where the interactive OAuth flow can write next
    # to the code. JSON-based: production (Railway) -- paste the *contents*
    # of the local credentials/ files in as env vars instead, since that
    # directory is gitignored and Railway's filesystem doesn't survive
    # redeploys anyway. See LiveGmailClient.__init__.
    gmail_oauth_client_secret_file: str = "./credentials/gmail_client_secret.json"
    gmail_oauth_token_file: str = "./credentials/gmail_token.json"
    gmail_oauth_client_secret_json: str = ""
    gmail_oauth_token_json: str = ""
    gmail_sender_address: str = ""

    # Firebase (Firestore for data, Firebase Auth for staff login).
    # Production (Railway): paste the *contents* of the service-account JSON
    # (Firebase console > Project settings > Service accounts > Generate new
    # private key) into FIREBASE_SERVICE_ACCOUNT_JSON. Local dev can point
    # FIREBASE_SERVICE_ACCOUNT_FILE at the downloaded file instead.
    firebase_project_id: str = ""
    firebase_service_account_json: str = ""
    firebase_service_account_file: str = ""
    # Prepended to every Firestore collection name, so local testing can use
    # e.g. "test_" (test_leads, test_pipeline_state, ...) in the same project
    # without any risk of touching the real data. Production leaves it empty.
    firestore_collection_prefix: str = ""
    # Comma-separated staff emails allowed into the dashboard API. Firebase
    # Auth lets anyone with the (public) web API key try to create an account
    # unless sign-ups are disabled, so a valid login alone is not enough:
    # outside development this MUST be set, or every request is refused.
    staff_email_allowlist: str = ""

    # Company research (app/services/company_research.py). Optional: without a
    # Tavily key it still works from the lead's email domain, it just can't
    # search for companies whose website it can't guess.
    tavily_api_key: str = ""

    # Company info
    company_name: str = "VRT Management Group, LLC"
    company_address: str = "1 Botsford Hill Road, PO Box 150, Botsford, CT 06404"
    company_notification_email: str = ""

    # Sending controls (§8)
    daily_send_cap: int = 30
    follow_up_wait_days: int = 3
    max_follow_ups: int = 3

    # Models (§6)
    drafting_model: str = "gpt-4o-mini"
    classification_model: str = "gpt-4o-mini"
    chatbot_modification_model: str = "gpt-4o"

    # App
    environment: str = "development"
    use_mock_integrations: bool = True

    # CORS (§11): the deployed frontend's origin(s), comma-separated (e.g.
    # "https://leads.vercel.app"). Wide-open ("*") only in development,
    # where the exact localhost port varies by machine -- production must
    # name its origin(s) explicitly or every browser request from the
    # deployed frontend gets rejected before it ever reaches a route.
    allowed_origins: str = ""

    # Public base URL this API is reachable at (used to build absolute links
    # that go out in emails, e.g. the unsubscribe link -- localhost in dev,
    # the real Railway URL in production, per §11).
    public_base_url: str = "http://localhost:8000"

    # Signs the unsubscribe link so a lead's email can't be guessed and
    # unsubscribed by a third party from the lead_id alone. Set a stable
    # value in production (.env) -- if unset (or left blank in .env), a
    # random one is generated per process start, which would invalidate
    # already-sent links on restart.
    unsubscribe_secret: str = ""

    # Background polling (§6.7/§8): how often the server checks Gmail for
    # new replies/bounces on its own, instead of relying on staff to click
    # "Check replies". Minutes, not seconds -- replies aren't time-sensitive
    # at sub-minute granularity, and standard cron scheduling (Railway's
    # included) doesn't support sub-minute intervals anyway; this runs as an
    # in-process loop instead. Set to 0 to disable and rely on the manual
    # button / an external scheduler only.
    # A tick reads the Gmail cursor (1 Firestore read), looks up only the
    # leads that new mail or a due follow-up actually concerns, and nothing
    # else -- so an idle tick costs about one read. 300s still means a real
    # reply is noticed within 5 minutes worst case.
    auto_tick_interval_seconds: int = 300

    # True when no UNSUBSCRIBE_SECRET was provided and a random one was generated for this process.
    _unsubscribe_secret_generated: bool = PrivateAttr(default=False)

    @property
    def unsubscribe_secret_generated(self) -> bool:
        return self._unsubscribe_secret_generated

    def collection(self, name: str) -> str:
        """A Firestore collection name, with the optional test prefix applied."""
        return f"{self.firestore_collection_prefix}{name}"

    @model_validator(mode="after")
    def _default_unsubscribe_secret(self) -> "Settings":
        if not self.unsubscribe_secret:
            self.unsubscribe_secret = secrets.token_urlsafe(32)
            self._unsubscribe_secret_generated = True
        return self


settings = Settings()
