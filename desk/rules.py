"""Published lien-law reference data, imported as published.

The Stitch Storage dataset summarizes each state's self-storage lien statute. It is useful for orientation and for
whoever writes a reviewed rule version, and it is not itself a rule: `statute_read_on` is the day its authors read
the statute, not an effective date, and `earliest_sale` is their modeled summary, not a deadline."""
from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import RuleSourceRow

STITCH_CSV = Path(__file__).parent / "reference" / "stitch_lien_laws.csv"
STITCH_SOURCE = "stitch-storage-lien-laws"
STITCH_ATTRIBUTION = ("Stitch Storage, Self-Storage Lien Laws dataset, https://stitchstorage.com/self-storage-lien-laws/data, "
                      "licensed CC BY 4.0. Stored as published.")


@event.listens_for(RuleSourceRow, "before_update")
def _published_rows_are_never_edited(mapper, connection, target):
    raise ValueError("published source rows are never edited; import a new file instead")


def import_stitch(session: Session, path: Path = STITCH_CSV) -> int:
    """Store every row of this exact file once. Returns how many rows were added (0 if the file was imported before)."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if session.scalar(select(RuleSourceRow.id).where(RuleSourceRow.file_sha256 == digest).limit(1)) is not None:
        return 0
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    session.add_all(RuleSourceRow(source=STITCH_SOURCE, file_sha256=digest, jurisdiction=r["state_code"], row=r,
                                  attribution=STITCH_ATTRIBUTION) for r in rows)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()   # another process imported the same file at the same moment
        return 0
    return len(rows)


def reference_row(session: Session, jurisdiction: str) -> RuleSourceRow | None:
    """The most recently imported published row for a state."""
    return session.scalar(select(RuleSourceRow).where(RuleSourceRow.jurisdiction == jurisdiction)
                          .order_by(RuleSourceRow.imported_at.desc(), RuleSourceRow.id.desc()).limit(1))
