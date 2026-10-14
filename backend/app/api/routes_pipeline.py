"""
Send + tick endpoints (§6 stages 4-8).

`/pipeline/send` -- rate-limited batch send of approved drafts (§8).
`/pipeline/tick` -- poll Gmail for new replies and run the outreach state
machine (see app/pipeline/tick.py for the actual logic -- bounce/unsubscribe
detection, reply dedup, classification, qualification hand-off, follow-up
timing). Intended to be hit by a scheduler (Railway cron, or an external
trigger) rather than a human, though staff can also trigger it manually from
the dashboard for a live demo.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_gmail, get_pipeline_state, get_store, require_staff
from app.api.schemas import LeadIdsRequest, PipelineConfigResponse, SendBatchResult, SendStatsResponse, TickResult
from app.config import settings
from app.models.lead import AIResponseClassification, EmailStatus, Lead
from app.pipeline.outreach_graph import next_follow_up_due_at, send_follow_up_now
from app.pipeline.send_batch import leads_sent_today, send_approved_batch
from app.pipeline.tick import run_pipeline_tick
from app.services import schedule_service
from app.services.schedule_service import Schedule
from app.services.gmail_service import GmailClientBase
from app.services.storage_service import LeadStoreBase, PipelineStateStore

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/config", response_model=PipelineConfigResponse)
async def get_pipeline_config(_staff=Depends(require_staff)):
    """Timing settings the dashboard needs to compute/display next-follow-up-due itself."""
    return PipelineConfigResponse(
        follow_up_wait_days=settings.follow_up_wait_days,
        max_follow_ups=settings.max_follow_ups,
        daily_send_cap=settings.daily_send_cap,
    )


@router.get("/schedule", response_model=Schedule)
async def get_follow_up_schedule(_staff=Depends(require_staff)):
    """Follow-up wait: the default plus any per-company / per-stage overrides."""
    return schedule_service.get_schedule()


@router.put("/schedule", response_model=Schedule)
async def update_follow_up_schedule(body: Schedule, _staff=Depends(require_staff)):
    """
    Replaces the whole schedule. Takes effect on the very next tick, and
    applies to leads already waiting on a follow-up too (the due date is
    always computed from last_sent_at plus whatever the schedule says now).
    """
    return schedule_service.set_schedule(body)


@router.get("/send-stats", response_model=SendStatsResponse)
async def get_send_stats(store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """
    Separate from /config on purpose -- the frontend caches that response
    for the whole session (it's static settings), but "sent today" is live
    state that changes as sends actually happen, so it needs its own
    endpoint the dashboard can poll/refetch instead of one fixed at page load.
    """
    return SendStatsResponse(sent_today=leads_sent_today(store), daily_cap=settings.daily_send_cap)


@router.post("/send", response_model=SendBatchResult)
async def send_batch(body: LeadIdsRequest, gmail: GmailClientBase = Depends(get_gmail),
                      store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    # send_approved_batch is synchronous, blocking network I/O (Gmail,
    # OpenAI) for potentially dozens of leads in a row -- run it on a worker
    # thread, not the event loop's own thread. Without this, one staff
    # member sending a batch freezes the *entire app* for every other
    # request (including everyone else's dashboard just trying to load the
    # lead list) for the whole duration of that send, not just their own
    # request. Caught 2026-09-14: the sibling /tick endpoint had this exact
    # bug and froze the app during a long cold-start poll.
    return await asyncio.to_thread(send_approved_batch, body.lead_ids, gmail, store)


@router.post("/tick", response_model=list[TickResult])
async def tick(gmail: GmailClientBase = Depends(get_gmail), store: LeadStoreBase = Depends(get_store),
               state_store: PipelineStateStore = Depends(get_pipeline_state),
               _staff=Depends(require_staff)):
    # Same reasoning as /send above -- this is what actually froze the
    # dashboard for everyone during the first post-deploy cold-start poll.
    outcomes = await asyncio.to_thread(run_pipeline_tick, gmail, store, state_store)
    return [TickResult(lead_id=o.lead_id, action=o.action) for o in outcomes]


@router.post("/leads/{lead_id}/send-follow-up-now", response_model=Lead)
async def send_follow_up_now_endpoint(lead_id: str, gmail: GmailClientBase = Depends(get_gmail),
                                       store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """
    Staff-triggered early follow-up (§8's automatic wait window still applies
    to the *next* one -- see next_follow_up_due_at, which recomputes from
    whenever this actually sends, not the originally-scheduled time).
    """
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead.email_status != EmailStatus.sent:
        raise HTTPException(status_code=409, detail=f"Lead is not awaiting a reply (email_status={lead.email_status}).")
    if lead.ai_response_classification in (AIResponseClassification.interested, AIResponseClassification.not_interested):
        raise HTTPException(status_code=409, detail="Lead already replied -- no more automatic follow-ups apply.")
    if lead.follow_up_count >= settings.max_follow_ups:
        raise HTTPException(status_code=409, detail=f"Already sent the maximum of {settings.max_follow_ups} follow-ups.")
    return await asyncio.to_thread(send_follow_up_now, lead, gmail, store)
