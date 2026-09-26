from app.minutes.providers.base import LLMError, LLMProvider, LLMResult


def get_provider(app) -> LLMProvider:
    """Provider selected by LLM_PROVIDER, cached per app (tests inject a fake here)."""
    if "llm_provider" not in app.extensions:
        name = app.config["LLM_PROVIDER"]
        if name == "anthropic":
            from app.minutes.providers.anthropic_provider import AnthropicProvider

            app.extensions["llm_provider"] = AnthropicProvider(
                model=app.config["LLM_MODEL"],
                api_key=app.config["ANTHROPIC_API_KEY"],
                effort=app.config["LLM_EFFORT"],
                max_output_tokens=app.config["LLM_MAX_OUTPUT_TOKENS"],
                fallbacks=app.config["LLM_FALLBACKS_ENABLED"],
            )
        else:
            raise LLMError("not_configured", f"unknown LLM_PROVIDER {name!r}")
    return app.extensions["llm_provider"]


__all__ = ["LLMError", "LLMProvider", "LLMResult", "get_provider"]
