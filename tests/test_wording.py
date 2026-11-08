"""What reaches a page is written for a person: no provider ids, no record ids, no state codes, times as people read them."""
from __future__ import annotations

from datetime import datetime

from desk.wording import plain, when, words


def test_a_provider_id_in_brackets_or_bare_is_taken_out():
    assert plain("$240.00 on Nov 8, 2026 (payment_ofK0Ux1ENYC). An FMS task asks a person to act.") == "$240.00 on Nov 8, 2026. An FMS task asks a person to act."
    assert plain("The marketplace still shows listing_x7Hq2abcdef for this sale.") == "The marketplace still shows for this sale."


def test_a_record_id_is_taken_out_and_a_state_code_is_written_as_words():
    assert plain("Payments workitem:4 and payment:12 say the attempt is NEEDS_STAFF.") == "Payments and say the attempt is needs staff."
    assert words("NEEDS_STAFF") == "needs staff" and words("counsel_review_required") == "counsel review required" and words(None) == ""


def test_a_time_a_feed_wrote_is_written_as_a_person_reads_it():
    assert plain("its feed, at 2026-11-08T17:53:15.573390). Cancelling") == "its feed, at Nov 8, 2026, 12:53 PM). Cancelling"
    assert plain("its feed, at 2026-11-08T17:53:15Z", zone="America/Chicago") == "its feed, at Nov 8, 2026, 11:53 AM"
    assert plain("at 2026-13-45T99:00:00") == "at 2026-13-45T99:00:00"           # not a time: left as it is


def test_ordinary_words_and_amounts_are_left_alone():
    said = "Unit B-1042. The FMS ledger shows $0.00 owed. A STOP from this number is in effect until START."
    assert plain(said) == said and plain(None) is None


def test_the_brackets_a_removed_id_leaves_behind_go_with_it():
    assert plain("a $240.00 payment succeeded on Nov 8, 2026 (payment_ofK0Ux1ENYC), but the attempt (payment:1) is unconfirmed") == "a $240.00 payment succeeded on Nov 8, 2026, but the attempt is unconfirmed"


def test_the_status_of_a_sale_or_a_listing_is_written_as_words():
    text = "It closes when: auctions/search shows this auction and lease CANCELLED. The site's feed shows LISTED, then WITHDRAWN."
    assert plain(text) == "It closes when: auctions/search shows this auction and lease cancelled. The site's feed shows listed, then withdrawn."
    assert plain("Cancelled and Listed are left alone") == "Cancelled and Listed are left alone"       # only the code is a code


def test_a_time_the_desk_keeps_in_utc_is_read_on_the_facility_clock():
    at = datetime(2026, 11, 8, 23, 18)
    assert when(at) == "Nov 8, 2026, 6:18 PM" and when(at, "America/Chicago") == "Nov 8, 2026, 5:18 PM"
    assert when(datetime(2026, 11, 9, 5, 5), "America/New_York") == "Nov 9, 2026, 12:05 AM" and when(None) == "an unknown time"
    assert "UTC" not in when(at)
