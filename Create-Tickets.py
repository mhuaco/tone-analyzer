#!/usr/bin/env python3
"""
Seed a Zendesk instance with realistic test data:
- N organizations
- Multiple users per organization (with roles: end-users)
- 50 tickets, each from a real user/org, with varied subjects, priorities,
  statuses, tags, and channels — so it looks like an active support queue.

SETUP (OAuth client_credentials)
---------------------------------
1. pip install requests faker
2. Set these in your environment (e.g. via a .env file loaded by your shell,
   or `export` them directly). NEVER commit this file or paste secrets into
   chat/tickets/logs -- rotate immediately if one leaks:

   export ZENDESK_SUBDOMAIN="your-subdomain"
   export ZENDESK_CLIENT_ID="your-oauth-client-id"
   export ZENDESK_CLIENT_SECRET="your-oauth-client-secret"

   $env:ZENDESK_SUBDOMAIN = "your-subdomain"
   $env:ZENDESK_CLIENT_ID = "your-oauth-client-id"
   $env:ZENDESK_CLIENT_SECRET = "your-oauth-client-secret"

   These come from Admin Center -> Apps and integrations -> APIs -> OAuth
   clients. The script exchanges them for a short-lived bearer token via the
   client_credentials grant, then uses that token for all API calls.

3. Run: python3 seed_zendesk.py

Docs: https://developer.zendesk.com/documentation/ticketing/working-with-oauth/creating-and-using-oauth-tokens-with-the-api/#client-credentials-grant
"""

import os
import sys
import time
import random
import requests
from faker import Faker

fake = Faker()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN")
CLIENT_ID = os.environ.get("ZENDESK_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ZENDESK_CLIENT_SECRET")

# Space-separated OAuth scopes needed for this script. Adjust in your OAuth
# client config if your Zendesk instance restricts available scopes.
OAUTH_SCOPES = "organizations:write users:write tickets:write"

NUM_ORGS = 10
USERS_PER_ORG_RANGE = (2, 6)   # each org gets a random number of users in this range
NUM_TICKETS = 50

if not all([SUBDOMAIN, CLIENT_ID, CLIENT_SECRET]):
    sys.exit(
        "Missing credentials. Set ZENDESK_SUBDOMAIN, ZENDESK_CLIENT_ID, and "
        "ZENDESK_CLIENT_SECRET as environment variables before running."
    )

BASE_URL = f"https://{SUBDOMAIN}.zendesk.com/api/v2"
TOKEN_URL = f"https://{SUBDOMAIN}.zendesk.com/oauth/tokens"


def get_access_token():
    """Exchange client_id/client_secret for a short-lived bearer token."""
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": OAUTH_SCOPES,
    }
    resp = requests.post(TOKEN_URL, json=payload)
    if resp.status_code != 200:
        sys.exit(
            f"Failed to get OAuth access token: {resp.status_code} {resp.text[:300]}\n"
            "Double check the client is 'Confidential' kind and the scopes are enabled "
            "for this OAuth client in Admin Center."
        )
    return resp.json()["access_token"]


ACCESS_TOKEN = get_access_token()

SESSION = requests.Session()
SESSION.headers.update({
    "Content-Type": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}",
})


