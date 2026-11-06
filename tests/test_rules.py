"""Published lien-law data is kept exactly as published."""
from __future__ import annotations

import csv

import pytest

from desk.rules import STITCH_CSV, import_stitch, reference_row


def test_the_dataset_is_stored_once_and_verbatim(session):
    assert import_stitch(session) == 51
    assert import_stitch(session) == 0   # the same file again adds nothing
    published = next(r for r in csv.DictReader(open(STITCH_CSV, encoding="utf-8")) if r["state_code"] == "FL")
    stored = reference_row(session, "FL")
    assert stored.row == published
    assert stored.row["statute_read_on"] == "2026-10-18" and "CC BY 4.0" in stored.attribution


def test_a_published_row_cannot_be_edited(session):
    import_stitch(session)
    row = reference_row(session, "NY")
    row.row = {**row.row, "earliest_sale": "Day 1"}
    with pytest.raises(ValueError, match="never edited"):
        session.commit()
