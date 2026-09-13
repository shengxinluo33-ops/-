"""【工具1】FileSystem：在沙箱里读写、新建、遍历、追加、删除。

一个工具六个动作，靠 action 参数分派。为什么不拆成六个 @tool：
任务要求"6 个工具"，文件系统算一个，拆开就把工具列表撑到 11 个，
模型挑工具时的干扰项反而变多。

安全边界：所有路径都会过 common.resolve_in_sandbox，
沙箱外的路径一律拒绝，错误以字符串返回给模型而不是抛异常。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from langchain_core.tools import tool

from config import SANDBOX_ROOT, get_logger
from tools.common import fail, ok, resolve_in_sandbox, truncate

log = get_logger("tool.filesystem")

ACTIONS = ("list", "read", "write", "append", "mkdir", "delete")


@tool
def file_system(action: str, path: str = ".", content: str = "", encoding: str = "utf-8") -> str:
    """本地文件与文件夹操作。所有路径只能在沙箱内，不能访问沙箱以外的目录。

    Args:
        action: 要做的动作。list=列出目录内容；read=读取文本；write=覆盖写入；
            append=在文件末尾追加；mkdir=新建文件夹；delete=删除文件或文件夹
        path: 相对沙箱的路径，例如 "notes/ch1.txt"；写 "." 表示沙箱根目录
        content: write / append 时要写入的文本，其他动作不用填
        encoding: 读写文本用的编码，默认 utf-8
    """
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return fail("FileSystem", f"未知动作 {action!r}，可选：{'、'.join(ACTIONS)}")

    log.debug("file_system action=%s path=%s", action, path)
    try:
        target = resolve_in_sandbox(path)
    except Exception as e:  # noqa: BLE001 越界/不存在都当数据交回模型
        return fail("FileSystem 解析路径", e)

    handler = {
        "list": _list,
        "read": _read,
        "write": _write,
        "append": _append,
        "mkdir": _mkdir,
        "delete": _delete,
    }[action]
    try:
        return handler(target, content, encoding)
    except Exception as e:  # noqa: BLE001 工具内部错误也要当数据
        log.warning("file_system %s 失败：%s", action, e)
        return fail(f"FileSystem {action}", e)


# ---------------------------------------------------------------- 各动作


def _list(target: Path, _content: str, _encoding: str) -> str:
    if not target.exists():
        return fail("FileSystem list", f"目录不存在：{target}")
    if target.is_file():
        return ok(f"{target.relative_to(SANDBOX_ROOT)} 是文件，大小 {target.stat().st_size} 字节")

    lines = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        mark = "目录" if item.is_dir() else "文件"
        size = "" if item.is_dir() else f"，{item.stat().st_size} 字节"
        lines.append(f"  [{mark}] {item.name}{size}")
    head = f"{target.relative_to(SANDBOX_ROOT) or '.'} 下共 {len(lines)} 项：\n"
    return ok(truncate(head + "\n".join(lines), label="目录列表"))


def _read(target: Path, _content: str, encoding: str) -> str:
    if not target.exists():
        return fail("FileSystem read", f"文件不存在：{target}")
    if target.is_dir():
        return fail("FileSystem read", f"{target} 是目录，请用 action=list")
    text = target.read_text(encoding=encoding, errors="replace")
    return ok(f"{target.name}（{len(text)} 字）：\n{truncate(text, label='文件内容')}")


def _write(target: Path, content: str, encoding: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding=encoding)
    return ok(f"已写入 {target.relative_to(SANDBOX_ROOT)}，共 {len(content)} 字")


def _append(target: Path, content: str, encoding: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding=encoding) as f:
        f.write(content)
    return ok(f"已追加到 {target.relative_to(SANDBOX_ROOT)}，追加 {len(content)} 字，文件现共 {target.stat().st_size} 字节")


def _mkdir(target: Path, _content: str, _encoding: str) -> str:
    target.mkdir(parents=True, exist_ok=True)
    return ok(f"目录就绪：{target.relative_to(SANDBOX_ROOT)}")


def _delete(target: Path, _content: str, _encoding: str) -> str:
    if not target.exists():
        return fail("FileSystem delete", f"不存在：{target}")
    if target.resolve() == SANDBOX_ROOT:
        return fail("FileSystem delete", "不允许删除沙箱根目录")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return ok(f"已删除 {target.relative_to(SANDBOX_ROOT)}")


if __name__ == "__main__":
    # 不联网、不依赖 Key 的小自测：建目录 → 写 → 读 → 追加 → 列 → 删
    print(file_system.invoke({"action": "mkdir", "path": "_demo"}))
    print(file_system.invoke({"action": "write", "path": "_demo/a.txt", "content": "第一章 初见\n"}))
    print(file_system.invoke({"action": "append", "path": "_demo/a.txt", "content": "第二章 再见\n"}))
    print(file_system.invoke({"action": "read", "path": "_demo/a.txt"}))
    print(file_system.invoke({"action": "list", "path": "_demo"}))
    print(file_system.invoke({"action": "delete", "path": "_demo"}))
    print(file_system.invoke({"action": "read", "path": "/etc/passwd"}))
