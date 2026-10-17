"""一键启动完整演示环境：baseline(8620) + candidate(8621) + 验收器网页(8622)。

用法：
    python run_demo.py
然后浏览器打开 http://127.0.0.1:8622

配置：启动前会读取项目根的 `.env`（如端口、DEMO_DEFECTS），各子服务启动时**各自**再读一遍
`.env`（所以 LLM_API_KEY 的来源会如实显示为 `.env`）。真实环境变量优先于 `.env`。
模板见 `.env.example`。
端口也可直接用环境变量覆盖：DEMO_BASELINE_PORT / DEMO_CANDIDATE_PORT / VERIFIER_PORT。
按 Ctrl+C 停止全部服务。
"""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import time

from verifier.envfile import parse_env_file

ROOT = pathlib.Path(__file__).resolve().parent


def port_is_available(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    file_env = parse_env_file()
    if file_env:
        print(f"[run_demo] 发现 .env，包含：{', '.join(sorted(file_env))}（由各服务自行读取）")

    def setting(name: str, default: str) -> str:
        """优先级：真实环境变量 > .env > 默认值。"""
        return os.environ.get(name) or file_env.get(name) or default

    baseline_port = int(setting("DEMO_BASELINE_PORT", "8620"))
    candidate_port = int(setting("DEMO_CANDIDATE_PORT", "8621"))
    verifier_port = int(setting("VERIFIER_PORT", "8622"))

    for name, port in (("baseline", baseline_port), ("candidate", candidate_port), ("验收器", verifier_port)):
        if not port_is_available(port):
            print(
                f"[run_demo] 端口 {port}（{name}）不可用。可能是被其他程序占用，或被 Windows 保留端口段排除\n"
                f"（查看：netsh interface ipv4 show excludedportrange protocol=tcp）。\n"
                f"请用环境变量换端口，例如：$env:DEMO_BASELINE_PORT=18620 后重试。"
            )
            return 2

    child_env = {
        **os.environ,
        "DEMO_BASELINE_PORT": str(baseline_port),
        "DEMO_CANDIDATE_PORT": str(candidate_port),
        "VERIFIER_PORT": str(verifier_port),
    }

    services = [
        (
            "baseline 示例服务（修改前）",
            [sys.executable, str(ROOT / "demo" / "run_baseline.py")],
            f"http://127.0.0.1:{baseline_port}/healthz",
        ),
        (
            "candidate 示例服务（修改后，含演示缺陷）",
            [sys.executable, str(ROOT / "demo" / "run_candidate.py")],
            f"http://127.0.0.1:{candidate_port}/healthz",
        ),
        (
            "改动验收器",
            [sys.executable, "-m", "verifier"],
            f"http://127.0.0.1:{verifier_port}/api/llm/status",
        ),
    ]

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, cmd, ready_url in services:
            print(f"[run_demo] 启动 {name} ...")
            p = subprocess.Popen(cmd, cwd=str(ROOT), env=child_env)
            procs.append((name, p))
            if not wait_ready(ready_url):
                print(f"[run_demo] 警告：{name} 在 20 秒内未就绪，请查看其日志。")
            else:
                print(f"[run_demo] {name} 已就绪")
        print()
        print("[run_demo] 全部服务已启动。")
        print(
            f"[run_demo] 打开浏览器访问 http://127.0.0.1:{verifier_port} ，"
            "点击左侧「创建演示任务」，再切到「运行与报告」运行验收。"
        )
        print("[run_demo] 按 Ctrl+C 停止全部服务。")
        while True:
            for name, p in procs:
                code = p.poll()
                if code is not None:
                    print(f"[run_demo] {name} 已退出（代码 {code}），停止其余服务。")
                    return code
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[run_demo] 收到 Ctrl+C，正在停止服务…")
        return 0
    finally:
        for _, p in procs:
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


def wait_ready(url: str, timeout: float = 20.0) -> bool:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
