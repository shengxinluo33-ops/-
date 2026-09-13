"""工具之间共用的两件小事：路径落在沙箱内、结果别太长。

为什么要有这个文件：

    · 路径检查散落在 6 个工具里，迟早有一个漏掉。收在一处，
      写新工具时只要调 resolve_in_sandbox 就自动受保护。
    · Agent 把工具输出原样塞回对话，一份 5 万字的抓取结果能直接把
      上下文撑爆。所有返回大文本的工具统一在出口截一刀。
"""

from __future__ import annotations

from pathlib import Path

from config import SANDBOX_ROOT

MAX_CHARS = 6000


class OutsideSandbox(ValueError):
    """路径越出沙箱。当成数据交回模型，让它换个路径重试。"""


def resolve_in_sandbox(path: str | Path, *, must_exist: bool = False) -> Path:
    """把任意路径解析成沙箱内的绝对路径；越界或不存在就抛错。

    用 resolve() 而不是拼接字符串，这样 `../..`、`a/../../etc` 这类
    穿越写法会被先展开再比较，绕不过去。
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = SANDBOX_ROOT / candidate
    resolved = candidate.resolve()

    if resolved != SANDBOX_ROOT and SANDBOX_ROOT not in resolved.parents:
        raise OutsideSandbox(
            f"路径越出沙箱：{resolved}\n允许的范围只有 {SANDBOX_ROOT}，请改用相对路径。"
        )
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"沙箱内找不到：{resolved}")
    return resolved


def truncate(text: str, limit: int = MAX_CHARS, label: str = "输出") -> str:
    """超长就截断并说明被截了多少，避免模型以为自己拿到了全文。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（{label}过长，已截断到 {limit} 字，原文共 {len(text)} 字）"


def ok(text: str) -> str:
    """成功返回的统一前缀，方便模型和人一眼看出成败。"""
    return f"[OK] {text}"


def fail(what: str, err: BaseException | str) -> str:
    """失败也返回字符串，不抛异常 —— 把错误当数据交回模型让它自愈。"""
    detail = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
    return f"[ERROR] {what}失败 —— {detail}"
