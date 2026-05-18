"""S3-compatible image host adapter.

Uploads a local JPEG to the configured S3-compatible bucket and returns
the public HTTPS URL the operator (or another API) can fetch it from.

Instagram's Graph API requires a publicly reachable HTTPS `image_url` for
container creation — it does not accept binary uploads. This module is
that hosting layer. LinkedIn does not use this path (it accepts binary
uploads directly into its own CDN).

Works with any S3-compatible service:

  * Cloudflare R2  — set `image_host_endpoint` to `https://<id>.r2.cloudflarestorage.com`,
                     `image_host_region` to `"auto"` (the default)
  * AWS S3         — leave `image_host_endpoint` blank, set the AWS region
  * Backblaze B2   — set the B2 S3 endpoint + region

Required `Settings`:
    image_host_bucket          — bucket name
    image_host_public_base_url — HTTPS URL prefix where objects are served
                                  (e.g. `https://cards.example.com` if you
                                  front the bucket with a custom domain, or
                                  `https://<bucket>.<endpoint-host>` for R2's
                                  public bucket URL)
    image_host_access_key
    image_host_secret_key

Raises `ImageHostError` on any upload failure.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from src.common.config import Settings


class ImageHostError(RuntimeError):
    """Raised when image upload to the configured bucket fails."""


def upload_image(settings: Settings, local_path: Path, *, object_key: str) -> str:
    """Upload `local_path` to S3 with `object_key`. Return public HTTPS URL.

    `object_key` is the in-bucket path (e.g. `posts/abc/poster.jpg`).
    The returned URL is built from `image_host_public_base_url`, not from
    the S3 endpoint, so the bucket can sit behind a custom domain or CDN.
    """
    _require_image_host_settings(settings)
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:
        raise ImageHostError(
            "boto3 is required for Instagram publishing image upload; "
            "run `pip install boto3`"
        ) from exc

    client = boto3.client(
        "s3",
        endpoint_url=settings.image_host_endpoint or None,
        aws_access_key_id=settings.image_host_access_key,
        aws_secret_access_key=settings.image_host_secret_key,
        region_name=settings.image_host_region or "auto",
        config=BotoConfig(signature_version="s3v4"),
    )

    try:
        with local_path.open("rb") as fh:
            client.put_object(
                Bucket=settings.image_host_bucket,
                Key=object_key,
                Body=fh.read(),
                ContentType="image/jpeg",
                CacheControl="public, max-age=86400",
            )
    except Exception as exc:
        raise ImageHostError(
            f"Failed to upload {local_path.name} to bucket "
            f"{settings.image_host_bucket!r}: {exc}"
        ) from exc

    base = settings.image_host_public_base_url.rstrip("/")
    return f"{base}/{quote(object_key)}"


def _require_image_host_settings(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("IMAGE_HOST_BUCKET", settings.image_host_bucket),
            ("IMAGE_HOST_PUBLIC_BASE_URL", settings.image_host_public_base_url),
            ("IMAGE_HOST_ACCESS_KEY", settings.image_host_access_key),
            ("IMAGE_HOST_SECRET_KEY", settings.image_host_secret_key),
        )
        if not value
    ]
    if missing:
        raise ImageHostError(
            f"Image hosting requires {', '.join(missing)} in .env "
            "(needed for Instagram publishing — LinkedIn uses binary upload)"
        )
