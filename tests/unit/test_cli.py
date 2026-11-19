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


def test_db_error_hint():
    from ministp.__main__ import db_error_hint
    garbled = 'connection to server at "127.0.0.1", port 5432 failed: ?????: "postgres"?? password ????'
    assert "비밀번호가 맞지 않아요" in db_error_hint(garbled)
    assert "실행 중이 아니에요" in db_error_hint("connection refused")
    assert db_error_hint("other").startswith("DB 접속 실패: other")


def test_wrong_db_password_prints_hint(capsys):
    import psycopg
    from ministp import __main__ as cli

    def connect(*a, **k):
        raise psycopg.OperationalError('failed: "postgres" password authentication failed')
    env = {"MINISTP_DATABASE_URL": "postgresql://postgres:x@localhost/ministp"}
    assert cli.run(["db-init"], environ=env, connect=connect) == 1
    assert "비밀번호가 맞지 않아요" in capsys.readouterr().out
