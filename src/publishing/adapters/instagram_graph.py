"""Instagram Graph API adapter.

Posts one `PostPackage` to an Instagram **Business or Creator** account via
the Facebook Graph API. Personal IG accounts cannot use the publishing API;
the account must be linked to a Facebook Page that the access token's user
manages.

Four-step flow:

    1. Upload the JPEG to a public HTTPS URL                   (image_host)
    2. POST /<v>/<ig_user_id>/media?image_url=<URL>&caption=…  → creation_id
    3. POST /<v>/<ig_user_id>/media_publish?creation_id=…      → media_id
    4. GET  /<v>/<media_id>?fields=permalink                   → permalink

Returns `(media_id, permalink)` so the caller can persist both on the
`post_instances[]` row.

Required `Settings` fields (all from .env):
    ig_access_token         — long-lived Page access token for the FB Page
                              that owns the IG Business account. Scopes:
                              `instagram_basic`, `instagram_content_publish`,
                              `pages_show_list`, `pages_read_engagement`,
                              `business_management`. Long-lived tokens expire
                              after ~60 days; refresh via Graph API.
    ig_business_account_id  — IG Business account ID. Find with:
                              `GET /me/accounts` → page id → `GET /<page-id>?fields=instagram_business_account`
    ig_api_version          — `v21.0` by default
    image_host_*            — see src/publishing/image_host.py

Raises `InstagramPublishError` with the Graph response body attached when
any step returns non-2xx.
"""
from __future__ import annotations

from pathlib import Path

import requests

from src.common.config import Settings
from src.contracts.package import PostPackage
from src.publishing.image_host import ImageHostError, upload_image

_BASE = "https://graph.facebook.com"
_DEFAULT_API_VERSION = "v21.0"


class InstagramPublishError(RuntimeError):
    """Raised when any of the publish steps fails.

    The first arg is a short summary; `.status_code` and `.body` carry the
    full Graph response so the caller can surface them to the operator.
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def publish_to_instagram(package: PostPackage, settings: Settings) -> tuple[str, str]:
    """Post `package` to Instagram. Returns (media_id, permalink)."""
    _require_settings(settings)
    if package.channel != "instagram":
        raise InstagramPublishError(
            f"Instagram adapter received a non-instagram package (channel={package.channel!r})"
        )
    if not package.media:
        raise InstagramPublishError("Instagram adapter requires at least one image asset")

    asset = package.media[0]
    image_path = Path(asset.local_path)
    if not image_path.exists():
        raise InstagramPublishError(f"Image asset missing on disk: {image_path}")

    try:
        public_url = upload_image(
            settings,
            image_path,
            object_key=f"posts/{package.post_id}/{image_path.name}",
        )
    except ImageHostError as exc:
        raise InstagramPublishError(f"Image hosting failed: {exc}") from exc

    token = settings.ig_access_token
    ig_user_id = settings.ig_business_account_id
    api_version = settings.ig_api_version or _DEFAULT_API_VERSION

    container_id = _create_container(
        token=token,
        ig_user_id=ig_user_id,
        api_version=api_version,
        image_url=public_url,
        caption=package.content.text,
    )

    media_id = _publish_container(
        token=token,
        ig_user_id=ig_user_id,
        api_version=api_version,
        container_id=container_id,
    )

    permalink = _get_permalink(token=token, api_version=api_version, media_id=media_id)
    return media_id, permalink


# ---------------------------------------------------------------------------
# Step 2 — create the media container
# ---------------------------------------------------------------------------
def _create_container(
    *, token: str, ig_user_id: str, api_version: str, image_url: str, caption: str
) -> str:
    url = f"{_BASE}/{api_version}/{ig_user_id}/media"
    params = {
        "image_url": image_url,
        "caption": caption,
        "access_token": token,
    }
    resp = requests.post(url, params=params, timeout=60)
    _raise_for(resp, step="createContainer")
    container_id = (resp.json() or {}).get("id")
    if not container_id:
        raise InstagramPublishError(
            f"createContainer response missing id: {resp.text[:300]}",
            status_code=resp.status_code,
            body=resp.text,
        )
    return container_id


# ---------------------------------------------------------------------------
# Step 3 — publish the container
# ---------------------------------------------------------------------------
def _publish_container(
    *, token: str, ig_user_id: str, api_version: str, container_id: str
) -> str:
    url = f"{_BASE}/{api_version}/{ig_user_id}/media_publish"
    params = {"creation_id": container_id, "access_token": token}
    resp = requests.post(url, params=params, timeout=60)
    _raise_for(resp, step="mediaPublish")
    media_id = (resp.json() or {}).get("id")
    if not media_id:
        raise InstagramPublishError(
            f"mediaPublish response missing id: {resp.text[:300]}",
            status_code=resp.status_code,
            body=resp.text,
        )
    return media_id


# ---------------------------------------------------------------------------
# Step 4 — permalink lookup (best-effort)
# ---------------------------------------------------------------------------
def _get_permalink(*, token: str, api_version: str, media_id: str) -> str:
    """Fetch the canonical post permalink. Falls back to a synthetic URL
    pattern on transient failure — the post has already been published at
    this point, so we don't want to raise just because the read failed."""
    url = f"{_BASE}/{api_version}/{media_id}"
    params = {"fields": "permalink", "access_token": token}
    try:
        resp = requests.get(url, params=params, timeout=30)
        if 200 <= resp.status_code < 300:
            permalink = (resp.json() or {}).get("permalink")
            if permalink:
                return permalink
    except requests.RequestException:
        pass
    return f"https://www.instagram.com/p/{media_id}/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _raise_for(resp: requests.Response, *, step: str) -> None:
    if 200 <= resp.status_code < 300:
        return
    body = resp.text[:1000]
    if resp.status_code == 400:
        msg = (
            f"{step}: 400 Bad Request — Graph rejected the request. Common causes: "
            "image_url is not HTTPS or not reachable from Facebook's servers, "
            "caption longer than 2200 chars, image aspect ratio outside 4:5..1.91:1, "
            "or the IG account isn't a Business/Creator account linked to a Facebook Page"
        )
    elif resp.status_code == 401:
        msg = (
            f"{step}: 401 Unauthorized — IG_ACCESS_TOKEN is invalid, expired, or "
            "revoked. Long-lived Page tokens expire after ~60 days"
        )
    elif resp.status_code == 403:
        msg = (
            f"{step}: 403 Forbidden — token is missing one of "
            "`instagram_content_publish`, `instagram_basic`, `pages_show_list`, "
            "`pages_read_engagement`; or the linked IG account is not Business/Creator"
        )
    elif resp.status_code == 404:
        msg = (
            f"{step}: 404 Not Found — IG_BUSINESS_ACCOUNT_ID is wrong, or the "
            "container expired (containers are valid for 24h before publish)"
        )
    elif resp.status_code == 429:
        msg = (
            f"{step}: 429 — rate limit hit (Instagram allows up to 25 API publishes "
            "per 24h per IG account)"
        )
    else:
        msg = f"{step}: HTTP {resp.status_code}"
    if body:
        msg = f"{msg} | Graph response: {body[:300]}"
    raise InstagramPublishError(msg, status_code=resp.status_code, body=body)


def _require_settings(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("IG_ACCESS_TOKEN", settings.ig_access_token),
            ("IG_BUSINESS_ACCOUNT_ID", settings.ig_business_account_id),
        )
        if not value
    ]
    if missing:
        raise InstagramPublishError(
            f"Instagram publishing requires {', '.join(missing)} in .env"
        )
