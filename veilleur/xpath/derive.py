"""LLM-driven xpath derivation.

Provider-agnostic: depends only on the :class:`LLMClient` Protocol.
The concrete :class:`HttpxLLMClient` here speaks the OpenAI-compatible
``/chat/completions`` shape, configured by the ``LLM_API_URL``,
``LLM_MODEL_NAME`` and ``LLM_API_KEY`` environment variables.

The prompt was originally adapted from ``~/projects/rss-ify/prompt.txt``
but has diverged: anchor paths now carry ``id`` / ``class`` hints
(see :mod:`veilleur.xpath.anchors`) and the prompt explains that
format so the model can target classes/ids instead of fragile
positional indices.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from veilleur.xpath.types import (
    Anchor,
    LLMClient,
    LLMClientError,
    XPathDerivationFailed,
)

#: Verbatim copy of ``~/projects/rss-ify/prompt.txt``. Substitution
#: placeholders: ``{title}``, ``{url}``, ``{listing}``.
PROMPT_TEMPLATE = """Given the following descriptive paths for links on a webpage, return an xpath expression that only matches links to articles. Do not return navigation links, tag/category links, or other irrelevant links, just links that appear to be an article so that the user can use them to build a RSS feed. The page is called {title} and is from {url}.

Each line below describes one ``<a>`` element. The path uses a CSS-like
notation: ``tag#id`` when the element has an id, ``tag.class1.class2``
when it has classes, ``tag#id.class`` when it has both, and ``tag[N]``
(1-indexed among same-tag siblings) only when neither id nor class is
available. Prefer xpath predicates that target ids and class names
over positional indices when possible. The href is shown raw — if
hrefs are relative (e.g. ``2025/post/index.html``), substring
predicates like ``contains(@href, '/2025/')`` will NOT match them; use
``starts-with(@href, '2025/')`` or class/id structure instead.

{listing}

Return only an xpath expression if you are able to match all of the articles and no other unwanted links, with no commentary or other messages, no markdown, no code fences, no backticks — just the raw xpath expression on a single line. If you are unable to write such an xpath expression, just write 'unable'.
"""


def _build_listing(anchors: list[Anchor]) -> str:
    """Format the anchor block exactly as ``derive_xpath.py`` does."""
    return "\n".join(f"{a.xpath} | text={a.text!r} | href={a.href}" for a in anchors)


def render_prompt(title: str, url: str, anchors: list[Anchor]) -> str:
    """Render the canonical prompt with anchor listing substituted."""
    return PROMPT_TEMPLATE.format(title=title, url=url, listing=_build_listing(anchors))


_THINKING_RE = re.compile(
    r"<\s*(think|thinking|thought|reasoning)\s*>.*?<\s*/\s*\1\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_thinking(raw: str) -> str:
    """Remove chain-of-thought blocks like ``<think>...</think>``.

    Some open-weight models (e.g. DeepSeek/Qwen-style "thinking" variants)
    prepend their reasoning inside a ``<think>`` tag before the actual
    answer. Strip those so downstream xpath validation sees only the
    expression.
    """
    return _THINKING_RE.sub("", raw)


def _strip_fences(raw: str) -> str:
    r"""Strip optional triple-backtick fences and an optional ``xpath`` tag.

    Mirrors the post-processing in ``derive_xpath.py`` so models that
    produce ```xpath\n...\n``` still get cleaned up.
    """
    text = raw.strip()
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        body = body.rsplit("```", 1)[0]
        if body.lstrip().lower().startswith("xpath"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        text = body.strip()
    return text


async def derive_xpath(
    title: str,
    url: str,
    anchors: list[Anchor],
    client: LLMClient,
) -> str:
    """Ask the LLM for an xpath expression that selects article links.

    Args:
        title: The page title (from ``extract_anchors``).
        url: The URL the page was fetched from.
        anchors: Filtered anchors (already capped by the extractor).
        client: An :class:`LLMClient` implementation.

    Returns:
        The raw xpath string returned by the model. Not validated here —
        :func:`veilleur.xpath.apply.apply_xpath` validates against the page.

    Raises:
        XPathDerivationFailed: The model returned ``unable`` (case-insensitive)
            or an empty reply.
        LLMClientError: Underlying transport / auth failure (raised by the
            client implementation).
    """
    prompt = render_prompt(title, url, anchors)
    raw = await client.complete(prompt)
    cleaned = _strip_fences(_strip_thinking(raw))
    if not cleaned:
        raise XPathDerivationFailed("LLM returned an empty reply")
    if cleaned.lower() == "unable":
        raise XPathDerivationFailed("LLM returned 'unable'")
    return cleaned


# --- HttpxLLMClient -----------------------------------------------------------


class HttpxLLMClient:
    """OpenAI-compatible chat-completions client backed by ``httpx.AsyncClient``.

    Sends to ``{api_url}/chat/completions`` with a Bearer token. The
    same env-var triple drives any OpenAI-compatible endpoint
    (Anthropic via proxy, Ollama, vLLM, LM Studio, etc.).
    """

    def __init__(
        self,
        api_url: str,
        model: str,
        api_key: str,
        http: httpx.AsyncClient,
        temperature: float = 0.0,
    ) -> None:
        if not api_url:
            raise LLMClientError("api_url is required")
        if not model:
            raise LLMClientError("model is required")
        if not api_key:
            raise LLMClientError("api_key is required")
        # Trailing-slash normalize so callers can pass either form.
        self._api_url = api_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._http = http
        self._temperature = temperature

    @classmethod
    def from_env(cls, http: httpx.AsyncClient) -> HttpxLLMClient:
        """Construct from ``LLM_API_URL`` / ``LLM_MODEL_NAME`` / ``LLM_API_KEY``.

        Raises:
            LLMClientError: If any of the three env vars is missing/empty.
        """
        api_url = os.environ.get("LLM_API_URL", "")
        model = os.environ.get("LLM_MODEL_NAME", "")
        api_key = os.environ.get("LLM_API_KEY", "")
        missing = [
            name
            for name, value in (
                ("LLM_API_URL", api_url),
                ("LLM_MODEL_NAME", model),
                ("LLM_API_KEY", api_key),
            )
            if not value
        ]
        if missing:
            raise LLMClientError(f"missing required environment variables: {', '.join(missing)}")
        return cls(api_url=api_url, model=model, api_key=api_key, http=http)

    async def complete(self, prompt: str) -> str:
        """Send a single user-turn prompt and return the assistant's text.

        Raises:
            LLMClientError: On non-2xx response, transport error, or
                malformed JSON shape.
        """
        endpoint = f"{self._api_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = await self._http.post(endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMClientError(f"transport error calling {endpoint}: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise LLMClientError(f"LLM returned HTTP {response.status_code}: {response.text[:500]}")

        try:
            data: Any = response.json()
        except ValueError as exc:
            raise LLMClientError(f"LLM returned non-JSON body: {exc}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMClientError(f"unexpected response shape: {data!r}") from exc

        if not isinstance(content, str):
            raise LLMClientError(f"expected string content, got {type(content).__name__}")
        return content
