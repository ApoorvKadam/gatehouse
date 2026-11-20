"""Collections on the FMS pages: Today lists what waits for staff and takes an acknowledgement; a tenant's page shows the plan, each
unit's overlock and lien eligibility with the reason, exemptions and promises, and takes staff steps only through the gate, a refusal
said once; the past-due list shows who is held and why; the Rules page shows the owner's plan policy and schedule as settings with
a bounded form. Each session works on its own copy."""
import re

from tests.test_fms import record_of, seen, sign_in, site


def token(page: str) -> str:
    return re.search(r'name="token" value="([0-9a-f]{12})"', page).group(1)


def step(c, url: str, page: str, **data):
    return c.post(url.rstrip("/") + "/collections", data={"token": token(page), **data}, follow_redirects=False)


def test_today_lists_the_breached_plan_until_staff_acknowledge_it(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c, sign_in(app) as other:
        page = c.get("/today").text
        text = seen(page)
        assert "Collections waiting for staff" in text and "Rhea Mills Payment plan breached since Oct 26" in text
        assert "1 collections item waiting for staff" in seen(c.get("/home").text)
        assert "The part due Oct 22 was not paid within 3 grace days." in text
        task = re.search(r'name="task" value="([A-Z0-9]+)"', page).group(1)
        r = c.post("/collections/acknowledge", data={"task": task}, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/today#collections")
        assert c.post("/collections/acknowledge", data={"task": task}, follow_redirects=False).status_code == 303     # a second press: nothing
        after = seen(c.get("/today").text)
        assert "Payment plan breached" not in after and "Nothing in collections is waiting for staff." in after
        assert "Payment plan breached" in seen(other.get("/today").text)                                          # another copy keeps it


def test_the_breached_plan_page_shows_every_part_and_staff_reinstate_it_through_the_gate(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        url = record_of(c, "Rhea Mills")
        page = c.get(url).text
        text = seen(page)
        assert "Payment plan Breached, waiting for staff $180.00 in all, under plan policy version 1" in text
        assert "OK, $50 now and five payments after that. I can do that." in text and "agreed Oct 8 at 11:00 am" in text
        for row in ("First payment Oct 8 $50.00 Paid", "Payment 1 Oct 22 $26.00 Missed", "Payment 2 Nov 5 $26.00 Missed",
                    "Payment 3 Nov 19 $26.00 Scheduled", "Payment 5 Dec 17 $26.00 Scheduled"):
            assert row in text, row
        assert "Waiting for staff The payment plan was breached; staff review it before any step" in text
        assert "Clear Not due on the owner's schedule: 45 days after the due date, and the oldest unpaid charge is 44 days past due" in text
        plan = re.search(r'name="plan" value="([A-Z0-9]+)"', page).group(1)
        assert step(c, url, page, action="review_plan", plan=plan, outcome="reinstate", reason="").status_code == 422
        r = step(c, url, page, action="review_plan", plan=plan, outcome="reinstate", reason="She pays the missed parts on Friday")
        assert r.headers["location"] == url + "#collections"
        after = seen(c.get(url).text)
        assert "Payment plan In good standing" in after and "Held The tenant's payment plan is in good standing" in after
        assert "Reinstate the plan" not in after and "Nothing changed" not in after
        assert "Payment plan breached" not in seen(c.get("/today").text)     # the decision settled its task
        # End pressed on the same page afterwards is a decision of its own, and the gate refuses it: the plan is no longer breached
        step(c, url, page, action="review_plan", plan=plan, outcome="end", reason="Ending it instead")
        assert "Nothing changed. The plan is active; staff review a breached plan or one sent to them." in seen(c.get(url).text)


def test_ending_the_breached_plan_lets_the_overlock_go_ahead_and_clears_today(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        url = record_of(c, "Rhea Mills")
        page = c.get(url).text
        plan = re.search(r'name="plan" value="([A-Z0-9]+)"', page).group(1)
        step(c, url, page, action="review_plan", plan=plan, outcome="end", reason="She is moving out at the end of the month")
        text = seen(c.get(url).text)
        assert "Payment plan Ended" in text and "Payment 1 Oct 22 $26.00 Missed" in text
        assert "Payment 3 Nov 19 $26.00 Payment" in text                    # a part that never came due shows no state
        assert "Due" in text and "Record an overlock" in text
        assert "Payment plan breached" not in seen(c.get("/today").text)
        assert "0 collections items waiting for staff" in seen(c.get("/home").text)


def test_a_held_overlock_is_refused_once_and_a_due_one_is_recorded_and_lifted_only_after_payment(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        iris = record_of(c, "Iris Nolan")
        page = c.get(iris).text
        assert "Held The tenant's payment plan is in good standing" in seen(page) and "Record an overlock" not in seen(page)
        lease = re.search(r'<option value="([A-Z0-9]+)">Unit', page).group(1)
        step(c, iris, page, action="overlock", lease=lease)                    # tried anyway: the gate refuses it
        assert "Nothing changed. The tenant's payment plan is in good standing." in seen(c.get(iris).text)
        assert "Nothing changed" not in seen(c.get(iris).text)                 # said once
        tom = record_of(c, "Tom Becker")
        page = c.get(tom).text
        text = seen(page)
        assert "days past due; the owner's schedule says 10" in text and "Record an overlock" in text
        own = re.search(r'<option value="([A-Z0-9]+)">Unit', page).group(1)
        step(c, tom, page, action="overlock", lease=lease)                     # Iris's unit, on Tom's page
        assert "Nothing changed. No such unit on this tenant's account." in seen(c.get(tom).text)
        page = c.get(tom).text
        step(c, tom, page, action="overlock", lease=own)
        step(c, tom, page, action="overlock", lease=own)                       # a second press gets the first decision back
        page = c.get(tom).text
        text = seen(page)
        assert "Recorded Since Nov 14, by staff" in text and "Lift the overlock" in text and "Nothing changed" not in text
        step(c, tom, page, action="lift_overlock", lease=own)
        assert "Nothing changed. An overlock is lifted only after a payment the processor confirmed." in seen(c.get(tom).text)


def test_staff_grant_and_revoke_an_exemption_that_holds_the_overlock(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        tom = record_of(c, "Tom Becker")
        page = c.get(tom).text
        lease = re.search(r'<option value="([A-Z0-9]+)">Unit', page).group(1)
        step(c, tom, page, action="grant_exemption", lease=lease, scope_overlock="1", reason="In hospital until the 24th", until="2026-11-24")
        page = c.get(tom).text
        text = seen(page)
        assert "Overlock held until Nov 24" in text and "In hospital until the 24th. Granted by the office manager." in text
        assert "Held An exemption holds it until Nov 24: In hospital until the 24th" in text and "Record an overlock" not in text
        exemption = re.search(r'name="exemption" value="([A-Z0-9]+)"', page).group(1)
        step(c, tom, page, action="revoke_exemption", lease=lease, exemption=exemption)
        text = seen(c.get(tom).text)
        assert "No exemption on this tenant's units." in text and "Record an overlock" in text
        page = c.get(tom).text
        step(c, tom, page, action="grant_exemption", lease=lease, scope_overlock="1", reason="Too long", until="2027-06-01")
        assert "Nothing changed. An exemption ends within the next 90 days." in seen(c.get(tom).text)


def test_the_past_due_list_shows_who_is_held_and_why(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        page = c.get("/collections").text
        table = page.split('id="h-past"', 1)[1].split("</table>", 1)[0]
        rows = re.findall(r"(?s)<tr>(.*?)</tr>", table.split("<tbody>", 1)[1])
        assert len(rows) == 23 and all(re.search(r'class="fms-state overlock"><span class="chip [a-z]+">[A-Z]', r) for r in rows)
        olga = next(r for r in rows if "Olga Rivera" in r)
        assert "Moved out" in olga and "The lease ended Sep 30, so there is no unit to overlock; staff handle what is still owed" in olga
        text = seen(page)
        assert "Rhea Mills" in text and "Breached, waiting for staff Waiting for staff The payment plan was breached" in text
        assert "In good standing Held The tenant's payment plan is in good standing" in text
        assert "Due" in text and "days past due; the owner's schedule says 10" in text


def test_the_rules_page_shows_the_owners_plan_settings_and_adopts_a_new_version(fms_url, sim):
    app = site(fms_url)
    with sign_in(app) as c, sign_in(app) as other:
        text = seen(c.get("/rules").text)
        assert "Tenants who have rented at least 14 days and owe at most $1,500.00" in text
        assert "90 days, with at most 6 payments after the first, two weeks apart" in text and "At least $50.00, due the day the tenant agrees" in text
        assert "Overlock 10 days after the due date" in text and "Lien eligibility 45 days after the due date" in text
        assert "These are the owner's settings, not legal deadlines." in text
        r = c.post("/rules/schedule", data={"overlock_days": "0", "lien_days": "45"}, follow_redirects=False)
        assert r.headers["location"] == "/rules#in-force"
        assert "Nothing changed. Overlock must be a whole number from 1 to 90 days." in seen(c.get("/rules").text)
        c.post("/rules/schedule", data={"overlock_days": "10", "lien_days": "45"}, follow_redirects=False)
        assert "Nothing changed. That is already the schedule in force." in seen(c.get("/rules").text)
        r = c.post("/rules/schedule", data={"overlock_days": "15", "lien_days": "45"}, follow_redirects=False)
        assert r.headers["location"] == "/rules#schedule"
        text = seen(c.get("/rules").text)
        assert "Schedule version 2" in text and "Overlock 15 days after the due date" in text and "Nothing changed" not in text
        policy = {"min_tenure_days": "14", "max_debt_cents": "1500", "longest_days": "90", "most_instalments": "6", "min_upfront_cents": "60",
                  "min_instalment_cents": "25", "grace_days": "3"}
        c.post("/rules/plan-policy", data={**policy, "min_upfront_cents": "60.5"}, follow_redirects=False)
        assert "Nothing changed. Smallest first payment must be a whole number from 0 to 1,000 dollars." in seen(c.get("/rules").text)
        c.post("/rules/plan-policy", data=policy)
        text = seen(c.get("/rules").text)
        assert "Policy version 2" in text and "At least $60.00, due the day the tenant agrees" in text
        assert "Overlock 10 days after the due date" in seen(other.get("/rules").text)                          # another copy keeps its own


def test_no_record_id_or_test_wording_on_the_collections_pages(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        for url in ("/today", "/billing", "/rules", record_of(c, "Rhea Mills"), record_of(c, "Iris Nolan"), record_of(c, "Tom Becker")):
            text = seen(c.get(url).text)
            assert not re.search(r"\b(?:PL|PR|EX|PI|TK|L|T|C)\d{4}\b", text), url
            assert not re.search(r"simulat|sim_pay|test mode", text, re.I), url


def test_today_lists_every_due_overlock_and_records_one_from_the_list(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        page = c.get("/today").text
        text = seen(page)
        assert "Overlocks due on the owner's schedule" in text
        assert "The owner's schedule makes an overlock due 10 days after the due date." in text
        due = re.findall(r'name="lease" value="([A-Z0-9]+)"><input type="hidden" name="next" value="today">', page)
        assert len(due) >= 2
        section = lambda t: t.split("Overlocks due on the owner's schedule")[1].split("Handed to staff")[0]
        assert "Tom Becker" in section(text) and "Record an overlock" in section(text)
        assert "Iris Nolan" not in section(text)     # held by her plan
        home = seen(c.get("/home").text)
        assert f"{len(due)} overlocks due on the owner's schedule" in home
        tom = record_of(c, "Tom Becker").rstrip("/")
        lease = re.search(r'>Tom Becker</a></td>\s*<td>[A-Z0-9]+</td>.*?name="lease" value="([A-Z0-9]+)"', page, re.S).group(1)
        r = c.post(tom + "/collections", data={"token": token(page), "action": "overlock", "lease": lease, "next": "today"}, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/today#overlocks")
        after = seen(c.get("/today").text)
        assert "Tom Becker" not in section(after) and "Nothing changed" not in after
        assert f"{len(due) - 1} overlock" in seen(c.get("/home").text)
        assert "Recorded Since" in seen(c.get(tom).text)
