import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import anthropic

from app.config import settings
from app.llm_analyzer import tool_input
from app.schemas import MonthlySynthesisResult
from app.slack_client import post_monthly_report
from app.zendesk_client import (
    TONE_TAG_PREFIX,
    TOPIC_TAG_PREFIX,
    extract_renewal_date,
    get_organization,
    require_field_ids,
    search_scored_tickets,
    update_organization_health,
)

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

MIN_TICKETS_FOR_ORG_INSIGHT = 3  # don't risk-score an account off one or two tickets
MIN_TICKETS_FOR_COMPONENT_INSIGHT = 3
TOP_PROBLEM_AREAS_N = 5
TONE_CATEGORIES = ("positive", "neutral", "frustrated", "angry")

SYNTHESIS_TOOL = {
    "name": "record_monthly_synthesis",
    "description": "Record churn-risk and product-friction insights from pre-aggregated ticket tone stats.",
    # See the note on TONE_TOOL in llm_analyzer.py: without strict the API doesn't enforce this
    # schema, so a list-typed field can come back as a string and fail in Pydantic instead.
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "overall_summary": {"type": "string", "description": "1-2 sentence company-wide summary"},
            "org_insights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "org_id": {"type": "integer"},
                        "churn_risk": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                        # Scale is spelled out in the description because strict mode rejects
                        # numeric minimum/maximum -- otherwise the model can quietly answer on a
                        # 0-1 scale and 0.85 renders as "0.9/10 confidence".
                        "product_confidence": {
                            "type": "number",
                            "description": "0 to 10, where 10 = fully confident in the product",
                        },
                        "why": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["org_id", "churn_risk", "product_confidence", "why"],
                    "additionalProperties": False,
                },
            },
            "component_insights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "component": {"type": "string"},
                        "common_themes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["component", "common_themes"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["overall_summary", "org_insights", "component_insights"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """You are a customer success analyst producing a monthly report from \
