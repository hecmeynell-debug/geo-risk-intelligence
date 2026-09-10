"""The candidate source registry.

The most important assertions here are the negative ones: nothing in the registry claims
to be verified, because nobody has verified anything yet (NG-2).
"""

from __future__ import annotations

import pytest

from gri.ingestion.adapters import HtmlNoticeAdapter, JsonApiAdapter, RssAdapter
from gri.ingestion.registry import CANDIDATE_SOURCES, SourceConfig, source_by_slug
from gri.taxonomy import RETRIEVAL_METHODS


class TestRegistryHonesty:
    """These guard the claim the README makes about what has and has not been checked."""

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_no_endpoint_is_claimed_verified(self, config: SourceConfig) -> None:
        assert config.endpoint_verified is False

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_no_format_is_claimed_confirmed(self, config: SourceConfig) -> None:
        assert config.format_confirmed is False

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_every_source_says_why_it_is_in_niche(self, config: SourceConfig) -> None:
        """NG-6: a source with no stated rationale is scope creep waiting to happen."""
        assert config.niche_rationale.strip()

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_every_source_tells_the_reviewer_what_to_check(self, config: SourceConfig) -> None:
        assert config.review_notes.strip()


class TestRegistryShape:
    def test_within_the_permitted_source_count(self) -> None:
        # CONSTRAINTS.md Section 3: 5-10 sources, a ceiling rather than a target.
        assert 5 <= len(CANDIDATE_SOURCES) <= 10

    def test_slugs_are_unique(self) -> None:
        slugs = [c.slug for c in CANDIDATE_SOURCES]
        assert len(slugs) == len(set(slugs))

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_retrieval_method_is_supported(self, config: SourceConfig) -> None:
        assert config.retrieval_method in RETRIEVAL_METHODS

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_poll_interval_is_within_policy(self, config: SourceConfig) -> None:
        assert 1800 <= config.poll_interval_seconds <= 3600

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_feed_url_is_https(self, config: SourceConfig) -> None:
        assert config.feed_url.startswith("https://")

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_adapter_can_be_built(self, config: SourceConfig) -> None:
        """A registry entry whose adapter options are wrong should fail here, not at 3am."""
        adapter = config.build_adapter()
        assert adapter.retrieval_method == config.retrieval_method

    @pytest.mark.parametrize("config", CANDIDATE_SOURCES, ids=lambda c: c.slug)
    def test_built_adapter_survives_an_empty_payload(self, config: SourceConfig) -> None:
        assert config.build_adapter().parse(b"", config.feed_url) == []

    def test_adapter_engines_are_covered(self) -> None:
        built = {type(c.build_adapter()) for c in CANDIDATE_SOURCES}
        assert built == {RssAdapter, JsonApiAdapter, HtmlNoticeAdapter}


class TestLookup:
    def test_finds_a_known_slug(self) -> None:
        assert source_by_slug(CANDIDATE_SOURCES[0].slug) is CANDIDATE_SOURCES[0]

    def test_unknown_slug_raises(self) -> None:
        with pytest.raises(KeyError):
            source_by_slug("no-such-source")

    def test_unsupported_retrieval_method_raises(self) -> None:
        config = SourceConfig(
            slug="x",
            name="x",
            publisher="x",
            feed_url="https://x.invalid/",
            retrieval_method="bulk_file",
        )
        with pytest.raises(ValueError, match="no adapter engine"):
            config.build_adapter()
