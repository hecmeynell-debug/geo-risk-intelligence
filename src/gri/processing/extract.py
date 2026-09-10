"""Schema-constrained extraction, with the Sonnet 5 -> Opus 5 cascade.

ADR-0002 D2 and D3. Generation is constrained to :class:`gri.schemas.ExtractionResult`
via ``client.messages.parse``, so schema-invalid output is a non-event rather than
something we retry around.

Every call — including each half of a cascade — is recorded with its model, prompt
version, token counts, latency, and computed cost, so cost per document is measured
rather than estimated.

The provider is an interface. Tests use :class:`ScriptedExtractionProvider`, so the suite
runs offline with no key and no spend.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from gri.config import get_settings
from gri.logging import get_logger
from gri.processing.prompts import EXTRACT_EVENT_V1, render_extraction_prompt
from gri.schemas import ExtractionResult

log = get_logger(__name__)

#: ADR-0002 D2. Exact model ID strings, no date suffixes.
DEFAULT_MODEL = "claude-sonnet-5"
ESCALATION_MODEL = "claude-opus-5"

#: USD per million tokens, as published. Used to compute cost_usd per call so that
#: cost-per-document is a measured number.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

MAX_TOKENS = 16000

#: Documents longer than this are not silently truncated -- they are refused, and the
#: caller decides. Truncating would drop the very passage a citation might need.
MAX_DOCUMENT_CHARS = 400_000


class DocumentTooLong(Exception):
    """The document exceeds what we will send in a single request."""


@dataclass
class ExtractionCall:
    """Telemetry for one model call."""

    model: str
    prompt_name: str
    prompt_version: str
    result: ExtractionResult | None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    escalated: bool = False
    error: str | None = None
    request_id: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None and self.error is None


@dataclass
class ExtractionOutcome:
    """The full result for one document, including any escalation."""

    calls: list[ExtractionCall] = field(default_factory=list)

    @property
    def final(self) -> ExtractionCall | None:
        """The call whose result we keep: the last successful one."""
        for call in reversed(self.calls):
            if call.succeeded:
                return call
        return self.calls[-1] if self.calls else None

    @property
    def result(self) -> ExtractionResult | None:
        final = self.final
        return final.result if final else None

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def total_latency_ms(self) -> int:
        return sum(c.latency_ms for c in self.calls)

    @property
    def did_escalate(self) -> bool:
        return any(c.escalated for c in self.calls)


def compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost of one call. Unknown models cost 0.0 and say so rather than guessing."""
    rates = PRICING_USD_PER_MTOK.get(model)
    if rates is None:
        log.warning("unknown_model_pricing", model=model)
        return 0.0
    input_rate, output_rate = rates
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


class ExtractionProvider(Protocol):
    """Turns a rendered prompt into a validated :class:`ExtractionResult`."""

    def extract(self, prompt: str, model: str) -> ExtractionCall: ...


