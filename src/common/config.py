from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator, model_validator

load_dotenv()


class Settings(BaseModel):
    # --- database ---
    database_url: str

    # --- discovery ---
    gh_token: str
    velocity_window_hours: int = 72
    repo_max_age_days: int = 365
    star_growth_min_pct: float = 50.0
    star_base_min: int = 10
    max_candidates_per_run: int = 15
    devpost_max_projects_per_run: int = 25

    # --- OpenAI ---
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4-mini"
    max_evaluations_per_run: int = 5

    # --- output ---
    output_dir: str = "output"

    # --- scheduling ---
    schedule_hour: int = 6
    schedule_jitter_minutes: int = 15
    timezone_name: str = "UTC"

    # --- publishing: LinkedIn API ---
    # Optional. When set, `python -m src publish <post_id>` can post an
    # already-exported PostPackage to LinkedIn via the Posts API. See
    # Doc/services/publishing.md → "LinkedIn API adapter".
    linkedin_client_id: str | None = None
    linkedin_client_secret: str | None = None
    linkedin_access_token: str | None = None
    linkedin_actor_urn: str | None = None
    linkedin_api_version: str = "202604"

    # --- publishing: Instagram Graph API ---
    # Optional. When set, `python -m src publish <post_id>` can post an
    # already-exported PostPackage to an Instagram Business/Creator account
    # via the Facebook Graph API. The image is uploaded to the configured
    # image host first (Instagram requires a public HTTPS URL — it does not
    # accept binary uploads). See Doc/services/publishing.md → "Instagram
    # Graph API adapter".
    ig_access_token: str | None = None
    ig_business_account_id: str | None = None
    ig_app_id: str | None = None
    ig_app_secret: str | None = None
    ig_api_version: str = "v21.0"

    # --- publishing: S3-compatible image host (required for IG publishing) ---
    # Works with Cloudflare R2 (set image_host_endpoint), AWS S3 (leave
    # endpoint blank), Backblaze B2 (set endpoint), or any S3-compatible
    # service. Not used by the LinkedIn adapter — LinkedIn accepts binary
    # uploads directly.
    image_host_endpoint: str | None = None
    image_host_bucket: str | None = None
    image_host_region: str = "auto"
    image_host_public_base_url: str | None = None
    image_host_access_key: str | None = None
    image_host_secret_key: str | None = None

    @property
    def llm_model(self) -> str:
        return self.openai_model

    @field_validator("gh_token")
    @classmethod
    def gh_token_required(cls, v: str, info) -> str:
        if not v:
            raise ValueError("gh_token must not be empty")
        return v

    @field_validator("database_url")
    @classmethod
    def database_url_required(cls, v: str, info) -> str:
        if not v:
            raise ValueError("database_url must not be empty")
        return v

    @model_validator(mode="after")
    def openai_key_present(self) -> "Settings":
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for LLM and image generation")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        for var in ("GH_TOKEN", "DATABASE_URL", "OPENAI_API_KEY"):
            if not os.environ.get(var):
                raise RuntimeError(
                    f"Missing required environment variable: {var}.\n"
                    "Copy .env.template to .env and fill in the values."
                )

        return cls(
            database_url=os.environ["DATABASE_URL"],
            gh_token=os.environ["GH_TOKEN"],
            velocity_window_hours=int(os.environ.get("VELOCITY_WINDOW_HOURS", "72")),
            repo_max_age_days=int(os.environ.get("REPO_MAX_AGE_DAYS", "365")),
            star_growth_min_pct=float(os.environ.get("STAR_GROWTH_MIN_PCT", "50")),
            star_base_min=int(os.environ.get("STAR_BASE_MIN", "10")),
            max_candidates_per_run=int(os.environ.get("MAX_CANDIDATES_PER_RUN", "15")),
            devpost_max_projects_per_run=int(
                os.environ.get("DEVPOST_MAX_PROJECTS_PER_RUN", "25")
            ),
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
            max_evaluations_per_run=int(os.environ.get("MAX_EVALUATIONS_PER_RUN", "5")),
            output_dir=os.environ.get("OUTPUT_DIR", "output"),
            schedule_hour=int(os.environ.get("SCHEDULE_HOUR", "6")),
            schedule_jitter_minutes=int(os.environ.get("SCHEDULE_JITTER_MINUTES", "15")),
            timezone_name=os.environ.get("TIMEZONE", "UTC"),
            linkedin_client_id=os.environ.get("LINKEDIN_CLIENT_ID"),
            linkedin_client_secret=os.environ.get("LINKEDIN_CLIENT_SECRET"),
            linkedin_access_token=os.environ.get("LINKEDIN_ACCESS_TOKEN"),
            linkedin_actor_urn=os.environ.get("LINKEDIN_ACTOR_URN"),
            linkedin_api_version=os.environ.get("LINKEDIN_API_VERSION", "202604"),
            ig_access_token=os.environ.get("IG_ACCESS_TOKEN"),
            ig_business_account_id=os.environ.get("IG_BUSINESS_ACCOUNT_ID"),
            ig_app_id=os.environ.get("IG_APP_ID"),
            ig_app_secret=os.environ.get("IG_APP_SECRET"),
            ig_api_version=os.environ.get("IG_API_VERSION", "v21.0"),
            image_host_endpoint=os.environ.get("IMAGE_HOST_ENDPOINT"),
            image_host_bucket=os.environ.get("IMAGE_HOST_BUCKET"),
            image_host_region=os.environ.get("IMAGE_HOST_REGION", "auto"),
            image_host_public_base_url=os.environ.get("IMAGE_HOST_PUBLIC_BASE_URL"),
            image_host_access_key=os.environ.get("IMAGE_HOST_ACCESS_KEY"),
            image_host_secret_key=os.environ.get("IMAGE_HOST_SECRET_KEY"),
        )
