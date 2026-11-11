import json
from datetime import datetime, timezone

from server import routing, homebot_shortlist as hs, homebot_review as hr
from tests.test_homebot_shortlist import on, client, inventory, submit, current
from tests.test_routing import install, reply


def test_real_job_uses_adapter_persists_evidence_and_revocation_blocks_review(on, tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    monkeypatch.setattr(routing, "_post", lambda body: reply())
    inventory(on.db)
    eid = submit(on)
    outcomes = hs.run_pending(on.db)
    assert outcomes
    sl = current(on, eid)
    result = json.loads(sl["result_json"])
    assert result["route_evidence"]["provider"] == "google-routes"
    assert result["route_evidence"]["requests_attempted"] > 0
    assert result["route_evidence"]["routes_measured"] > 0
    assert result["route_evidence"]["failures"] == 0
    assert hs.freshness(on.db, sl) is None
    assert hr.packet(on.db, sl)["options"]
    monkeypatch.setenv("MMAI_ROUTING", "0")
    assert "Routing permission" in hs.freshness(on.db, sl)
    import pytest
    with pytest.raises(hr.PacketIncomplete):
        hr.packet(on.db, sl)
