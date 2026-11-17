from ministp import cli


def test_asks_only_for_missing_values(monkeypatch):
    asked = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: asked.append(prompt) or " v ")
    env = cli.ask_missing({"MINISTP_KIS_APP_KEY": "k"})
    assert env["MINISTP_KIS_APP_KEY"] == "k"
    assert env["MINISTP_KIS_APP_SECRET"] == env["MINISTP_KIS_ACCOUNT"] == "v"
    assert len(asked) == 2
