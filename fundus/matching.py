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


def excluded(words: list[str], title: str | None) -> bool:
    """Steht ein Ausschlusswort im Titel? Nur Wortanfänge zählen: «dell» trifft «Dell XPS», nicht «Modell»;
    «defekt» trifft auch «defekter». Bewusst nur der Titel – in Beschreibungen steht oft «keine Defekte»."""
    text = (title or "").lower()
    return any(re.search(r"(?<!\w)" + re.escape(w.lower()), text) for w in words if w)


_NUMBERED = re.compile(r"^([^\W\d_]*)(\d+)$")       # Modell: «m1», «16», «4»
_SIZED = re.compile(r"^(\d+)([^\W\d_]+)$")           # Grösse mit Einheit: «9m», «16gb», «512gb»


def _numbered(text: str | None) -> list[tuple[str, str, str]]:
    """Modellnummern und Grössen im Text als (Art, Buchstaben, Ziffern):
    «M2» → ("modell", "m", "2"), «14"» → ("modell", "", "14"), «12m» → ("grösse", "m", "12")."""
    out = []
    for w in re.findall(r"\w+", (text or "").lower()):
        if m := _NUMBERED.match(w):
            out.append(("modell", m.group(1), m.group(2)))
        elif m := _SIZED.match(w):
            out.append(("grösse", m.group(2), m.group(1)))
    return out


def _conflicts(word: str, text: str) -> bool:
    """Nennt der Titel statt des Gesuchten eine andere Nummer derselben Art?
    «m1» gesucht, «M2» im Titel → ja; «16» gesucht, «14"» → ja; «9m» gesucht, «12m» → ja; «16gb» gesucht, «8GB» → ja.
    «2021» (andere Stellenzahl) oder «16GB» bei gesuchtem «16» zählen nicht."""
    found = _numbered(word)
    if not found:
        return False
    kind, letters, digits = found[0]
    return any(k == kind and l == letters and d != digits and (kind == "grösse" or len(d) == len(digits))
               for k, l, d in _numbered(text))


def matches_query(query: str, text: str) -> bool:
    """Passt der Titel zur Suche? Der Vorfilter soll nur klar Falsches aussortieren – im Zweifel entscheidet
    die KI mit der Beschreibung.
    - Nennt der Titel eine andere Modellnummer (M2 statt M1, 14 statt 16, «Pro 3» statt «Pro 4»): nein.
    - Fehlt eine Modellnummer nur («MacBook Pro 16 inch 2021» bei «MacBook Pro 16 M1»): ja, wenn alle übrigen
      Suchwörter dastehen – viele schreiben das Jahr statt des Chips.
    - Sonst dürfen Wörter ohne Ziffer fehlen (oft die Marke): eines bei 3 Wörtern, bis zur Hälfte ab 4."""
    have = tokens(text) | set(re.findall(r"\w+", (text or "").lower()))
    words = re.findall(r"\w+", (query or "").lower())
    missing = [w for w in words if w not in have and not tokens(w) <= have]
    if any(_conflicts(w, text) for w in missing):
        return False
    with_digits = [w for w in missing if any(ch.isdigit() for ch in w)]
    plain = [w for w in missing if w not in with_digits]
    if with_digits:
        return not plain
    allowed = len(words) // 2 if len(words) >= 4 else (1 if len(words) == 3 else 0)
    return len(plain) <= allowed
