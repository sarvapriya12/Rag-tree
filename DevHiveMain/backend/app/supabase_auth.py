from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .config import Settings


def _configured(settings: Settings, invitation: bool = False) -> None:
    parsed = urlsplit(settings.supabase_url)
    if ((parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}))
            or not parsed.netloc or parsed.username or parsed.password):
        raise HTTPException(503, "Supabase Auth is not configured")
    if not settings.supabase_publishable_key:
        raise HTTPException(503, "Supabase Auth is not configured")
    if invitation and (not settings.supabase_secret_key or not settings.site_url):
        raise HTTPException(503, "Supabase invitations are not configured")


def verified_user(settings: Settings, access_token: str) -> dict:
    _configured(settings)
    try:
        response = httpx.get(f"{settings.supabase_url}/auth/v1/user", timeout=10,
                             headers={"apikey": settings.supabase_publishable_key,
                                      "Authorization": f"Bearer {access_token}"})
    except httpx.RequestError as exc:
        raise HTTPException(502, "Supabase Auth is unavailable") from exc
    if response.status_code != 200:
        raise HTTPException(401, "Supabase session is invalid")
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Invalid Supabase Auth response") from exc
    if (not isinstance(data, dict) or not isinstance(data.get("id"), str)
            or not isinstance(data.get("email"), str) or not data.get("email_confirmed_at")):
        raise HTTPException(401, "A verified Supabase email is required")
    return data


def send_invitation(settings: Settings, email: str) -> None:
    _configured(settings, invitation=True)
    headers = {"apikey": settings.supabase_secret_key,
               "Content-Type": "application/json"}
    if not settings.supabase_secret_key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {settings.supabase_secret_key}"
    try:
        response = httpx.post(f"{settings.supabase_url}/auth/v1/invite",
                              params={"redirect_to": f"{settings.site_url}/auth/callback"},
                              json={"email": email}, headers=headers, timeout=15)
    except httpx.RequestError as exc:
        raise HTTPException(502, "Supabase invitation service is unavailable") from exc
    if response.status_code in {200, 201}:
        return
    # Supabase's invite endpoint rejects people who already have a Supabase
    # account. Send them a magic link instead, with account creation disabled.
    if response.status_code in {400, 422} and "already" in response.text.lower():
        try:
            response = httpx.post(f"{settings.supabase_url}/auth/v1/otp",
                                  params={"redirect_to": f"{settings.site_url}/auth/callback"},
                                  json={"email": email, "create_user": False,
                                        "options": {"email_redirect_to": f"{settings.site_url}/auth/callback"}},
                                  headers={"apikey": settings.supabase_publishable_key,
                                           "Content-Type": "application/json"}, timeout=15)
        except httpx.RequestError as exc:
            raise HTTPException(502, "Supabase invitation service is unavailable") from exc
        if response.status_code in {200, 201}:
            return
    raise HTTPException(502, f"Supabase could not send the invitation (HTTP {response.status_code})")
