"""Data contracts shared by every stage: ingest -> audit -> issue -> fix -> test -> dashboard."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Speaker = Literal["bot", "user"]
Slot = Literal["product", "quantity", "unit", "spec", "name", "city", "confirm", "handoff", "greeting", "other"]
Scope = Literal["bl_collection", "seller_unavailable_handoff", "oncall_astbuy", "refusal", "unclear"]
Variant = Literal["main", "enrich", "redial", "redial_ast", "unknown"]
Verdict = Literal["Pass", "Fatal", "NonFatal", "NeedsReview"]
Basis = Literal["observed", "spoken_proxy", "unavailable"]


class Turn(BaseModel):
    idx: int
    speaker: Speaker
    start_sec: float
    end_sec: float
    text: str
    # Audio-only signals from transcription (None = not available, e.g. older transcripts)
    tone: Literal["calm", "irritated", "confused", "unknown"] | None = None  # user turns
    overlap: bool | None = None  # this turn started while the other side was still speaking
    audio_unclear: bool | None = None  # audio too noisy or faint to transcribe with confidence
    bot_misheard: bool | None = None  # bot turns: the reply shows the bot misunderstood the user's last turn


class Silence(BaseModel):
    start_sec: float
    end_sec: float

    @property
    def duration(self) -> float:
        return self.end_sec - self.start_sec


class CallRecord(BaseModel):
    """Output of INGEST."""

    call_id: str
    audio_path: str | None = None
    duration_sec: float
    language: str = "hinglish"
    turns: list[Turn]
    silences: list[Silence] = Field(default_factory=list)


class BotTurnLabel(BaseModel):
    idx: int
    slot: Slot
    options_offered: list[str] = Field(default_factory=list)
    fits_confirmed_product: bool | None = None  # None = not a spec question. Question and options together.
    question_fits_product: bool | None = None  # spec turns only: the question alone, judged without its options
    spec_name: str | None = None  # the ISQ being asked, e.g. "brand" (spec turns only)


class UserTurnLabel(BaseModel):
    idx: int
    frustrated: float = 0.0  # probability 0..1
    asks_for_human: float = 0.0
    gave_value_for: Slot | None = None  # slot this answer fills, if any


class Ledger(BaseModel):
    """What the buyer asked for vs what the bot captured, extracted by an LLM (AUDIT step 1)."""

    call_id: str
    scope: Scope
    variant: Variant = "unknown"
    initial_product: str | None = None
    final_product: str | None = None
    product_changed_at_turn: int | None = None
    product_match: bool | None = None  # bot's final confirmed product == product buyer finally wants
    buyer_facts: dict[str, str] = Field(default_factory=dict)  # slot -> value buyer stated
    bot_confirmed: dict[str, str] = Field(default_factory=dict)  # slot -> value bot read back ("shadow BL")
    bot_turns: list[BotTurnLabel] = Field(default_factory=list)
    user_turns: list[UserTurnLabel] = Field(default_factory=list)
    model: str = ""


class Evidence(BaseModel):
    turn_idx: int | None = None
    start_sec: float | None = None
    end_sec: float | None = None
    quote: str | None = None


class Finding(BaseModel):
    """One rubric parameter checked on one call."""

    parameter: str  # e.g. "looping", "dead_air", "category_isq", "qty_probing"
    status: Literal["Pass", "Fatal", "NonFatal", "NotApplicable", "Unknown"]
    reason: str
    basis: Basis
    evidence: list[Evidence] = Field(default_factory=list)
    onset_turn: int | None = None  # turn where the fault started (not just context)


RiskFlag = Literal["wrong_requirement", "frustration", "loops", "low_confidence", "human_request"]


class AuditRecord(BaseModel):
    """Output of AUDIT."""

    call_id: str
    verdict: Verdict
    scope: Scope
    findings: list[Finding]
    flags: list[RiskFlag] = Field(default_factory=list)
    first_fault_turn: int | None = None
    review_reason: str | None = None


class Cause(BaseModel):
    """Output of ISSUE: one root cause with its member calls."""

    cause_id: str
    name: str
    description: str
    type: Literal["prompt", "asr", "knowledge_gap", "flow_logic", "channel"]  # channel: bad audio, never patched
    parameter: str
    # Calls where this parameter is the call's root finding (#71). The counts, support and rank use only these.
    fatal_calls: list[str] = Field(default_factory=list)
    nonfatal_calls: list[str] = Field(default_factory=list)
    symptom_calls: list[str] = Field(default_factory=list)  # parameter failed here, but an earlier finding is the root
    suspected_type: str = ""  # tool (product_change) or data (category_isq): informational, needs traces to confirm
    fixability: float = 1.0
    priority: float = 0.0
    rank_score: float = 0.0  # wilson_low * mean_severity * fixability; used for sorting
    freq_ci_low: float = 0.0
    freq_ci_high: float = 0.0
    # ranked = a confirmed pattern. Below MIN_SUPPORT: watch (has a Fatal) or isolated. The rest is set by
    # issue.classify_causes: single_category (spans < 2 known categories), outlier (the OLD replay did not
    # reproduce it), pattern_unconfirmed (a pattern so far, no gate row yet).
    status: Literal["ranked", "isolated", "watch", "outlier", "single_category", "pattern_unconfirmed"] = "ranked"
    # Owner and evidence (#90). No trace fields exist yet, so issue.build_causes never claims proof: a cause
    # needs traces (per-turn speech-to-text, tool calls, call-start values) for that. None = an old causes.json
    # file before #90; the dashboard hides the chips and fix.route falls back to the suspected_type rule.
    owner: Literal["speech", "data", "tool", "prompt", "runtime", "unresolved"] | None = None
    owner_evidence: Literal["proof", "likely", "missing"] | None = None
    suspect: Literal["data", "tool"] | None = None  # for owner unresolved: which system needs the trace hunt
    owner_note: str | None = None
