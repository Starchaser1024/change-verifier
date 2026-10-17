"""读取项目根目录的 `.env` 文件（自己实现，不引第三方依赖）。

用途：把 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`、端口等本地配置写进项目根的
`.env`，不必每次开终端都敲一遍环境变量。`.env` 已在 `.gitignore` 里，不会进仓库。

支持的语法（够用的最小集合）：
- `KEY=VALUE`；空行与 `#` 开头的行忽略；支持 `export KEY=VALUE`；
- 值可用单引号或双引号包起来（双引号内支持 \\n \\t \\" 转义）；未加引号时「空白 + #」之后算行内注释；
- 文件允许 UTF-8 BOM；文件不存在/路径非法时静默返回空字典。

优先级：**真实环境变量优先**。默认不覆盖已存在的环境变量（需要强制覆盖时传 `override=True`）。
这样「临时 export 一个值」永远能盖住 `.env`，符合直觉。
"""

from __future__ import annotations

import os
import pathlib
from typing import Optional, Union

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / ".env"
ENV_FILE_OVERRIDE_VAR = "VERIFIER_ENV_FILE"

# 记录本进程中「由 .env 设置」的变量名，用于 /api/llm/status 告诉用户 Key 是从哪来的
LOADED_FROM_ENV_FILE: set[str] = set()


def parse_env(text: str) -> dict[str, str]:
    """把 .env 文本解析成字典（不做任何环境变量写入）。"""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
        else:
            for marker in (" #", "\t#"):
                cut = value.find(marker)
                if cut != -1:
                    value = value[:cut].rstrip()
        values[key] = value
    return values


def env_file_path(path: Optional[Union[str, pathlib.Path]] = None) -> pathlib.Path:
    """确定要读取的 .env 路径：显式参数 > 环境变量 VERIFIER_ENV_FILE > 项目根 .env。"""
    if path is not None:
        return pathlib.Path(path)
    override = os.environ.get(ENV_FILE_OVERRIDE_VAR)
    if override and override.strip():
        return pathlib.Path(override.strip())
    return DEFAULT_ENV_FILE


def parse_env_file(path: Optional[Union[str, pathlib.Path]] = None) -> dict[str, str]:
    """只读解析 .env（**不**写入 os.environ）。文件不存在时返回空字典。"""
    env_path = env_file_path(path)
    try:
        if not env_path.is_file():
            return {}
        return parse_env(env_path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


def load_env(
    path: Optional[Union[str, pathlib.Path]] = None, override: bool = False
) -> dict[str, str]:
    """把 .env 读进 os.environ，返回本次实际写入的键值。

    - 默认不覆盖已存在的环境变量（真实环境变量优先）；
    - 文件不存在时返回空字典，不抛错。
    """
    applied: dict[str, str] = {}
    for key, value in parse_env_file(path).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        LOADED_FROM_ENV_FILE.add(key)
        applied[key] = value
    return applied


def was_loaded(name: str) -> bool:
    """当前进程里该变量的值是否来自 .env（而不是系统环境变量）。"""
    return name in LOADED_FROM_ENV_FILE
