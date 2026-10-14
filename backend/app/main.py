"""
FastAPI entrypoint. Run locally with:
    uvicorn app.main:app --reload --port 8000

Deployed on Railway per CLAUDE.md §11.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes_draft_changes import router as draft_changes_router
from app.api.routes_drafts import router as drafts_router
from app.api.routes_leads import router as leads_router
from app.api.routes_pipeline import router as pipeline_router
from app.api.routes_public import router as public_router
from app.config import settings
from app.pipeline.scheduler import auto_tick_loop
from app.startup_checks import check_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outreach.startup")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    errors, warnings = check_settings(settings)
    for w in warnings:
        logger.warning("Config: %s", w)
    if errors and settings.environment != "development":
        for e in errors:
            logger.error("Config: %s", e)
        raise RuntimeError("Refusing to start with an incomplete configuration:\n  - " + "\n  - ".join(errors))
    for e in errors:
        logger.warning("Config (would block a production start): %s", e)
    # Background reply/bounce polling (§6.7, §8) -- see app/pipeline/
    # scheduler.py for why this is an in-process loop rather than a cron
    # job. Cancelled cleanly on shutdown rather than left dangling.
    task = asyncio.create_task(auto_tick_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="VRT Email Automation",
    description="Automated lead contact and follow-up for VRT Management Group, LLC.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        ["*"]
        if settings.environment == "development"
        else [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(leads_router)
app.include_router(drafts_router)
app.include_router(draft_changes_router)
app.include_router(pipeline_router)
app.include_router(public_router)

# Serves the logo for outbound email HTML (app/services/email_template.py) --
# an email can't reference a relative frontend path like /logo.png, it needs
# a real reachable URL, and the backend already has a known public one
# (settings.public_base_url) regardless of the frontend's own deployment state.
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mock_integrations": settings.use_mock_integrations,
        "auto_tick_interval_seconds": settings.auto_tick_interval_seconds,
    }
