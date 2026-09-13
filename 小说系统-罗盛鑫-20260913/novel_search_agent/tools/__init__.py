"""6 个工具的汇总出口。

工具在这里汇总一次，agent.py 只 import ALL_TOOLS —— 加新工具时
改这一个文件就够了，不用去主流程里再挂一遍。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from tools.code_interpreter import code_interpreter
from tools.db_query import db_query
from tools.filesystem import file_system
from tools.git_tool import git
from tools.web_scrape import web_scrape
from tools.web_search import web_search

# 顺序就是给模型看的工具顺序，把最常用的文件系统和代码解释器放前面
ALL_TOOLS: list[BaseTool] = [
    file_system,
    code_interpreter,
    web_search,
    web_scrape,
    db_query,
    git,
]

TOOL_NAMES: list[str] = [t.name for t in ALL_TOOLS]


def get_tools(names: list[str] | None = None) -> list[BaseTool]:
    """按名字取工具；不传就全给。"""
    if not names:
        return list(ALL_TOOLS)
    wanted = {n.strip().lower() for n in names}
    picked = [t for t in ALL_TOOLS if t.name in wanted]
    unknown = wanted - {t.name for t in picked}
    if unknown:
        raise ValueError(f"没有这些工具：{'、'.join(sorted(unknown))}；可用：{'、'.join(TOOL_NAMES)}")
    return picked


if __name__ == "__main__":
    for t in ALL_TOOLS:
        print(f"{t.name:18s} {t.description.splitlines()[0]}")
