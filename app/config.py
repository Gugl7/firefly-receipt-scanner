from functools import lru_cache

try:
    from pydantic_settings import BaseSettings
    from pydantic import AliasChoices, Field, computed_field
    _PYDANTIC_V2 = True
except ImportError:
    from pydantic import BaseSettings, Field
    computed_field = lambda f: f
    AliasChoices = None
    _PYDANTIC_V2 = False


class Settings(BaseSettings):
    firefly_iii_url: str = ""
    firefly_iii_token: str = ""

    # Google Gemini Configuration
    google_ai_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str | None = None

    if _PYDANTIC_V2:
        # LLM Provider: "auto", "9router", "openai", or "gemini"
        llm_provider: str = Field(
            default="auto",
            validation_alias=AliasChoices("LLM_PROVIDER", "PROVIDER"),
        )

        # 9Router / OpenAI-compatible / Ollama Configuration
        router_base_url: str | None = Field(
            default=None,
            validation_alias=AliasChoices(
                "ROUTER_BASE_URL",
                "LLM_BASE_URL",
                "OPENAI_BASE_URL",
                "9ROUTER_BASE_URL",
                "OLLAMA_BASE_URL",
                "OLLAMA_URL",
                "OLLAMA_HOST",
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
                "ROUTER_MODEL",
                "LLM_MODEL",
                "OPENAI_MODEL",
                "9ROUTER_MODEL",
                "OLLAMA_MODEL",
            ),
        )

        # Automatic Provider Fallback (e.g. Gemini -> Local Ollama if Gemini is overloaded/down)
        llm_fallback_enabled: bool = Field(
            default=True,
            validation_alias=AliasChoices(
                "LLM_FALLBACK_ENABLED",
                "FALLBACK_TO_OLLAMA",
                "OLLAMA_FALLBACK",
                "ENABLE_LLM_FALLBACK",
            ),
        )

        # Account Card Mapping (e.g. "1234:Checking, 5678:Credit Card, cash:Cash wallet" or JSON)
        account_card_mappings: str = Field(
            default="",
            validation_alias=AliasChoices(
                "ACCOUNT_CARD_MAPPINGS", "CARD_MAPPINGS", "ACCOUNT_MAPPINGS"
            ),
        )

        # Local OCR Configuration
        ocr_enabled: bool = Field(
            default=False,
            validation_alias=AliasChoices(
                "OCR_ENABLED", "ENABLE_OCR", "LOCAL_OCR_ENABLED", "LOCAL_OCR"
            ),
        )
        ocr_engine: str = Field(
            default="tesseract",
            validation_alias=AliasChoices("OCR_ENGINE", "LOCAL_OCR_ENGINE"),
        )
        ocr_language: str = Field(
            default="eng+deu",
            validation_alias=AliasChoices(
                "OCR_LANGUAGE", "TESSERACT_LANG", "TESSERACT_LANGUAGES"
            ),
        )

        # LLM Request Timeout (seconds) - default 180s for local models & CPU inference
        llm_timeout: float = Field(
            default=180.0,
            validation_alias=AliasChoices(
                "LLM_TIMEOUT", "ROUTER_TIMEOUT", "TIMEOUT_SECONDS", "REQUEST_TIMEOUT"
            ),
        )

        model_config = {"env_file": ".env", "extra": "ignore"}
    else:
        llm_provider: str = "auto"
        router_base_url: str | None = None
        router_api_key: str = ""
        router_model: str | None = None
        llm_fallback_enabled: bool = True
        account_card_mappings: str = ""
        ocr_enabled: bool = False
        ocr_engine: str = "tesseract"
        ocr_language: str = "eng+deu"
        llm_timeout: float = 180.0

        class Config:
            env_file = ".env"
            extra = "ignore"

        def __init__(self, **values):
            aliases = {
                "ROUTER_BASE_URL": "router_base_url",
                "LLM_BASE_URL": "router_base_url",
                "OPENAI_BASE_URL": "router_base_url",
                "9ROUTER_BASE_URL": "router_base_url",
                "OLLAMA_BASE_URL": "router_base_url",
                "OLLAMA_URL": "router_base_url",
                "OLLAMA_HOST": "router_base_url",
                "LLM_PROVIDER": "llm_provider",
                "PROVIDER": "llm_provider",
                "ROUTER_API_KEY": "router_api_key",
                "LLM_API_KEY": "router_api_key",
                "OPENAI_API_KEY": "router_api_key",
                "9ROUTER_API_KEY": "router_api_key",
                "OLLAMA_API_KEY": "router_api_key",
                "ROUTER_MODEL": "router_model",
                "LLM_MODEL": "router_model",
                "OPENAI_MODEL": "router_model",
                "9ROUTER_MODEL": "router_model",
                "OLLAMA_MODEL": "router_model",
                "LLM_FALLBACK_ENABLED": "llm_fallback_enabled",
                "FALLBACK_TO_OLLAMA": "llm_fallback_enabled",
                "OLLAMA_FALLBACK": "llm_fallback_enabled",
                "ENABLE_LLM_FALLBACK": "llm_fallback_enabled",
                "ACCOUNT_CARD_MAPPINGS": "account_card_mappings",
                "CARD_MAPPINGS": "account_card_mappings",
                "ACCOUNT_MAPPINGS": "account_card_mappings",
                "OCR_ENABLED": "ocr_enabled",
                "ENABLE_OCR": "ocr_enabled",
                "LOCAL_OCR_ENABLED": "ocr_enabled",
                "LOCAL_OCR": "ocr_enabled",
                "OCR_ENGINE": "ocr_engine",
                "LOCAL_OCR_ENGINE": "ocr_engine",
                "OCR_LANGUAGE": "ocr_language",
                "TESSERACT_LANG": "ocr_language",
                "TESSERACT_LANGUAGES": "ocr_language",
                "LLM_TIMEOUT": "llm_timeout",
                "ROUTER_TIMEOUT": "llm_timeout",
                "TIMEOUT_SECONDS": "llm_timeout",
                "REQUEST_TIMEOUT": "llm_timeout",
            }
            for k, v in list(values.items()):
                if k in aliases and aliases[k] not in values:
                    values[aliases[k]] = v
            super().__init__(**values)

    @computed_field
    @property
    def firefly_api_url(self) -> str:
        return self.firefly_iii_url.rstrip("/") + "/api/v1/"

    @computed_field
    @property
    def effective_llm_provider(self) -> str:
        provider = self.llm_provider.lower().strip()
        if provider in ("9router", "router", "openai", "ollama", "local"):
            return "9router"
        if provider == "gemini":
            return "gemini"
        # "auto" or unspecified: prefer gemini if API key is set, otherwise router/ollama if URL is set
        if self.google_ai_api_key:
            return "gemini"
        if self.router_base_url:
            return "9router"
        return "gemini"

    @computed_field
    @property
    def effective_router_model(self) -> str:
        if self.router_model:
            return self.router_model
        if self.router_base_url and (
            "11434" in self.router_base_url or "ollama" in self.router_base_url.lower()
        ):
            return "qwen2.5:3b"
        return self.gemini_model

    @computed_field
    @property
    def router_chat_url(self) -> str:
        base = (self.router_base_url or "http://localhost:11434/v1").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"


@lru_cache
def get_settings() -> Settings:
    return Settings()
