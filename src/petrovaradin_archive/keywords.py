from __future__ import annotations

def contains_keyword(text: str, keywords: list[str]) -> bool:
    """Match a search root, including its grammatical derivatives."""
    folded = text.casefold()
    return any(keyword.casefold() in folded for keyword in keywords)


def detect_locations(text: str, candidate_keywords: list[str]) -> str | None:
    """Tag already-matched Petrovaradin content with the sub-locations it mentions.

    This only categorizes text that already passed the main keyword match — it does
    not expand discovery to new search roots. See config/settings.yaml candidate_keywords.
    """
    folded = text.casefold()
    hits = [keyword for keyword in candidate_keywords if keyword.casefold() in folded]
    return ", ".join(hits) if hits else None
