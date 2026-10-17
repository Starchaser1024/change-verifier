"""启动 candidate（修改后，默认含 5 个演示缺陷）服务：http://127.0.0.1:8621

端口可用 DEMO_CANDIDATE_PORT 覆盖。
缺陷可用 DEMO_DEFECTS 指定子集（变异评估用），例如：
    $env:DEMO_DEFECTS="A,C"   # 只保留缺陷 A 和 C
    $env:DEMO_DEFECTS="none"  # 修好全部演示缺陷
不设置时与演示行为一致（全部 5 个缺陷开启）。
以上变量也可以写在项目根的 `.env` 里（真实环境变量优先）。
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from demo.services import DEFAULT_CANDIDATE_DEFECTS, create_app, parse_defects  # noqa: E402
from verifier.envfile import load_env  # noqa: E402

if __name__ == "__main__":
    load_env()
    port = int(os.environ.get("DEMO_CANDIDATE_PORT", "8621"))
    defects = parse_defects(os.environ.get("DEMO_DEFECTS"))
    active = sorted(defects) if defects is not None else sorted(DEFAULT_CANDIDATE_DEFECTS)
    print(f"[run_candidate] 生效的演示缺陷：{', '.join(active) if active else '无'}")
    uvicorn.run(create_app("candidate", defects=defects), host="127.0.0.1", port=port, log_level="info")
