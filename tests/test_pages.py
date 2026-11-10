"""The preview's screens: what a first-time visitor sees, and that reading them changes nothing."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from desk.adapter.cubby import CubbyAdapter
from desk.api import create_app
from desk.config import Settings
from desk.db import Base
from desk.models import DelinquencyCase, Lease

from .conftest import FULL_KEY, ORLANDO


def preview(fms_url, adapter=None, **kw):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token="op-token", staff_token="staff-token",
                        public_view=True, demo_seed=True, **kw)
    return create_app(settings, adapter or CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False)


def account_of(app, unit: str) -> int:
    """The number the desk gave a unit's account. Numbers follow the order the cases were opened, so a new seeded tenant moves them."""
    with app.state.session_factory() as s:
        return s.scalar(select(DelinquencyCase.id).join(Lease, Lease.id == DelinquencyCase.lease_id).where(Lease.unit_name == unit))


def counts(app) -> dict:
    with app.state.session_factory() as s:
        return {t.name: s.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def test_today_lists_each_open_item_with_what_proves_it_done(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/today").text
        for head in ("Payment outcome unknown", "Lien file out of date", "Payment returned", "Past due"):
            assert head in page
        assert page.index("Payment outcome unknown") < page.index("Payment returned")
        assert "A person names the FMS payment that is this attempt" in page
        assert "Synthetic technical preview" in page
        albany = visitor.get("/today", params={"facility": "fac_NSalbany001"}).text
        assert "Counsel review required" in albany and "Payment outcome unknown" not in albany
        # a facility id that is not ours is not quietly swapped for another facility
        assert visitor.get("/today", params={"facility": "fac_HBtampa0001"}).status_code == 404
        assert visitor.get("/today?facility=").status_code == 404


def test_the_lost_payment_shows_both_sides_and_offers_no_payment_button(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/payments/1").text
        assert "Outcome unknown: held for a person" in page
        assert "No usable answer" in page and "never sent again" in page
        assert "&ldquo;Yes, go ahead&rdquo;" in page or "Yes, go ahead" in page
        # the simulator's own record, saved when the world was built: one charge, applied
        assert "The simulator&#39;s own record" in page or "The simulator's own record" in page
        assert "succeeded, applied to the ledger (possible match)" in page and "did not retry it" in page
        returned = visitor.get("/payments/2").text
        assert "returned by the bank" in returned and "DIRECT_DEBIT_RETURN" not in returned and "(this attempt)" in returned and "did not retry it" not in returned
        assert "<form" not in page.split("<main>")[1] and "Mark paid" not in page


def test_the_record_keeps_each_source_in_its_own_lane(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        rows = visitor.get(f"/api/facilities/{ORLANDO}/queue").json()["rows"]
        gina = next(r for r in rows if r["lease"] == "lease_OkfrE0505")["case_id"]
        page = visitor.get(f"/accounts/{gina}").text
        for lane in ("The FMS said", "Gatehouse observed", "Gatehouse concluded", "A person decided"):
            assert lane in page
        assert "9 legal checks recorded" in page and "Payment made outside Gatehouse" in page
        lien = visitor.get(f"/accounts/{gina}/lien").text
        assert "Account changed: re-review" in lien and "Changed since the evaluation" in lien
        assert "compliant" not in lien.replace("never says a file is compliant", "")


def test_reading_every_page_writes_nothing(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        before = counts(app)
        pages = ["/", "/today", "/today?facility=fac_NSalbany001", "/reminder-check", "/reminder-check?facility=fac_NSalbany001", "/inbox", "/evaluation", "/operations"]
        for case_id in range(1, 7):
            pages += [f"/accounts/{case_id}", f"/accounts/{case_id}/lien"]
        pages += ["/payments/1", "/payments/2"]
        for path in pages:
            assert visitor.get(path).status_code in (200, 404), path
        assert counts(app) == before
        assert visitor.get("/accounts/999").status_code == 404 and visitor.get("/payments/999").status_code == 404


def test_the_screens_exist_only_in_the_preview(fms_url, sim):
    settings = Settings(database_url="sqlite:///:memory:", fms_base_url=fms_url, fms_api_key=FULL_KEY, fms_timeout_seconds=5.0,
                        reconcile_every_seconds=60, operator_token="op-token")
    with TestClient(create_app(settings, CubbyAdapter(fms_url, FULL_KEY, 5.0), start_worker=False)) as c:
        assert c.get("/").status_code == 404 and c.get("/today").status_code == 404 and c.get("/payments/1").status_code == 404


def test_a_payment_still_pending_at_the_fms_is_shown_as_in_progress(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor, TestClient(app, headers={"X-Desk-Token": "op-token"}) as desk:
        sim.behave("lease_RamnA0012", "pending_then_succeed", lag=5)
        rows = desk.get(f"/api/facilities/{ORLANDO}/queue").json()["rows"]
        case_id = next(r for r in rows if r["lease"] == "lease_RamnA0012")["case_id"]
        check = desk.post(f"/api/cases/{case_id}/identity", json={"last_name": "Raman", "phone_last4": "0119", "unit_name": "A-012"}).json()
        proposal = desk.post(f"/api/cases/{case_id}/proposals", json={"identity_check_id": check["identity_check_id"], "amount": 9500}).json()
        paid = desk.post(f"/api/cases/{case_id}/proposals/{proposal['proposal_id']}/confirm",
                         json={"confirmed_by": "J. Ortiz", "tenant_answer": "Yes"}).json()
        assert paid["state"] == "PENDING_AT_FMS"
        page = visitor.get("/today").text
        unit = page[page.index("Unit A-012") - 400: page.index("Unit A-012") + 400]
        assert "Payment in progress" in unit and "No payment or lien step in progress" not in unit


def test_a_lien_file_shows_its_state_as_of_its_evaluation_beside_the_balance_now(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor, TestClient(app, headers={"X-Desk-Token": "op-token"}) as desk:
        # the tenant pays the rest at the counter; Gatehouse sees it on its next read of the FMS
        sim.post("/sim/payments/offline", {"allocations": {"lease_OkfrE0505": 11500}})
        assert desk.post("/api/reconcile").status_code == 200
        lien = visitor.get(f"/accounts/{account_of(app, 'E-505')}/lien").text
        assert "Account changed: re-review</span><span class=\"muted small\">as of" in lien and "Owed on this account, per the FMS: <b>$0.00</b>" in lien
        page = visitor.get("/today").text
        row = page[page.index("Lien file out of date"): page.index("Lien file out of date") + 1500]
        assert "Unit E-505" in row and "As of the evaluation on" in row
        # paid in full with the sale still listed: the sale is now the most urgent thing on Today
        assert page.index("Sale still listed, nothing owed") < page.index("Lien file out of date")


def test_the_seeded_lien_file_is_not_called_stale_by_its_own_payment(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        lien = visitor.get(f"/accounts/{account_of(app, 'E-505')}/lien").text
        assert "Account changed: re-review</span><span class=\"muted small\">as of" in lien and "not reflected here until the file is evaluated again" in lien


def test_a_class_is_styled_by_one_rule_of_its_own():
    """Two screens that share a class name restyle each other. The Messages step markers and the Evaluation flow cells both used `node`,
    and the flow text was squeezed into 36 px boxes. A class gets one rule of its own; a variant is a second class or a longer selector."""
    from pathlib import Path

    css = re.sub(r"/\*.*?\*/", "", (Path(__file__).parent.parent / "desk" / "static" / "gatehouse.css").read_text(), flags=re.S)
    rules, depth, rule = [], 0, ""
    for ch in css:
        depth += (ch == "{") - (ch == "}")
        if depth == 0 and ch == "}":
            rules.append(rule)
            rule = ""
        else:
            rule += ch
    own = [sel for sel in (r.split("{", 1)[0].strip() for r in rules) if re.fullmatch(r"\.[\w-]+", sel)]
    assert len(own) > 50                                  # the stylesheet was read, not an empty string
    assert sorted({sel for sel in own if own.count(sel) > 1}) == []


EVIDENCE_PAGES = ("/payments/", "/accounts/", "/sales/", "/access/")


def test_no_page_shows_a_state_code_that_the_desk_has_a_word_for(fms_url, sim):
    """A crawl of every page the preview links to, from the overview. A code the FMS or the desk uses for a state is written as words wherever a
    person reads it. An id may stay on an evidence page, where a person compares it with the FMS; a code may not."""
    import html as H

    from desk.wording import STATE_WORDS
    with TestClient(preview(fms_url)) as visitor:
        seen, queue, hits = set(), ["/"], {}
        while queue and len(seen) < 90:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            page = visitor.get(path)
            assert page.status_code == 200, path
            body = re.sub(r"<(script|style|svg)[^>]*>.*?</\1>", " ", page.text, flags=re.S)
            for href in re.findall(r'<a [^>]*href="(/[^"#]*)"', body):
                href = H.unescape(href)
                if not href.startswith(("/static", "/api")) and not href.endswith((".mp4", ".jpg", ".png", ".webm")):
                    queue.append(href)
            text = H.unescape(re.sub(r"<[^>]+>", " ", body))
            found = sorted(code for code in STATE_WORDS if re.search(rf"\b{code}\b", text))
            # a provider id belongs on an evidence page only: the overview, Today, Messages, Evaluation and Operations are read by people, not compared
            if not path.startswith(EVIDENCE_PAGES):
                found += sorted(set(re.findall(r"\b(?:payment|auction|listing|task|lease|cust|customer)_[A-Za-z0-9]{4,}\b", text)))
            if found:
                hits[path] = found
        assert len(seen) >= 20                                       # the crawl reached the evidence pages, not only the overview
        assert hits == {}, hits


def test_the_front_page_says_where_it_fits_in_plain_words(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/").text
        assert "Where it fits" in page and "It does not replace what you run." in page
        for failure in ("A card is charged and the FMS never shows it", "A tenant paid and is still locked out", "A paid unit is still listed",
                        "A STOP that a later message would ignore"):
            assert failure in page, failure


def test_the_front_page_shows_every_built_feature_with_a_link_into_it(fms_url, sim):
    app = preview(fms_url)
    with TestClient(app) as visitor:
        page = visitor.get("/").text
        for heading in ("A payment whose answer was lost is held, not retried.", "Every fact keeps its source.",
                        "A payment that comes back reopens the account.", "A lien file goes back to review when the account has changed.",
                        "Where no rules are reviewed, the file goes to counsel.",
                        "A reminder tool should not text a tenant who asked to stop, or who is on a payment plan."):
            assert heading in page
        florida, albany = (f"/accounts/{account_of(app, unit)}/lien" for unit in ("E-505", "D-310"))
        for link in ("/today", "/payments/1", "/payments/2", florida, albany, "/reminder-check"):
            assert f'href="{link}"' in page and visitor.get(link).status_code == 200
        assert "cents" not in page   # amounts read as dollars
        # the excerpt is the check's own rows: the tenant who texted STOP, and the arrangement the FMS has not paused
        assert "<b>Must not be texted.</b> This number texted STOP" in page
        assert "<b>Not paused in the FMS.</b> The FMS has a payment arrangement" in page


def test_findings_are_written_in_dollars_and_dates_and_quote_records_as_they_are():
    from desk.lien_files import ledger_changes
    from desk.liens import dollars, on
    assert dollars(16500) == "$165.00" and on("2026-11-06") == "Nov 6, 2026" and on("2026-02-30 rent") == "2026-02-30 rent"
    then = {"bookEntries": [{"bookEntryId": "be1", "type": "CHARGE", "amount": 5000, "amountPaid": 0,
                             "details": {"description": 'Fee "5000 cents 2026-10-06" late', "accruesOn": "2026-10-06"}}]}
    now = {"bookEntries": [{"bookEntryId": "be1", "type": "CHARGE", "amount": 5000, "amountPaid": 5000,
                            "details": {"description": 'Fee "5000 cents 2026-10-06" late', "accruesOn": "2026-10-07"}}]}
    changes = ledger_changes(then, now)
    # the description is quoted from the ledger exactly; only Gatehouse's own words carry dollars and dates
    assert 'the Fee "5000 cents 2026-10-06" late charge\'s amount paid changed from $0.00 to $50.00' in changes
    assert 'the Fee "5000 cents 2026-10-06" late charge\'s due date changed from Oct 6, 2026 to Oct 7, 2026' in changes



def test_changed_since_never_pairs_two_different_findings():
    from desk.web import changed_findings
    a = {"requirement": "account unchanged since notice", "basis": "policy", "status": "review", "detail": "charge A paid", "action": "x"}
    b = {"requirement": "account unchanged since notice", "basis": "policy", "status": "review", "detail": "charge B paid", "action": "x"}
    assert changed_findings([a, b], [b, a]) == []                      # reordered, nothing changed
    c = dict(b, detail="charge B waived")
    assert changed_findings([a, b], [a, c]) == [(b, None), (None, c)]  # B's finding gone, a new one: not "B became C"
    assert changed_findings([a], []) == [(a, None)]                    # a resolved finding still shows
    assert changed_findings([], [a]) == [(None, a)]



def test_lien_page_survives_a_document_name_without_text_and_marks_corrections(fms_url, sim):
    """Evidence may name a document whose text was not kept; a corrected record is marked as replaced."""
    from desk.models import LienEvidence
    app = preview(fms_url)
    client = TestClient(app).__enter__()
    with app.state.session_factory() as s:
        e = s.scalars(select(LienEvidence).order_by(LienEvidence.id)).first()
        s.add(LienEvidence(case_id=e.case_id, kind=e.kind, facts=e.facts, document_name="scan.pdf", document_sha256=None,
                           document_text=None, entered_by="front desk (synthetic)", supersedes_id=e.id))
        s.commit()
        case_id = e.case_id
    page = client.get(f"/accounts/{case_id}/lien")
    assert page.status_code == 200
    assert "Document content not kept" in page.text and "Replaced by a later correction" in page.text
    assert "Recorded after the evaluation shown" in page.text



def test_access_shows_on_today_and_its_own_page_without_any_credential(fms_url, sim):
    """The seeded world's access work: a paid tenant still locked out (its task marked done, the item still open), a
    returned payment past the rule with no lock, and a facility with no recorded rule. No code or combination shows."""
    with TestClient(preview(fms_url)) as visitor:
        today = visitor.get("/today").text
        assert "Access not restored" in today and "Restriction not in place" in today
        assert "does not close this item" in today
        albany = visitor.get("/today", params={"facility": "fac_NSalbany001"}).text
        assert "No access rule is recorded" in albany
        front = visitor.get("/").text
        assert "Access and overlocks" in front and "Not verified" in front
        links = sorted(set(re.findall(r'href="(/access/\d+)"', today + albany + front)))
        assert links
        pages = [today, albany, front] + [visitor.get(l).text for l in links]
        for link in links:
            assert visitor.get(link).status_code == 200
        accounts = sorted(set(re.findall(r'href="(/accounts/\d+)"', " ".join(pages))))
        pages += [visitor.get(a).text for a in accounts]
        assert any("Access, as the FMS records it" in p for p in pages)
        from fmssim.seed import ACCESS_CODES, LOCKS
        secrets_ = [code for _, _, code in ACCESS_CODES] + [combo for stock in LOCKS.values() for _, combo in stock]
        for page in pages:
            for secret in secrets_:
                assert f">{secret}<" not in page and f" {secret} " not in page and f"&ldquo;{secret}" not in page
        assert visitor.get("/access/99999").status_code == 404


def test_the_overview_plays_the_whole_walkthrough_and_the_sound_is_the_visitors_to_start(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        page = visitor.get("/").text
        video = page.split('<video id="film"')[1].split(">")[0]
        assert "controls" in video and "autoplay" not in video and "muted" not in video and "loop" not in video
        assert "film/gatehouse-walkthrough.mp4" in page and 'class="button primary film-play"' in page
        moved = visitor.get("/film", follow_redirects=False)                                 # no second page to find the film on
        assert 'href="/film"' not in page and moved.status_code == 301 and moved.headers["location"] == "/"
        assert "gatehouse-teaser" not in page and "gatehouse-film" not in page


def test_the_film_files_are_there_and_small_enough_to_ship():
    from pathlib import Path
    folder = Path(__file__).parent.parent / "desk" / "static" / "film"
    sizes = {p.name: p.stat().st_size for p in folder.iterdir()}
    assert set(sizes) == {"gatehouse-walkthrough.mp4", "gatehouse-walkthrough.jpg"}
    assert sizes["gatehouse-walkthrough.mp4"] < 95_000_000 and sizes["gatehouse-walkthrough.jpg"] < 400_000      # the film is 2560x1440; GitHub refuses a file over 100 MB


def test_the_walkthrough_answers_a_byte_range_so_the_player_can_seek(fms_url, sim):
    with TestClient(preview(fms_url)) as visitor:
        got = visitor.get("/static/film/gatehouse-walkthrough.mp4", headers={"Range": "bytes=100-199"})
        assert got.status_code == 206 and len(got.content) == 100
        assert got.headers["content-range"].startswith("bytes 100-199/")


def test_operations_shows_what_the_agent_did_and_not_what_it_cost(fms_url, sim):
    """A spend figure answers no question a visitor has at that moment; the server enforces the limit and says so when it is hit."""
    with TestClient(preview(fms_url)) as visitor:
        text = visitor.get("/operations").text
        assert "Our agent today" in text and "Explorations" in text
        assert "Spent" not in text and "$0." not in text
