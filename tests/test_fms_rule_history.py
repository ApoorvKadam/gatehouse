"""Going back to an earlier version of an owner rule on the Rules page. A scripted client stands in for the model."""
from .test_fms import TWO_MONTHS, TWO_MONTHS_RULE, RuleScript, paid_in_time, record_of, run, seen, sign_in, site


def adopt_two_months(c) -> None:
    p = c.post("/api/fms/rules", json={"sentence": TWO_MONTHS}).json()
    assert c.post("/rules/approve", data={"proposal": p["id"]}, follow_redirects=False).headers["location"] == "/rules#in-force"


def go_back(c, version, rule="late_fee_waiver"):
    return c.post("/rules/restore", data={"rule": rule, "version": version}, follow_redirects=False)


def test_the_rules_page_lists_every_version_newest_first_and_going_back_makes_the_next_case_follow_the_old_one(fms_url, sim):
    app = site(fms_url, RuleScript(paid_in_time, TWO_MONTHS_RULE))
    with sign_in(app) as c, sign_in(app) as other:
        assert "Go back to this version" not in seen(c.get("/rules").text)             # one version: the panel above already shows it
        adopt_two_months(c)
        adopt_two_months(other)
        page = seen(c.get("/rules").text)
        assert page.index("Version 2 In force") < page.index("Version 1 Adopted") and page.count("Go back to this version") == 1
        assert "by the owner" in page and page.count("each of the 6 completed billing months") == 1
        assert page.count("each of the 2 completed billing months") == 1                  # the version in force is shown by its panel, not again in the list
        # one copy keeps version 2: Maria's late fee is waived under it, exactly once
        assert run(c, "maria", shift="s1", run_id="r1").status_code == 200
        maria = record_of(c, "Maria Lopez")
        assert seen(c.get(maria).text).count("Late fee waived under the owner's rule") == 1      # one ledger line: waived once
        # the other goes back to version 1: it is adopted as version 3 and says which version it restored
        done = go_back(other, 1)
        assert done.status_code == 303 and done.headers["location"] == "/rules#in-force"
        back = seen(other.get("/rules").text)
        assert back.index("Version 3 In force") < back.index("Version 2 Adopted") < back.index("Version 1 Adopted")
        assert "going back to version 1" in back and back.count("Go back to this version") == 1 and "says the same as the one in force" in back
        assert "late_fee_waiver" not in back
        assert run(other, "maria", shift="s1", run_id="r1").status_code == 200
        assert "waived under the owner's rule" not in seen(other.get(record_of(other, "Maria Lopez")).text)    # her fee stands again
        # nothing leaks between copies
        assert "Version 3" not in seen(c.get("/rules").text) and seen(c.get("/rules").text).count("Version 2") == 2


def test_going_back_to_the_version_in_force_a_repeat_or_one_that_never_existed_changes_nothing_and_says_why_in_plain_words(fms_url, sim):
    app = site(fms_url, RuleScript(paid_in_time, TWO_MONTHS_RULE))
    with sign_in(app) as c:
        adopt_two_months(c)
        for version, rule, why in (("2", "late_fee_waiver", "That version already says what is in force."), ("9", "late_fee_waiver", "There is no version 9 of that rule."),
                                   ("x", "late_fee_waiver", "There is no version 0 of that rule."), ("", "late_fee_waiver", "There is no version 0 of that rule."),
                                   ("1", "send_to_staff", "There is no version 1 of that rule."), ("1", "no_such_rule", "There is no version 1 of that rule.")):
            r = go_back(c, version, rule)
            assert r.status_code == 303 and r.headers["location"] == "/rules#in-force", (version, rule)
            page = seen(c.get("/rules").text)
            assert f"Nothing changed. {why}" in page and "no_such_rule" not in page and "send_to_staff" not in page, (version, rule)
            assert "Nothing changed" not in seen(c.get("/rules").text)               # said once
        assert "Version 3" not in seen(c.get("/rules").text)
        assert go_back(c, 1).headers["location"] == "/rules#in-force"
        assert "Nothing changed" not in seen(c.get("/rules").text) and "Version 3" in seen(c.get("/rules").text)
        go_back(c, 1)                                                                  # version 3 already says what version 1 said
        page = seen(c.get("/rules").text)
        assert "Nothing changed. That version already says what is in force." in page and "Version 4" not in page


def test_a_refusal_not_yet_shown_is_dropped_when_a_later_going_back_succeeds(fms_url, sim):
    app = site(fms_url, RuleScript(paid_in_time, TWO_MONTHS_RULE))
    with sign_in(app) as c:
        adopt_two_months(c)
        assert go_back(c, 2).headers["location"] == "/rules#in-force"         # refused: version 2 is in force
        assert go_back(c, 1).headers["location"] == "/rules#in-force"         # then it works, before the Rules page was opened
        page = seen(c.get("/rules").text)
        assert "Nothing changed" not in page and "Version 3 In force" in page


def test_a_proposal_read_before_going_back_is_refused_at_its_approval(fms_url, sim):
    app = site(fms_url, RuleScript(paid_in_time, TWO_MONTHS_RULE))
    with sign_in(app) as c:
        adopt_two_months(c)
        p = c.post("/api/fms/rules", json={"sentence": TWO_MONTHS}).json()
        assert go_back(c, 1).headers["location"] == "/rules#in-force"
        assert c.post("/rules/approve", data={"proposal": p["id"]}, follow_redirects=False).headers["location"].startswith("/rules?stale=1")
