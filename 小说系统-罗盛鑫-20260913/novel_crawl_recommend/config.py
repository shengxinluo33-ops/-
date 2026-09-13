"""配置与日志：整个项目只有这一处读环境变量、只有这一处建 logger。

需要的环境变量（写在 .env 里，见 .env.example）：

    TAVILY_API_KEY      分类榜单搜索要用（工具3 那套）
    EMBEDDING_API_KEY   简介向量化要用；不填则降级为本地哈希向量（见 store.py）
    EMBEDDING_BASE_URL  embedding 服务地址，必须和 EMBEDDING_MODEL 成套配置。
                        留空会退回 BASE_URL（聊天模型的地址），那两家通常不是
                        一个渠道，报 model_not_found 就先查这里。
    EMBEDDING_MODEL     默认 BAAI/bge-m3（硅基流动）

    API_KEY / BASE_URL / MODEL_ID
                        intent.py 的"大白话 → 检索条件"要用。不填也能跑，
                        意图解析会自动降级成规则匹配（见 intent.py 模块 docstring）。

    INTENT_TIMEOUT      意图解析的超时秒数，默认 8。实测这个模型最慢到过 20 秒，
                        卡死它是为了让页面别一直转圈，超时就走规则兜底。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("NOVEL_CRAWL_DATA") or (ROOT / "data")).resolve()
LOG_DIR = ROOT / "logs"

DB_PATH = DATA_DIR / "novel.db"
INDEX_PATH = DATA_DIR / "novels.faiss"
META_PATH = DATA_DIR / "novels_faiss_meta.json"
FAILED_LOG = LOG_DIR / "failed_urls.log"

for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_BASE_URL = ""

# 聊天模型（intent.py 的意图解析用）。跟 embedding 通常是两家服务商，
# 所以各自有独立的 BASE_URL/MODEL，别混着用。
DEFAULT_BASE_URL = ""
DEFAULT_MODEL_ID = ""

# 爬虫礼貌设置：每次请求之间随机睡 1~3 秒，别把人家站点打疼。
# 跑大批量（几百次请求）之前用环境变量把延时调大，单请求礼貌挡不住总量风控。
CRAWL_MIN_DELAY = float(os.getenv("CRAWL_MIN_DELAY", 1.0))
CRAWL_MAX_DELAY = float(os.getenv("CRAWL_MAX_DELAY", 3.0))
CRAWL_TIMEOUT = 20          # 单个页面超时（秒）
CRAWL_MIN_TEXT = 120        # 静态抓取正文少于这个字数，才值得动用浏览器
CRAWL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def env(name: str, default: str = "") -> str:
    """读环境变量，去掉首尾空白。"""
    return (os.getenv(name) or default).strip()


# ---------------------------------------------------------------- 日志

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """拿一个同时写控制台和 logs/crawl.log 的 logger。

    重复调用不会重复挂 handler —— 这是日志里"同一行打印四遍"的经典坑，
    靠 logger.handlers 判空挡掉。
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))

    file_handler = logging.FileHandler(LOG_DIR / "crawl.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


def log_failed_url(url: str, reason: str) -> None:
    """抓失败的 URL 一行一条落盘，供重试和排查用。"""
    with FAILED_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{_now()} | {url} | {reason}\n")


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    log = get_logger("config")
    log.info("项目根目录：%s", ROOT)
    log.info("数据库：%s", DB_PATH)
    log.info("向量索引：%s", INDEX_PATH)
    for key in ("TAVILY_API_KEY", "EMBEDDING_API_KEY"):
        log.info("%s：%s", key, "已设置" if env(key) else "（空）")
    log.info("EMBEDDING_BASE_URL=%s", env("EMBEDDING_BASE_URL") or "（空，会退回 BASE_URL）")
    log.info("EMBEDDING_MODEL=%s", env("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL))
