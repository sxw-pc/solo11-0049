from __future__ import annotations

import socket

from swapdesk.cli import main


def test_demo_never_opens_network(monkeypatch, tmp_path) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in swapdesk")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    code = main(["demo", "--workdir", str(tmp_path / "offline")])
    assert code == 0
