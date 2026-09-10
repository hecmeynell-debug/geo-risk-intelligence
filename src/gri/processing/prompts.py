"""Versioned extraction prompts.

ADR-0001 D6: prompt name and version are recorded on every extraction, and the template
is hashed so CI can detect an edited prompt that kept its version number. An evaluation
number that cannot be attributed to a specific prompt is not a measurement.

**Editing a template without bumping its version is a bug.** ``scripts/check_prompts.py``
fails when a registered hash no longer matches its text.
"""

from __future__ import annotations

from dataclasses import dataclass

from gri.ingestion.normalise import sha256_text
from gri.taxonomy import EVENT_TYPES, SECTORS, SEVERITY_LEVELS, sorted_values


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    purpose: str
    template: str

    @property
    def hash(self) -> str:
        return sha256_text(self.template)

    def render(self, **kwargs: object) -> str:
        return self.template.format(**kwargs)


_TAXONOMY = ", ".join(sorted_values(EVENT_TYPES))
_SECTORS = ", ".join(sorted_values(SECTORS))
_SEVERITIES = ", ".join(SEVERITY_LEVELS)

# The schema itself is enforced by constrained generation, so this prompt does not
# describe the output shape. It describes the *judgement*: what counts as in niche, how
# severity is assigned, and when to abstain. Restating the schema here would be a second
# source of truth that could drift from gri.schemas.
EXTRACT_EVENT_V1 = PromptTemplate(
    name="extract_event",
    version="v1",
    purpose="Extract one disruption event from one source document, with verbatim citations.",
    template="""You extract structured records of **maritime, energy, and supply-chain disruption** from official and public notices.

Your output is not a summary for a reader. It is a record that a human analyst will audit
against the source text, quote by quote. Everything you assert must be supported by text
you copy verbatim from the document.

# Scope

In scope: disruption to shipping, ports, canals and chokepoints; pipelines, refineries,
LNG and other energy infrastructure; electricity supply where it affects energy delivery;
subsea cables; freight, customs and border delays; and public regulatory or sanctions
notices that bear directly on those.

Out of scope: everything else. Political, military, financial and general news reporting
is out of scope **unless** the document itself reports an operational effect on the above
— and then you record the operational disruption, not the underlying political event.

If the document is out of scope, set `in_niche` to false and stop. Do not stretch a
document to fit.

Event type must be one of: {taxonomy}
Affected sectors must be drawn from: {sectors}

# Evidence rules

These are not style preferences. A record that breaks them is rejected by automated
checks before a human ever sees it.

1. **Quote verbatim.** Every quote you supply is searched for, character by character, in
   the stored source text. Paraphrasing, tidying punctuation, correcting a typo, or
   stitching together two separated passages will all fail. Copy exactly.
2. **Quote what supports the field.** Attach each quote to the field it evidences.
3. **Never assert what the document does not say.** No background knowledge, no inferred
   cause, no projected consequence. If you know something about this port that the notice
   does not state, it does not belong in the record.
4. **Attribute.** The document *reports* something. Write the summary that way.
5. **No individuals.** Record organisations, states, authorities, facilities and vessels.
   If the document names a person, leave them out entirely — do not record them in any
   field.

# Severity

Severity describes **reported operational disruption**, not geopolitical importance and
not harm to people. Assign it only from what the document states.

{severities}

- `informational` — an advisory or notice with no confirmed operational impact reported.
- `low` — localised impact at one facility or route, reported as resolved or resolving
  within about 24 hours, with no throughput reduction reported.
- `moderate` — sustained disruption of roughly one to seven days at a single node, or a
  measurable but contained reduction in throughput reported by the source.
- `high` — corridor-level or multi-node disruption, more than seven days at a critical
  node, or a source-reported material reduction in regional throughput or supply.
- `severe` — chokepoint or corridor closure with source-reported cross-regional effects.

**If the document reports no impact information, severity is `informational`.** Never
infer severity from urgent tone, prominence, or the seriousness of the place involved.

# Abstaining

Abstaining is correct behaviour, not failure. Set `abstained` to true when the document
is in scope but the evidence will not support a record — the account is too thin, the
key facts are hedged or contradictory, or you cannot find text that supports the fields
you would have to fill.

Prefer abstaining over guessing. A record you had to reach for is worse than no record.

# Confidence

`confidence` measures **how well the text you cited supports the record you built** — not
how likely the event is to be real, and not how important it is. Below 0.60 means weak,
partial, or conflicting evidence. Be honest here; a well-calibrated low score is more
useful than an optimistic high one.

---

Source: {source_name} ({publisher})
Published: {published_at}
URL: {url}
Title: {title}

Document text:
\"\"\"
{document_text}
\"\"\"
""",
)

#: Sent to the escalation pass. It is additive: the base prompt still applies, and this
#: says why the document is being looked at a second time.
ESCALATION_SUFFIX_V1 = PromptTemplate(
    name="extract_event_escalation_suffix",
    version="v1",
    purpose="Appended on the Opus pass when the first extraction was weak.",
    template="""

---

# Second pass

A first extraction of this document was weak and is being redone. What went wrong:

{escalation_reason}

Do not assume the first attempt was wrong about everything, and do not manufacture a
stronger record than the text supports. If the honest outcome is still a low confidence
score or an abstention, give that — an accurate abstention here is a better result than a
confident record that the evidence does not carry.

Pay particular attention to copying quotes exactly as they appear in the text above.
""",
)

