from functools import lru_cache

from pydantic import AliasChoices, Field, computed_field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    firefly_iii_url: str = ""
    firefly_iii_token: str = ""

    # Google Gemini Configuration
    google_ai_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str | None = None

    # LLM Provider: "auto", "9router", "openai", or "gemini"
    llm_provider: str = Field(
        default="auto",
        validation_alias=AliasChoices("LLM_PROVIDER", "PROVIDER"),
    )

    # 9Router / OpenAI-compatible Configuration
    router_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ROUTER_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL", "9ROUTER_BASE_URL"
        ),
    )
    router_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ROUTER_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "9ROUTER_API_KEY"
        ),
    )
    router_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ROUTER_MODEL", "LLM_MODEL", "OPENAI_MODEL", "9ROUTER_MODEL"
        ),
    )

    @computed_field
    @property
    def firefly_api_url(self) -> str:
        return self.firefly_iii_url.rstrip("/") + "/api/v1/"

    @computed_field
    @property
    def effective_llm_provider(self) -> str:
        provider = self.llm_provider.lower().strip()
        if provider in ("9router", "router", "openai"):
            return "9router"
        if provider == "gemini":
            return "gemini"
        # "auto" or unspecified: if router_base_url is given, use 9router; else gemini
        if self.router_base_url:
            return "9router"
        return "gemini"

    @computed_field
    @property
    def effective_router_model(self) -> str:
        return self.router_model or self.gemini_model

    @computed_field
    @property
    def router_chat_url(self) -> str:
        base = (self.router_base_url or "http://localhost:20128/v1").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
