from datetime import datetime

import httpx

from app.config import settings
from app.zendesk_auth import BASE_URL, auth_headers

TONE_TAG_PREFIX = "tone_"
TOPIC_TAG_PREFIX = "topic_"
SCORED_TAG = "tone_scored"
CHURN_RISK_TAG_PREFIX = "churn_risk_"


async def get_ticket(ticket_id: int) -> dict:
    async with httpx.AsyncClient(headers=await auth_headers(), timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/tickets/{ticket_id}.json")
        resp.raise_for_status()
        return resp.json()["ticket"]


async def get_ticket_comments(ticket_id: int) -> list[dict]:
    async with httpx.AsyncClient(headers=await auth_headers(), timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/tickets/{ticket_id}/comments.json")
        resp.raise_for_status()
        return resp.json()["comments"]


def build_transcript(ticket: dict, comments: list[dict]) -> str:
    """Flatten ticket + comments into a readable transcript for the LLM.
    Keeps public comments only by default (skip internal agent notes) so we're
    scoring customer-facing exchanges, not internal agent chatter.

    Authorship is decided against the ticket's requester, not its assignee: only the
    requester is the customer whose tone we score, and everyone else on a public comment
    (assignee, a second agent, a collaborator) is support. Keying off the assignee instead
    mislabelled every agent reply that didn't come from the currently assigned agent -- and
    every assignee reply that arrived by email rather than the web client -- as `[Customer]`,
    which fed agent apologies and escalation language into the frustration score.
    """
    requester_id = ticket.get("requester_id")
    lines = [f"Subject: {ticket.get('subject', '(no subject)')}"]
    for c in comments:
        if not c.get("public", True):
            continue
        author = "Customer" if c.get("author_id") == requester_id else "Agent"
        lines.append(f"[{author}]: {c.get('plain_body', c.get('body', ''))}")
    return "\n\n".join(lines)


def require_field_ids() -> None:
    """Fail loudly if the four custom field IDs aren't configured. Both the write path
    (update_ticket_tone) and the read path (the monthly job) depend on them: without them
    the monthly job looks up custom fields by a None id, matches nothing, and silently
    renders an empty report rather than erroring."""
    missing = [
        name
        for name, value in [
            ("ZENDESK_FRUSTRATION_FIELD_ID", settings.zendesk_frustration_field_id),
            ("ZENDESK_CONFIDENCE_FIELD_ID", settings.zendesk_confidence_field_id),
            ("ZENDESK_SUMMARY_FIELD_ID", settings.zendesk_summary_field_id),
            ("ZENDESK_ANALYZED_AT_FIELD_ID", settings.zendesk_analyzed_at_field_id),
        ]
        if value is None
    ]
    if missing:
        raise RuntimeError(
            f"Missing Zendesk custom field IDs in .env: {', '.join(missing)}. "
            "Run `python -m scripts.setup_zendesk_fields` and paste the printed IDs into .env."
        )


async def update_ticket_tone(
    ticket_id: int,
    tone_category: str,
    component_tags: list[str],
    frustration_score: float,
    confidence: float,
    summary: str,
    analyzed_at: datetime,
) -> None:
    """Write the AI analysis onto the ticket itself: tone/topic as tags (replacing any
    previous analysis tags from an earlier pass on this same ticket), and the numeric/text
    detail as custom fields so it can be filtered and reported on in Zendesk Explore.

    component_tags must come from the fixed Topic vocabulary in app/schemas.py -- values are
    already tag-safe (lowercase, underscored), so no slugifying happens here."""
    require_field_ids()

    ticket = await get_ticket(ticket_id)
    kept_tags = [
        t
        for t in ticket.get("tags", [])
        if not (t.startswith(TONE_TAG_PREFIX) or t.startswith(TOPIC_TAG_PREFIX))
    ]
    new_tags = (
        kept_tags
        + [SCORED_TAG, f"{TONE_TAG_PREFIX}{tone_category}"]
        + [f"{TOPIC_TAG_PREFIX}{c}" for c in component_tags]
    )

    payload = {
        "ticket": {
            "tags": new_tags,
            "custom_fields": [
                {"id": settings.zendesk_frustration_field_id, "value": frustration_score},
                {"id": settings.zendesk_confidence_field_id, "value": confidence},
                {"id": settings.zendesk_summary_field_id, "value": summary},
                {"id": settings.zendesk_analyzed_at_field_id, "value": analyzed_at.date().isoformat()},
            ],
        }
    }
    async with httpx.AsyncClient(headers=await auth_headers(), timeout=30) as client:
        resp = await client.put(f"{BASE_URL}/tickets/{ticket_id}.json", json=payload)
        resp.raise_for_status()


async def search_scored_tickets(start: datetime, end: datetime) -> list[dict]:
    """Zendesk Search API: every ticket carrying our SCORED_TAG that was updated in
    [start, end). Paginated. Note: Search API caps results at 1000 -- fine for a pilot;
    move to the Incremental Export API if monthly volume grows past that.
    """
    query = f"type:ticket tags:{SCORED_TAG} updated>={start.date().isoformat()} updated<{end.date().isoformat()}"
    results: list[dict] = []
    async with httpx.AsyncClient(headers=await auth_headers(), timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/search.json", params={"query": query, "per_page": 100})
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        next_page = data.get("next_page")
        while next_page:
            resp = await client.get(next_page)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data["results"])
            next_page = data.get("next_page")
    return results


async def get_organization(org_id: int) -> dict:
    async with httpx.AsyncClient(headers=await auth_headers(), timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/organizations/{org_id}.json")
        resp.raise_for_status()
        return resp.json()["organization"]


def extract_renewal_date(organization: dict) -> str | None:
    """Pulls a renewal date from an org custom field, if ZENDESK_RENEWAL_FIELD_ID is configured.
    Returns an ISO date string or None. If you track renewal dates in a CRM instead, skip this
    and join renewal dates onto customer_health in the monthly job from that source."""
    if not settings.zendesk_renewal_field_id:
        return None
    fields = organization.get("organization_fields") or {}
    # Zendesk custom org fields are keyed by field key, not id, in the API response --
    # if your field key differs, adjust this lookup accordingly.
    return fields.get("renewal_date")


async def update_organization_health(
    org_id: int,
    churn_risk: str,
    product_confidence: float,
    why: list[str],
    updated_at: datetime,
) -> None:
    """Write the monthly churn-risk verdict onto the Zendesk Organization record itself
    (tag + custom fields) so it's visible year-round in Zendesk, not just in that month's
    Slack post -- and so agents/CS can filter or trigger on `churn_risk_high` directly."""
    org = await get_organization(org_id)
    kept_tags = [t for t in org.get("tags", []) if not t.startswith(CHURN_RISK_TAG_PREFIX)]
    new_tags = kept_tags + [f"{CHURN_RISK_TAG_PREFIX}{churn_risk.lower()}"]

    payload = {
        "organization": {
            "tags": new_tags,
            "organization_fields": {
                "product_confidence": product_confidence,
                "tone_health_why": "\n".join(f"- {w}" for w in why),
                "tone_health_updated_at": updated_at.date().isoformat(),
            },
        }
    }
    async with httpx.AsyncClient(headers=await auth_headers(), timeout=30) as client:
        resp = await client.put(f"{BASE_URL}/organizations/{org_id}.json", json=payload)
        resp.raise_for_status()
