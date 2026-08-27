# Zendesk Tone Analyzer

Matches this pipeline:

```
Zendesk (ticket new/open/updated/replied/solved/closed) -> Webhook -> FastAPI -> Zendesk API (full ticket)
  -> LLM -> Structured Analysis -> written back onto the ticket (tags + custom fields)
  -> [end of month] -> Monthly Job -> Zendesk Search API -> stats in plain Python
  -> LLM synthesizes churn risk / product themes -> written onto Organizations -> Slack
```

**Key design principle:** Zendesk is the only system of record -- there is no separate
database. The LLM scores tone continuously, one ticket at a time, as tickets move through
their lifecycle, and the result is written straight onto the ticket as tags (tone category,
topics) and custom fields (frustration score, confidence, summary). The monthly job does NOT
re-analyze raw transcripts -- it searches Zendesk for tickets already tagged/scored that
month, computes stats in plain Python (percentages, averages, trends) from the tags/fields
already sitting on those tickets, and sends only that *pre-aggregated* summary to the LLM for
one synthesis pass (churn-risk reasoning, common-theme labeling). This keeps the monthly job
fast and cheap regardless of ticket volume, and keeps the only moving infrastructure piece as
FastAPI + the LLM call -- no database to provision, migrate, or back up.

## Where the data lives

| Data | Where in Zendesk |
|---|---|
| Tone category (positive/neutral/frustrated/angry) | Ticket tag: `tone_<category>` |
| Marker that a ticket has been AI-scored at least once | Ticket tag: `tone_scored` |
| Product areas referenced | Ticket tags: `topic_<name>`, one per area, from a **fixed vocabulary** (see `Topic` in `app/schemas.py`): `gateway`, `dashboard`, `pump`, `sync`, `mdcb`, `operator`, `helm_charts`, `sso` (covers Identity Broker). The LLM can only pick from this list -- it can't invent new topic tags. An earlier freeform version was dropped because it produced near-duplicate tags across tickets (e.g. "export" vs "csv_export"), which fragments Explore reporting. Add a new area by extending the `Topic` literal in `schemas.py` *and* the matching enum in `llm_analyzer.py`'s `TONE_TOOL`. |
| Frustration score (0-10), confidence (0-1), one-line summary, analyzed date | Ticket custom fields (created by `scripts/setup_zendesk_fields.py`) |
| Per-org churn risk (LOW/MEDIUM/HIGH) | Organization tag: `churn_risk_<level>` |
| Per-org product confidence, "why" bullets, last-updated date | Organization custom fields |
| Monthly report | Not stored anywhere new -- rendered fresh from the data above each run and posted to Slack. Slack channel history is the archive. |

Because tone/topic/risk live as ordinary tags and fields, Zendesk Explore, ticket views,
triggers, and automations all work against them directly -- no export or sync step.

## Report structure (three levels)

1. **Company** -- overall tone mix, avg frustration, top problem areas. Pure computed stats, no LLM.
2. **Customer** -- per-org frustration score + trend vs prior month, churn_risk (LOW/MEDIUM/HIGH),
   product_confidence (0-10), and "why" bullets. Computed stats + LLM reasoning, merged by `org_id`.
   Orgs with fewer than `MIN_TICKETS_FOR_ORG_INSIGHT` (default 3) tickets get stats but no risk
   call -- not enough signal to avoid false positives.
3. **Product** -- per-component (e.g. "gateway", "sso") ticket count, avg frustration, orgs
   affected, trend, and common_themes. Same computed-stats + LLM-reasoning merge, by component
   name, drawn from the fixed `topic_*` vocabulary (see "Where the data lives" above).

This mirrors the two audiences from the original brief: customer-health rows are what you'd feed
to CS/commercial (Slack `SLACK_WEBHOOK_CS`); product-health rows are what you'd feed to
Product/Engineering (`SLACK_WEBHOOK_PRODUCT` / `SLACK_WEBHOOK_ENGINEERING`).

