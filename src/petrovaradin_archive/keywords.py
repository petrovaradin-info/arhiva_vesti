from __future__ import annotations

def contains_keyword(text: str, keywords: list[str]) -> bool:
    """Match a search root, including its grammatical derivatives."""
    folded = text.casefold()
    return any(keyword.casefold() in folded for keyword in keywords)
