import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "kis_smoke", Path(__file__).parents[2] / "scripts" / "kis_smoke.py")
kis_smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kis_smoke)


def test_asks_only_for_missing_values(monkeypatch):
    asked = []
    monkeypatch.setattr(kis_smoke.getpass, "getpass", lambda prompt: asked.append(prompt) or " v ")
    env = kis_smoke.ask_missing({"MINISTP_KIS_APP_KEY": "k"})
    assert env["MINISTP_KIS_APP_KEY"] == "k"
    assert env["MINISTP_KIS_APP_SECRET"] == env["MINISTP_KIS_ACCOUNT"] == "v"
    assert len(asked) == 2
