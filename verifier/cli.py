"""改动验收器命令行入口（`change-verifier` / `python -m verifier`）。

支持三种模式：
- `serve`（无参数默认）：启动 FastAPI 交互式验收工作台（默认 http://127.0.0.1:8622）
- `demo`：一键启动本地三服务演示栈（baseline 8620 + candidate 8621 + 验收器 8622）
- `verify`：无头（Headless）双环境验收门禁，适用于 GitHub Actions / CI 流水线或终端脚本；
  支持从 JSON 规则文件、内置演示规则或 SQLite 已确认规则版本读取检查，输出脱敏报告并返回门禁退出码。

退出码约定（`verify`）：
- `0`：门禁通过（未触发 `--fail-on` 中指定的失败分类；默认零回归、零新需求未满足、零无法判断）
- `1`：验收未通过（存在回归或 `--fail-on` 指定的阻断项）
- `2`：参数、规则校验、未确认草稿或安全限制错误
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Optional, Sequence

from . import __version__, demo_data
from .classification import CLASSIFICATION_LABELS, CLASSIFICATION_ORDER
from .db import db_path, now_iso
from .envfile import load_env, parse_env_file
from .executor import DEFAULT_TIMEOUT_S
from .models import validate_checks
from .report import build_json_report, render_markdown
from .runner import run_checks
from .safety import SafetyError, check_run_safety, validate_target_url

DEFAULT_FAIL_ON = ("regression", "not_met", "unverifiable")


def port_is_available(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="change-verifier",
        description="改动验收器：基于人工确认规则的双环境（baseline vs candidate）HTTP API 回归与新需求验收工具。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # ---------------- serve ----------------
    p_serve = subparsers.add_parser("serve", help="启动网页验收工作台（无子命令时的默认行为）")
    p_serve.add_argument("--host", default=None, help="监听地址（默认读取 VERIFIER_HOST 或 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=None, help="监听端口（默认读取 VERIFIER_PORT 或 8622）")
    p_serve.add_argument("--log-level", default="info", help="uvicorn 日志级别（默认 info）")

    # ---------------- demo ----------------
    p_demo = subparsers.add_parser("demo", help="一键启动本地三服务完整演示环境（baseline + candidate + 验收器）")
    p_demo.add_argument("--baseline-port", type=int, default=None, help="baseline 端口（默认 8620）")
    p_demo.add_argument("--candidate-port", type=int, default=None, help="candidate 端口（默认 8621）")
    p_demo.add_argument("--port", type=int, default=None, help="验收器网页端口（默认 8622）")

    # ---------------- verify ----------------
    p_verify = subparsers.add_parser(
        "verify",
        help="无头执行双环境验收检查（CI 门禁模式）：零阻断项返回 0，发现回归等阻断项返回 1",
    )
    src = p_verify.add_mutually_exclusive_group(required=True)
    src.add_argument("--rules", type=pathlib.Path, help="JSON 规则文件路径（支持检查数组 `[...]` 或 `{\"checks\": [...]}`）")
    src.add_argument("--demo-rules", action="store_true", help="使用内置的 10 条已确认演示规则")
    src.add_argument("--rule-version-id", type=int, help="从 SQLite 加载指定规则版本 ID")
    src.add_argument("--task-id", type=int, help="从 SQLite 加载指定任务的最新已确认规则版本")

    p_verify.add_argument("--baseline", default=None, help="修改前（baseline）环境 URL，如 http://127.0.0.1:8620")
    p_verify.add_argument("--candidate", default=None, help="修改后（candidate）环境 URL，如 http://127.0.0.1:8621")
    p_verify.add_argument("--db", type=pathlib.Path, default=None, help="SQLite 数据库路径（默认读取 VERIFIER_DB）")
    p_verify.add_argument("--allow-draft", action="store_true", help="允许执行 SQLite 中尚未确认（draft）的规则版本")
    p_verify.add_argument("--allow-remote", action="store_true", help="允许访问非本机（非 127.0.0.1/localhost）目标环境")
    p_verify.add_argument(
        "--allow-remote-writes",
        action="store_true",
        help="允许向远程环境发送 POST/PUT/PATCH/DELETE 请求（需同时开启 --allow-remote）",
    )
    p_verify.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"单步请求默认超时秒数（默认 {DEFAULT_TIMEOUT_S:g}）",
    )
    p_verify.add_argument(
        "--fail-on",
        default=",".join(DEFAULT_FAIL_ON),
        help=(
            "触发非零退出码(1)的结果分类列表，逗号分隔；"
            f"默认 '{','.join(DEFAULT_FAIL_ON)}'（仅拦截回归可传 '--fail-on regression'，仅生成报告传 '--fail-on none'）"
        ),
    )
    p_verify.add_argument("-o", "--output", type=pathlib.Path, default=None, help="导出脱敏报告路径（.md 或 .json）")
    p_verify.add_argument("--json-output", type=pathlib.Path, default=None, help="额外导出结构化 JSON 报告路径")
    p_verify.add_argument("--task-name", default=None, help="报告中的任务名称（覆盖默认值）")
    p_verify.add_argument("--requirement", default=None, help="报告中的需求描述（覆盖默认值）")
    p_verify.add_argument("-q", "--quiet", action="store_true", help="仅输出总体结论，不逐条打印检查明细")

    return parser


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    loaded = load_env()
    if loaded:
        print(f"[verifier] 已从 .env 读取：{', '.join(sorted(loaded))}")
    host = args.host or os.environ.get("VERIFIER_HOST", "127.0.0.1")
    port = args.port if args.port is not None else int(os.environ.get("VERIFIER_PORT", "8622"))
    uvicorn.run("verifier.main:app", host=host, port=port, log_level=args.log_level)
    return 0


def run_demo_stack(
    *,
    baseline_port_override: Optional[int] = None,
    candidate_port_override: Optional[int] = None,
    verifier_port_override: Optional[int] = None,
) -> int:
    file_env = parse_env_file()
    if file_env:
        print(f"[run_demo] 发现 .env，包含：{', '.join(sorted(file_env))}（由各服务自行读取）")

    def setting(name: str, default: str) -> str:
        return os.environ.get(name) or file_env.get(name) or default

    baseline_port = baseline_port_override or int(setting("DEMO_BASELINE_PORT", "8620"))
    candidate_port = candidate_port_override or int(setting("DEMO_CANDIDATE_PORT", "8621"))
    verifier_port = verifier_port_override or int(setting("VERIFIER_PORT", "8622"))

    for name, port in (("baseline", baseline_port), ("candidate", candidate_port), ("验收器", verifier_port)):
        if not port_is_available(port):
            print(
                f"[run_demo] 端口 {port}（{name}）不可用。可能是被其他程序占用，或被 Windows 保留端口段排除\n"
                f"（查看：netsh interface ipv4 show excludedportrange protocol=tcp）。\n"
                f"请用环境变量或参数换端口，例如：$env:DEMO_BASELINE_PORT=18620 后重试。"
            )
            return 2

    root_dir = pathlib.Path(__file__).resolve().parents[1]
    child_env = {
        **os.environ,
        "DEMO_BASELINE_PORT": str(baseline_port),
        "DEMO_CANDIDATE_PORT": str(candidate_port),
        "VERIFIER_PORT": str(verifier_port),
    }

    services = [
        (
            "baseline 示例服务（修改前）",
            [sys.executable, "-m", "demo.run_baseline"],
            f"http://127.0.0.1:{baseline_port}/healthz",
        ),
        (
            "candidate 示例服务（修改后，含演示缺陷）",
            [sys.executable, "-m", "demo.run_candidate"],
            f"http://127.0.0.1:{candidate_port}/healthz",
        ),
        (
            "改动验收器",
            [sys.executable, "-m", "verifier", "serve"],
            f"http://127.0.0.1:{verifier_port}/api/llm/status",
        ),
    ]

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, cmd, ready_url in services:
            print(f"[run_demo] 启动 {name} ...")
            p = subprocess.Popen(cmd, cwd=str(root_dir), env=child_env)
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


def _parse_fail_on(raw: str) -> set[str]:
    cleaned = raw.strip()
    if not cleaned or cleaned.lower() == "none":
        return set()
    codes: set[str] = set()
    for part in cleaned.replace("，", ",").split(","):
        code = part.strip()
        if not code:
            continue
        if code not in CLASSIFICATION_LABELS:
            valid = ", ".join(sorted(CLASSIFICATION_LABELS))
            raise ValueError(f"未知的 --fail-on 分类 {code!r}；可选值：{valid}，或 none")
        codes.add(code)
    return codes


def _load_json_rules(path: pathlib.Path) -> tuple[list[dict], dict, dict]:
    if not path.is_file():
        raise ValueError(f"规则文件不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"无法读取或解析规则 JSON（{path}）：{e}") from e

    if isinstance(raw, list):
        checks = raw
        task_info = {"id": "cli", "name": f"CLI 验收（{path.name}）", "requirement": ""}
        version_info = {"id": "file", "version": 1, "status": "confirmed", "note": str(path), "confirmed_at": None}
        return checks, task_info, version_info

    if isinstance(raw, dict) and isinstance(raw.get("checks"), list):
        checks = raw["checks"]
        nested_task = raw.get("task") if isinstance(raw.get("task"), dict) else {}
        nested_ver = raw.get("rule_version") if isinstance(raw.get("rule_version"), dict) else {}
        task_info = {
            "id": nested_task.get("id", "cli"),
            "name": raw.get("name") or nested_task.get("name") or f"CLI 验收（{path.name}）",
            "requirement": raw.get("requirement") or nested_task.get("requirement") or "",
            "baseline_url": raw.get("baseline_url") or nested_task.get("baseline_url"),
            "candidate_url": raw.get("candidate_url") or nested_task.get("candidate_url"),
        }
        version_info = {
            "id": nested_ver.get("id", "file"),
            "version": raw.get("version") or nested_ver.get("version", 1),
            "status": nested_ver.get("status", "confirmed"),
            "note": raw.get("note") or nested_ver.get("note") or str(path),
            "confirmed_at": nested_ver.get("confirmed_at"),
        }
        return checks, task_info, version_info

    raise ValueError(f"规则文件 {path} 格式错误：顶层必须是检查数组 `[...]` 或包含 `checks` 数组的对象")


def _load_sqlite_rules(
    *,
    sqlite_path: pathlib.Path,
    rule_version_id: Optional[int],
    task_id: Optional[int],
    allow_draft: bool,
) -> tuple[list[dict], dict, dict]:
    if not sqlite_path.is_file():
        raise ValueError(f"SQLite 数据库不存在：{sqlite_path}")

    conn = sqlite3.connect(str(sqlite_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if rule_version_id is not None:
            v_row = conn.execute("SELECT * FROM rule_versions WHERE id = ?", (rule_version_id,)).fetchone()
            if v_row is None:
                raise ValueError(f"SQLite 中不存在规则版本 ID={rule_version_id}")
            t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (v_row["task_id"],)).fetchone()
            if t_row is None:
                raise ValueError(f"规则版本 ID={rule_version_id} 关联的任务不存在")
        else:
            t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if t_row is None:
                raise ValueError(f"SQLite 中不存在任务 ID={task_id}")
            if allow_draft:
                v_row = conn.execute(
                    "SELECT * FROM rule_versions WHERE task_id = ? ORDER BY version DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
            else:
                v_row = conn.execute(
                    "SELECT * FROM rule_versions WHERE task_id = ? AND status = 'confirmed' ORDER BY version DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if v_row is None:
                    any_row = conn.execute(
                        "SELECT version, status FROM rule_versions WHERE task_id = ? ORDER BY version DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if any_row is not None:
                        raise ValueError(
                            f"任务 #{task_id} 仅有未确认的草稿规则（最新 v{any_row['version']}）；"
                            "请先在工作台确认版本，或显式传入 --allow-draft"
                        )
                    raise ValueError(f"任务 #{task_id} 还没有任何规则版本")
    finally:
        conn.close()

    version_info = dict(v_row)
    if version_info["status"] != "confirmed" and not allow_draft:
        raise ValueError(
            f"规则版本 v{version_info['version']}（ID={version_info['id']}）状态为草稿（draft）；"
            "CI 门禁默认只执行人工确认过的规则版本，如确需执行草稿请传入 --allow-draft"
        )
    checks = json.loads(version_info.pop("checks_json"))
    task_info = dict(t_row)
    task_info.pop("endpoints_json", None)
    return checks, task_info, version_info


def _write_report_file(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def cmd_verify(args: argparse.Namespace) -> int:
    load_env()
    try:
        fail_codes = _parse_fail_on(args.fail_on)
    except ValueError as e:
        print(f"[verify] 参数错误：{e}", file=sys.stderr)
        return 2

    if args.timeout <= 0 or args.timeout > 60:
        print("[verify] 参数错误：--timeout 必须在 (0, 60] 秒之间", file=sys.stderr)
        return 2

    try:
        if args.demo_rules:
            checks = demo_data.demo_checks()
            task_info: dict = {
                "id": "demo",
                "name": demo_data.DEMO_TASK["name"],
                "requirement": demo_data.DEMO_TASK["requirement"],
            }
            version_info: dict = {
                "id": "demo-v1",
                "version": 1,
                "status": "confirmed",
                "note": demo_data.demo_rule_note(),
                "confirmed_at": None,
            }
        elif args.rules is not None:
            checks, task_info, version_info = _load_json_rules(args.rules)
        else:
            target_db = args.db if args.db is not None else db_path()
            checks, task_info, version_info = _load_sqlite_rules(
                sqlite_path=target_db,
                rule_version_id=args.rule_version_id,
                task_id=args.task_id,
                allow_draft=args.allow_draft,
            )
    except ValueError as e:
        print(f"[verify] 规则加载失败：{e}", file=sys.stderr)
        return 2

    if not checks:
        print("[verify] 规则校验失败：checks 不能为空数组", file=sys.stderr)
        return 2

    validation_errors = validate_checks(checks)
    if validation_errors:
        print("[verify] 规则校验失败：", file=sys.stderr)
        for err in validation_errors:
            print(f"  - {err}", file=sys.stderr)
        return 2

    raw_baseline = args.baseline or task_info.get("baseline_url")
    raw_candidate = args.candidate or task_info.get("candidate_url")
    if not raw_baseline or not raw_candidate:
        print("[verify] 参数错误：必须提供 --baseline 与 --candidate 目标环境地址", file=sys.stderr)
        return 2

    allow_remote = bool(args.allow_remote or task_info.get("allow_remote"))
    allow_remote_writes = bool(args.allow_remote_writes or task_info.get("allow_remote_writes"))

    try:
        baseline_url = validate_target_url(raw_baseline, "修改前")
        candidate_url = validate_target_url(raw_candidate, "修改后")
        check_run_safety(
            baseline_url,
            candidate_url,
            checks,
            allow_remote=allow_remote,
            allow_remote_writes=allow_remote_writes,
        )
    except SafetyError as e:
        print(f"[verify] {e}", file=sys.stderr)
        return 2

    task_meta = {
        "id": task_info.get("id", "cli"),
        "name": args.task_name or task_info.get("name") or "CLI 无头改动验收",
        "requirement": args.requirement if args.requirement is not None else task_info.get("requirement", ""),
        "baseline_url": baseline_url,
        "candidate_url": candidate_url,
    }

    started_at = now_iso()
    outcome = run_checks(checks, baseline_url, candidate_url, default_timeout=args.timeout)
    finished_at = now_iso()
    result = outcome["payload"]

    run_meta = {
        "id": "headless",
        "status": outcome["status"],
        "started_at": started_at,
        "finished_at": finished_at,
        "error": None,
    }

    if args.output is not None:
        if args.output.suffix.lower() == ".json":
            report_obj = build_json_report(task_meta, version_info, run_meta, result)
            _write_report_file(args.output, json.dumps(report_obj, ensure_ascii=False, indent=2))
        else:
            md_text = render_markdown(task_meta, version_info, run_meta, result)
            _write_report_file(args.output, md_text)

    if args.json_output is not None:
        report_obj = build_json_report(task_meta, version_info, run_meta, result)
        _write_report_file(args.json_output, json.dumps(report_obj, ensure_ascii=False, indent=2))

    summary = result["summary"]
    counts = summary["counts"]
    print(f"[verify] 结论：{summary['headline']}")
    breakdown = " · ".join(
        f"{CLASSIFICATION_LABELS[code]} {counts[code]}"
        for code in CLASSIFICATION_ORDER
        if counts.get(code)
    )
    if breakdown:
        print(f"[verify] 统计（共 {summary['total_checks']} 项）：{breakdown}")

    if not args.quiet:
        for cmp in result["checks"]:
            cl = cmp["classification"]
            print(f"  [{cl['label']}] {cmp['id']} — {cmp['name']}")

    if args.output is not None:
        print(f"[verify] 报告已写入：{args.output}")
    if args.json_output is not None:
        print(f"[verify] JSON 报告已写入：{args.json_output}")

    triggered = [code for code in CLASSIFICATION_ORDER if code in fail_codes and counts.get(code, 0) > 0]
    if triggered:
        labels = "、".join(f"{CLASSIFICATION_LABELS[c]}({counts[c]})" for c in triggered)
        print(f"[verify] 门禁未通过：命中阻断项 {labels}", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not args_list:
        args = parser.parse_args(["serve"])
    else:
        args = parser.parse_args(args_list)

    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "demo":
        return run_demo_stack(
            baseline_port_override=args.baseline_port,
            candidate_port_override=args.candidate_port,
            verifier_port_override=args.port,
        )
    if args.command == "verify":
        return cmd_verify(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
