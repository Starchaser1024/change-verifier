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
import sqlite3
import subprocess
import sys
from typing import Optional, Sequence

from . import __version__, demo_data
from .classification import CLASSIFICATION_LABELS, CLASSIFICATION_ORDER
from .db import db_path, now_iso
from .envfile import load_env, parse_env_file
from .executor import DEFAULT_TIMEOUT_S
from .models import validate_checks
from .process_manager import (  # noqa: F401 - 保持向后兼容导出
    ManagedService,
    ManagedServiceError,
    ManagedServiceSpec,
    allocate_free_ports,
    managed_dual_services,
    parse_env_overrides as _parse_env_overrides,
    port_is_available,
    read_log_tail as _read_log_tail,
    render_cmd_placeholders as _render_cmd_placeholders,
    run_managed_demo_stack,
    split_command_template as _split_command_template,
    terminate_processes as _terminate_processes,
    wait_ready,
)
from .report import build_json_report, render_markdown
from .runner import run_checks
from .safety import SafetyError, check_run_safety, validate_target_url

DEFAULT_FAIL_ON = ("regression", "not_met", "unverifiable")


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
    p_demo.add_argument("--ready-timeout", type=float, default=20.0, help="单个服务启动就绪超时秒数（默认 20）")

    # ---------------- verify ----------------
    p_verify = subparsers.add_parser(
        "verify",
        help="无头执行双环境验收检查（CI 门禁模式）：零阻断项返回 0，发现回归等阻断项返回 1",
    )
    src = p_verify.add_mutually_exclusive_group(required=True)
    src.add_argument("--rules", type=pathlib.Path, help='JSON 规则文件路径（支持检查数组 `[...]` 或 `{"checks": [...]}`）')
    src.add_argument("--demo-rules", action="store_true", help="使用内置的 10 条已确认演示规则")
    src.add_argument("--rule-version-id", type=int, help="从 SQLite 加载指定规则版本 ID")
    src.add_argument("--task-id", type=int, help="从 SQLite 加载指定任务的最新已确认规则版本")

    p_verify.add_argument("--baseline", default=None, help="修改前（baseline）环境 URL，如 http://127.0.0.1:8620")
    p_verify.add_argument("--candidate", default=None, help="修改后（candidate）环境 URL，如 http://127.0.0.1:8621")
    p_verify.add_argument("--baseline-cmd", default=None, help="托管启动 baseline 本地服务的命令模板；支持 {port}/{host}/{env}/{temp_dir}")
    p_verify.add_argument("--candidate-cmd", default=None, help="托管启动 candidate 本地服务的命令模板；支持 {port}/{host}/{env}/{temp_dir}")
    p_verify.add_argument("--baseline-cwd", type=pathlib.Path, default=None, help="托管 baseline 服务进程的工作目录")
    p_verify.add_argument("--candidate-cwd", type=pathlib.Path, default=None, help="托管 candidate 服务进程的工作目录")
    p_verify.add_argument("--baseline-env", action="append", default=[], metavar="KEY=VALUE", help="向托管 baseline 进程注入额外环境变量（可重复指定）")
    p_verify.add_argument("--candidate-env", action="append", default=[], metavar="KEY=VALUE", help="向托管 candidate 进程注入额外环境变量（可重复指定）")
    p_verify.add_argument("--ready-path", default="/healthz", help="托管服务就绪探针路径（默认 /healthz）")
    p_verify.add_argument("--ready-timeout", type=float, default=20.0, help="托管服务启动就绪超时秒数（默认 20）")
    p_verify.add_argument("--db", type=pathlib.Path, default=None, help="SQLite 数据库路径（默认读取 VERIFIER_DB）")
    p_verify.add_argument("--allow-draft", action="store_true", help="允许执行 SQLite 中尚未确认（draft）的规则版本")
    p_verify.add_argument("--allow-remote", action="store_true", help="允许访问非本机（非 127.0.0.1/localhost）目标环境")
    p_verify.add_argument("--allow-remote-writes", action="store_true", help="允许向远程环境发送写入类请求（需同时开启 --allow-remote）")
    p_verify.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help=f"单步请求默认超时秒数（默认 {DEFAULT_TIMEOUT_S:g}）")
    p_verify.add_argument(
        "--fail-on",
        default=",".join(DEFAULT_FAIL_ON),
        help=f"触发退出码 1 的结果分类（逗号分隔，默认 '{','.join(DEFAULT_FAIL_ON)}'；仅拦截回归传 regression，仅出报告传 none）",
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
    ready_timeout: float = 20.0,
) -> int:
    if ready_timeout <= 0:
        print("[run_demo] 参数错误：--ready-timeout 必须大于 0", file=sys.stderr)
        return 2

    file_env = parse_env_file()
    if file_env:
        print(f"[run_demo] 发现 .env，包含：{', '.join(sorted(file_env))}（由各服务自行读取）")

    def setting(name: str, default: str) -> str:
        return os.environ.get(name) or file_env.get(name) or default

    baseline_port = baseline_port_override or int(setting("DEMO_BASELINE_PORT", "8620"))
    candidate_port = candidate_port_override or int(setting("DEMO_CANDIDATE_PORT", "8621"))
    verifier_port = verifier_port_override or int(setting("VERIFIER_PORT", "8622"))
    return run_managed_demo_stack(
        root_dir=pathlib.Path(__file__).resolve().parents[1],
        baseline_port=baseline_port,
        candidate_port=candidate_port,
        verifier_port=verifier_port,
        ready_timeout=ready_timeout,
        port_available_fn=port_is_available,
        wait_ready_fn=wait_ready,
        popen_fn=subprocess.Popen,
    )


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
        task_info = {"id": "cli", "name": f"CLI 验收（{path.name}）", "requirement": ""}
        version_info = {"id": "file", "version": 1, "status": "confirmed", "note": str(path), "confirmed_at": None}
        return raw, task_info, version_info

    if isinstance(raw, dict) and isinstance(raw.get("checks"), list):
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
        return raw["checks"], task_info, version_info

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
    if args.ready_timeout <= 0 or args.ready_timeout > 300:
        print("[verify] 参数错误：--ready-timeout 必须在 (0, 300] 秒之间", file=sys.stderr)
        return 2
    if not args.ready_path or not args.ready_path.startswith("/"):
        print("[verify] 参数错误：--ready-path 必须以 / 开头", file=sys.stderr)
        return 2
    if args.baseline and args.baseline_cmd:
        print("[verify] 参数错误：--baseline 与 --baseline-cmd 不能同时指定", file=sys.stderr)
        return 2
    if args.candidate and args.candidate_cmd:
        print("[verify] 参数错误：--candidate 与 --candidate-cmd 不能同时指定", file=sys.stderr)
        return 2
    if (args.baseline_cwd or args.baseline_env) and not args.baseline_cmd:
        print("[verify] 参数错误：--baseline-cwd / --baseline-env 需配合 --baseline-cmd 使用", file=sys.stderr)
        return 2
    if (args.candidate_cwd or args.candidate_env) and not args.candidate_cmd:
        print("[verify] 参数错误：--candidate-cwd / --candidate-env 需配合 --candidate-cmd 使用", file=sys.stderr)
        return 2
    for label, cwd in (("baseline", args.baseline_cwd), ("candidate", args.candidate_cwd)):
        if cwd is not None and not cwd.is_dir():
            print(f"[verify] 参数错误：{label} 工作目录不存在：{cwd}", file=sys.stderr)
            return 2

    try:
        baseline_tokens = _split_command_template(args.baseline_cmd) if args.baseline_cmd is not None else None
        candidate_tokens = _split_command_template(args.candidate_cmd) if args.candidate_cmd is not None else None
        baseline_env_pairs = _parse_env_overrides(args.baseline_env, "--baseline-env")
        candidate_env_pairs = _parse_env_overrides(args.candidate_env, "--candidate-env")
    except ValueError as e:
        print(f"[verify] 参数错误：{e}", file=sys.stderr)
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

    need_ports = int(baseline_tokens is not None) + int(candidate_tokens is not None)
    port_iter = iter(allocate_free_ports(need_ports) if need_ports else [])
    baseline_port = next(port_iter) if baseline_tokens is not None else None
    candidate_port = next(port_iter) if candidate_tokens is not None else None

    raw_baseline = f"http://127.0.0.1:{baseline_port}" if baseline_port is not None else (args.baseline or task_info.get("baseline_url"))
    raw_candidate = f"http://127.0.0.1:{candidate_port}" if candidate_port is not None else (args.candidate or task_info.get("candidate_url"))
    if not raw_baseline or not raw_candidate:
        print("[verify] 参数错误：必须提供 --baseline（或 --baseline-cmd）与 --candidate（或 --candidate-cmd）", file=sys.stderr)
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

    specs: list[ManagedServiceSpec] = []
    if baseline_tokens is not None and baseline_port is not None:
        specs.append(ManagedServiceSpec("baseline", baseline_tokens, baseline_port, baseline_url, args.baseline_cwd, baseline_env_pairs))
    if candidate_tokens is not None and candidate_port is not None:
        specs.append(ManagedServiceSpec("candidate", candidate_tokens, candidate_port, candidate_url, args.candidate_cwd, candidate_env_pairs))

    try:
        with managed_dual_services(specs, ready_path=args.ready_path, ready_timeout=args.ready_timeout) as started:
            if started and not args.quiet:
                print(f"[verify] 托管服务已就绪：{', '.join(f'{s.env_label}={s.base_url}' for s in started)}")

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
    except ManagedServiceError as e:
        print(f"[verify] {e.message}", file=sys.stderr)
        if e.log_tail:
            print(f"[verify] {e.env_label} 服务日志：\n{e.log_tail}", file=sys.stderr)
        return 1

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
            _write_report_file(args.output, render_markdown(task_meta, version_info, run_meta, result))

    if args.json_output is not None:
        report_obj = build_json_report(task_meta, version_info, run_meta, result)
        _write_report_file(args.json_output, json.dumps(report_obj, ensure_ascii=False, indent=2))

    summary = result["summary"]
    counts = summary["counts"]
    print(f"[verify] 结论：{summary['headline']}")
    breakdown = " · ".join(f"{CLASSIFICATION_LABELS[code]} {counts[code]}" for code in CLASSIFICATION_ORDER if counts.get(code))
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
    args = parser.parse_args(["serve"] if not args_list else args_list)

    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "demo":
        return run_demo_stack(
            baseline_port_override=args.baseline_port,
            candidate_port_override=args.candidate_port,
            verifier_port_override=args.port,
            ready_timeout=args.ready_timeout,
        )
    if args.command == "verify":
        return cmd_verify(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
