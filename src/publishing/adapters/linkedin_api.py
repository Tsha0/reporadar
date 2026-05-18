"""LinkedIn Posts API adapter.

Posts one `PostPackage` to a LinkedIn personal feed via the versioned
`/rest/posts` API. Three-step flow per LinkedIn's docs:

    1. POST /rest/images?action=initializeUpload  → returns uploadUrl + image URN
    2. PUT  <uploadUrl> (binary)                  → uploads the JPEG bytes
    3. POST /rest/posts                           → creates the feed post

Returns `(external_post_id, external_post_url)` so the caller can persist
both on the `post_instances[]` row.

Required `Settings` fields (all from .env):
    linkedin_access_token   — minted with `w_member_social` scope
    linkedin_actor_urn      — `urn:li:person:<sub>`; from `GET /v2/userinfo`
    linkedin_api_version    — YYYYMM, pinned via `LinkedIn-Version` header

Raises `LinkedInPublishError` with the LinkedIn response body attached when
any step returns non-2xx. The caller (publishing.service) should catch this
and mark the post_instance as `failed` with the error message.
"""
from __future__ import annotations

from pathlib import Path

import requests

from src.common.config import Settings
from src.contracts.package import PostPackage

_BASE = "https://api.linkedin.com"
_REST_VERSION = "2.0.0"  # X-Restli-Protocol-Version, unrelated to LinkedIn-Version