def request_with_retry(method, url, **kwargs):
    """Basic wrapper that respects Zendesk's 429 rate-limit responses."""
    while True:
        resp = SESSION.request(method, url, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        return resp


# ---------------------------------------------------------------------------
# Sample data pools for realistic variety
# ---------------------------------------------------------------------------
TICKET_TEMPLATES = [
    ("Can't log into my account", "I keep getting an 'invalid credentials' error even after resetting my password.", "problem", "urgent"),
    ("Billing charge looks wrong", "My invoice this month is higher than expected — can someone check?", "problem", "high"),
    ("Feature request: dark mode", "Would love to see a dark mode option in the dashboard.", "task", "low"),
    ("Export is failing silently", "When I click export to CSV nothing happens, no error either.", "incident", "high"),
    ("How do I add a teammate?", "Trying to invite a colleague to my workspace, can't find the option.", "question", "normal"),
    ("API returning 500 errors", "Our integration started failing this morning with intermittent 500s.", "incident", "urgent"),
    ("Cancel my subscription", "I'd like to cancel my plan at the end of the billing cycle.", "task", "normal"),
    ("Data missing after sync", "Some records disappeared after the last sync with our CRM.", "problem", "high"),
    ("Slow dashboard load times", "Pages are taking 10+ seconds to load since yesterday.", "problem", "normal"),
    ("Request for SSO setup help", "We're trying to configure SAML SSO and need guidance.", "task", "normal"),
    ("Mobile app crashes on launch", "The iOS app crashes immediately after opening on iOS 18.", "incident", "high"),
    ("Question about usage limits", "Not sure what happens if we go over our monthly limit.", "question", "low"),
    ("Wrong permissions on new user", "New teammate got admin access when they should be view-only.", "problem", "normal"),
    ("Refund request", "We were double charged and would like a refund for the duplicate.", "task", "high"),
    ("Integration with Slack not working", "Notifications stopped coming through to our Slack channel.", "problem", "normal"),
]

STATUSES = ["new", "open", "pending", "hold", "solved"]
CHANNELS = ["email", "web", "chat", "api"]
TAGS_POOL = ["vip", "trial", "enterprise", "bug", "billing", "onboarding", "follow_up", "escalation"]


# ---------------------------------------------------------------------------
# Step 1: Create organizations
# ---------------------------------------------------------------------------
def create_organizations(n):
    orgs = []
    print(f"Creating {n} organizations...")
    for i in range(n):
        name = fake.unique.company()
        payload = {"organization": {"name": name, "notes": fake.catch_phrase()}}
        resp = request_with_retry("POST", f"{BASE_URL}/organizations.json", json=payload)
        if resp.status_code == 201:
            org = resp.json()["organization"]
            orgs.append(org)
            print(f"  [{i+1}/{n}] Created org: {org['name']} (id={org['id']})")
        else:
            print(f"  Failed to create org '{name}': {resp.status_code} {resp.text[:200]}")
    return orgs


# ---------------------------------------------------------------------------
# Step 2: Create users, distributed across organizations
# ---------------------------------------------------------------------------
def create_users(orgs):
    users = []
    print("Creating users for each organization...")
    for org in orgs:
        num_users = random.randint(*USERS_PER_ORG_RANGE)
        for _ in range(num_users):
            name = fake.name()
            email = fake.unique.email()
            payload = {
                "user": {
                    "name": name,
                    "email": email,
                    "role": "end-user",
                    "organization_id": org["id"],
                    "verified": True,
                }
            }
            resp = request_with_retry("POST", f"{BASE_URL}/users.json", json=payload)
            if resp.status_code == 201:
                user = resp.json()["user"]
                users.append(user)
                print(f"  Created user: {user['name']} <{user['email']}> @ {org['name']}")
            else:
                print(f"  Failed to create user '{name}': {resp.status_code} {resp.text[:200]}")
    return users


# ---------------------------------------------------------------------------
# Step 3: Create tickets, each tied to a random real user
# ---------------------------------------------------------------------------
def create_tickets(users, n):
    print(f"Creating {n} tickets...")
    created = 0
    for i in range(n):
        requester = random.choice(users)
        subject, body, ticket_type, priority = random.choice(TICKET_TEMPLATES)
        payload = {
            "ticket": {
                "subject": subject,
                "comment": {"body": f"{body}\n\n(From: {requester['name']})"},
                "requester_id": requester["id"],
                "priority": priority,
                "type": ticket_type,
                "status": random.choice(STATUSES),
                "via": {"channel": random.choice(CHANNELS)},
                "tags": random.sample(TAGS_POOL, k=random.randint(0, 2)),
            }
        }
        resp = request_with_retry("POST", f"{BASE_URL}/tickets.json", json=payload)
        if resp.status_code == 201:
            ticket = resp.json()["ticket"]
            created += 1
            print(f"  [{i+1}/{n}] Ticket #{ticket['id']}: {subject} (from {requester['name']})")
        else:
            print(f"  Failed to create ticket '{subject}': {resp.status_code} {resp.text[:200]}")
    return created


def main():
    orgs = create_organizations(NUM_ORGS)
    if not orgs:
        sys.exit("No organizations were created — check your credentials/permissions.")

    users = create_users(orgs)
    if not users:
        sys.exit("No users were created — check your credentials/permissions.")

    created = create_tickets(users, NUM_TICKETS)
    print(f"\nDone. {len(orgs)} orgs, {len(users)} users, {created}/{NUM_TICKETS} tickets created.")


if __name__ == "__main__":
    main()