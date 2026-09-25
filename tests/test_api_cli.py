import io
import json

import pytest

from app import api

from tests.test_service import _vision_asset


def run(capsys, argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = api.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_stdout_is_only_one_json_envelope(stocker_root, capsys):
    asset_id = _vision_asset(stocker_root)
    capsys.readouterr()

    code, out, _ = run(capsys, ["metadata.build", "--params", json.dumps({"asset_id": asset_id})])

    assert code == 0
    assert out.count("\n") == 1
    envelope = json.loads(out)
    assert envelope["ok"] and envelope["outcome"] == "DRAFTED"


def test_worker_prints_go_to_stderr(stocker_root, capsys, monkeypatch):
    from app import worker
    from tests.conftest import FakeAnalyzer, make_image

    monkeypatch.setattr(worker, "LocalAnalyzer", FakeAnalyzer)
    make_image(stocker_root)

    code, out, err = run(capsys, ["asset.process_file", "--params", '{"path": "data/incoming/photo.jpg"}'])

    assert code == 0
    assert json.loads(out)["outcome"] == "AI_PASSED"
    assert "WORKER:" in err and "WORKER:" not in out


def test_params_from_stdin(stocker_root, capsys, monkeypatch):
    asset_id = _vision_asset(stocker_root)
    capsys.readouterr()

    code, out, _ = run(capsys, ["asset.get", "--params", "-"], stdin=json.dumps({"asset_id": asset_id}), monkeypatch=monkeypatch)

    assert code == 0 and json.loads(out)["data"]["id"] == asset_id


def test_not_ok_exit_code_1(stocker_root, capsys):
    code, out, _ = run(capsys, ["asset.get", "--params", '{"asset_id": 999}'])

    assert code == 1
    assert json.loads(out)["error"]["code"] == "ASSET_NOT_FOUND"


def test_actor_flag_forbidden(stocker_root, capsys):
    asset_id = _vision_asset(stocker_root)
    run(capsys, ["metadata.build", "--params", json.dumps({"asset_id": asset_id})])

    code, out, _ = run(capsys, ["metadata.approve", "--params", json.dumps({"asset_id": asset_id}), "--actor", "workflow:n8n"])

    assert code == 1 and json.loads(out)["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("params", ["{not json", "[1, 2]"])
def test_bad_params_is_usage_error(stocker_root, capsys, params):
    with pytest.raises(SystemExit) as info:
        api.main(["asset.get", "--params", params])

    assert info.value.code == 2


def test_pretty_output(stocker_root, capsys):
    code, out, _ = run(capsys, ["operations.list", "--pretty"])

    assert code == 0 and out.startswith("{\n")
    assert json.loads(out)["ok"]