# --------------------------------------------------------------------------------------
# v2: the same instructions, split at a cache breakpoint
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedPromptTemplate:
    """A prompt split into a cacheable prefix and a per-document remainder.

    Prompt caching is a *prefix* match: any byte change in the cached block invalidates
    it. So the split has to be exact -- the system half must contain nothing that varies
    per document, or the cache never hits and the only effect is added complexity.

    ``hash`` covers both halves, so editing either without bumping the version is caught
    by ``scripts/check_prompts.py`` (ADR-0001 D6).
    """

    name: str
    version: str
    purpose: str
    #: Stable across every call. Sent as a cached ``system`` block.
    system_template: str
    #: Varies per document. Sent as the user message, after the breakpoint.
    user_template: str

    @property
    def hash(self) -> str:
        return sha256_text(self.system_template + "" + self.user_template)

    def render_system(self) -> str:
        return self.system_template.format(
            taxonomy=_TAXONOMY, sectors=_SECTORS, severities=_SEVERITIES
        )

    def render_user(self, **kwargs: object) -> str:
        return self.user_template.format(**kwargs)


#: Hash separator, so ("ab", "c") and ("a", "bc") cannot collide.
SPLIT_SENTINEL = chr(30)

#: Where v1 stops being stable instructions and starts being this document. Splitting
#: v1's own template guarantees v2's wording is byte-identical rather than merely
#: careful -- a reworded v2 would confound the cost comparison with a behaviour change.
_V1_SPLIT_AT = EXTRACT_EVENT_V1.template.index(chr(10) + "---" + chr(10) + chr(10) + "Source: ")
_V1_INSTRUCTIONS = EXTRACT_EVENT_V1.template[:_V1_SPLIT_AT]
_V1_DOCUMENT_BLOCK = EXTRACT_EVENT_V1.template[_V1_SPLIT_AT:]


#: v2 is v1 restructured, not rewritten. The instruction text is taken verbatim from
#: EXTRACT_EVENT_V1 -- only *where* it is sent changed, so a cost comparison between the
#: two versions is not confounded by a wording change.
#:
#: Measured motivation (ADR-0002): a 228-character notice cost 5,270 input tokens because
#: this block is ~5,000 tokens of identical bytes resent on every call.
EXTRACT_EVENT_V2 = CachedPromptTemplate(
    name="extract_event",
    version="v2",
    purpose=(
        "Extract one disruption event from one source document, with verbatim citations."
        " Identical wording to v1, split at a cache breakpoint."
    ),
    system_template=_V1_INSTRUCTIONS,
    user_template=_V1_DOCUMENT_BLOCK,
)


#: Same wording as v1. Under v2 it is appended to the USER message rather than the
#: whole prompt, because the escalation reason varies per call and anything volatile
#: in the cached prefix would invalidate the cache on every request.
ESCALATION_SUFFIX_V2 = PromptTemplate(
    name="extract_event_escalation_suffix",
    version="v2",
    purpose="Appended to the user message on the Opus pass when the first extraction was weak.",
    template=ESCALATION_SUFFIX_V1.template,
)

#: Every released prompt, of either shape. check_prompts.py hashes them all.
REGISTRY: dict[tuple[str, str], PromptTemplate | CachedPromptTemplate] = {
    (p.name, p.version): p
    for p in (EXTRACT_EVENT_V1, ESCALATION_SUFFIX_V1, EXTRACT_EVENT_V2, ESCALATION_SUFFIX_V2)
}


def get_prompt(name: str, version: str) -> PromptTemplate | CachedPromptTemplate:
    try:
        return REGISTRY[(name, version)]
    except KeyError:
        raise KeyError(f"no prompt {name!r} version {version!r}") from None


def render_extraction_prompt(
    *,
    source_name: str,
    publisher: str,
    published_at: str,
    url: str,
    title: str,
    document_text: str,
    escalation_reason: str | None = None,
) -> tuple[str, PromptTemplate]:
    """Render the extraction prompt, optionally with the escalation suffix."""
    base = EXTRACT_EVENT_V1
    rendered = base.template.format(
        taxonomy=_TAXONOMY,
        sectors=_SECTORS,
        severities=_SEVERITIES,
        source_name=source_name,
        publisher=publisher,
        published_at=published_at,
        url=url,
        title=title,
        document_text=document_text,
    )
    if escalation_reason:
        rendered += ESCALATION_SUFFIX_V1.template.format(escalation_reason=escalation_reason)
    return rendered, base


def render_extraction_v2(
    *,
    source_name: str,
    publisher: str,
    published_at: str,
    url: str,
    title: str,
    document_text: str,
    escalation_reason: str | None = None,
) -> tuple[str, str, CachedPromptTemplate]:
    """Render v2 as ``(system_text, user_text, template)``.

    ``system_text`` must be byte-identical on every call for the cache to hit -- it is
    built only from the taxonomy constants, never from the document.
    """
    system_text = EXTRACT_EVENT_V2.render_system()
    user_text = EXTRACT_EVENT_V2.render_user(
        source_name=source_name,
        publisher=publisher,
        published_at=published_at,
        url=url,
        title=title,
        document_text=document_text,
    )
    if escalation_reason:
        user_text += ESCALATION_SUFFIX_V2.template.format(escalation_reason=escalation_reason)
    return system_text, user_text, EXTRACT_EVENT_V2
