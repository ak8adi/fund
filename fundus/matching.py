"""Passt ein Inserat-Titel zur Suche? Günstiger Vorfilter vor der KI.

Geprüft an echten Daten (Okt. 2026): behält 130 von 133 relevanten Treffern und entfernt
71 % der Fehltreffer, bevor sie gespeichert, gemeldet oder von Gemini bewertet werden.
"""

from __future__ import annotations

import re

# «Sensor4» und «4S» wie «Sensor 4» bzw. «4 S» behandeln
_LETTER_DIGIT = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")


def tokens(text: str | None) -> set[str]:
    return set(re.findall(r"\w+", _LETTER_DIGIT.sub(" ", (text or "").lower())))


def matches_query(query: str, text: str) -> bool:
    """Alle Suchwörter müssen vorkommen. Ausnahme: bei 3+ Wörtern darf ein Wort ohne Ziffer fehlen
    (oft die Marke: «Urban Glide 3» statt «Thule Urban Glide 3»). Modellnummern müssen immer passen."""
    wanted = tokens(query)
    missing = wanted - tokens(text)
    if any(any(ch.isdigit() for ch in w) for w in missing):
        return False
    return len(missing) <= (1 if len(wanted) >= 3 else 0)
