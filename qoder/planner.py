"""Turn PowerContext memory into a review plan."""

from __future__ import annotations

import re

TOPIC_PREFIX = re.compile(r"^\[(?P<topic>[^\]]+)\]")
WEAK_ONLY_MARK = "只复习薄弱点"


def weakness_counts(entries: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.get("kind") != "weakness":
            continue
        match = TOPIC_PREFIX.match(entry.get("text", ""))
        if match:
            topic = match.group("topic")
            counts[topic] = counts.get(topic, 0) + 1
    return counts


def prefers_weak_only(entries: list[dict]) -> bool:
    for entry in reversed(entries):
        if entry.get("kind") == "preference" and WEAK_ONLY_MARK in entry.get("text", ""):
            return True
    return False


def build_plan(
    questions: list[dict],
    entries: list[dict],
    weak_only: bool,
    size: int = 6,
) -> tuple[list[dict], dict[str, int]]:
    counts = weakness_counts(entries)
    ranked: list[tuple[float, dict]] = []
    for question in questions:
        weak_hits = counts.get(question["topic"], 0)
        if weak_only and weak_hits == 0:
            continue
        score = float(question.get("difficulty", 1)) + 2.0 * weak_hits
        ranked.append((score, question))
    if not ranked:
        ranked = [(float(q.get("difficulty", 1)), q) for q in questions]
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [question for _, question in ranked[:size]], counts