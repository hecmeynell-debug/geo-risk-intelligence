"""Run one synthetic document through REAL extraction, end to end.

This is the one thing the test suite deliberately cannot check: the suite runs against a
scripted provider so it needs no key and no spend, which means the real API path — the
constrained-output call, the token accounting, the cascade — is never exercised by CI.

**This script costs money.** It makes one or two real model calls and prints the measured
cost. It is a manual smoke test, not part of any automated run.

The document is synthetic (see ``--help``), so nothing here depends on a publisher whose
terms have not been reviewed.

Usage::

    docker compose run --rm migrate python scripts/live_extraction_smoke.py
    docker compose run --rm migrate python scripts/live_extraction_smoke.py --fabricate

``--fabricate`` swaps in a document that does not support the obvious conclusion, to see
whether the model abstains or invents — the behaviour the whole project is built around.
"""

from __future__ import annotations

import argparse
import sys

from gri.config import get_settings
from gri.ingestion.normalise import normalise_text
from gri.processing.extract import (
    AnthropicExtractionProvider,
    extract_document,
)
from gri.processing.verify import verify_evidence

#: A clear, well-evidenced notice. Invented; any resemblance to a real advisory is
#: coincidental and the place names are deliberately placeholders.
CLEAR_NOTICE = normalise_text(
    "NOTICE TO MARINERS 2026-114. The port of Example is closed to all commercial "
    "traffic with immediate effect following a channel obstruction identified during a "
    "routine survey on 1 September 2026. Vessels should expect delays of up to 72 hours. "
    "Container and bulk operations are suspended until the channel has been re-surveyed. "
    "The Example Port Authority will issue a further update at 0600 local time. "
    "Anchorage remains available to the north of the fairway."
)

#: Thin and hedged: names no date, no confirmed impact, and attributes everything to
#: unnamed reports. The correct outcome is an abstention or a low-confidence record --
#: not a confident one.
THIN_NOTICE = normalise_text(
    "Local reports suggest there may have been some disruption at a port in the region "
    "earlier this week. Officials have not confirmed whether operations were affected. "
    "Shipping sources indicated that conditions were being monitored."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fabricate",
        action="store_true",
        help="use a thin, hedged notice to test whether the model abstains rather than invents",
    )
    parser.add_argument(
        "--no-escalation", action="store_true", help="disable the Opus escalation pass"
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.anthropic_api_key:
        print(
            "no GRI_ANTHROPIC_API_KEY set. The SDK may still resolve ANTHROPIC_API_KEY or an\n"
            "`ant auth login` profile; if this fails, set GRI_ANTHROPIC_API_KEY in .env.",
            file=sys.stderr,
        )

    document = THIN_NOTICE if args.fabricate else CLEAR_NOTICE
    label = "thin/hedged" if args.fabricate else "clear and well-evidenced"

    print(f"Document: {label}")
    print(f"Length:   {len(document)} characters")
    print("-" * 78)

    provider = AnthropicExtractionProvider(api_key=settings.anthropic_api_key)

    def count_failures(candidate: object) -> int:
        event = getattr(candidate, "event", None)
        if event is None:
            return 0
        return verify_evidence(event.evidence, document).failed

    outcome = extract_document(
        provider=provider,
        source_name="Example Maritime Authority Notices",
        publisher="Example Maritime Authority",
        published_at="2026-09-01T08:30:00+00:00",
        url="https://notices.example.invalid/notice/2026-114",
        title="Notice to Mariners 2026-114",
        document_text=document,
        verify_citations=count_failures,
        allow_escalation=not args.no_escalation,
    )

    for index, call in enumerate(outcome.calls, start=1):
        marker = " (escalated)" if call.escalated else ""
        print(f"\nCall {index}: {call.model}{marker}")
        print(f"  prompt        {call.prompt_name} {call.prompt_version}")
        print(f"  tokens        in={call.input_tokens} out={call.output_tokens}")
        print(f"  latency       {call.latency_ms} ms")
        print(f"  cost          ${call.cost_usd:.6f}")
        if call.request_id:
            print(f"  request id    {call.request_id}")
        if call.error:
            print(f"  ERROR         {call.error}")

    result = outcome.result
    print("\n" + "-" * 78)

    if result is None:
        print("No result returned.")
        return 1

    print(f"in_niche:  {result.in_niche}")
    print(f"abstained: {result.abstained}")
    if result.abstention_reason:
        print(f"reason:    {result.abstention_reason}")

    if not result.produced_event or result.event is None:
        print("\nOutcome: no event extracted (this is a success path, not a failure).")
        print(f"Total cost: ${outcome.total_cost_usd:.6f}")
        return 0

    event = result.event
    print(f"\nevent_type:  {event.event_type}")
    print(f"event_date:  {event.event_date}")
    print(f"severity:    {event.severity}")
    print(f"confidence:  {event.confidence}")
    print(f"title:       {event.title}")
    print(f"summary:     {event.summary}")
    print(f"locations:   {[loc.name for loc in event.locations]}")
    print(f"actors:      {[(a.name, a.entity_type) for a in event.actors]}")
    print(f"sectors:     {event.affected_sectors}")

    report = verify_evidence(event.evidence, document)
    print(f"\nCitation verification: {report.checked - report.failed}/{report.checked} verified")
    for quote in report.quotes:
        mark = "OK  " if quote.verified else "FAIL"
        print(f"  [{mark}] ({quote.field_supported}) {quote.quote[:88]}")
        if not quote.verified:
            print(f"          -> {quote.failure_reason}")

    print(f"\nTotal cost:    ${outcome.total_cost_usd:.6f}")
    print(f"Total latency: {outcome.total_latency_ms} ms")
    print(f"Escalated:     {outcome.did_escalate}")

    if not report.all_verified:
        print("\nThis record would NOT be published: citation verification failed.")
        return 0

    print("\nThis record would be published or routed to review, with verified citations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
