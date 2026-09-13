"""配置与日志：整个项目只有这一处读环境变量、只有这一处建 logger。

设计上守两条规矩：

    · 不在 import 时索要密钥    配置和自测都要能在没有 Key 的机器上运行，
                                只有在真正要建 LLM 的时候才去拿 Key。
    · 一切落盘都在沙箱里        FileSystem / Git / DBQuery 三个会写磁盘的工具
                                都只能碰 SANDBOX_ROOT 以内的路径，避免 Agent
                                自作主张改到课程目录上去。

需要的环境变量（写在 .env 里即可，见 .env.example）：

    API_KEY           聊天模型密钥，必填
    BASE_URL          OpenAI 兼容端点，默认 https://api.agnes-ai.cn/v1
    MODEL_ID          模型名，默认 agnes-2.5-flash
    TAVILY_API_KEY    WebSearch 要用，不填则该工具报错但不影响其他 5 个
    EMBEDDING_API_KEY FAISS 向量化要用；不填时自动降级为本地哈希向量（见 db_query.py）
    EMBEDDING_BASE_URL embedding 的服务地址，必须和 EMBEDDING_MODEL 成套配置
    EMBEDDING_MODEL   默认 BAAI/bge-m3（硅基流动）
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------- 路径

ROOT = Path(__file__).resolve().parent
SANDBOX_ROOT = Path(os.getenv("NOVEL_AGENT_SANDBOX") or (ROOT / "sandbox")).resolve()
LOG_DIR = ROOT / "logs"
# 自测时换成临时目录，避免测试用例把正式的库和向量索引弄脏
DATA_DIR = Path(os.getenv("NOVEL_AGENT_DATA") or (ROOT / "data")).resolve()

for _d in (SANDBOX_ROOT, LOG_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------- 模型

DEFAULT_BASE_URL = "https://api.agnes-ai.cn/v1"
DEFAULT_MODEL_ID = "agnes-2.5-flash"
# 硅基流动上的中文向量模型，注册只要国内手机号、有免费额度。
# 别再用 nvidia/nemotron-3-embed-1b 了：模型本身没问题，但 NVIDIA 注册
# 要海外手机号收 OTP，国内收不到（2026-09-02 实测）。详见 .env.example。
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"


def env(name: str, default: str = "") -> str:
    """读环境变量，去掉首尾空白。"""
    return (os.getenv(name) or default).strip()


def require(name: str, prompt: str) -> str:
    """取值，取不到就用 getpass 问一次，并回写环境变量供本次进程复用。"""
    value = env(name)
    if value:
        return value
    from getpass import getpass

    value = getpass(prompt).strip()
    if not value:
        raise RuntimeError(f"{name} 不能为空")
    os.environ[name] = value
    return value


def llm_ready() -> bool:
    """聊天模型的三个变量是否齐了。自测用它决定要不要跑联网的用例。"""
    return bool(env("API_KEY") and env("BASE_URL", DEFAULT_BASE_URL) and env("MODEL_ID", DEFAULT_MODEL_ID))


# ---------------------------------------------------------------- 日志

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """拿一个同时写控制台和 logs/agent.log 的 logger。

    重复调用不会重复挂 handler —— 这是日志文件里出现"同一行打印四遍"的
    经典坑，靠 logger.handlers 判空挡掉。
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))

    file_handler = logging.FileHandler(LOG_DIR / "agent.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


if __name__ == "__main__":
    log = get_logger("config")
    log.info("项目根目录：%s", ROOT)
    log.info("沙箱目录：%s", SANDBOX_ROOT)
    log.info("BASE_URL=%s", env("BASE_URL", DEFAULT_BASE_URL))
    log.info("MODEL_ID=%s", env("MODEL_ID", DEFAULT_MODEL_ID))
    for key in ("API_KEY", "TAVILY_API_KEY", "EMBEDDING_API_KEY"):
        log.info("%s：%s", key, "已设置" if env(key) else "（空）")
    log.info("聊天模型可用：%s", llm_ready())
