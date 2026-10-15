"""pytest 共享 fixtures：启动真实的示例服务（uvicorn 线程）与验收器 TestClient。"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import threading
import time

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(app, port: int):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def wait_ready(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(base_url + "/healthz", timeout=1).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪: {base_url}")


@pytest.fixture(scope="session")
def baseline_server():
    from demo.services import create_app

    port = free_port()
    server, thread = start_server(create_app("baseline"), port)
    base = f"http://127.0.0.1:{port}"
    wait_ready(base)
    yield base
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="session")
def candidate_server():
    from demo.services import create_app

    port = free_port()
    server, thread = start_server(create_app("candidate"), port)
    base = f"http://127.0.0.1:{port}"
    wait_ready(base)
    yield base
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="session")
def dead_url():
    """一个没有任何服务监听的本地地址（连接会被立即拒绝）。"""
    return f"http://127.0.0.1:{free_port()}"


@pytest.fixture(scope="session")
def verifier_client(tmp_path_factory):
    os.environ["VERIFIER_DB"] = str(tmp_path_factory.mktemp("verifier_db") / "verifier.db")
    from fastapi.testclient import TestClient

    from verifier.main import app

    with TestClient(app) as client:
        yield client
