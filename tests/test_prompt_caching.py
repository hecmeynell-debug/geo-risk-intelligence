"""Prompt caching: the split, and the invariants it depends on.

Caching is a prefix match, so the whole saving rests on one property: the cached block
must be byte-identical on every call. These tests pin that, and pin that v2 did not
quietly reword v1 while restructuring it.
"""

from __future__ import annotations

import pytest

from gri.processing.extract import (
    CACHE_READ_MULTIPLIER,
    DEFAULT_MODEL,
    compute_cost_usd,
)
from gri.processing.prompts import (
    EXTRACT_EVENT_V1,
    EXTRACT_EVENT_V2,
    REGISTRY,
    render_extraction_v2,
)


class TestV2IsV1Restructured:
    """v2 changed where the text is sent, not what it says."""

    def test_halves_concatenate_back_to_v1(self) -> None:
        combined = EXTRACT_EVENT_V2.system_template + EXTRACT_EVENT_V2.user_template
        assert combined == EXTRACT_EVENT_V1.template

    def test_v1_is_still_registered(self) -> None:
        """History must stay attributable: v1 measurements refer to a prompt that exists."""
        assert ("extract_event", "v1") in REGISTRY
        assert ("extract_event", "v2") in REGISTRY

    def test_v1_and_v2_hash_differently(self) -> None:
        assert EXTRACT_EVENT_V1.hash != EXTRACT_EVENT_V2.hash

    def test_v2_hash_covers_both_halves(self) -> None:
        from dataclasses import replace

        moved = replace(
            EXTRACT_EVENT_V2,
            system_template=EXTRACT_EVENT_V2.system_template + " ",
        )
        assert moved.hash != EXTRACT_EVENT_V2.hash


class TestCachedBlockIsStable:
    """The one property the entire saving depends on."""

    def _render(self, **overrides: str) -> tuple[str, str]:
        kwargs = {
            "source_name": "Example Authority",
            "publisher": "Example",
            "published_at": "2026-09-01T08:30:00+00:00",
            "url": "https://notices.example.invalid/1",
            "title": "A notice",
            "document_text": "The port is closed.",
        }
        kwargs.update(overrides)
        system, user, _ = render_extraction_v2(**kwargs)  # type: ignore[arg-type]
        return system, user

    def test_system_block_is_identical_across_different_documents(self) -> None:
        first, _ = self._render(document_text="One document.")
        second, _ = self._render(document_text="An entirely different document.")
        assert first == second

    def test_system_block_is_identical_across_different_sources(self) -> None:
        first, _ = self._render(source_name="A", publisher="A", url="https://a.invalid/")
        second, _ = self._render(source_name="B", publisher="B", url="https://b.invalid/")
        assert first == second

    def test_system_block_is_identical_with_and_without_escalation(self) -> None:
        """The escalation reason varies per call, so it must sit after the breakpoint."""
        plain, _ = self._render()
        escalated_system, escalated_user, _ = render_extraction_v2(
            source_name="Example Authority",
            publisher="Example",
            published_at="2026-09-01T08:30:00+00:00",
            url="https://notices.example.invalid/1",
            title="A notice",
            document_text="The port is closed.",
            escalation_reason="citations failed verification",
        )
        assert escalated_system == plain
        assert "citations failed verification" in escalated_user

    @pytest.mark.parametrize(
        "marker", ["UNIQUE_DOC_MARKER", "https://unique.example.invalid/x", "A Unique Title"]
    )
    def test_nothing_per_document_leaks_into_the_cached_block(self, marker: str) -> None:
        system, _ = self._render(document_text=marker, url=marker, title=marker)
        assert marker not in system

    def test_system_block_has_no_unrendered_placeholders(self) -> None:
        system, _ = self._render()
        assert "{" not in system and "}" not in system

    def test_system_block_is_large_enough_to_be_worth_caching(self) -> None:
        """Below the model's minimum cacheable prefix, caching silently does nothing."""
        system, _ = self._render()
        assert len(system) > 2000


class TestCacheAwareCosting:
    def test_cache_read_is_much_cheaper_than_full_input(self) -> None:
        full = compute_cost_usd(DEFAULT_MODEL, 5270, 150)
        cached = compute_cost_usd(DEFAULT_MODEL, 270, 150, cache_read_tokens=5000)
        assert cached < full * 0.4

    def test_cache_write_costs_more_than_plain_input(self) -> None:
        plain = compute_cost_usd(DEFAULT_MODEL, 5000, 0)
        written = compute_cost_usd(DEFAULT_MODEL, 0, 0, cache_write_tokens=5000)
        assert written > plain

    def test_cache_read_multiplier_is_applied(self) -> None:
        plain = compute_cost_usd(DEFAULT_MODEL, 1000, 0)
        read = compute_cost_usd(DEFAULT_MODEL, 0, 0, cache_read_tokens=1000)
        assert read == pytest.approx(plain * CACHE_READ_MULTIPLIER)

    def test_zero_cache_tokens_matches_the_old_calculation(self) -> None:
        assert compute_cost_usd(DEFAULT_MODEL, 1000, 500) == compute_cost_usd(
            DEFAULT_MODEL, 1000, 500, 0, 0
        )
