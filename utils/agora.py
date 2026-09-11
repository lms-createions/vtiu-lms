"""Agora RTC token helpers.

The App ID may be used by browser clients. The App Certificate must remain
server-side and should only be supplied through environment configuration.
"""

from datetime import datetime, timedelta, timezone
import requests


WHITEBOARD_API_URL = "https://api.netless.link/v5"


def build_rtc_token(app_id, app_certificate, channel_name, uid, role, expires_in=3600):
    """Build a short-lived Agora RTC token for a channel participant."""
    if not app_id or not app_certificate:
        raise RuntimeError(
            "Agora is not configured. Set AGORA_APP_ID and AGORA_APP_CERTIFICATE."
        )

    try:
        from agora_token_builder.RtcTokenBuilder import (
            RtcTokenBuilder,
            Role_Publisher,
            Role_Subscriber,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Agora token support is unavailable. Install agora-token-builder==1.0.0."
        ) from exc

    token_role = Role_Publisher if role == "host" else Role_Subscriber
    privilege_expiry = int(
        (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).timestamp()
    )
    return RtcTokenBuilder.buildTokenWithUid(
        app_id,
        app_certificate,
        channel_name,
        int(uid),
        token_role,
        privilege_expiry,
    )


def create_whiteboard_room(sdk_token, region):
    """Create a Whiteboard room through Agora's server API."""
    if not sdk_token:
        raise RuntimeError("Whiteboard is not configured. Set WHITEBOARD_SDK_TOKEN.")

    response = requests.post(
        f"{WHITEBOARD_API_URL}/rooms",
        headers={
            "token": sdk_token,
            "region": region,
            "Content-Type": "application/json",
        },
        json={"isRecord": False},
        timeout=15,
    )
    if response.status_code != 201:
        raise RuntimeError(f"Whiteboard room creation failed: {response.status_code}")
    room = response.json()
    if not room.get("uuid"):
        raise RuntimeError("Whiteboard room response did not contain a UUID.")
    return room["uuid"]


def build_whiteboard_room_token(sdk_token, room_uuid, region, role, lifespan=3600000):
    """Create a short-lived Whiteboard room token for a user."""
    if not sdk_token or not room_uuid:
        raise RuntimeError("Whiteboard room credentials are incomplete.")

    response = requests.post(
        f"{WHITEBOARD_API_URL}/tokens/rooms/{room_uuid}",
        headers={
            "token": sdk_token,
            "region": region,
            "Content-Type": "application/json",
        },
        json={"lifespan": lifespan, "role": role},
        timeout=15,
    )
    if response.status_code != 201:
        raise RuntimeError(f"Whiteboard token creation failed: {response.status_code}")
    token = response.json()
    if isinstance(token, dict):
        token = token.get("token") or token.get("roomToken")
    if not token:
        raise RuntimeError("Whiteboard token response was empty.")
    return token
