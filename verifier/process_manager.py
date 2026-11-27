"""本地托管服务进程编排：端口预留、跨平台命令拆分、就绪探针轮询与生命周期回收。

供 `change-verifier demo` 与 `change-verifier verify --baseline-cmd / --candidate-cmd` 共用，
保证临时状态目录隔离、UTF-8 日志落盘以及异常/退出时的可靠子进程回收。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import pathlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import IO, Callable, Iterator, Optional, Sequence


def port_is_available(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def allocate_free_ports(count: int = 1) -> list[int]:
    """同时绑定 `count` 个本地回环临时端口后再统一释放，避免连续分配拿到相同端口。"""
    sockets: list[socket.socket] = []
    ports: list[int] = []
    try:
        for _ in range(count):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            ports.append(int(s.getsockname()[1]))
            sockets.append(s)
        return ports
    finally:
        for s in sockets:
            s.close()


def terminate_processes(procs: Sequence[subprocess.Popen]) -> None:
    """优雅终止子进程列表；超时未退出时强制 kill 并回收句柄。"""
    for p in procs:
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                pass


def wait_ready(url: str, timeout: float = 20.0, proc: Optional[subprocess.Popen] = None) -> bool:
    """轮询就绪地址直至返回 <500；若传入 `proc` 且进程提前退出则立即返回 False。"""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return True
        except Exception:
            pass
        if proc is not None and proc.poll() is not None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
    return False


def split_command_template(cmd_template: str) -> list[str]:
    """按当前平台（Windows / POSIX）拆分启动命令模板。"""
    cleaned = cmd_template.strip()
    if not cleaned:
        raise ValueError("启动命令不能为空字符串")
    try:
        if os.name == "nt":
            raw_tokens = shlex.split(cleaned, posix=False)
            tokens: list[str] = []
            for tok in raw_tokens:
                if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
                    quote = tok[0]
                    tok = tok[1:-1]
                    if quote == '"':
                        tok = tok.replace('\\"', '"')
                elif "=" in tok:
                    key, _, val = tok.partition("=")
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                        quote = val[0]
                        val = val[1:-1]
                        if quote == '"':
                            val = val.replace('\\"', '"')
                        tok = f"{key}={val}"
                tokens.append(tok)
        else:
            tokens = shlex.split(cleaned, posix=True)
    except ValueError as e:
        raise ValueError(f"无法解析启动命令 {cmd_template!r}：{e}") from e
    if not tokens:
        raise ValueError("启动命令解析后为空")
    return tokens


def render_cmd_placeholders(
    text: str,
    *,
    port: int,
    host: str,
    env_label: str,
    temp_dir: pathlib.Path,
) -> str:
    return (
        text.replace("{port}", str(port))
        .replace("{host}", host)
        .replace("{env}", env_label)
        .replace("{temp_dir}", str(temp_dir))
    )


def parse_env_overrides(items: Sequence[str], flag_name: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in items:
        key, sep, val = raw.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"{flag_name} 格式错误 {raw!r}，应为 KEY=VALUE")
        pairs.append((key, val))
    return pairs


def read_log_tail(log_path: pathlib.Path, max_lines: int = 20) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:])


@dataclass(frozen=True)
class ManagedServiceSpec:
    env_label: str
    tokens: list[str]
    port: int
    base_url: str
    cwd: Optional[pathlib.Path] = None
    env_pairs: Sequence[tuple[str, str]] = ()


@dataclass
class ManagedService:
    env_label: str
    port: int
    base_url: str
    ready_url: str
    temp_dir: pathlib.Path
    log_path: pathlib.Path
    proc: subprocess.Popen


class ManagedServiceError(RuntimeError):
    """托管子服务启动失败、提前退出或就绪超时。"""

    def __init__(self, env_label: str, message: str, *, log_tail: str = "") -> None:
        super().__init__(message)
        self.env_label = env_label
        self.message = message
        self.log_tail = log_tail


@contextmanager
def managed_dual_services(
    specs: Sequence[ManagedServiceSpec],
    *,
    ready_path: str = "/healthz",
    ready_timeout: float = 20.0,
) -> Iterator[list[ManagedService]]:
    """拉起一组本地受管服务，等待全部就绪，并在退出上下文时统一终止与清理临时目录。"""
    if not specs:
        yield []
        return

    temp_root = pathlib.Path(tempfile.mkdtemp(prefix="change_verifier_"))
    managed_procs: list[subprocess.Popen] = []
    log_handles: list[IO[str]] = []
    launched: list[ManagedService] = []

    try:
        for spec in specs:
            inst_dir = temp_root / spec.env_label
            inst_dir.mkdir(parents=True, exist_ok=True)
            argv = [
                render_cmd_placeholders(
                    tok, port=spec.port, host="127.0.0.1", env_label=spec.env_label, temp_dir=inst_dir
                )
                for tok in spec.tokens
            ]
            child_env = {
                **os.environ,
                "HOST": "127.0.0.1",
                "PORT": str(spec.port),
                "VERIFIER_TARGET_HOST": "127.0.0.1",
                "VERIFIER_TARGET_PORT": str(spec.port),
                "VERIFIER_TARGET_ENV": spec.env_label,
                "VERIFIER_TEMP_DIR": str(inst_dir),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            }
            if spec.env_label == "baseline":
                child_env["DEMO_BASELINE_PORT"] = str(spec.port)
            else:
                child_env["DEMO_CANDIDATE_PORT"] = str(spec.port)

            for key, val in spec.env_pairs:
                child_env[key] = render_cmd_placeholders(
                    val, port=spec.port, host="127.0.0.1", env_label=spec.env_label, temp_dir=inst_dir
                )

            log_path = inst_dir / "service.log"
            log_fh = log_path.open("w+", encoding="utf-8")
            log_handles.append(log_fh)
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(spec.cwd) if spec.cwd is not None else None,
                    env=child_env,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                )
            except OSError as e:
                raise ManagedServiceError(spec.env_label, f"启动托管 {spec.env_label} 服务失败：{e}") from e

            managed_procs.append(proc)
            launched.append(
                ManagedService(
                    env_label=spec.env_label,
                    port=spec.port,
                    base_url=spec.base_url,
                    ready_url=f"{spec.base_url.rstrip('/')}{ready_path}",
                    temp_dir=inst_dir,
                    log_path=log_path,
                    proc=proc,
                )
            )

        deadline = time.monotonic() + ready_timeout
        for svc in launched:
            remaining = max(0.05, deadline - time.monotonic())
            ready = wait_ready(svc.ready_url, timeout=remaining, proc=svc.proc)
            exit_code = svc.proc.poll()
            if exit_code is not None or not ready:
                if exit_code is not None:
                    msg = f"托管 {svc.env_label} 服务在就绪前提前退出（退出码 {exit_code}）"
                else:
                    msg = f"托管 {svc.env_label} 服务在 {ready_timeout:g} 秒内未就绪（{svc.ready_url}）"
                raise ManagedServiceError(svc.env_label, msg, log_tail=read_log_tail(svc.log_path))

        yield launched
    finally:
        terminate_processes(managed_procs)
        for fh in log_handles:
            try:
                fh.close()
            except OSError:
                pass
        shutil.rmtree(temp_root, ignore_errors=True)


def run_managed_demo_stack(
    *,
    root_dir: pathlib.Path,
    baseline_port: int,
    candidate_port: int,
    verifier_port: int,
    ready_timeout: float = 20.0,
    port_available_fn: Callable[[int], bool] = port_is_available,
    wait_ready_fn: Callable[..., bool] = wait_ready,
    popen_fn: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> int:
    """启动并守护本地三服务演示栈（baseline + candidate + 验收器），任一退出或 Ctrl+C 时统一清理。"""
    for name, port in (("baseline", baseline_port), ("candidate", candidate_port), ("验收器", verifier_port)):
        if not port_available_fn(port):
            print(
                f"[run_demo] 端口 {port}（{name}）不可用。可能是被其他程序占用，或被 Windows 保留端口段排除\n"
                f"（查看：netsh interface ipv4 show excludedportrange protocol=tcp）。\n"
                f"请用环境变量或参数换端口，例如：$env:DEMO_BASELINE_PORT=18620 后重试。"
            )
            return 2

    child_env = {
        **os.environ,
        "DEMO_BASELINE_PORT": str(baseline_port),
        "DEMO_CANDIDATE_PORT": str(candidate_port),
        "VERIFIER_PORT": str(verifier_port),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }

    services = [
        ("baseline 示例服务（修改前）", [sys.executable, "-m", "demo.run_baseline"], f"http://127.0.0.1:{baseline_port}/healthz"),
        (
            "candidate 示例服务（修改后，含演示缺陷）",
            [sys.executable, "-m", "demo.run_candidate"],
            f"http://127.0.0.1:{candidate_port}/healthz",
        ),
        ("改动验收器", [sys.executable, "-m", "verifier", "serve"], f"http://127.0.0.1:{verifier_port}/api/llm/status"),
    ]

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, cmd, ready_url in services:
            print(f"[run_demo] 启动 {name} ...")
            p = popen_fn(cmd, cwd=str(root_dir), env=child_env)
            procs.append((name, p))
            ready = wait_ready_fn(ready_url, timeout=ready_timeout, proc=p)
            exit_code = p.poll()
            if exit_code is not None:
                print(
                    f"[run_demo] 错误：{name} 在启动期间提前退出（代码 {exit_code}），正在终止已启动服务。",
                    file=sys.stderr,
                )
                return 1
            if not ready:
                print(
                    f"[run_demo] 错误：{name} 在 {ready_timeout:g} 秒内未就绪（{ready_url}），正在终止已启动服务。",
                    file=sys.stderr,
                )
                return 1
            for prev_name, prev_proc in procs[:-1]:
                prev_code = prev_proc.poll()
                if prev_code is not None:
                    print(
                        f"[run_demo] 错误：{prev_name} 在启动阶段意外退出（代码 {prev_code}），正在终止已启动服务。",
                        file=sys.stderr,
                    )
                    return 1
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
        terminate_processes([p for _, p in procs])