class LinkedInPublishError(RuntimeError):
    """Raised when any of the three publish steps fails.

    The first arg is a short summary; `.status_code` and `.body` carry the
    full LinkedIn response so the caller can surface them to the operator.
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def publish_to_linkedin(package: PostPackage, settings: Settings) -> tuple[str, str]:
    """Post `package` to LinkedIn. Returns (post_urn, permalink)."""
    _require_settings(settings)
    if package.channel != "linkedin":
        raise LinkedInPublishError(
            f"LinkedIn adapter received a non-linkedin package (channel={package.channel!r})"
        )
    if not package.media:
        raise LinkedInPublishError("LinkedIn adapter requires at least one image asset")

    asset = package.media[0]
    image_path = Path(asset.local_path)
    if not image_path.exists():
        raise LinkedInPublishError(f"Image asset missing on disk: {image_path}")

    token = settings.linkedin_access_token
    actor_urn = settings.linkedin_actor_urn
    api_version = settings.linkedin_api_version

    image_urn = _initialize_image_upload(token, actor_urn, api_version)
    upload_url = image_urn["uploadUrl"]
    image_urn_value = image_urn["image"]

    _upload_image_bytes(upload_url, image_path, token)

    post_urn = _create_post(
        token=token,
        actor_urn=actor_urn,
        api_version=api_version,
        commentary=package.content.text,
        image_urn=image_urn_value,
        alt_text=asset.alt_text or "",
    )

    permalink = f"https://www.linkedin.com/feed/update/{post_urn}/"
    return post_urn, permalink


# ---------------------------------------------------------------------------
# Step 1 — initializeUpload
# ---------------------------------------------------------------------------
def _initialize_image_upload(token: str, actor_urn: str, api_version: str) -> dict:
    url = f"{_BASE}/rest/images?action=initializeUpload"
    headers = _versioned_headers(token, api_version, content_type="application/json")
    body = {"initializeUploadRequest": {"owner": actor_urn}}
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    _raise_for(resp, step="initializeUpload")

    value = (resp.json() or {}).get("value") or {}
    if "uploadUrl" not in value or "image" not in value:
        raise LinkedInPublishError(
            f"initializeUpload response missing uploadUrl/image: {value!r}",
            status_code=resp.status_code,
            body=resp.text,
        )
    return {"uploadUrl": value["uploadUrl"], "image": value["image"]}


# ---------------------------------------------------------------------------
# Step 2 — binary upload
# ---------------------------------------------------------------------------
def _upload_image_bytes(upload_url: str, image_path: Path, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    with image_path.open("rb") as fh:
        resp = requests.put(upload_url, data=fh.read(), headers=headers, timeout=120)
    if not 200 <= resp.status_code < 300:
        raise LinkedInPublishError(
            f"Image upload returned {resp.status_code}",
            status_code=resp.status_code,
            body=resp.text[:500],
        )


# ---------------------------------------------------------------------------
# Step 3 — create the post
# ---------------------------------------------------------------------------
def _create_post(
    *,
    token: str,
    actor_urn: str,
    api_version: str,
    commentary: str,
    image_urn: str,
    alt_text: str,
) -> str:
    url = f"{_BASE}/rest/posts"
    headers = _versioned_headers(token, api_version, content_type="application/json")
    body = {
        "author": actor_urn,
        "commentary": _escape_commentary(commentary),
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "content": {"media": {"id": image_urn, "altText": alt_text or ""}},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    resp = requests.post(url, json=body, headers=headers, timeout=60)
    _raise_for(resp, step="createPost")

    # Versioned posts API returns the post URN in the x-restli-id header.
    post_urn = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id")
    if not post_urn:
        raise LinkedInPublishError(
            f"createPost succeeded ({resp.status_code}) but no x-restli-id header on response",
            status_code=resp.status_code,
            body=resp.text[:500],
        )
    return post_urn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _versioned_headers(token: str, api_version: str, *, content_type: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": api_version,
        "X-Restli-Protocol-Version": _REST_VERSION,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _raise_for(resp: requests.Response, *, step: str) -> None:
    if 200 <= resp.status_code < 300:
        return
    body = resp.text[:1000]
    if resp.status_code == 401:
        msg = f"{step}: 401 Unauthorized — LINKEDIN_ACCESS_TOKEN is invalid or expired"
    elif resp.status_code == 403:
        msg = (
            f"{step}: 403 Forbidden — token is missing the `w_member_social` scope "
            "or the app does not have the Share on LinkedIn product attached"
        )
    elif resp.status_code == 422:
        msg = f"{step}: 422 Unprocessable Entity — request body rejected by LinkedIn"
    elif resp.status_code == 426:
        msg = (
            f"{step}: 426 Upgrade Required — LINKEDIN_API_VERSION is not active. "
            "Bump it (or the default in src/common/config.py) to a currently-active "
            "YYYYMM. LinkedIn supports each version for ~12 months but skips some "
            "months (e.g. 202512, 202605) — probe with curl if unsure."
        )
    else:
        msg = f"{step}: HTTP {resp.status_code}"
    if body:
        msg = f"{msg} | LinkedIn response: {body[:300]}"
    raise LinkedInPublishError(msg, status_code=resp.status_code, body=body)


def _require_settings(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("LINKEDIN_ACCESS_TOKEN", settings.linkedin_access_token),
            ("LINKEDIN_ACTOR_URN", settings.linkedin_actor_urn),
            ("LINKEDIN_API_VERSION", settings.linkedin_api_version),
        )
        if not value
    ]
    if missing:
        raise LinkedInPublishError(
            f"LinkedIn publishing requires {', '.join(missing)} in .env"
        )


def _escape_commentary(text: str) -> str:
    """LinkedIn's Posts API treats `(`, `)`, `<`, `>`, `@`, `#`, `*`, `_`, `{`,
    `}`, `[`, `]`, `\\`, `|`, `~` as 'entity' delimiters that must be escaped
    with a leading backslash unless used as part of a real mention/hashtag.

    Our captions are plain prose + hashtags. Plain hashtags (`#foo`) are
    actually fine — LinkedIn renders them as hashtags. The big risk is `(` and
    `)` inside text breaking the parser. We escape the symbols that aren't
    safe to leave as literals.
    """
    needs_escape = "()<>@*_{}[]\\|~"
    out: list[str] = []
    for ch in text:
        if ch in needs_escape:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)
