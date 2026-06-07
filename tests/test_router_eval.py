"""Opt-in router accuracy eval (M6-style live smoke, but scored).

Unlike the rest of the suite, this calls the **real** Azure OpenAI router on a
labeled set of utterances and scores intent + slot extraction. It's marked `eval`
and deselected by default (see pyproject `addopts`), so the normal `pytest` loop
stays fast and offline. Run it with:

    pytest -m eval -s          # -s to see the scorecard

It skips cleanly when Azure credentials aren't configured.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

_CASES_FILE = Path(__file__).parent / "router_eval_cases.jsonl"
CASES = [
    json.loads(line)
    for line in _CASES_FILE.read_text(encoding="utf-8").splitlines()
    if line.strip()
]

# Gate: the model must classify intent at least this well to pass.
INTENT_THRESHOLD = 0.90

# Slots compared as free text (lenient); the rest are compared exactly.
_TEXT_SLOTS = {"route", "location", "direction_hint"}


@pytest.fixture(scope="module")
def live_router():
    try:
        from openai import AsyncAzureOpenAI

        from dexter.agent.router import Router
        from dexter.config import get_settings

        settings = get_settings()
        client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )
        return Router(client, settings.azure_openai_deployment_router)
    except Exception as exc:  # noqa: BLE001 - any config/credential gap -> skip, don't fail
        pytest.skip(f"router eval needs Azure config: {exc}")


def _norm(value) -> str:
    return " ".join(str(value).lower().split()) if value is not None else ""


def _slot_ok(key: str, expected, actual) -> bool:
    if key in _TEXT_SLOTS:
        e, a = _norm(expected), _norm(actual)
        return bool(e) and bool(a) and (e in a or a in e)
    return expected == actual  # follow_up / offset: exact


async def test_router_intent_and_slot_accuracy(live_router):
    runs = [
        (case, await live_router.route(case["utterance"], history=case.get("history")))
        for case in CASES
    ]

    intent_hits = 0
    slot_total = 0
    slot_hits = 0
    confusion: Counter[tuple[str, str]] = Counter()
    misses: list[str] = []

    for case, slots in runs:
        expected = case["expected"]
        utterance = case["utterance"]
        if slots.intent == expected["intent"]:
            intent_hits += 1
        else:
            confusion[(expected["intent"], slots.intent)] += 1
            misses.append(f"  intent {expected['intent']}->{slots.intent}: {utterance!r}")
        for key, want in expected.items():
            if key == "intent":
                continue
            slot_total += 1
            got = getattr(slots, key, None)
            if _slot_ok(key, want, got):
                slot_hits += 1
            else:
                misses.append(f"  slot[{key}] want {want!r} got {got!r}: {utterance!r}")
        # `forbid`: slots that must NOT be carried over (e.g. a stop from another route).
        for key in case.get("forbid", []):
            slot_total += 1
            got = getattr(slots, key, None)
            if not got:
                slot_hits += 1
            else:
                misses.append(f"  forbid[{key}] got {got!r} (should be empty): {utterance!r}")

    intent_acc = intent_hits / len(runs)
    slot_acc = slot_hits / slot_total if slot_total else 1.0

    scorecard = [
        "",
        f"Router eval: {len(runs)} cases",
        f"  intent accuracy: {intent_acc:.0%} ({intent_hits}/{len(runs)})",
        f"  slot accuracy:   {slot_acc:.0%} ({slot_hits}/{slot_total})",
    ]
    if confusion:
        scorecard.append("  intent confusions (expected -> got):")
        scorecard += [f"    {exp} -> {got}: {n}" for (exp, got), n in confusion.most_common()]
    if misses:
        scorecard.append("  misses:")
        scorecard += misses
    report = "\n".join(scorecard)
    print(report)

    assert intent_acc >= INTENT_THRESHOLD, (
        f"intent accuracy {intent_acc:.0%} below {INTENT_THRESHOLD:.0%}\n{report}"
    )
