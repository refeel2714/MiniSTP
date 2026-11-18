from ministp import cli


def test_asks_only_for_missing_values(monkeypatch):
    asked = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: asked.append(prompt) or " v ")
    env = cli.ask_missing({"MINISTP_KIS_APP_KEY": "k"})
    assert env["MINISTP_KIS_APP_KEY"] == "k"
    assert env["MINISTP_KIS_APP_SECRET"] == env["MINISTP_KIS_ACCOUNT"] == "v"
    assert len(asked) == 2


def test_database_url_quotes_password(monkeypatch):
    from ministp import __main__ as cli
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "p@ss/word")
    assert cli.database_url({}) == "postgresql://postgres:p%40ss%2Fword@localhost:5432/ministp"
    assert cli.database_url({"MINISTP_DATABASE_URL": "postgresql://x"}) == "postgresql://x"