## 1. Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Zendesk + Anthropic creds, leave the four ZENDESK_*_FIELD_ID blank for now
```

**Zendesk auth is OAuth, not an API token.** Zendesk is retiring API tokens (phased out
starting July 28, 2026 -- new accounts created after that date can't create tokens at all).
This app authenticates with the OAuth `client_credentials` grant instead, which is what
Zendesk recommends for server-side automation with no end user present:

1. Admin Center -> Apps and integrations -> APIs -> OAuth clients -> **Add OAuth client**
2. Client kind: **Confidential**. No redirect URI is needed for this flow.
3. Save the generated client ID and secret into `ZENDESK_CLIENT_ID` / `ZENDESK_CLIENT_SECRET` in `.env`.

`app/zendesk_auth.py` exchanges those for a short-lived bearer token on demand and caches
it in memory, re-requesting automatically as it nears expiry -- no refresh-token handling
needed since `client_credentials` doesn't issue one. Note: actions taken by this token
(tag/field updates, comments) are attributed in Zendesk to whichever admin created the
OAuth client.

Create the custom fields the app writes AI analysis into:

```bash
python -m scripts.setup_zendesk_fields
```

This creates four ticket fields (`Tone Frustration Score`, `Tone Confidence`, `Tone Summary`,
`Tone Analyzed At`) and three organization fields (`Product Confidence`, `Tone Health Why`,
`Tone Health Updated At`) via the Zendesk API, and prints the ticket field IDs to paste into
`.env`. It's idempotent -- safe to re-run, it skips fields that already exist.

Optionally drag the new ticket fields onto your ticket form in Admin Center so agents see them
in the ticket sidebar; the API writes to them regardless of form placement.

## 2. Run the API

```bash
uvicorn app.main:app --reload --port 8000
```

Expose it publicly (e.g. `ngrok http 8000` for local testing, or deploy to Fly.io/Render/AWS/etc. for production) so Zendesk can reach it.

## 3. Configure Zendesk

Zendesk webhooks can subscribe directly to Zendesk events -- no separate trigger needed (and a
webhook can't use both a trigger *and* an event subscription; it's one or the other). Admin
Center -> Apps and integrations -> Webhooks -> Create webhook:
- Endpoint URL: `https://<your-domain>/webhooks/zendesk/ticket-event`
- Method: `POST`, Request format: `JSON`
- Connect to: **Zendesk events**, and subscribe to just:
  - **Status changed** -- fires on every status transition; `app/main.py` filters this down to
    only actually analyze when the new status is `solved` or `closed`
- Authentication: enable "Signing secret" and put the same value in `ZENDESK_WEBHOOK_SECRET` in `.env`

**Don't subscribe to "Tags changed."** This app writes tags back onto the ticket as its own
analysis output; subscribing to tag-change events would make the webhook re-trigger itself on
every analysis pass, in a loop. "Status changed" is never touched by our own write-back (which
only ever changes tags/custom fields, never status), so there's no feedback loop.

**Design trade-off, chosen deliberately:** analyzing only at solved/closed means a ticket that
stays open/on-hold past the end of a reporting month contributes zero data to that month's
report -- it won't show up until it resolves (if it ever does). The alternative (score
continuously on create/comment/status-change) avoids that gap but costs more LLM calls per
ticket. This app is currently configured for solved/closed-only; see `RESOLVED_STATUSES` in
`app/main.py` if you want to revisit that trade-off later.

Event-subscribed webhooks send Zendesk's own event envelope as the body (not a custom JSON body
you define) -- the ticket id and status live at `detail.id` / `detail.status`, e.g.
`{"type": "zen:event-type:ticket.status_changed", "detail": {"id": "50", "status": "SOLVED", ...}, ...}`.
`app/main.py` reads both from there.

The API responds immediately and, if the new status is solved/closed, scores the ticket in a
background task (fetches the full ticket + comments from Zendesk, sends to Claude, writes the
tone tag/topic tags/custom fields straight back onto the ticket). Each pass replaces the
ticket's previous `tone_*`/`topic_*` tags with the fresh result, so a ticket's tags always
reflect its latest analyzed state, not an accumulation of history.

