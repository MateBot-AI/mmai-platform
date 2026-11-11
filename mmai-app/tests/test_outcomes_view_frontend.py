"""inc1b front-end regression for the P2 'Outcomes by person' view (static source checks; browser QA by root is the live check).
Fails on increment 1 (8204978b…): the Groups header's 'Outcomes by person' button had no click listener, and the outcomes view had
no small-screen fit rules (375px: the standing filter select and nowrap badges forced horizontal overflow)."""
from __future__ import annotations

import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[1]
STAFF = (APP / "client" / "staff.js").read_text(encoding="utf-8")
CSS = (APP / "client" / "styles.css").read_text(encoding="utf-8")


def _view(name: str) -> str:
    m = re.search(rf"views\.{name} = async p => \{{(.*?)\n  \}};", STAFF, re.S)
    assert m, f"views.{name} not found"
    return m.group(1)


def test_groups_header_outcomes_button_is_wired_to_navigate():
    groups = _view("groups")
    assert 'data-go="outcomes"' in groups, "the button must stay in the Groups header"
    wiring = re.search(r"querySelectorAll\('\[data-go\]'\)\.forEach\(b => b\.addEventListener\('click', \(\) => navigate\(b\.dataset\.go\)\)\)", groups)
    assert wiring, "every data-go button in the Groups view must call navigate(view) - inc1 had no listener"
    assert groups.index('data-go="outcomes"') < wiring.start(), "wiring runs after the header is rendered"
    assert "views.outcomes = async p =>" in STAFF and "outcomes: 'groups'" in STAFF    # route + nav highlight still present


def test_outcomes_view_is_scoped_for_small_screen_fit_without_hiding_content():
    out = _view("outcomes")
    assert out.count('<div class="outcomes">') == 1 and "</details></div>`;" in out    # one scoped wrapper around the whole view
    assert 'class="toolbar outcomes-filters"' in out
    # labels and columns are all kept: nothing is hidden by the fit rules
    for col in ("Person", "Standing", "What is happening and why", "Next step", "History"):
        assert f"<th>{col}</th>" in out
    rules = CSS[CSS.index(".outcomes-filters select"):]
    assert re.search(r"\.outcomes-filters select \{[^}]*min-width: 0;[^}]*max-width: 100%;", rules)
    assert re.search(r"\.outcomes \.tbl \.badge \{ white-space: normal; \}", rules)
    small = re.search(r"@media \(max-width: 640px\) \{\s*\.outcomes \.tbl td \{[^}]*\}\s*\.outcomes \.tbl td::before \{[^}]*position: static;", rules)
    assert small, "stacked cells show their label above the text on small screens"
    assert "display: none" not in rules and "overflow: hidden" not in rules and "text-overflow" not in rules


def test_outcomes_view_escapes_the_ledger_values_it_prints():
    out = _view("outcomes")
    for expr in ("esc(x.name || 'Name not recorded')", "esc(x.role || '')", "esc(x.state_label || x.state || '')", "esc(x.outcome || '')",
                 "esc(x.next_action || '')", "esc(r.text)", "esc(states[k].label || k)", "esc(led.business_date || '')", "esc(t)"):
        assert expr in out, expr
    for raw in ("${x.name}", "${x.outcome}", "${x.next_action}", "${r.text}", "${x.state_label}", "${t}"):
        assert raw not in out, f"unescaped {raw}"
    assert "innerHTML" in out and ".innerHTML = x." not in out and "insertAdjacentHTML" not in out
