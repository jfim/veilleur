"""LLM-driven xpath derivation, iterative.

Each call to :func:`derive_xpath` runs a short feedback loop:

1. Show the model an id-numbered listing of anchors.
2. Ask it to reply with two lines — ``articles:`` (the ids it intends to
   match) and ``xpath:`` (the expression).
3. Run that xpath against the same parsed tree, mapping matched
   ``<a>`` elements back to their numeric ids by Python object identity.
4. If intended == actual, return the xpath. Otherwise feed the diff back
   and let the model try again, up to ``VEILLEUR_XPATH_MAX_ATTEMPTS``
   turns.

Provider-agnostic: depends only on the :class:`LLMClient` Protocol.
The concrete :class:`HttpxLLMClient` here speaks the OpenAI-compatible
``/chat/completions`` shape, configured by ``LLM_API_URL``,
``LLM_MODEL_NAME`` and ``LLM_API_KEY``.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx
import lxml.etree

from veilleur.xpath.prompt import DEFAULT_PROMPT_TEMPLATE, load_active_template
from veilleur.xpath.types import (
    Anchor,
    LLMClient,
    LLMClientError,
    XPathAttempt,
    XPathDerivationFailed,
)

#: Bundled default. Kept as a re-export for back-compat; the active
#: template (potentially overridden via ``VEILLEUR_PROMPT_FILE``) is
#: resolved per-call by :func:`render_prompt`.
PROMPT_TEMPLATE = DEFAULT_PROMPT_TEMPLATE


def _build_listing(anchors: list[Anchor]) -> str:
    """Format the anchor block, prefixed with 1-based numeric ids."""
    return "\n".join(
        f"id={i} | {a.xpath} | text={a.text!r} | href={a.href}"
        for i, a in enumerate(anchors, start=1)
    )


def _format_previous_attempts(attempts: list[XPathAttempt]) -> str:
    """Render prior attempts as a feedback block; empty on turn 1."""
    if not attempts:
        return ""
    lines: list[str] = ["", "Previous attempts (none worked — do not repeat them):"]
    for a in attempts:
        if a.parse_error is not None:
            lines.append(f"  attempt {a.turn}: reply could not be parsed ({a.parse_error}).")
            continue
        if a.unable:
            lines.append(f"  attempt {a.turn}: you replied `unable`.")
            continue
        intended = ",".join(str(i) for i in a.intended_ids) or "(none)"
        actual = ",".join(str(i) for i in a.actual_ids) or "(none)"
        line = f"  attempt {a.turn}: xpath={a.xpath}\n"
        line += f"    you intended ids: {intended}\n"
        suffix = ""
        if a.extras_outside_listing:
            suffix = f" + {a.extras_outside_listing} match(es) outside the listing"
        line += f"    actually matched ids: {actual}{suffix}"
        intended_set = set(a.intended_ids)
        actual_set = set(a.actual_ids)
        missing = sorted(intended_set - actual_set)
        extras = sorted(actual_set - intended_set)
        if missing:
            line += f" (missing: {','.join(str(i) for i in missing)})"
        if extras:
            line += f" (extra: {','.join(str(i) for i in extras)})"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def render_prompt(
    title: str,
    url: str,
    anchors: list[Anchor],
    previous_attempts: list[XPathAttempt] | None = None,
) -> str:
    """Render the active prompt with anchors and optional feedback substituted.

    Honors ``VEILLEUR_PROMPT_FILE`` via :func:`load_active_template`. Custom
    templates that don't include a ``{previous_attempts}`` placeholder will
    still work — the iterative loop just won't be able to feed prior
    diagnostics back into the prompt for those overrides.
    """
    template = load_active_template()
    fields: dict[str, str] = {
        "title": title,
        "url": url,
        "listing": _build_listing(anchors),
        "previous_attempts": _format_previous_attempts(previous_attempts or []),
    }
    try:
        return template.format(**fields)
    except KeyError:
        # Override template references a placeholder we don't supply; fall
        # back to the bundled default rather than crash a scrape.
        return DEFAULT_PROMPT_TEMPLATE.format(**fields)


_THINKING_RE = re.compile(
    r"<\s*(think|thinking|thought|reasoning)\s*>.*?<\s*/\s*\1\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_thinking(raw: str) -> str:
    """Remove chain-of-thought blocks like ``<think>...</think>``."""
    return _THINKING_RE.sub("", raw)


def _strip_fences(raw: str) -> str:
    r"""Strip optional triple-backtick fences and an optional ``xpath`` tag."""
    text = raw.strip()
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        body = body.rsplit("```", 1)[0]
        if body.lstrip().lower().startswith("xpath"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        text = body.strip()
    return text


@dataclass(frozen=True, slots=True)
class _ParsedReply:
    """Outcome of parsing one model turn."""

    xpath: str | None
    intended_ids: tuple[int, ...]
    unable: bool
    parse_error: str | None


_ARTICLES_RE = re.compile(r"^\s*articles\s*:\s*(.*?)\s*$", re.IGNORECASE)
_XPATH_RE = re.compile(r"^\s*xpath\s*:\s*(.*?)\s*$", re.IGNORECASE)


def _parse_ids(raw: str, max_id: int) -> tuple[tuple[int, ...] | None, str | None]:
    """Parse a comma-separated id list. Returns (ids, error).

    Empty list is allowed (model may intend to match nothing — though
    that effectively guarantees a failed turn). Out-of-range ids are
    rejected with an explanatory error so the model can correct.
    """
    raw = raw.strip()
    if not raw:
        return ((), None)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    ids: list[int] = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            return (None, f"could not parse id {p!r} as an integer")
        if n < 1 or n > max_id:
            return (None, f"id {n} is out of range (valid: 1..{max_id})")
        ids.append(n)
    # Deduplicate while preserving order.
    seen: set[int] = set()
    unique: list[int] = []
    for n in ids:
        if n in seen:
            continue
        seen.add(n)
        unique.append(n)
    return (tuple(unique), None)


def _parse_reply(raw: str, max_id: int) -> _ParsedReply:
    """Parse one model turn into structured form."""
    cleaned = _strip_fences(_strip_thinking(raw))
    if not cleaned:
        return _ParsedReply(
            xpath=None,
            intended_ids=(),
            unable=False,
            parse_error="reply was empty",
        )
    if cleaned.strip().lower() == "unable":
        return _ParsedReply(
            xpath=None,
            intended_ids=(),
            unable=True,
            parse_error=None,
        )
    articles_line: str | None = None
    xpath_line: str | None = None
    for line in cleaned.splitlines():
        if articles_line is None:
            m = _ARTICLES_RE.match(line)
            if m is not None:
                articles_line = m.group(1)
                continue
        if xpath_line is None:
            m = _XPATH_RE.match(line)
            if m is not None:
                xpath_line = m.group(1)
                continue
    if articles_line is None or xpath_line is None:
        missing = []
        if articles_line is None:
            missing.append("`articles:` line")
        if xpath_line is None:
            missing.append("`xpath:` line")
        return _ParsedReply(
            xpath=None,
            intended_ids=(),
            unable=False,
            parse_error=f"missing {' and '.join(missing)}",
        )
    if not xpath_line.strip():
        return _ParsedReply(
            xpath=None,
            intended_ids=(),
            unable=False,
            parse_error="`xpath:` line was empty",
        )
    ids, err = _parse_ids(articles_line, max_id)
    if err is not None or ids is None:
        return _ParsedReply(
            xpath=None,
            intended_ids=(),
            unable=False,
            parse_error=err or "failed to parse `articles:` line",
        )
    return _ParsedReply(
        xpath=xpath_line.strip(),
        intended_ids=ids,
        unable=False,
        parse_error=None,
    )


def _run_xpath(
    root: object,
    xpath: str,
    id_by_element: dict[int, int],
) -> tuple[tuple[int, ...], int] | str:
    """Run *xpath* on *root* and map matches to anchor ids.

    Returns ``(ids, extras)`` on success — ``ids`` are the anchor ids
    that the xpath matched (in order, deduped) and ``extras`` is the
    count of matches that were not in the listing (filtered-out
    anchors or non-``<a>`` elements). Returns a string error message
    on syntax/eval failure.
    """
    try:
        result: Any = root.xpath(xpath)  # type: ignore[attr-defined]
    except (lxml.etree.XPathSyntaxError, lxml.etree.XPathEvalError) as exc:
        return f"xpath did not compile: {exc}"
    if not isinstance(result, list):
        return f"xpath did not return a node-set (got {type(result).__name__})"
    matched_ids: list[int] = []
    seen: set[int] = set()
    extras = 0
    for el in result:
        anchor_id = id_by_element.get(id(el))
        if anchor_id is None:
            extras += 1
            continue
        if anchor_id in seen:
            continue
        seen.add(anchor_id)
        matched_ids.append(anchor_id)
    return (tuple(matched_ids), extras)


@dataclass(frozen=True, slots=True)
class DerivationOutcome:
    """Successful return value of :func:`derive_xpath`."""

    xpath: str
    attempts: tuple[XPathAttempt, ...]


async def derive_xpath(
    title: str,
    url: str,
    anchors: list[Anchor],
    client: LLMClient,
    *,
    root: object | None = None,
    elements: tuple[object, ...] = (),
    max_attempts: int | None = None,
) -> DerivationOutcome:
    """Run the iterative derivation loop. Returns the xpath + attempt trace.

    Args:
        title: Page title (from ``extract_anchors``).
        url: URL the page was fetched from.
        anchors: Anchor listing the model will see.
        client: LLM transport.
        root: Parsed lxml tree to evaluate candidate xpaths against. May
            be ``None`` only if you don't care about validation (the loop
            then degrades to a single-shot, which is what tests of pure
            prompt rendering rely on).
        elements: One lxml element per ``anchors[i]``, in the same order.
            Required when ``root`` is provided.
        max_attempts: Override the env-driven default for this call. Must
            be >= 1.

    Raises:
        XPathDerivationFailed: model said ``unable``, the budget was
            exhausted without a clean match, or the reply was unparseable
            on the final turn.
        LLMClientError: transport / auth failure (raised by client).
    """
    if max_attempts is None:
        max_attempts = _resolve_max_attempts()
    if max_attempts < 1:
        raise XPathDerivationFailed(f"max_attempts must be >= 1, got {max_attempts}")

    id_by_element: dict[int, int]
    if root is not None:
        if len(elements) != len(anchors):
            raise XPathDerivationFailed(
                f"elements length {len(elements)} != anchors length {len(anchors)}"
            )
        id_by_element = {id(el): i for i, el in enumerate(elements, start=1)}
    else:
        id_by_element = {}

    attempts: list[XPathAttempt] = []
    for turn in range(1, max_attempts + 1):
        prompt = render_prompt(title, url, anchors, attempts)
        raw = await client.complete(prompt)
        parsed = _parse_reply(raw, max_id=len(anchors))

        if parsed.unable:
            attempts.append(
                XPathAttempt(
                    turn=turn,
                    xpath=None,
                    intended_ids=(),
                    actual_ids=(),
                    extras_outside_listing=0,
                    parse_error=None,
                    unable=True,
                    ok=False,
                )
            )
            raise XPathDerivationFailed(
                f"LLM returned 'unable' on attempt {turn}/{max_attempts}"
            )

        if parsed.parse_error is not None or parsed.xpath is None:
            attempts.append(
                XPathAttempt(
                    turn=turn,
                    xpath=None,
                    intended_ids=(),
                    actual_ids=(),
                    extras_outside_listing=0,
                    parse_error=parsed.parse_error or "unknown parse failure",
                    unable=False,
                    ok=False,
                )
            )
            continue

        if root is None:
            # Pure single-shot mode: trust the reply.
            attempts.append(
                XPathAttempt(
                    turn=turn,
                    xpath=parsed.xpath,
                    intended_ids=parsed.intended_ids,
                    actual_ids=parsed.intended_ids,
                    extras_outside_listing=0,
                    parse_error=None,
                    unable=False,
                    ok=True,
                )
            )
            return DerivationOutcome(xpath=parsed.xpath, attempts=tuple(attempts))

        run_result = _run_xpath(root, parsed.xpath, id_by_element)
        if isinstance(run_result, str):
            attempts.append(
                XPathAttempt(
                    turn=turn,
                    xpath=parsed.xpath,
                    intended_ids=parsed.intended_ids,
                    actual_ids=(),
                    extras_outside_listing=0,
                    parse_error=run_result,
                    unable=False,
                    ok=False,
                )
            )
            continue

        actual_ids, extras = run_result
        ok = (
            extras == 0
            and tuple(sorted(parsed.intended_ids)) == tuple(sorted(actual_ids))
        )
        attempts.append(
            XPathAttempt(
                turn=turn,
                xpath=parsed.xpath,
                intended_ids=parsed.intended_ids,
                actual_ids=actual_ids,
                extras_outside_listing=extras,
                parse_error=None,
                unable=False,
                ok=ok,
            )
        )
        if ok:
            return DerivationOutcome(xpath=parsed.xpath, attempts=tuple(attempts))

    raise XPathDerivationFailed(
        f"LLM did not converge on a matching xpath in {max_attempts} attempt(s)"
    )


def _resolve_max_attempts() -> int:
    """Read the cap from settings; fall back to the env var directly."""
    try:
        from veilleur.config import get_settings

        return int(get_settings().VEILLEUR_XPATH_MAX_ATTEMPTS)
    except Exception:
        raw = os.environ.get("VEILLEUR_XPATH_MAX_ATTEMPTS", "3")
        try:
            return int(raw)
        except ValueError:
            return 3


# --- HttpxLLMClient -----------------------------------------------------------


class HttpxLLMClient:
    """OpenAI-compatible chat-completions client backed by ``httpx.AsyncClient``."""

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
        self._api_url = api_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._http = http
        self._temperature = temperature

    @classmethod
    def from_env(cls, http: httpx.AsyncClient) -> HttpxLLMClient:
        """Construct from ``LLM_API_URL`` / ``LLM_MODEL_NAME`` / ``LLM_API_KEY``."""
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

        Transport errors and retryable HTTP statuses (429, 5xx) are retried
        with exponential backoff before surfacing as :class:`LLMClientError`.
        Non-retryable HTTP errors (4xx other than 429) fail immediately.
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

        max_retries = 3
        backoff = 1.0
        last_exc: Exception | None = None
        last_status: int | None = None
        last_body: str | None = None
        for attempt in range(1, max_retries + 1):
            try:
                response = await self._http.post(endpoint, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == max_retries:
                    raise LLMClientError(
                        f"transport error calling {endpoint} after {max_retries} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            status = response.status_code
            if 200 <= status < 300:
                break
            if status == 429 or 500 <= status < 600:
                last_status = status
                last_body = response.text[:500]
                if attempt == max_retries:
                    raise LLMClientError(
                        f"LLM returned HTTP {status} after {max_retries} attempts: {last_body}"
                    )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            raise LLMClientError(f"LLM returned HTTP {status}: {response.text[:500]}")
        else:  # pragma: no cover - loop always breaks or raises
            if last_exc is not None:
                raise LLMClientError(f"transport error calling {endpoint}: {last_exc}") from last_exc
            raise LLMClientError(f"LLM returned HTTP {last_status}: {last_body}")

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
