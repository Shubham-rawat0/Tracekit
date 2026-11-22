import errno
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend.apps.db.sqllite import default_db_path
from tracekit import cli


class _ImmediateThread:
    def join(self, timeout=None):
        return None


def test_dashboard_parser_defaults():
    args = cli.build_parser().parse_args(["dashboard", "--db", "/tmp/t.db"])
    assert args.command == "dashboard"
    assert args.db == "/tmp/t.db"
    assert args.host == "127.0.0.1"
    assert args.port == 8766
    assert args.open is False
    assert args.token is None


def test_dashboard_parser_flags():
    args = cli.build_parser().parse_args(
        ["dashboard", "--host", "0.0.0.0", "--port", "9000", "--open", "--token", "fixed"]
    )
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.open is True
    assert args.token == "fixed"


def test_main_expands_db_and_dispatches_sync_command(monkeypatch):
    captured = {}

    def fake(args):
        captured.update(vars(args))
        return 0

    monkeypatch.setattr(cli, "_cmd_dashboard", fake)
    assert cli.main(["dashboard", "--db", "~/traces.db"]) == 0
    assert captured["db"] == str(Path("~/traces.db").expanduser())
    assert "~" not in captured["db"]


def test_main_uses_default_db_path(monkeypatch):
    captured = {}

    def fake(args):
        captured.update(vars(args))
        return 0

    monkeypatch.delenv("TRACEKIT_DB_PATH", raising=False)
    monkeypatch.setattr(cli, "_cmd_dashboard", fake)
    assert cli.main(["dashboard"]) == 0
    assert captured["db"] == str(default_db_path())


def test_main_uses_env_db_path(monkeypatch, tmp_path):
    captured = {}

    def fake(args):
        captured.update(vars(args))
        return 0

    env_db = tmp_path / "env.db"
    monkeypatch.setenv("TRACEKIT_DB_PATH", str(env_db))
    monkeypatch.setattr(cli, "_cmd_dashboard", fake)
    assert cli.main(["dashboard"]) == 0
    assert captured["db"] == str(env_db)


def test_main_still_dispatches_async_commands(monkeypatch):
    captured = {}

    async def fake(args):
        captured.update(vars(args))
        return 0

    monkeypatch.setattr(cli, "_cmd_list", fake)
    assert cli.main(["list", "--db", "/tmp/async.db"]) == 0
    assert captured["db"] == "/tmp/async.db"


def _stub_server_module(monkeypatch, start):
    from tracekit.dashboard import server

    monkeypatch.setattr(server, "start_dashboard_server", start)
    monkeypatch.setattr(server, "stop_dashboard_server", lambda *args: None)


def test_dashboard_prints_url_and_generates_token(monkeypatch, capsys):
    captured = {}

    def start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(dashboard_http_thread=_ImmediateThread()), object(), object()

    _stub_server_module(monkeypatch, start)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    assert cli.main(["dashboard", "--db", "/tmp/token.db", "--open"]) == 0
    output = capsys.readouterr().out
    assert "Dashboard running at: http://127.0.0.1:8766/?token=" in output
    assert len(captured["token"]) >= 32
    assert len(opened) == 1
    assert opened[0] == f"http://127.0.0.1:8766/?token={captured['token']}"


def test_dashboard_uses_fixed_token_and_warns_on_other_host(monkeypatch, capsys):
    captured = {}

    def start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(dashboard_http_thread=_ImmediateThread()), object(), object()

    _stub_server_module(monkeypatch, start)
    assert cli.main(
        ["dashboard", "--db", "/tmp/fixed.db", "--host", "0.0.0.0", "--token", "abc"]
    ) == 0
    captured_out = capsys.readouterr()
    assert captured["token"] == "abc"
    assert "http://0.0.0.0:8766/?token=abc" in captured_out.out
    assert "WARNING: Binding to 0.0.0.0 exposes the dashboard on your local network." in captured_out.err


def test_dashboard_port_collision_is_reported(monkeypatch, capsys):
    def start(**kwargs):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    _stub_server_module(monkeypatch, start)
    assert cli.main(["dashboard", "--db", "/tmp/collision.db"]) == 2
    assert "port 8766 is already in use" in capsys.readouterr().err


@pytest.mark.parametrize("db_arg", [None, "/tmp/other.db"])
def test_dashboard_forwards_db_to_server(monkeypatch, db_arg):
    captured = {}

    def start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(dashboard_http_thread=_ImmediateThread()), object(), object()

    _stub_server_module(monkeypatch, start)
    argv = ["dashboard", "--token", "t"] + ([] if db_arg is None else ["--db", db_arg])
    assert cli.main(argv) == 0
    assert captured["db_path"] == (db_arg or str(default_db_path()))
