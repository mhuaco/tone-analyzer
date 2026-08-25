from datetime import datetime

import httpx

from app.config import settings

_RISK_EMOJI = {"HIGH": "\U0001F534", "MEDIUM": "\U0001F7E1", "LOW": "\U0001F7E2"}


async def _post(webhook_url: str | None, text: str) -> None:
    if not webhook_url:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(webhook_url, json={"text": text})
        resp.raise_for_status()


def _company_summary(period_start: datetime, company_stats: dict, overall_summary: str) -> str:
    tp = company_stats["tone_pct"]
    return (
        f"*Customer Tone Report -- {period_start.strftime('%B %Y')}*\n"
        f"{company_stats['ticket_count']} tickets analyzed | avg frustration {company_stats['avg_frustration']}/10\n"
        f"Positive {tp['positive']}% · Neutral {tp['neutral']}% · "
        f"Frustrated {tp['frustrated']}% · Angry {tp['angry']}%\n\n{overall_summary}"
    )


async def post_monthly_report(
    period_start: datetime,
    company_stats: dict,
    customer_health: list[dict],
    product_health: list[dict],
    overall_summary: str,
) -> None:
    """Posts a Customer Success-focused summary (at-risk accounts) to the CS Slack channel,
    and a Product/Engineering-focused summary (recurring pain points) to those channels."""
    summary = _company_summary(period_start, company_stats, overall_summary)

    at_risk = [c for c in customer_health if c.get("churn_risk") in ("HIGH", "MEDIUM")]
    at_risk.sort(key=lambda c: 0 if c["churn_risk"] == "HIGH" else 1)
    cs_lines = [
        f"{_RISK_EMOJI.get(c['churn_risk'], '')} *{c['org_name']}* -- {c['avg_frustration']}/10 frustration, "
        f"{c['churn_risk']} risk"
        + (f", renews {c['renewal_date']}" if c.get("renewal_date") else "")
        for c in at_risk[:10]
    ]
    cs_text = summary + "\n\n*At-risk accounts*\n" + ("\n".join(cs_lines) if cs_lines else "None flagged this month.")
    await _post(settings.slack_webhook_cs, cs_text)

    top_products = sorted(product_health, key=lambda p: -p["ticket_count"])[:5]
    prod_lines = [
        f"*{p['component']}* -- {p['ticket_count']} tickets, {p['orgs_affected']} orgs affected, "
        f"avg frustration {p['avg_frustration']}/10"
        for p in top_products
    ]
    prod_text = summary + "\n\n*Top product pain points*\n" + (
        "\n".join(prod_lines) if prod_lines else "No recurring themes this month."
    )
    for url in (settings.slack_webhook_product, settings.slack_webhook_engineering):
        await _post(url, prod_text)
