"""The outside-written lien set is frozen: its hash is pinned before any part is run on it, and every label uses words the set
itself defines, so a later runner never has to guess what a label means."""
import hashlib
import json
from pathlib import Path

SET = Path(__file__).parent.parent / "core" / "graded" / "liens.json"


def test_the_lien_set_is_frozen_and_every_label_uses_its_own_words():
    assert hashlib.sha256(SET.read_bytes()).hexdigest() == "fdc35f743a4f6bbab1a466a636b7831e48dfd637bd5ee545cec42545f990a073"
    s = json.loads(SET.read_text())
    items = s["graded"]
    assert len(items) == 80 and len({i["id"] for i in items}) == 80
    assert [sum(i["group"] == g for i in items) for g in ("ordinary", "account_changes", "adversarial")] == [30, 30, 20]
    for i in items:
        label = i["label"]
        assert label["ask"] in s["asks"] and label["decision"] in ("allow", "refuse"), i["id"]
        assert (label["why"] is None) == (label["decision"] == "allow"), i["id"]
        assert label["why"] is None or label["why"] in s["why"], i["id"]
        assert set(label["effects"]) <= set(s["effects"]) and set(label["never"]) <= set(s["never"]), i["id"]
        assert label["part"] in (1, 2, 3, 4, "collections"), i["id"]
