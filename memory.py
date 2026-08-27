"""What Stark remembers between restarts.

The conversation history in brain.py is deliberately short and dies with the
process. This is the other kind of memory: a handful of facts the user has
asked Stark to keep, written to memory.json and folded into the system prompt
every turn, so recalling one costs nothing extra.

It is a small, flat list on purpose. Anything bigger wants a real store, and
anything bigger than the system prompt would be paid for on every single
request.
"""
from __future__ import annotations

import json
from datetime import date

from config import BASE_DIR

MEMORY_PATH = BASE_DIR / "memory.json"
MAX_FACTS = 60          # a hard cap, so the prompt can't grow without bound
MAX_FACT_CHARS = 200


def load() -> list[dict]:
    """Every remembered fact, oldest first. Never raises."""
    if not MEMORY_PATH.exists():
        return []
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        return [f for f in data if isinstance(f, dict) and f.get("text")]
    except Exception as exc:
        print(f"[memory] could not read memory.json: {exc}")
        return []


def _save(facts: list[dict]) -> None:
    MEMORY_PATH.write_text(
        json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def remember(fact: str) -> str:
    """Store one fact. Returns what Stark should say about it."""
    fact = " ".join(fact.split())[:MAX_FACT_CHARS]
    if not fact:
        return "There was nothing to remember, sir."

    facts = load()
    if any(f["text"].lower() == fact.lower() for f in facts):
        return "I already have that noted, sir."

    facts.append({"text": fact, "added": date.today().isoformat()})
    del facts[:-MAX_FACTS]  # keep the most recent, drop the oldest
    _save(facts)
    return "I'll remember that."


def forget(about: str) -> str:
    """Drop every fact matching `about`. Returns what Stark should say."""
    needle = " ".join(about.split()).lower()
    if not needle:
        return "I'm not sure what to forget, sir."

    facts = load()
    if needle in ("everything", "all of it", "all"):
        if not facts:
            return "There was nothing to forget, sir."
        _save([])
        return f"Forgotten - all {len(facts)} of them."

    kept = [f for f in facts if needle not in f["text"].lower()]
    dropped = len(facts) - len(kept)
    if not dropped:
        return f"I have nothing noted about {about}, sir."
    _save(kept)
    return "Forgotten." if dropped == 1 else f"Forgotten, all {dropped} of them."


def as_prompt() -> str:
    """The block appended to the system prompt, or "" when there is nothing."""
    facts = load()
    if not facts:
        return ""
    lines = "\n".join(f"- {f['text']}" for f in facts)
    return (
        "\n\nThings the user has asked you to remember. Treat them as true and "
        "use them without being asked, but don't recite them unprompted:\n"
        f"{lines}"
    )