class AnthropicExtractionProvider:
    """The real provider, using constrained generation via ``messages.parse``."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            import anthropic

            # A bare constructor resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
            # `ant auth login` profile. Only pass a key when one was injected explicitly.
            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key
                else anthropic.Anthropic()
            )
        return self._client

    def extract(self, prompt: str, model: str) -> ExtractionCall:
        import anthropic

        call = ExtractionCall(
            model=model,
            prompt_name=EXTRACT_EVENT_V1.name,
            prompt_version=EXTRACT_EVENT_V1.version,
            result=None,
        )
        client = self._get_client()
        started = time.perf_counter()

        try:
            response = client.messages.parse(  # type: ignore[attr-defined]
                model=model,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                output_format=ExtractionResult,
            )
        except anthropic.APIStatusError as exc:
            call.error = f"api_status_error {exc.status_code}: {exc.message}"
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            log.error("extraction_api_error", model=model, status=exc.status_code)
            return call
        except anthropic.APIConnectionError as exc:
            call.error = f"api_connection_error: {exc}"
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            log.error("extraction_connection_error", model=model, error=str(exc))
            return call

        call.latency_ms = int((time.perf_counter() - started) * 1000)
        call.request_id = getattr(response, "_request_id", None)

        usage = getattr(response, "usage", None)
        if usage is not None:
            call.input_tokens = getattr(usage, "input_tokens", 0) or 0
            call.output_tokens = getattr(usage, "output_tokens", 0) or 0
        call.cost_usd = compute_cost_usd(model, call.input_tokens, call.output_tokens)

        # A refusal is an outcome to record, not an exception to swallow.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            call.error = f"refusal: {category}"
            log.warning("extraction_refused", model=model, category=category)
            return call

        call.result = response.parsed_output
        return call


class ScriptedExtractionProvider:
    """Returns pre-built results in order. Test-support only.

    Lets the whole pipeline be exercised — including the cascade — with no key, no
    network, and no spend, which is what keeps the Phase 4 harness runnable in CI.
    """

    def __init__(self, results: list[ExtractionResult | Exception | None]) -> None:
        self._results = list(results)
        self.calls_made: list[tuple[str, str]] = []

    def extract(self, prompt: str, model: str) -> ExtractionCall:
        self.calls_made.append((model, prompt))
        call = ExtractionCall(
            model=model,
            prompt_name=EXTRACT_EVENT_V1.name,
            prompt_version=EXTRACT_EVENT_V1.version,
            result=None,
            input_tokens=1000,
            output_tokens=250,
            latency_ms=5,
        )
        call.cost_usd = compute_cost_usd(model, call.input_tokens, call.output_tokens)

        if not self._results:
            call.error = "scripted provider exhausted"
            return call

        nxt = self._results.pop(0)
        if isinstance(nxt, Exception):
            call.error = str(nxt)
        else:
            call.result = nxt
        return call


def should_escalate(
    call: ExtractionCall,
    citation_failures: int,
    review_threshold: float = 0.60,
) -> str | None:
    """Reason to re-run on the stronger model, or None.

    ADR-0002 D2: escalate only when the first pass was weak, so Opus is spent on the
    cases where judgement actually matters.
    """
    if call.error is not None:
        return f"the first pass failed: {call.error}"

    result = call.result
    if result is None:
        return "the first pass returned no result"

    if citation_failures > 0:
        return (
            f"{citation_failures} quote(s) from the first pass could not be found in the"
            " source text, which means they were not copied verbatim"
        )

    if (
        result.produced_event
        and result.event is not None
        and result.event.confidence < review_threshold
    ):
        return (
            f"the first pass reported confidence {result.event.confidence:.2f}, below"
            f" the {review_threshold} review threshold"
        )

    # An abstention on an in-niche document is worth a second, more capable look --
    # but an out-of-niche judgement is cheap and reliable, so it is not escalated.
    if result.in_niche and result.abstained:
        return (
            "the first pass judged the document in scope but abstained; a second look may"
            " find evidence it missed"
        )

    return None


def extract_document(
    *,
    provider: ExtractionProvider,
    source_name: str,
    publisher: str,
    published_at: str,
    url: str,
    title: str,
    document_text: str,
    verify_citations: object | None = None,
    allow_escalation: bool = True,
) -> ExtractionOutcome:
    """Extract one document, escalating to the stronger model when the first pass is weak.

    ``verify_citations`` is a callable taking an :class:`ExtractionResult` and returning a
    count of failed quotes. It is injected rather than imported so this module stays
    independent of storage.
    """
    if len(document_text) > MAX_DOCUMENT_CHARS:
        raise DocumentTooLong(
            f"document is {len(document_text)} characters, over the "
            f"{MAX_DOCUMENT_CHARS} limit; truncating could drop a cited passage"
        )

    outcome = ExtractionOutcome()

    prompt, _template = render_extraction_prompt(
        source_name=source_name,
        publisher=publisher,
        published_at=published_at,
        url=url,
        title=title,
        document_text=document_text,
    )

    first = provider.extract(prompt, DEFAULT_MODEL)
    outcome.calls.append(first)

    failures = _count_failures(first, verify_citations)
    reason = should_escalate(first, failures) if allow_escalation else None

    if reason is None:
        log.info(
            "extraction_complete",
            model=first.model,
            escalated=False,
            cost_usd=round(first.cost_usd, 6),
        )
        return outcome

    log.info("extraction_escalating", reason=reason, to_model=ESCALATION_MODEL)

    escalation_prompt, _ = render_extraction_prompt(
        source_name=source_name,
        publisher=publisher,
        published_at=published_at,
        url=url,
        title=title,
        document_text=document_text,
        escalation_reason=reason,
    )
    second = provider.extract(escalation_prompt, ESCALATION_MODEL)
    second.escalated = True
    outcome.calls.append(second)

    log.info(
        "extraction_complete",
        model=second.model,
        escalated=True,
        cost_usd=round(outcome.total_cost_usd, 6),
    )
    return outcome


def _count_failures(call: ExtractionCall, verify_citations: object | None) -> int:
    if verify_citations is None or call.result is None:
        return 0
    if not call.result.produced_event or call.result.event is None:
        return 0
    return int(verify_citations(call.result))  # type: ignore[operator]


_default_provider: ExtractionProvider | None = None


def get_provider() -> ExtractionProvider:
    global _default_provider
    if _default_provider is None:
        settings = get_settings()
        _default_provider = AnthropicExtractionProvider(api_key=settings.anthropic_api_key)
    return _default_provider


def set_provider(provider: ExtractionProvider | None) -> None:
    """Override the provider. Test-support only."""
    global _default_provider
    _default_provider = provider
