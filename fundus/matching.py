"""Passt ein Inserat-Titel zur Suche? Günstiger Vorfilter vor der KI.

Geprüft an echten Daten (Okt. 2026): behält 130 von 133 relevanten Treffern und entfernt
71 % der Fehltreffer, bevor sie gespeichert, gemeldet oder von Gemini bewertet werden.
"""

from __future__ import annotations

import re
import unicodedata

# «Sensor4» und «4S» wie «Sensor 4» bzw. «4 S» behandeln
_LETTER_DIGIT = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")


# Technische Angaben: «16GB» oder «16 GB» ist Arbeitsspeicher, keine Modellgrösse «16». Ohne das erfüllte ein
# MacBook Pro 14" mit 16 GB RAM das Suchwort «16» (Okt./Nov. 2026: 14-Zoll-Geräte in der 16-Zoll-Suche).
_SPEC_UNIT = r"(?:gb|tb|mb|go|to|ghz|mhz|hz|mah|wh|w|v|core|cores|kern)"
_SPEC = re.compile(rf"^\d+{_SPEC_UNIT}$")
_SPEC_PAIR = re.compile(rf"(?<![\w.,])(\d+)[ \u00a0-]?({_SPEC_UNIT})(?!\w)")


_SPEC_SLASH = re.compile(rf"(?<![\w.,])(\d+) ?/ ?(\d+) ?({_SPEC_UNIT})(?!\w)")   # «16/512 GB» = 16 GB und 512 GB
_STORAGE_FROM = 100   # GB: darunter Arbeitsspeicher, ab hier Massenspeicher – nicht miteinander vergleichen


def fold(text: str | None) -> str:
    """Klein und ohne Akzente: «POÄNG» = «poang», «Fauteuil à bascule» = «fauteuil a bascule». Verkäufer schreiben
    Produktnamen oft ohne Umlaut – sonst fehlte «poäng» im Titel «Poang Schaukelstuhl»."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", (text or "").lower()) if not unicodedata.combining(ch))


def words(text: str | None) -> list[str]:
    """Wörter des Texts, klein und ohne Akzente; «16 GB» und «16-Core» als ein Wort («16gb», «16core»)."""
    text = _SPEC_SLASH.sub(r"\1\3 \2\3", fold(text))
    return re.findall(r"\w+", _SPEC_PAIR.sub(r"\1\2", text))


def tokens(text: str | None) -> set[str]:
    out: set[str] = set()
    for w in words(text):
        if _SPEC.match(w):
            out.add(w)
        else:
            out.update(re.findall(r"\w+", _LETTER_DIGIT.sub(" ", w)))
    return out


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
    for w in words(text):
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
    def comparable(d: str) -> bool:
        if kind == "modell":
            return len(d) == len(digits)
        if letters in ("gb", "go"):  # «256 GB» (SSD) widerspricht «16GB» (RAM) nicht
            return (int(d) >= _STORAGE_FROM) == (int(digits) >= _STORAGE_FROM)
        return True
    return any(k == kind and l == letters and d != digits and comparable(d) for k, l, d in _numbered(text))


def matches_query(query: str, text: str) -> bool:
    """Passt der Titel zur Suche? Der Vorfilter soll nur klar Falsches aussortieren – im Zweifel entscheidet
    die KI mit der Beschreibung.
    - Nennt der Titel eine andere Modellnummer (M2 statt M1, 14 statt 16, «Pro 3» statt «Pro 4»): nein.
    - Fehlt eine Modellnummer nur («MacBook Pro 16 inch 2021» bei «MacBook Pro 16 M1»): ja, wenn alle übrigen
      Suchwörter dastehen – viele schreiben das Jahr statt des Chips.
    - Sonst dürfen Wörter ohne Ziffer fehlen (oft die Marke): eines bei 3 Wörtern, bis zur Hälfte ab 4."""
    have = tokens(text) | set(words(text))
    wanted = words(query)
    joined = {w for a, b in zip(wanted, wanted[1:]) if a + b in have for w in (a, b)}   # «mac book» = «MacBook»
    missing = [w for w in wanted if w not in have and w not in joined and not tokens(w) <= have]
    if any(_conflicts(w, text) for w in missing):
        return False
    with_digits = [w for w in missing if any(ch.isdigit() for ch in w)]
    plain = [w for w in missing if w not in with_digits]
    if with_digits:
        return not plain
    allowed = len(wanted) // 2 if len(wanted) >= 4 else (1 if len(wanted) == 3 else 0)
    return len(plain) <= allowed


MAX_ALTS = 2   # Alternativ-Begriffe pro Suche: jeder kostet pro Lauf eine Abfrage mehr je Plattform


def search_terms(search: dict) -> list[str]:
    """Suchtext und seine Alternativ-Begriffe (offizieller Produktname, andere Schreibweise), höchstens MAX_ALTS."""
    return [search.get("query") or ""] + list(search.get("alt_queries") or [])[:MAX_ALTS]


def matches_search(search: dict, text: str) -> bool:
    """Wie matches_query, aber ein Titel darf statt des Suchtexts auch einen Alternativ-Begriff tragen
    («Schaukelstuhl Poäng» bei der Suche «ikea schaukelstuhl» mit Alternative «Poäng Schaukelstuhl»)."""
    return any(matches_query(q, text) for q in search_terms(search) if q.strip())


def clean_alts(query: str, alts) -> list[str]:
    """Alternativen aufräumen: kurz, ohne Doppel und ohne solche, die nur Wörter des Suchtexts enthalten
    (die findet der Suchtext schon)."""
    have, base = set(words(query)), tokens(query)
    out: list[str] = []
    for a in alts or []:
        a = " ".join(str(a).replace(",", " ").split())[:40]
        w = set(words(a))
        # nur Wörter des Suchtexts → findet der Suchtext schon; alle Wörter des Suchtexts und mehr → bloss eine
        # Untervariante («Thule Urban Glide 3 Doppel»), die der Suchtext ebenfalls findet
        if w and not w <= have and not tokens(a) >= base and all(set(words(o)) != w for o in out):
            out.append(a)
    return out[:MAX_ALTS]
