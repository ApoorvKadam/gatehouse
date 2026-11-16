"""The hand-written graded sets (core/graded/), and how their charge references resolve against the generated year.

A charge reference names what a tenant means the way a person would: "late_fee 2026-11" (the fee for that month), "#2" for
the second posting of the same fee, "@1" for the tenant's second lease, "admin_fee", "rent 2026-10". A list means any of them."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .books import Books

GRADED = Path(__file__).with_name("graded")


def load(name: str) -> dict:
    return json.loads((GRADED / f"{name}.json").read_text())


def labels_hash() -> str:
    """The hash every report carries, so a result can be tied to the exact labels it was scored against: the rubric and the
    items of the graded sets (notes and the page's shift are left out, so rewording a note does not change it)."""
    h = hashlib.sha256((GRADED / "rubric.md").read_bytes())
    for name in ("cases", "outside"):
        h.update(json.dumps(load(name)["items"], sort_keys=True).encode())
    return h.hexdigest()[:12]


def resolve(books: Books, leases: list[str], ref: str) -> str | None:
    """One reference to one charge id on the given tenant's leases, or None."""
    parts = ref.split()
    kind, rest = parts[0], parts[1:]
    month = next((p for p in rest if p[:4].isdigit()), None)
    copy = next((int(p[1:]) for p in rest if p.startswith("#")), 1)
    lease_i = next((int(p[1:]) for p in rest if p.startswith("@")), 0)
    if lease_i >= len(leases):
        return None
    lease = leases[lease_i]
    obligation = f"{kind}:{lease}:{month}" if month else f"{kind}:{lease}"
    ids = books.obligations.get(obligation, [])
    return ids[copy - 1] if len(ids) >= copy else None


def resolve_any(books: Books, leases: list[str], ref) -> list[str]:
    refs = ref if isinstance(ref, list) else [ref]
    return [c for c in (resolve(books, leases, r) for r in refs if r) if c]
