"""一键启动完整演示环境：baseline(8620) + candidate(8621) + 验收器网页(8622)。

用法：
    python run_demo.py
    # 或使用统一 CLI：
    python -m verifier demo
然后浏览器打开 http://127.0.0.1:8622

配置：启动前会读取项目根的 `.env`（如端口、DEMO_DEFECTS），各子服务启动时**各自**再读一遍
`.env`（所以 LLM_API_KEY 的来源会如实显示为 `.env`）。真实环境变量优先于 `.env`。
模板见 `.env.example`。
端口也可直接用环境变量覆盖：DEMO_BASELINE_PORT / DEMO_CANDIDATE_PORT / VERIFIER_PORT。
按 Ctrl+C 停止全部服务。
"""

from __future__ import annotations

from verifier.cli import port_is_available, run_demo_stack, wait_ready  # noqa: F401


def main() -> int:
    return run_demo_stack()


if __name__ == "__main__":
    raise SystemExit(main())
