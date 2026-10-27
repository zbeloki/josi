"""Per-invocation OpenCode usage capture and cost derivation.

actl records how many tokens each OpenCode invocation used and what it cost,
so a task's usage can accumulate across worker attempts, reviews, and
review-format retries, and across CLI invocations. The raw numbers come from
OpenCode's own session metadata (``GET /api/session/<id>``: the session's
cumulative ``cost`` and ``tokens``); actl turns a session's cumulative totals
into one invocation's usage by subtracting the totals read before the turn.

Costs are used exactly as OpenCode reports them when available. When OpenCode
reports tokens but no cost, actl derives the input and output costs from the
model's pricing metadata (``GET /api/model``). When neither is available the
amount is recorded as unavailable rather than as zero, so a missing value can
never be mistaken for a free invocation. A usage record keeps the raw token
counts plus the available cost data and flags whether the cost is complete.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

# Invocation stages actl attributes usage to.
STAGE_TEST_WRITER = "test_writer"
STAGE_WORKER = "worker"
STAGE_REVIEW = "review"
STAGE_REVIEW_FORMAT_RETRY = "review_format_retry"

# Usage availability, stored with every usage record.
USAGE_AVAILABLE = "available"
USAGE_UNAVAILABLE = "unavailable"

# Where the total cost came from, when it is known at all.
COST_REPORTED = "reported"
COST_DERIVED = "derived"

# Accumulated-cost states shown by ``actl task show`` and ``actl run show``.
# ``unavailable`` means no usage was recorded at all, ``incomplete`` means
# some amount is missing, and ``complete`` means every amount is known.
TOTALS_COMPLETE = "complete"
TOTALS_INCOMPLETE = "incomplete"
TOTALS_UNAVAILABLE = "unavailable"

_MILLION = 1_000_000.0


@dataclass(frozen=True)
class Pricing:
    """A model's price in USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class Usage:
    """One invocation's token counts and available cost data.

    ``None`` means the amount is unavailable (OpenCode or the model pricing
    did not provide it); it is never treated as zero. ``available`` is False
    when no usage could be read at all, in which case every other field is
    None as well.
    """

    available: bool = False
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    cost_total: Optional[float] = None
    cost_input: Optional[float] = None
    cost_output: Optional[float] = None
    cost_source: Optional[str] = None

    @property
    def cost_complete(self) -> bool:
        """Whether total, input, and output costs are all known."""
        return (
            self.available
            and self.cost_total is not None
            and self.cost_input is not None
            and self.cost_output is not None
        )


def unavailable() -> Usage:
    """Return a usage value that records no data at all."""
    return Usage(available=False)


def parse_session(data) -> Usage:
    """Read the cumulative usage out of an OpenCode session payload.

    ``data`` is the ``data`` object of ``GET /api/session/<id>``. A missing
    ``tokens`` object and a missing ``cost`` mean the usage is unavailable;
    a present but zero value is real usage and is kept as zero.
    """
    if not isinstance(data, dict):
        return unavailable()
    tokens = data.get("tokens")
    cost = data.get("cost")
    if not isinstance(tokens, dict) and cost is None:
        return unavailable()
    cache = tokens.get("cache") if isinstance(tokens, dict) else None
    if not isinstance(cache, dict):
        cache = {}
    return Usage(
        available=True,
        input_tokens=_token(tokens, "input"),
        output_tokens=_token(tokens, "output"),
        reasoning_tokens=_token(tokens, "reasoning"),
        cache_read_tokens=_as_int(cache.get("read")),
        cache_write_tokens=_as_int(cache.get("write")),
        cost_total=_as_float(cost),
    )


def subtract(after: Usage, before: Optional[Usage]) -> Usage:
    """Return the usage one turn added to a session's cumulative totals.

    ``before`` is the cumulative usage read before the turn; ``None`` means
    the session was fresh, so ``after`` is already the turn's usage. When
    either side is unavailable the difference cannot be known and is marked
    unavailable instead of being guessed.
    """
    if not after.available:
        return unavailable()
    if before is None:
        return after
    if not before.available:
        return unavailable()
    return Usage(
        available=True,
        input_tokens=_difference(after.input_tokens, before.input_tokens),
        output_tokens=_difference(after.output_tokens, before.output_tokens),
        reasoning_tokens=_difference(
            after.reasoning_tokens, before.reasoning_tokens
        ),
        cache_read_tokens=_difference(
            after.cache_read_tokens, before.cache_read_tokens
        ),
        cache_write_tokens=_difference(
            after.cache_write_tokens, before.cache_write_tokens
        ),
        cost_total=_difference(after.cost_total, before.cost_total),
    )


def pricing_from_models(model: str, models) -> Optional[Pricing]:
    """Return the pricing for ``model`` from OpenCode's model list, or None.

    ``model`` is actl's ``provider/model`` (optionally ``#variant``) form.
    The variant never changes a model's price, so it is ignored here. A model
    whose pricing metadata is missing or incomplete yields None rather than a
    partial price.
    """
    provider, name, _variant = parse_model_ref(model)
    if not provider or not name:
        return None
    for info in models or ():
        if not isinstance(info, dict):
            continue
        if info.get("providerID") != provider or info.get("modelID") != name:
            continue
        return _pricing_from_cost(info.get("cost"))
    return None


