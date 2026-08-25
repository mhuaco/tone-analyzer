"""One-time setup: creates the Zendesk custom ticket fields this app writes AI analysis
into (organization fields are looked up/updated by key at runtime, so they don't need IDs
in .env). Safe to re-run -- skips any field that already exists.

Note: Zendesk's Ticket Fields API doesn't reliably persist the custom `key` we send on
creation (organization fields do), so existing-field detection for ticket fields matches
on title instead of key.

Usage:
    python -m scripts.setup_zendesk_fields
Then copy the printed ticket field IDs into .env.
"""

import asyncio

import httpx

from app.zendesk_auth import BASE_URL, auth_headers

TICKET_FIELDS = [
    {"type": "decimal", "title": "Tone Frustration Score", "key": "tone_frustration_score"},
    {"type": "decimal", "title": "Tone Confidence", "key": "tone_confidence"},
    {"type": "textarea", "title": "Tone Summary", "key": "tone_summary"},
    {"type": "date", "title": "Tone Analyzed At", "key": "tone_analyzed_at"},
]

ORG_FIELDS = [
    {"type": "decimal", "title": "Product Confidence", "key": "product_confidence"},
    {"type": "textarea", "title": "Tone Health Why", "key": "tone_health_why"},
    {"type": "date", "title": "Tone Health Updated At", "key": "tone_health_updated_at"},
]


async def _ensure_ticket_fields(client: httpx.AsyncClient) -> dict[str, int]:
    resp = await client.get(f"{BASE_URL}/ticket_fields.json", params={"per_page": 100})
    resp.raise_for_status()
    existing_by_title = {f["title"]: f["id"] for f in resp.json()["ticket_fields"]}

    ids: dict[str, int] = {}
    for spec in TICKET_FIELDS:
        if spec["title"] in existing_by_title:
            ids[spec["key"]] = existing_by_title[spec["title"]]
            print(f"  exists: {spec['title']} (id={ids[spec['key']]})")
            continue
        resp = await client.post(f"{BASE_URL}/ticket_fields.json", json={"ticket_field": spec})
        resp.raise_for_status()
        created = resp.json()["ticket_field"]
        ids[spec["key"]] = created["id"]
        print(f"  created: {spec['title']} (id={created['id']})")
    return ids


async def _ensure_org_fields(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{BASE_URL}/organization_fields.json", params={"per_page": 100})
    resp.raise_for_status()
    existing = {f["key"] for f in resp.json()["organization_fields"] if f.get("key")}

    for spec in ORG_FIELDS:
        if spec["key"] in existing:
            print(f"  exists: {spec['title']}")
            continue
        resp = await client.post(f"{BASE_URL}/organization_fields.json", json={"organization_field": spec})
        resp.raise_for_status()
        print(f"  created: {spec['title']}")


async def main() -> None:
    async with httpx.AsyncClient(headers=await auth_headers(), timeout=30) as client:
        print("Ticket fields:")
        ticket_ids = await _ensure_ticket_fields(client)
        print("\nOrganization fields:")
        await _ensure_org_fields(client)

    print("\nAdd these to .env:")
    print(f"ZENDESK_FRUSTRATION_FIELD_ID={ticket_ids['tone_frustration_score']}")
    print(f"ZENDESK_CONFIDENCE_FIELD_ID={ticket_ids['tone_confidence']}")
    print(f"ZENDESK_SUMMARY_FIELD_ID={ticket_ids['tone_summary']}")
    print(f"ZENDESK_ANALYZED_AT_FIELD_ID={ticket_ids['tone_analyzed_at']}")
    print(
        "\nOptional: drag the new ticket fields onto your ticket form in Admin Center so "
        "agents see them in the sidebar (Admin Center -> Ticket Fields / Ticket Forms). "
        "The API writes to them regardless of form placement."
    )


if __name__ == "__main__":
    asyncio.run(main())