## 4. Build Zendesk Explore dashboards

Since the AI output lives as tags and custom fields on tickets and organizations, build
dashboards directly in Zendesk Explore (Support dataset):

- **Tone mix over time**: metric = ticket count, attribute = `Ticket tags` filtered/grouped by
  `tone_positive` / `tone_neutral` / `tone_frustrated` / `tone_angry`
- **Avg frustration trend**: metric = average of the `Tone Frustration Score` ticket field, over
  time (monthly)
- **Per-organization health**: group by `Organization`, metric = average `Tone Frustration Score`;
  add `Organization tags` (`churn_risk_high` etc.) as a filter to spotlight at-risk accounts
- **Top pain points**: group by `Ticket tags` filtered to `topic_*`, metric = ticket count

This is admin-console configuration, not code -- no export or sync job needed because the
underlying fields are ordinary Zendesk data.

## 5. Run the monthly job

```bash
python -m app.monthly_job
```

This searches Zendesk (via the Search API) for tickets tagged `tone_scored` and updated in the
prior calendar month, computes stats in plain Python from their tags/fields (not raw
transcripts), and asks Claude to synthesize:
- **At-risk accounts** -- sustained/declining negative tone, written onto the Zendesk
  Organization as a `churn_risk_<level>` tag + `Product Confidence`/`Tone Health Why` fields, so
  it's visible in Zendesk year-round, not only in that month's Slack post
- **Product friction** -- components with clustered negative tone across multiple accounts

The rendered report is posted straight to Slack (see below) and printed to stdout; nothing is
persisted in a new store -- Slack's channel history is the report archive.

**Scheduling it for real:** the simplest option is a cron entry:
```
0 6 1 * * cd /path/to/project && venv/bin/python -m app.monthly_job
```
Or use the included `apscheduler` dependency to run it in-process if you'd rather not rely on system cron.

**Known limit:** the Zendesk Search API caps results at 1000 tickets per query. Fine for a
pilot; if monthly analyzed-ticket volume grows past that, switch `search_scored_tickets` in
`app/zendesk_client.py` to the Incremental Ticket Export API instead.

## 6. Configure Slack reporting

Create an [incoming webhook](https://api.slack.com/messaging/webhooks) per target channel
(Customer Success, Product, Engineering -- or fewer, if some should share a channel) and set:

```
SLACK_WEBHOOK_CS=https://hooks.slack.com/services/...
SLACK_WEBHOOK_PRODUCT=https://hooks.slack.com/services/...
SLACK_WEBHOOK_ENGINEERING=https://hooks.slack.com/services/...
```

Any of the three can be left blank to skip that channel. The CS channel gets the company
summary plus the top at-risk accounts; Product and Engineering get the company summary plus the
top recurring product pain points.

## 7. Renewal dates (optional)

If you track renewal dates as a Zendesk organization custom field, set `ZENDESK_RENEWAL_FIELD_ID`
in `.env` and adjust the field key lookup in `zendesk_client.extract_renewal_date` to match your
field's key. If renewal dates live in a CRM instead, skip this and join them onto `customer_health`
rows in `monthly_job.py` from that source -- `org_id`/`org_name` are already there to join on.

## 8. What's NOT built yet (by design)

- Retry/dead-letter handling on the webhook background task -- fine for a pilot, add a queue
  (e.g. Celery/RQ) before scaling to full ticket volume
- "Improving accounts" surfaced separately from "declining accounts" in the rendered
  report/Slack post -- currently sorted by frustration/risk only; easy to add once you decide
  whether CS wants that split out
- Automatic Zendesk trigger/automation off the `churn_risk_high` organization tag (e.g.
  auto-notify the assigned CSM) -- the tag is there to build that on top of, intentionally left
  as a manual Admin Center step until the rubric and thresholds are validated by hand
