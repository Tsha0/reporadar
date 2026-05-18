"""Image host (S3-compatible) tests — boto3 mocked, no network."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.publishing.image_host import ImageHostError, upload_image


class _StubSettings:
    def __init__(self, **overrides):
        self.image_host_endpoint = "https://abc.r2.cloudflarestorage.com"
        self.image_host_bucket = "test-bucket"
        self.image_host_region = "auto"
        self.image_host_public_base_url = "https://cdn.test.example"
        self.image_host_access_key = "AKIA-test"
        self.image_host_secret_key = "secret"
        for k, v in overrides.items():
            setattr(self, k, v)


@pytest.fixture
def fake_boto3(monkeypatch):
    """Inject a fake boto3 module so we can run without the real dep installed."""
    fake_boto3 = types.ModuleType("boto3")
    fake_botocore = types.ModuleType("botocore")
    fake_botocore_config = types.ModuleType("botocore.config")

    class _BotoConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_botocore_config.Config = _BotoConfig
    fake_botocore.config = fake_botocore_config

    s3_client = MagicMock()
    fake_boto3.client = MagicMock(return_value=s3_client)

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", fake_botocore_config)
    return fake_boto3, s3_client


def test_upload_returns_public_url(tmp_path: Path, fake_boto3):
    boto3_mod, s3_client = fake_boto3

    img = tmp_path / "poster.jpg"
    img.write_bytes(b"\xff\xd8jpegbytes")

    url = upload_image(_StubSettings(), img, object_key="posts/abc/poster.jpg")
    assert url == "https://cdn.test.example/posts/abc/poster.jpg"

    # boto3.client called with the right S3 args (endpoint, creds, region, sigv4)
    client_kwargs = boto3_mod.client.call_args.kwargs
    assert client_kwargs["endpoint_url"] == "https://abc.r2.cloudflarestorage.com"
    assert client_kwargs["aws_access_key_id"] == "AKIA-test"
    assert client_kwargs["aws_secret_access_key"] == "secret"
    assert client_kwargs["region_name"] == "auto"
    assert client_kwargs["config"].kwargs == {"signature_version": "s3v4"}

    # put_object received the file bytes + content type
    put_kwargs = s3_client.put_object.call_args.kwargs
    assert put_kwargs["Bucket"] == "test-bucket"
    assert put_kwargs["Key"] == "posts/abc/poster.jpg"
    assert put_kwargs["Body"] == b"\xff\xd8jpegbytes"
    assert put_kwargs["ContentType"] == "image/jpeg"


def test_upload_url_encodes_object_key_with_spaces(tmp_path: Path, fake_boto3):
    img = tmp_path / "poster.jpg"
    img.write_bytes(b"x")

    url = upload_image(_StubSettings(), img, object_key="posts/x y/p.jpg")
    # `quote` percent-encodes the space.
    assert url == "https://cdn.test.example/posts/x%20y/p.jpg"


def test_upload_strips_trailing_slash_from_base_url(tmp_path: Path, fake_boto3):
    img = tmp_path / "poster.jpg"
    img.write_bytes(b"x")

    settings = _StubSettings(image_host_public_base_url="https://cdn.test.example/")
    url = upload_image(settings, img, object_key="p.jpg")
    assert url == "https://cdn.test.example/p.jpg"


def test_upload_uses_none_endpoint_for_aws_s3(tmp_path: Path, fake_boto3):
    """Empty IMAGE_HOST_ENDPOINT means default AWS S3 endpoint, not literal ''."""
    boto3_mod, _ = fake_boto3
    img = tmp_path / "poster.jpg"
    img.write_bytes(b"x")

    upload_image(
        _StubSettings(image_host_endpoint=""),
        img,
        object_key="p.jpg",
    )
    assert boto3_mod.client.call_args.kwargs["endpoint_url"] is None


def test_upload_requires_bucket(tmp_path: Path, fake_boto3):
    img = tmp_path / "poster.jpg"
    img.write_bytes(b"x")
    with pytest.raises(ImageHostError, match="IMAGE_HOST_BUCKET"):
        upload_image(_StubSettings(image_host_bucket=None), img, object_key="p.jpg")


def test_upload_requires_public_base_url(tmp_path: Path, fake_boto3):
    img = tmp_path / "poster.jpg"
    img.write_bytes(b"x")
    with pytest.raises(ImageHostError, match="IMAGE_HOST_PUBLIC_BASE_URL"):
        upload_image(
            _StubSettings(image_host_public_base_url=None), img, object_key="p.jpg"
        )


def test_upload_requires_credentials(tmp_path: Path, fake_boto3):
    img = tmp_path / "poster.jpg"
    img.write_bytes(b"x")
    with pytest.raises(ImageHostError, match="IMAGE_HOST_ACCESS_KEY"):
        upload_image(_StubSettings(image_host_access_key=None), img, object_key="p.jpg")


def test_upload_wraps_put_failure(tmp_path: Path, fake_boto3):
    _, s3_client = fake_boto3
    s3_client.put_object.side_effect = RuntimeError("403 SignatureDoesNotMatch")

    img = tmp_path / "poster.jpg"
    img.write_bytes(b"x")
    with pytest.raises(ImageHostError, match="SignatureDoesNotMatch"):
        upload_image(_StubSettings(), img, object_key="p.jpg")


def test_upload_raises_clean_error_when_boto3_missing(tmp_path: Path, monkeypatch):
    """If boto3 isn't installed at all, we want a helpful message, not ModuleNotFoundError."""
    monkeypatch.setitem(sys.modules, "boto3", None)  # forces ImportError on `import boto3`
    img = tmp_path / "poster.jpg"
    img.write_bytes(b"x")
    with pytest.raises(ImageHostError, match="boto3 is required"):
        upload_image(_StubSettings(), img, object_key="p.jpg")
