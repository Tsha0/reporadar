from __future__ import annotations

import psycopg

from src.ai_gateway.images.openai_image import OpenAIImageClient
from src.ai_gateway.llm.base import LLMProvider
from src.ai_gateway.llm.openai import OpenAIProvider
from src.common.config import Settings


def get_llm_provider(settings: Settings, conn: psycopg.Connection, run_id: str) -> LLMProvider:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for LLM generation")
    return OpenAIProvider(conn, run_id, settings.openai_api_key, settings.openai_model)


def get_image_provider(
    settings: Settings,
    conn: psycopg.Connection,
    run_id: str,
    *,
    size: str = "1024x1024",
) -> OpenAIImageClient:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for image generation")
    return OpenAIImageClient(conn, run_id, settings.openai_api_key, size=size)
