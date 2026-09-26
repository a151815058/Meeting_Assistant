"""LLM provider interface (REQ-13). Minutes generation depends only on this, so the
vendor can be swapped (LLM_PROVIDER) without touching the generator."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResult:
    text: str
    model: str  # the model that actually answered (may differ after a server-side fallback)
    input_tokens: int
    output_tokens: int


class LLMError(Exception):
    """Provider failure with a stable, user-displayable code.

    Codes: not_configured, auth_failed, rate_limited, provider_unavailable,
    connection_failed, bad_request, refused, truncated.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def generate(self, system: str, user_content: str) -> LLMResult:
        """Single-turn generation. Raises LLMError."""

    def count_tokens(self, system: str, user_content: str) -> int | None:
        """Exact input token count, or None if the provider cannot tell (caller estimates)."""
        return None