PRE-AGGREGATED ticket tone statistics (not raw transcripts). For each organization given, \
decide churn_risk (LOW/MEDIUM/HIGH) and a product_confidence score (0-10, 10 = fully confident) \
based on: frustration trend vs prior month, ticket volume, tone mix, and whether the same \
problem recurs across multiple tickets. Write 'why' as short bullets citing concrete evidence \
(e.g. '6 SAML-related tickets', 'frustration up 32% vs July'). Only flag HIGH risk when the \
pattern is clear -- rising or sustained high frustration, not a single bad ticket. For each \
product component, summarize common_themes as 2-4 short bullets (e.g. 'Group mapping', \
'Configuration') based on the sample signals given, not generic guesses."""


@dataclass
class ScoredTicket:
    """One Zendesk ticket that carries our AI analysis tags/fields, parsed back out of the
    ticket JSON. Zendesk is the only store for this data -- there is no local database."""

    ticket_id: int
    org_id: int | None
    tone_category: str
    frustration_score: float
    confidence: float
    component_tags: list[str] = field(default_factory=list)
    summary: str = ""


def _parse_ticket(ticket: dict) -> ScoredTicket | None:
    tags = ticket.get("tags", [])
    tone_category = next(
        (t[len(TONE_TAG_PREFIX):] for t in tags if t.startswith(TONE_TAG_PREFIX) and t[len(TONE_TAG_PREFIX):] in TONE_CATEGORIES),
        None,
    )
    if tone_category is None:
        return None  # scored tag present but tone tag missing/stale -- skip rather than guess

    fields_by_id = {f["id"]: f["value"] for f in ticket.get("custom_fields", []) if f.get("value") is not None}
    frustration = fields_by_id.get(settings.zendesk_frustration_field_id)
    if frustration is None:
        return None

    return ScoredTicket(
        ticket_id=ticket["id"],
        org_id=ticket.get("organization_id"),
        tone_category=tone_category,
        frustration_score=float(frustration),
        confidence=float(fields_by_id.get(settings.zendesk_confidence_field_id, 0.0)),
        component_tags=[t[len(TOPIC_TAG_PREFIX):] for t in tags if t.startswith(TOPIC_TAG_PREFIX)],
        summary=str(fields_by_id.get(settings.zendesk_summary_field_id, "")),
    )


def _period_bounds(reference: datetime | None = None) -> tuple[datetime, datetime, datetime, datetime]:
    ref = reference or datetime.utcnow()
    period_end = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    period_start = (period_end - timedelta(days=1)).replace(day=1)
    prior_end = period_start
    prior_start = (prior_end - timedelta(days=1)).replace(day=1)
    return period_start, period_end, prior_start, prior_end


async def _fetch_rows(start: datetime, end: datetime) -> list[ScoredTicket]:
    tickets = await search_scored_tickets(start, end)
    rows = [_parse_ticket(t) for t in tickets]
    return [r for r in rows if r is not None]


def _tone_pct(rows: list[ScoredTicket]) -> dict:
    if not rows:
        return {k: 0.0 for k in TONE_CATEGORIES}
    counts = defaultdict(int)
    for r in rows:
        counts[r.tone_category] += 1
    return {k: round(100 * counts.get(k, 0) / len(rows), 1) for k in TONE_CATEGORIES}


def _avg_frustration(rows: list[ScoredTicket]) -> float:
    return round(sum(r.frustration_score for r in rows) / len(rows), 1) if rows else 0.0


def _top_components(rows: list[ScoredTicket], n: int) -> list[dict]:
    counts = defaultdict(int)
    for r in rows:
        for c in r.component_tags:
            counts[c] += 1
    return [{"component": c, "ticket_count": n_} for c, n_ in sorted(counts.items(), key=lambda x: -x[1])[:n]]


def compute_company_stats(rows: list[ScoredTicket]) -> dict:
    return {
        "ticket_count": len(rows),
        "tone_pct": _tone_pct(rows),
        "avg_frustration": _avg_frustration(rows),
        "top_problem_areas": _top_components(rows, TOP_PROBLEM_AREAS_N),
    }


async def _fetch_org_meta(org_ids: set[int]) -> dict[int, dict]:
    """One Zendesk API call per distinct org that appeared this month -- org name +
    renewal date live on the Organization record, not duplicated per ticket."""
    meta: dict[int, dict] = {}
    for org_id in org_ids:
        try:
            org = await get_organization(org_id)
            meta[org_id] = {"name": org.get("name") or f"Org {org_id}", "renewal_date": extract_renewal_date(org)}
        except Exception:
            meta[org_id] = {"name": f"Org {org_id}", "renewal_date": None}
    return meta


def compute_org_stats(rows: list[ScoredTicket], prior_rows: list[ScoredTicket], org_meta: dict[int, dict]) -> list[dict]:
    by_org = defaultdict(list)
    for r in rows:
        if r.org_id is not None:
            by_org[r.org_id].append(r)
    prior_by_org = defaultdict(list)
    for r in prior_rows:
        if r.org_id is not None:
            prior_by_org[r.org_id].append(r)

    out = []
    for org_id, org_rows in by_org.items():
        avg = _avg_frustration(org_rows)
        prior_avg = _avg_frustration(prior_by_org.get(org_id, []))
        trend_pct = round(100 * (avg - prior_avg) / prior_avg, 1) if prior_avg else None
        meta = org_meta.get(org_id, {"name": f"Org {org_id}", "renewal_date": None})
        out.append(
            {
                "org_id": org_id,
                "org_name": meta["name"],
                "ticket_count": len(org_rows),
                "avg_frustration": avg,
                "prev_avg_frustration": prior_avg,
                "trend_pct": trend_pct,
                "tone_pct": _tone_pct(org_rows),
                "top_issues": _top_components(org_rows, 3),
                "sample_signals": [r.summary for r in org_rows if r.summary][:6],
                "renewal_date": meta["renewal_date"],
                "eligible_for_insight": len(org_rows) >= MIN_TICKETS_FOR_ORG_INSIGHT,
            }
        )
    return sorted(out, key=lambda o: -o["avg_frustration"])


def compute_component_stats(rows: list[ScoredTicket], prior_rows: list[ScoredTicket]) -> list[dict]:
    by_component = defaultdict(list)
    for r in rows:
        for c in r.component_tags:
            by_component[c].append(r)
    prior_by_component = defaultdict(list)
    for r in prior_rows:
        for c in r.component_tags:
            prior_by_component[c].append(r)

    out = []
    for component, comp_rows in by_component.items():
        avg = _avg_frustration(comp_rows)
        prior_avg = _avg_frustration(prior_by_component.get(component, []))
        trend_pct = round(100 * (avg - prior_avg) / prior_avg, 1) if prior_avg else None
        out.append(
            {
                "component": component,
                "ticket_count": len(comp_rows),
                "orgs_affected": len({r.org_id for r in comp_rows if r.org_id is not None}),
                "avg_frustration": avg,
                "trend_pct": trend_pct,
                "sample_signals": [r.summary for r in comp_rows if r.summary][:6],
                "eligible_for_insight": len(comp_rows) >= MIN_TICKETS_FOR_COMPONENT_INSIGHT,
            }
        )
    return sorted(out, key=lambda c: -c["ticket_count"])


def synthesize(company_stats: dict, org_stats: list[dict], component_stats: list[dict]) -> MonthlySynthesisResult:
    eligible_orgs = [o for o in org_stats if o["eligible_for_insight"]]
    eligible_components = [c for c in component_stats if c["eligible_for_insight"]]

    if not eligible_orgs and not eligible_components:
        return MonthlySynthesisResult(overall_summary="Not enough ticket volume this period for account or product-level insights.")

    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[SYNTHESIS_TOOL],
        tool_choice={"type": "tool", "name": "record_monthly_synthesis"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Company stats:\n{json.dumps(company_stats, indent=2)}\n\n"
                    f"Per-organization stats:\n{json.dumps(eligible_orgs, indent=2)}\n\n"
                    f"Per-component stats:\n{json.dumps(eligible_components, indent=2)}"
                ),
            }
        ],
    )
    return MonthlySynthesisResult(**tool_input(response, "record_monthly_synthesis"))


def _risk_emoji(risk: str) -> str:
    return {"HIGH": "\U0001F534", "MEDIUM": "\U0001F7E1", "LOW": "\U0001F7E2"}.get(risk, "")


def _trend_arrow(trend_pct: float | None) -> str:
    if trend_pct is None:
        return ""
    return " ↑" if trend_pct > 0 else (" ↓" if trend_pct < 0 else "")


def render_report(period_start: datetime, company_stats: dict, customer_health: list[dict], product_health: list[dict], overall_summary: str) -> str:
    lines = [f"{period_start.strftime('%B %Y')}", "─" * 24, "", f"{company_stats['ticket_count']} tickets analyzed", ""]
    tp = company_stats["tone_pct"]
    lines += [
        f"Positive:      {tp['positive']}%",
        f"Neutral:       {tp['neutral']}%",
        f"Frustrated:    {tp['frustrated']}%",
        f"Angry:         {tp['angry']}%",
        "",
        f"Average frustration: {company_stats['avg_frustration']}/10",
        "",
        "Top problem areas:",
    ]
    for i, area in enumerate(company_stats["top_problem_areas"], 1):
        lines.append(f"{i}. {area['component']}")
    lines += ["", overall_summary, "", "CUSTOMER HEALTH", "─" * 24, ""]

    for c in customer_health:
        lines.append(f"{_risk_emoji(c.get('churn_risk', ''))} {c['org_name']}")
        lines.append(f"Tickets: {c['ticket_count']}")
        trend = f" (prev {c['prev_avg_frustration']}){_trend_arrow(c['trend_pct'])}" if c["prev_avg_frustration"] else ""
        lines.append(f"Average frustration: {c['avg_frustration']}/10{trend}")
        if c.get("churn_risk"):
            lines.append(f"Product confidence: {c.get('product_confidence', '?')}/10")
            lines.append(f"Churn risk: {c['churn_risk']}")
            if c.get("why"):
                lines.append("Why:")
                for w in c["why"]:
                    lines.append(f"  • {w}")
        if c.get("renewal_date"):
            lines.append(f"Renewal date: {c['renewal_date']}")
        lines.append("")

    lines += ["PRODUCT HEALTH", "─" * 24, ""]
    for p in product_health:
        lines.append(p["component"])
        lines.append("─" * len(p["component"]))
        lines.append(f"Tickets: {p['ticket_count']}")
        lines.append(f"Avg frustration: {p['avg_frustration']}/10")
        lines.append(f"Organizations affected: {p['orgs_affected']}")
        if p.get("trend_pct") is not None:
            arrow = "↑" if p["trend_pct"] > 0 else "↓"
            lines.append(f"{arrow} {abs(p['trend_pct'])}% vs prior month")
        if p.get("common_themes"):
            lines.append("Common themes:")
            for t in p["common_themes"]:
                lines.append(f"  • {t}")
        lines.append("")

    return "\n".join(lines)


async def run_monthly_report(reference: datetime | None = None) -> str:
    # _parse_ticket looks tickets' custom fields up by these IDs. Unset, every lookup is
    # keyed on None, matches nothing, and the run renders a clean-looking empty report
    # instead of failing -- so check before spending any API calls.
    require_field_ids()

    period_start, period_end, prior_start, prior_end = _period_bounds(reference)
    rows = await _fetch_rows(period_start, period_end)
    prior_rows = await _fetch_rows(prior_start, prior_end)

    org_ids = {r.org_id for r in rows if r.org_id is not None}
    org_meta = await _fetch_org_meta(org_ids)

    company_stats = compute_company_stats(rows)
    org_stats = compute_org_stats(rows, prior_rows, org_meta)
    component_stats = compute_component_stats(rows, prior_rows)

    synthesis = synthesize(company_stats, org_stats, component_stats)
    org_insight_by_id = {oi.org_id: oi for oi in synthesis.org_insights}
    component_insight_by_name = {ci.component: ci for ci in synthesis.component_insights}

    customer_health = []
    for o in org_stats:
        insight = org_insight_by_id.get(o["org_id"])
        row = dict(o)
        if insight:
            row["churn_risk"] = insight.churn_risk
            row["product_confidence"] = insight.product_confidence
            row["why"] = insight.why
        customer_health.append(row)

    product_health = []
    for c in component_stats:
        insight = component_insight_by_name.get(c["component"])
        row = dict(c)
        if insight:
            row["common_themes"] = insight.common_themes
        product_health.append(row)

    # Write the verdict back onto the Zendesk Organization record so churn risk is visible
    # in Zendesk year-round, not only inside that month's Slack post.
    for row in customer_health:
        if row.get("churn_risk"):
            await update_organization_health(
                row["org_id"], row["churn_risk"], row["product_confidence"], row["why"], datetime.utcnow()
            )

    report_text = render_report(period_start, company_stats, customer_health, product_health, synthesis.overall_summary)
    await post_monthly_report(period_start, company_stats, customer_health, product_health, synthesis.overall_summary)
    return report_text


if __name__ == "__main__":
    print(asyncio.run(run_monthly_report()))
