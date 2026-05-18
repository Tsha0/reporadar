"""LinkedIn API adapter tests — mocked HTTP, no network."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from src.contracts.content import GeneratedContent
from src.contracts.media import MediaAsset
from src.contracts.package import PostPackage
from src.publishing.adapters.linkedin_api import (
    LinkedInPublishError,
    _escape_commentary,
    publish_to_linkedin,
)


class _StubSettings:
    def __init__(self, **overrides):
        self.linkedin_access_token = "test-token"
        self.linkedin_actor_urn = "urn:li:person:TESTUSER"
        self.linkedin_api_version = "202411"
        for k, v in overrides.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, status_code: int, *, headers=None, json_body=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_body
        self.text = text or (str(json_body) if json_body else "")

    def json(self):
        return self._json


def _now():
    return datetime(2026, 5, 16, tzinfo=timezone.utc)


def _package(tmp_path: Path, *, channel="linkedin") -> PostPackage:
    image_path = tmp_path / "poster.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0fakebytes")

    content = GeneratedContent(
        channel=channel,
        content_format="commentary",
        text="Hello LinkedIn world (with parens).",
        generated_at=_now(),
        model="m",
        prompt_version="v",
        character_count=35,
    )
    asset = MediaAsset(
        asset_id="asset_test",
        channel=channel,
        local_path=str(image_path),
        width=1024,
        height=1536,
        aspect_ratio="2:3",
        alt_text="A poster",
        image_prompt_version="v",
        generated_at=_now(),
    )
    return PostPackage(
        post_id="post_linkedin_abc",
        project_id="proj_1",
        candidate_id="cand_1",
        run_id="run_1",
        channel=channel,
        content=content,
        media=[asset],
        source_links=["https://github.com/example/x"],
        created_at=_now(),
    )


def test_publish_happy_path_returns_urn_and_permalink(tmp_path):
    package = _package(tmp_path)

    init_resp = _FakeResponse(
        200,
        json_body={
            "value": {
                "uploadUrl": "https://upload.linkedin.com/abc",
                "image": "urn:li:image:TESTIMAGE",
            }
        },
    )
    upload_resp = _FakeResponse(201)
    create_resp = _FakeResponse(201, headers={"x-restli-id": "urn:li:share:7195000000000000000"})

    with patch.object(requests, "post", side_effect=[init_resp, create_resp]) as post_mock, \
         patch.object(requests, "put", return_value=upload_resp) as put_mock:

        post_urn, permalink = publish_to_linkedin(package, _StubSettings())

    assert post_urn == "urn:li:share:7195000000000000000"
    assert permalink == "https://www.linkedin.com/feed/update/urn:li:share:7195000000000000000/"

    # Three external calls in the right order with the right verbs.
    assert post_mock.call_count == 2
    assert put_mock.call_count == 1

    init_call, create_call = post_mock.call_args_list
    assert "/rest/images?action=initializeUpload" in init_call.args[0]
    assert init_call.kwargs["json"]["initializeUploadRequest"]["owner"] == "urn:li:person:TESTUSER"

    upload_call = put_mock.call_args
    assert upload_call.args[0] == "https://upload.linkedin.com/abc"
    assert upload_call.kwargs["data"] == b"\xff\xd8\xff\xe0fakebytes"

    assert "/rest/posts" in create_call.args[0]
    body = create_call.kwargs["json"]
    assert body["author"] == "urn:li:person:TESTUSER"
    assert body["content"]["media"]["id"] == "urn:li:image:TESTIMAGE"
    assert body["content"]["media"]["altText"] == "A poster"
    # Parens are escaped to avoid LinkedIn entity parsing.
    assert "\\(with parens\\)" in body["commentary"]


def test_publish_rejects_non_linkedin_channel(tmp_path):
    package = _package(tmp_path, channel="instagram")
    with pytest.raises(LinkedInPublishError, match="non-linkedin"):
        publish_to_linkedin(package, _StubSettings())


def test_publish_rejects_missing_image(tmp_path):
    package = _package(tmp_path)
    # Remove the file after the package was built.
    Path(package.media[0].local_path).unlink()
    with pytest.raises(LinkedInPublishError, match="missing on disk"):
        publish_to_linkedin(package, _StubSettings())


def test_publish_requires_settings(tmp_path):
    package = _package(tmp_path)
    with pytest.raises(LinkedInPublishError, match="LINKEDIN_ACCESS_TOKEN"):
        publish_to_linkedin(package, _StubSettings(linkedin_access_token=None))


def test_publish_translates_401(tmp_path):
    package = _package(tmp_path)
    bad_resp = _FakeResponse(401, text='{"message":"invalid token"}')
    with patch.object(requests, "post", return_value=bad_resp):
        with pytest.raises(LinkedInPublishError, match="401 Unauthorized") as excinfo:
            publish_to_linkedin(package, _StubSettings())
    assert excinfo.value.status_code == 401


def test_publish_translates_403_with_scope_hint(tmp_path):
    package = _package(tmp_path)
    bad_resp = _FakeResponse(403, text='{"message":"forbidden"}')
    with patch.object(requests, "post", return_value=bad_resp):
        with pytest.raises(LinkedInPublishError, match="w_member_social"):
            publish_to_linkedin(package, _StubSettings())


def test_escape_commentary_escapes_entity_chars():
    out = _escape_commentary("Hello (world) [test] {x} @user #tag")
    assert "\\(world\\)" in out
    assert "\\[test\\]" in out
    assert "\\{x\\}" in out
    assert "\\@user" in out
    # `#` is *not* an entity char in LinkedIn's parser — hashtags survive as-is.
    assert "#tag" in out