def parse_model_ref(model: str):
    """Split ``provider/model#variant`` into its three parts."""
    reference, _, variant = model.partition("#")
    provider, _, name = reference.partition("/")
    return provider, name, variant


def derive(usage: Usage, pricing: Optional[Pricing]) -> Usage:
    """Fill in input/output costs from ``pricing`` when possible.

    The total cost is OpenCode's reported value when it exists; otherwise it
    is the sum of the derived input and output costs. Missing pieces stay
    ``None`` so callers can tell an unknown amount from a zero one.
    """
    if not usage.available:
        return usage
    cost_input = cost_output = None
    if pricing is not None:
        cost_input = _input_cost(usage, pricing)
        cost_output = _output_cost(usage, pricing)
    total = usage.cost_total
    source = COST_REPORTED if total is not None else None
    if total is None and cost_input is not None and cost_output is not None:
        total = cost_input + cost_output
        source = COST_DERIVED
    return replace(
        usage,
        cost_total=total,
        cost_input=cost_input,
        cost_output=cost_output,
        cost_source=source,
    )


def format_lines(usage: Usage) -> list:
    """Return the log lines that record one invocation's usage.

    Token counts are written here, to the invocation's own log, and never to
    the terminal. Missing values are spelled ``unavailable`` so a gap is
    visible in the log instead of looking like a zero.
    """
    if not usage.available:
        return ["usage_status: " + USAGE_UNAVAILABLE]
    lines = [
        "usage: input=" + _number(usage.input_tokens)
        + " output=" + _number(usage.output_tokens)
        + " reasoning=" + _number(usage.reasoning_tokens)
        + " cache_read=" + _number(usage.cache_read_tokens)
        + " cache_write=" + _number(usage.cache_write_tokens),
    ]
    if (
        usage.cost_total is None
        and usage.cost_input is None
        and usage.cost_output is None
    ):
        lines.append("cost: unavailable")
    else:
        lines.append(
            "cost: total=" + _cost(usage.cost_total)
            + " input=" + _cost(usage.cost_input)
            + " output=" + _cost(usage.cost_output)
            + " source=" + (usage.cost_source or "unavailable")
            + " complete=" + ("yes" if usage.cost_complete else "no")
        )
    lines.append("usage_status: " + USAGE_AVAILABLE)
    return lines


def format_totals(totals) -> list:
    """Return the cost summary lines for accumulated usage totals.

    ``totals`` is the mapping returned by :func:`actl.tasks.usage_totals`.
    Only costs are shown here; per-invocation token counts stay in the logs.
    A total that no record carried is spelled ``unavailable``, and a total
    known only in part is flagged ``incomplete``, so a missing amount is
    never presented as a confirmed zero.
    """
    return [
        "cost_total: " + _cost(totals.get("total")),
        "cost_input: " + _cost(totals.get("input")),
        "cost_output: " + _cost(totals.get("output")),
        "cost_status: " + _totals_status(totals),
    ]


def _totals_status(totals) -> str:
    """Classify accumulated totals as complete, incomplete, or unavailable.

    ``complete`` means every amount is known. When no amount at all is known -
    no records, or records whose costs were all unavailable - the status is
    ``unavailable``; a partially known total is ``incomplete``. A missing
    amount therefore never reads as a confirmed zero.
    """
    if totals.get("complete"):
        return TOTALS_COMPLETE
    if all(totals.get(name) is None for name in ("total", "input", "output")):
        return TOTALS_UNAVAILABLE
    return TOTALS_INCOMPLETE


def _pricing_from_cost(cost) -> Optional[Pricing]:
    """Return a :class:`Pricing` from one model's ``cost`` array, or None."""
    if not isinstance(cost, list):
        return None
    entry = None
    for item in cost:
        # A tiered entry applies only above a context size; the untiered
        # entry is the model's base price and the right default here.
        if isinstance(item, dict) and "tier" not in item:
            entry = item
            break
    if entry is None and cost and isinstance(cost[0], dict):
        entry = cost[0]
    if entry is None:
        return None
    cache = entry.get("cache")
    if not isinstance(cache, dict):
        return None
    prices = (
        _as_float(entry.get("input")),
        _as_float(entry.get("output")),
        _as_float(cache.get("read")),
        _as_float(cache.get("write")),
    )
    if any(price is None for price in prices):
        return None
    return Pricing(*prices)


def _input_cost(usage: Usage, pricing: Pricing) -> Optional[float]:
    if usage.input_tokens is None:
        return None
    cache_read = usage.cache_read_tokens or 0
    cache_write = usage.cache_write_tokens or 0
    return (
        usage.input_tokens * pricing.input
        + cache_read * pricing.cache_read
        + cache_write * pricing.cache_write
    ) / _MILLION


def _output_cost(usage: Usage, pricing: Pricing) -> Optional[float]:
    if usage.output_tokens is None:
        return None
    reasoning = usage.reasoning_tokens or 0
    return (usage.output_tokens + reasoning) * pricing.output / _MILLION


def _token(tokens, name) -> Optional[int]:
    return _as_int(tokens.get(name)) if isinstance(tokens, dict) else None


def _difference(after, before):
    if after is None or before is None:
        return None
    return after - before


def _as_int(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _as_float(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _number(value) -> str:
    return "unavailable" if value is None else str(value)


def _cost(value) -> str:
    return "unavailable" if value is None else "%.6f" % value
