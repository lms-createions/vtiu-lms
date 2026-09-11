"""Agora RTC token helpers.

The App ID may be used by browser clients. The App Certificate must remain
server-side and should only be supplied through environment configuration.
"""

from datetime import datetime, timedelta, timezone


def build_rtc_token(app_id, app_certificate, channel_name, uid, role, expires_in=3600):
    """Build a short-lived Agora RTC token for a channel participant."""
    if not app_id or not app_certificate:
        raise RuntimeError(
            "Agora is not configured. Set AGORA_APP_ID and AGORA_APP_CERTIFICATE."
        )

    try:
        from agora_token_builder import RtcTokenBuilder, Role_Publisher, Role_Subscriber
    except ImportError as exc:
        raise RuntimeError(
            "Agora token support is unavailable. Install the agora-token-builder package."
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
