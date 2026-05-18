"""Publishing / Export service.

Manual export is the default mode — `manual_export_adapter` writes a JSON
package next to the rendered image so the operator can copy/paste and post
manually. The `linkedin_api` and `instagram_graph` adapters (under
`adapters/`) push a previously exported package to LinkedIn's feed via the
Posts API or to Instagram via the Graph API.
"""
from src.publishing.adapters import InstagramPublishError, LinkedInPublishError
from src.publishing.repository import (
    find_post_by_id,
    mark_manually_posted,
    mark_post_approved,
    mark_post_failed,
    mark_post_published,
    mark_post_rejected,
)
from src.publishing.service import (
    publish_packages,
    publish_post_to_instagram,
    publish_post_to_linkedin,
)

__all__ = [
    "publish_packages",
    "publish_post_to_linkedin",
    "publish_post_to_instagram",
    "find_post_by_id",
    "mark_manually_posted",
    "mark_post_approved",
    "mark_post_rejected",
    "mark_post_published",
    "mark_post_failed",
    "LinkedInPublishError",
    "InstagramPublishError",
]
