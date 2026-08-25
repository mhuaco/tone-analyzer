"""OAuth client_credentials auth for the Zendesk API. Zendesk is retiring API tokens
(phased out starting July 28, 2026) in favor of OAuth. client_credentials is the flow
Zendesk recommends for server-side automation with no end user present -- see
https://developer.zendesk.com/documentation/authentication/api-tokens-to-oauth/

Requires an OAuth client created in Admin Center -> Apps and integrations -> APIs ->
OAuth clients, client kind "Confidential" (no redirect URI needed for this flow).
"""

import asyncio
import time

import httpx

from app.config import settings

BASE_URL = f"https://{settings.zendesk_subdomain}.zendesk.com/api/v2"
_TOKEN_URL = f"https://{settings.zendesk_subdomain}.zendesk.com/oauth/tokens"
_SCOPE = "read write"

_token: str | None = None
_expires_at: float = 0.0
_lock = asyncio.Lock()


async def _fetch_token() -> tuple[str, float]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": settings.zendesk_client_id,
                "client_secret": settings.zendesk_client_secret,
                "scope": _SCOPE,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # Refresh a minute early so an in-flight request never races token expiry.
        return data["access_token"], time.monotonic() + data["expires_in"] - 60


async def get_access_token() -> str:
    """client_credentials tokens are short-lived (Zendesk defaults to 30 min) and carry
    no refresh token, so we just cache in memory and re-request once it's about to expire."""
    global _token, _expires_at
    async with _lock:
        if _token is None or time.monotonic() >= _expires_at:
            _token, _expires_at = await _fetch_token()
        return _token


async def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {await get_access_token()}"}
