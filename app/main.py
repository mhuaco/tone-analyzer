import asyncio
import hashlib
import hmac
import base64
from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from app.config import settings
from app.llm_analyzer import analyze_ticket_tone
from app.zendesk_client import build_transcript, get_ticket, get_ticket_comments, update_ticket_tone

app = FastAPI(title="Zendesk Tone Analyzer")

# Only analyze once a ticket reaches a resolved state, not on every lifecycle event.
# Trade-off (see README): a ticket that stays open/on-hold past the reporting window gets
# no data until it resolves. Zendesk statuses arrive in various casing, hence the .lower().
RESOLVED_STATUSES = {"solved", "closed"}


def verify_zendesk_signature(body: bytes, signature: str | None, timestamp: str | None) -> bool:
    """Verify Zendesk's webhook signing header. See Zendesk docs for 'Webhook signing secret'."""
    if not settings.zendesk_webhook_secret:
        return True  # signing not configured -- fine for local dev, do this before prod
    if not signature or not timestamp:
        return False
    message = timestamp.encode() + body
    expected = base64.b64encode(
        hmac.new(settings.zendesk_webhook_secret.encode(), message, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


async def process_ticket(ticket_id: int) -> None:
    ticket = await get_ticket(ticket_id)
    comments = await get_ticket_comments(ticket_id)
    transcript = build_transcript(ticket, comments)

    # analyze_ticket_tone wraps the synchronous Anthropic client, so calling it directly
    # would block this event loop -- and background tasks share the loop serving requests,
    # stalling every other webhook and /health for the duration of the LLM call.
    result = await asyncio.to_thread(analyze_ticket_tone, transcript)

    await update_ticket_tone(
        ticket_id,
        tone_category=result.tone_category,
        component_tags=result.component_tags,
        frustration_score=result.frustration_score,
        confidence=result.confidence,
        summary=result.summary,
        analyzed_at=datetime.utcnow(),
    )


@app.post("/webhooks/zendesk/ticket-event")
async def zendesk_ticket_event(
    request: Request,
    background_tasks: BackgroundTasks,
    x_zendesk_webhook_signature: str | None = Header(default=None),
    x_zendesk_webhook_signature_timestamp: str | None = Header(default=None),
):
    body = await request.body()
    if not verify_zendesk_signature(body, x_zendesk_webhook_signature, x_zendesk_webhook_signature_timestamp):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    # Event-subscribed webhooks send Zendesk's own event envelope, not a custom body --
    # the ticket id and current status live at detail.id / detail.status, e.g.
    # {"type": "zen:event-type:ticket.status_changed", "detail": {"id": "50", "status": "SOLVED", ...}}.
    detail = payload.get("detail") or {}
    ticket_id = detail.get("id")
    if not ticket_id:
        raise HTTPException(status_code=400, detail="No ticket id in payload")

    status = (detail.get("status") or "").lower()
    if status not in RESOLVED_STATUSES:
        return {"status": "skipped", "ticket_id": ticket_id, "ticket_status": status}

    # Run the fetch + LLM scoring after responding, so Zendesk's webhook doesn't time out.
    background_tasks.add_task(process_ticket, int(ticket_id))
    return {"status": "accepted", "ticket_id": ticket_id}


@app.get("/health")
def health():
    return {"status": "ok"}
