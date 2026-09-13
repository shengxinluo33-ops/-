"""【工具2】CodeInterpreter：跑 Python、装依赖、报错自动补一枪。

两个动作：

    run  执行一段代码，把 stdout / stderr / 返回码原样带回来
    pip  pip install 一个包

"自动修复"做两件事，都不猜模型的意图：

    1. 确定性修复：stderr 里出现 `No module named 'X'` 时，自动 pip 装 X 再跑一次。
       这是唯一能 100% 判定的报错，不需要模型参与。
    2. 把完整 traceback 交回模型：其余错误（语法错、KeyError、逻辑错）由模型
       自己看报错改代码。工具只负责"把错误说清楚"，不替模型编代码。

注意：代码解释器天然能读写任意路径 —— 这是它的能力边界，靠沙箱限制不住。
README 里写明了这一点，别在不可信的多租户环境里直接暴露它。
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.tools import tool

from config import SANDBOX_ROOT, get_logger
from tools.common import fail, ok, truncate

log = get_logger("tool.code")

ACTIONS = ("run", "pip")
_DEFAULT_TIMEOUT = 120
_MAX_TIMEOUT = 600
_MODULE_RE = re.compile(r"No module named ['\"]([^'\"]+)['\"]")


@tool
def code_interpreter(action: str, code: str = "", package: str = "",
                     timeout: int = _DEFAULT_TIMEOUT, autofix: bool = True) -> str:
    """执行 Python 代码或安装 pip 依赖。执行环境就是本项目所在的 Python 解释器。

    Args:
        action: run=执行代码；pip=安装包
        code: action=run 时要执行的完整 Python 代码，可以有多行、可以 import 任意已安装的库
        package: action=pip 时的包名，可带版本号，例如 "requests==2.32.5"
        timeout: 超时秒数，默认 120，最大 600
        autofix: 报 No module named 时是否自动 pip 安装后重试一次，默认开启
    """
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return fail("CodeInterpreter", f"未知动作 {action!r}，可选：{'、'.join(ACTIONS)}")

    if action == "pip":
        if not package.strip():
            return fail("CodeInterpreter pip", "package 不能为空")
        return _pip(package.strip(), timeout)

    if not code.strip():
        return fail("CodeInterpreter run", "code 不能为空")

    timeout = max(5, min(int(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
    result = _run(code, timeout)

    if result["returncode"] == 0 or not autofix:
        return _format(result)

    missing = _MODULE_RE.search(result["stderr"])
    if not missing:
        return _format(result)

    module = missing.group(1).split(".")[0]
    log.info("检测到缺模块 %s，自动安装后重试", module)
    install = _pip(module, timeout)
    if install.startswith("[ERROR]"):
        return _format(result) + f"\n[自动修复] 安装 {module} 失败，未重试：{install}"

    retried = _run(code, timeout)
    return f"[自动修复] 已安装 {module} 并重跑一次。\n" + _format(retried)


# ---------------------------------------------------------------- 内部


def _run(code: str, timeout: int) -> dict:
    """把代码写到临时文件再起子进程跑，好处是 traceback 里行号对得上。"""
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8",
                                     dir=SANDBOX_ROOT, delete=False) as f:
        f.write(code)
        script = Path(f.name)

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(SANDBOX_ROOT),
        )
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"执行超时（{timeout} 秒），已终止"}
    except Exception as e:  # noqa: BLE001
        return {"returncode": -1, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}
    finally:
        script.unlink(missing_ok=True)


def _pip(package: str, timeout: int) -> str:
    timeout = max(30, min(int(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
    log.info("pip install %s", package)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", package],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return fail("pip install", f"{timeout} 秒超时")

    if proc.returncode == 0:
        return ok(f"已安装 {package}")
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
    return fail("pip install", f"{package} 安装失败：\n" + "\n".join(tail))


def _format(result: dict) -> str:
    """把执行结果拼成人能读、模型也能读的一段话。"""
    parts = [f"返回码：{result['returncode']}"]
    if result["stdout"]:
        parts.append("标准输出：\n" + truncate(result["stdout"].rstrip(), label="stdout"))
    if result["stderr"]:
        parts.append("标准错误：\n" + truncate(result["stderr"].rstrip(), label="stderr"))
    if result["returncode"] == 0 and not result["stdout"] and not result["stderr"]:
        parts.append("（代码执行成功，没有任何输出。需要结果请显式 print）")
    prefix = "[OK]" if result["returncode"] == 0 else "[ERROR]"
    return prefix + " 代码执行结束\n" + "\n".join(parts)


if __name__ == "__main__":
    print(code_interpreter.invoke({"action": "run", "code": "print(1 + 1)"}))
    print(code_interpreter.invoke({"action": "run", "code": "1/0"}))
