import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api_server import (
    app,
    controllers, controller_detail, command, state,
    start_training_session, get_training_session,
    CommandRequest, StartSessionRequest,
)
from models.command import CommandAction
from models.controller import MODE_MANUAL


def test_list_controllers():
    resp = controllers()
    assert resp["count"] == 55
    assert "TRC 2" in resp["controllers"]
    assert resp["controllers"]["TRC 2"]["sp"] == 128.0


def test_controller_detail():
    fp = controller_detail("TRC 2")
    assert fp["tag"] == "TRC 2"
    assert fp["cascade"] is None
    assert fp["rev"] is True


def test_controller_detail_missing_404():
    with pytest.raises(HTTPException) as e:
        controller_detail("NOPE")
    assert e.value.status_code == 404


def test_command_set_sp():
    r = command(CommandRequest(tag="TRC 2", action=CommandAction.SET_SP, value=130))
    assert r["ok"] is True
    assert r["controller"]["sp"] == 130.0


def test_command_set_mode():
    r = command(CommandRequest(tag="TRC 2", action=CommandAction.SET_MODE, value=MODE_MANUAL))
    assert r["controller"]["mode"] == MODE_MANUAL


def test_command_bad_mode_422():
    with pytest.raises(HTTPException) as e:
        command(CommandRequest(tag="TRC 2", action=CommandAction.SET_MODE, value="OFF"))
    assert e.value.status_code == 422


def test_command_unknown_tag_422():
    with pytest.raises(HTTPException) as e:
        command(CommandRequest(tag="NOPE", action=CommandAction.SET_SP, value=100))
    assert e.value.status_code == 422


def test_command_hand_valve_auto_422():
    with pytest.raises(HTTPException) as e:
        command(CommandRequest(tag="HV 820", action=CommandAction.SET_MODE, value="АВТ"))
    assert e.value.status_code == 422


def test_training_session_lifecycle():
    # "BASELINE" resolves via _resolve_scenario()'s unconditional, DB-free
    # branch (_baseline_scenario()); an "LMS-{id}" id would instead require
    # a matching row to already exist in LmsContentStore's scenario table --
    # ambient state this test (and this whole module, which calls route
    # functions directly with no fixtures/isolation) doesn't set up. Using
    # BASELINE keeps the test deterministic on a fresh checkout while still
    # exercising the real session start/get lifecycle this test is for.
    s = start_training_session(StartSessionRequest(scenario_id="BASELINE", operator_id="op1"))
    assert s["status"] == "RUNNING"
    assert s["scenario_id"] == "BASELINE"
    assert s["operator_id"] == "op1"
    assert s["session_id"].startswith("TR-")
    got = get_training_session()
    assert got["session_id"] == s["session_id"]
    assert got["events"][0]["kind"] == "START"


def test_training_session_unknown_scenario_404():
    with pytest.raises(HTTPException) as e:
        start_training_session(StartSessionRequest(scenario_id="NO_SUCH"))
    assert e.value.status_code == 404


def test_state_includes_controllers():
    s = state()
    assert "controllers" in s
    assert "TRC 2" in s["controllers"]
    assert s["controllers"]["TRC 2"]["mode"] in ("АВТ", "РУЧ")


def test_root_and_favicon_routes_are_not_404():
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code in (200, 307, 308)

    favicon = client.get("/favicon.ico")
    assert favicon.status_code in (200, 204)
