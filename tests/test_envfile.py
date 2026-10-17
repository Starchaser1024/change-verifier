"""`.env` 加载器：解析规则、真实环境变量优先、缺失文件、Key 来源标记。

这些测试不依赖任何真实 Key，也不会去连模型接口。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from verifier import envfile, llm

KEY_VARS = ("LLM_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY")
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _clean(monkeypatch):
    for name in KEY_VARS + ("LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name in KEY_VARS:
        envfile.LOADED_FROM_ENV_FILE.discard(name)


def test_parse_basic_forms():
    text = "\n".join(
        [
            "# 注释行",
            "",
            "LLM_MODEL=gpt-4o-mini",
            "export LLM_BASE_URL=https://example.com/v1",
            "  LLM_API_KEY = 'sk-quoted'  ",
            'DOUBLE="line1\\nline2\\tend"',
            "INLINE=value # 行内注释",
            "TAB_INLINE=value\t# 也是注释",
            "EMPTY=",
            "NO_EQUALS_LINE",
            "=no_key",
        ]
    )
    values = envfile.parse_env(text)
    assert values["LLM_MODEL"] == "gpt-4o-mini"
    assert values["LLM_BASE_URL"] == "https://example.com/v1"
    assert values["LLM_API_KEY"] == "sk-quoted"
    assert values["DOUBLE"] == "line1\nline2\tend"
    assert values["INLINE"] == "value"
    assert values["TAB_INLINE"] == "value"
    assert values["EMPTY"] == ""
    assert "NO_EQUALS_LINE" not in values
    assert "" not in values


def test_bom_and_crlf(tmp_path):
    p = tmp_path / ".env"
    p.write_bytes("\ufeffLLM_MODEL=x\r\nLLM_API_KEY=y\r\n".encode("utf-8"))
    assert envfile.parse_env(p.read_text(encoding="utf-8-sig")) == {
        "LLM_MODEL": "x",
        "LLM_API_KEY": "y",
    }


def test_load_env_does_not_override_real_env(tmp_path, monkeypatch):
    _clean(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=from-file\nLLM_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "from-real-env")

    applied = envfile.load_env(env_file)

    assert os.environ["LLM_API_KEY"] == "from-file"
    assert os.environ["LLM_MODEL"] == "from-real-env", "真实环境变量必须优先于 .env"
    assert "LLM_API_KEY" in applied and "LLM_MODEL" not in applied
    assert envfile.was_loaded("LLM_API_KEY") is True
    assert envfile.was_loaded("LLM_MODEL") is False


def test_load_env_missing_file_is_silent(tmp_path):
    assert envfile.load_env(tmp_path / "不存在的目录" / ".env") == {}


def test_parse_env_file_is_read_only(tmp_path, monkeypatch):
    _clean(monkeypatch)
    p = tmp_path / ".env"
    p.write_text("LLM_API_KEY=sk-x\nLLM_MODEL=m\n", encoding="utf-8")

    values = envfile.parse_env_file(p)

    assert values == {"LLM_API_KEY": "sk-x", "LLM_MODEL": "m"}
    assert "LLM_API_KEY" not in os.environ, "parse_env_file 不应写入 os.environ"
    assert envfile.was_loaded("LLM_API_KEY") is False


def test_env_file_path_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv(envfile.ENV_FILE_OVERRIDE_VAR, str(tmp_path / "custom.env"))
    assert envfile.env_file_path() == tmp_path / "custom.env"
    assert envfile.env_file_path(tmp_path / "explicit.env") == tmp_path / "explicit.env"
    monkeypatch.delenv(envfile.ENV_FILE_OVERRIDE_VAR)
    assert envfile.env_file_path() == envfile.DEFAULT_ENV_FILE


def test_llm_status_reports_dotenv_source(tmp_path, monkeypatch):
    _clean(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=sk-from-dotenv\nLLM_BASE_URL=https://example.com/v1\n", encoding="utf-8")
    envfile.load_env(env_file)

    status = llm.llm_status()
    assert status["available"] is True
    assert status["base_url"] == "https://example.com/v1"
    assert status["source"] == ".env"


def test_llm_status_reports_process_env_source(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
    assert llm.llm_status()["source"] == "环境变量"


def test_llm_status_source_none_when_unconfigured(monkeypatch):
    _clean(monkeypatch)
    assert llm.llm_status()["source"] is None
    assert llm.llm_status()["available"] is False


def test_importing_app_loads_dotenv(tmp_path):
    """约定：Key 只放 .env —— 无论怎么启动（如 uvicorn verifier.main:app），导入应用就会读到。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=sk-from-dotenv\nLLM_BASE_URL=https://example.com/v1\n", encoding="utf-8")
    env = {
        **os.environ,
        "VERIFIER_ENV_FILE": str(env_file),
        "VERIFIER_DB": str(tmp_path / "verifier.db"),
        "PYTHONIOENCODING": "utf-8",
    }
    for name in KEY_VARS + ("LLM_BASE_URL", "LLM_MODEL"):
        env.pop(name, None)

    code = (
        "import json;"
        "from verifier.main import app;"  # noqa: F401 - 触发应用模块导入
        "from verifier.llm import llm_status;"
        "print(json.dumps(llm_status(), ensure_ascii=True))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=120
    )

    assert proc.returncode == 0, f"子进程失败：{proc.stderr}"
    status = json.loads(proc.stdout.strip().splitlines()[-1])
    assert status["available"] is True
    assert status["source"] == ".env"
    assert status["base_url"] == "https://example.com/v1"
