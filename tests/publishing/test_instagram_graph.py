"""Instagram Graph API adapter tests — mocked HTTP + mocked image host, no network."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from src.contracts.content import GeneratedContent
from src.contracts.media import MediaAsset
from src.contracts.package import PostPackage
from src.publishing.adapters.instagram_graph import (
    InstagramPublishError,
    publish_to_instagram,
)


class _StubSettings:
    def __init__(self, **overrides):
        self.ig_access_token = "test-ig-token"
        self.ig_business_account_id = "17841400000000000"
        self.ig_api_version = "v21.0"
        # Image host fields — present so _require_image_host_settings passes.
        self.image_host_endpoint = "https://abc.r2.cloudflarestorage.com"
        self.image_host_bucket = "test-bucket"
        self.image_host_region = "auto"
        self.image_host_public_base_url = "https://cdn.test.example"
        self.image_host_access_key = "AKIA-test"
        self.image_host_secret_key = "secret"
        for k, v in overrides.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, status_code: int, *, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (str(json_body) if json_body else "")

    def json(self):
        return self._json


def _now():
    return datetime(2026, 5, 17, tzinfo=timezone.utc)


def _package(tmp_path: Path, *, channel="instagram") -> PostPackage:
    image_path = tmp_path / "poster.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0fakebytes")

    content = GeneratedContent(
        channel=channel,
        content_format="caption",
        text="Hello Instagram!",
        generated_at=_now(),
        model="m",
        prompt_version="v",
        character_count=16,
    )
    asset = MediaAsset(
        asset_id="asset_test",
        channel=channel,
        local_path=str(image_path),
        width=1024,
        height=1024,
        aspect_ratio="1:1",
        alt_text="A poster",
        image_prompt_version="v",
        generated_at=_now(),
    )
    return PostPackage(
        post_id="post_instagram_abc",
        project_id="proj_1",
        candidate_id="cand_1",
        run_id="run_1",
        channel=channel,
        content=content,
        media=[asset],
        source_links=["https://github.com/example/x"],
        created_at=_now(),
    )


def test_publish_happy_path_returns_media_id_and_permalink(tmp_path):
    package = _package(tmp_path)

    container_resp = _FakeResponse(200, json_body={"id": "1789012345678901234"})
    publish_resp = _FakeResponse(200, json_body={"id": "17909999999999999"})
    permalink_resp = _FakeResponse(
        200,
        json_body={"permalink": "https://www.instagram.com/p/CxYzAbC/", "id": "17909999999999999"},
    )

    with patch(
        "src.publishing.adapters.instagram_graph.upload_image",
        return_value="https://cdn.test.example/posts/post_instagram_abc/poster.jpg",
    ) as upload_mock, patch.object(
        requests, "post", side_effect=[container_resp, publish_resp]
    ) as post_mock, patch.object(
        requests, "get", return_value=permalink_resp
    ) as get_mock:

        media_id, permalink = publish_to_instagram(package, _StubSettings())

    assert media_id == "17909999999999999"
    assert permalink == "https://www.instagram.com/p/CxYzAbC/"

    # image_host.upload_image called once with the right object key
    upload_mock.assert_called_once()
    assert upload_mock.call_args.kwargs["object_key"] == "posts/post_instagram_abc/poster.jpg"

    # Two POSTs: createContainer + mediaPublish
    assert post_mock.call_count == 2
    create_call, publish_call = post_mock.call_args_list

    assert create_call.args[0].endswith("/v21.0/17841400000000000/media")
    assert create_call.kwargs["params"]["image_url"] == (
        "https://cdn.test.example/posts/post_instagram_abc/poster.jpg"
    )
    assert create_call.kwargs["params"]["caption"] == "Hello Instagram!"
    assert create_call.kwargs["params"]["access_token"] == "test-ig-token"

    assert publish_call.args[0].endswith("/v21.0/17841400000000000/media_publish")
    assert publish_call.kwargs["params"]["creation_id"] == "1789012345678901234"

    # One GET for permalink
    assert get_mock.call_count == 1


def test_publish_falls_back_to_synthetic_permalink_on_lookup_failure(tmp_path):
    package = _package(tmp_path)

    container_resp = _FakeResponse(200, json_body={"id": "ctr_1"})
    publish_resp = _FakeResponse(200, json_body={"id": "media_xyz"})

    with patch(
        "src.publishing.adapters.instagram_graph.upload_image",
        return_value="https://cdn.test.example/posts/x.jpg",
    ), patch.object(
        requests, "post", side_effect=[container_resp, publish_resp]
    ), patch.object(
        requests, "get", side_effect=requests.RequestException("network down")
    ):
        media_id, permalink = publish_to_instagram(package, _StubSettings())

    assert media_id == "media_xyz"
    # Synthetic fallback when permalink fetch fails — the publish itself succeeded.
    assert permalink == "https://www.instagram.com/p/media_xyz/"


def test_publish_rejects_non_instagram_channel(tmp_path):
    package = _package(tmp_path, channel="linkedin")
    with pytest.raises(InstagramPublishError, match="non-instagram"):
        publish_to_instagram(package, _StubSettings())


def test_publish_rejects_missing_image(tmp_path):
    package = _package(tmp_path)
    Path(package.media[0].local_path).unlink()
    with pytest.raises(InstagramPublishError, match="missing on disk"):
        publish_to_instagram(package, _StubSettings())


def test_publish_requires_settings(tmp_path):
    package = _package(tmp_path)
    with pytest.raises(InstagramPublishError, match="IG_ACCESS_TOKEN"):
        publish_to_instagram(package, _StubSettings(ig_access_token=None))


def test_publish_wraps_image_host_failure(tmp_path):
    from src.publishing.image_host import ImageHostError

    package = _package(tmp_path)
    with patch(
        "src.publishing.adapters.instagram_graph.upload_image",
        side_effect=ImageHostError("bucket on fire"),
    ):
        with pytest.raises(InstagramPublishError, match="Image hosting failed"):
            publish_to_instagram(package, _StubSettings())


def test_publish_translates_400_with_hint(tmp_path):
    package = _package(tmp_path)
    bad_resp = _FakeResponse(400, text='{"error":{"message":"Image URL not reachable"}}')
    with patch(
        "src.publishing.adapters.instagram_graph.upload_image",
        return_value="https://cdn.test.example/posts/x.jpg",
    ), patch.object(requests, "post", return_value=bad_resp):
        with pytest.raises(InstagramPublishError, match="400 Bad Request") as excinfo:
            publish_to_instagram(package, _StubSettings())
    assert excinfo.value.status_code == 400
    assert "image_url" in str(excinfo.value)


def test_publish_translates_401(tmp_path):
    package = _package(tmp_path)
    bad_resp = _FakeResponse(401, text='{"error":{"message":"Invalid OAuth"}}')
    with patch(
        "src.publishing.adapters.instagram_graph.upload_image",
        return_value="https://cdn.test.example/posts/x.jpg",
    ), patch.object(requests, "post", return_value=bad_resp):
        with pytest.raises(InstagramPublishError, match="401 Unauthorized") as excinfo:
            publish_to_instagram(package, _StubSettings())
    assert excinfo.value.status_code == 401


def test_publish_translates_403_with_scope_hint(tmp_path):
    package = _package(tmp_path)
    bad_resp = _FakeResponse(403, text='{"error":{"message":"missing scope"}}')
    with patch(
        "src.publishing.adapters.instagram_graph.upload_image",
        return_value="https://cdn.test.example/posts/x.jpg",
    ), patch.object(requests, "post", return_value=bad_resp):
        with pytest.raises(InstagramPublishError, match="instagram_content_publish"):
            publish_to_instagram(package, _StubSettings())


def test_publish_translates_429(tmp_path):
    package = _package(tmp_path)
    bad_resp = _FakeResponse(429, text='{"error":{"message":"rate limit"}}')
    with patch(
        "src.publishing.adapters.instagram_graph.upload_image",
        return_value="https://cdn.test.example/posts/x.jpg",
    ), patch.object(requests, "post", return_value=bad_resp):
        with pytest.raises(InstagramPublishError, match="25 API publishes"):
            publish_to_instagram(package, _StubSettings())
