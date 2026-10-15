"""启动 baseline（修改前）演示服务：http://127.0.0.1:8620（可用 DEMO_BASELINE_PORT 覆盖）"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from demo.services import create_app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("DEMO_BASELINE_PORT", "8620"))
    uvicorn.run(create_app("baseline"), host="127.0.0.1", port=port, log_level="info")
