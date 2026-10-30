"""Registrierte Plattformen. Neue Plattform = Adapter-Klasse schreiben und hier eintragen."""

from __future__ import annotations

from .base import Adapter, AdapterError, BlockedError, GoneError
from .ebay import EbayAdapter
from .scraped import AnibisAdapter, RicardoAdapter, TuttiAdapter
from .kleinanzeigen import KleinanzeigenAdapter
from .willhaben import WillhabenAdapter
from .markt import MarktAdapter
from .blocket import BlocketAdapter

ADAPTER_CLASSES: list[type[Adapter]] = [
    RicardoAdapter, TuttiAdapter, AnibisAdapter, EbayAdapter, KleinanzeigenAdapter, WillhabenAdapter, MarktAdapter,
    BlocketAdapter,
]


def all_adapters(cfg: dict) -> list[Adapter]:
    return [cls(cfg) for cls in ADAPTER_CLASSES]


def adapter_for_source(adapters: list[Adapter], source: str) -> Adapter:
    name = source.split(":", 1)[0]
    for a in adapters:
        if a.name == name:
            return a
    raise KeyError(source)


__all__ = ["Adapter", "AdapterError", "BlockedError", "GoneError", "all_adapters", "adapter_for_source"]
