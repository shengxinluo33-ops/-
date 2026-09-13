"""自测：逐个调用 6 个工具，再离线跑一遍 LangGraph 图。

跑法：
    python selftest.py            # 全部用例
    python selftest.py filesystem # 只跑名字里含 filesystem 的用例

判定：
    PASS  断言通过
    SKIP  缺 Key / 没网络，用例跳过（不算失败，退出码仍是 0）
    FAIL  真的坏了，退出码 1

自测用独立的临时沙箱和数据目录（见文件最上面的 os.environ），
不会动 sandbox/ 和 data/ 里的正式内容。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="novel-selftest-"))
os.environ["NOVEL_AGENT_SANDBOX"] = str(_TMP / "sandbox")
os.environ["NOVEL_AGENT_DATA"] = str(_TMP / "data")

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.language_models.chat_models import SimpleChatModel

from config import SANDBOX_ROOT, env, get_logger
from tools import ALL_TOOLS, TOOL_NAMES
from tools.code_interpreter import code_interpreter
from tools.db_query import db_query
from tools.filesystem import file_system
from tools.git_tool import git
from tools.web_scrape import web_scrape
from tools.web_search import web_search

log = get_logger("selftest")

PASS, SKIP, FAIL = "PASS", "SKIP", "FAIL"


class Skipped(Exception):
    """环境不具备条件，跳过这个用例。"""


# ---------------------------------------------------------------- 用例


def test_tool_registry() -> str:
    """6 个工具都注册上了，且都有名字、描述、参数 schema。"""
    assert len(ALL_TOOLS) == 6, f"工具数量是 {len(ALL_TOOLS)}，应该是 6"
    names = [t.name for t in ALL_TOOLS]
    assert len(set(names)) == 6, f"工具名有重复：{names}"
    for t in ALL_TOOLS:
        assert t.name and t.description, f"{t.name} 缺描述"
        schema = t.args_schema if hasattr(t, "args_schema") else None
        assert schema is not None, f"{t.name} 没有参数 schema"
    return f"6 个工具：{'、'.join(TOOL_NAMES)}"


def test_filesystem() -> str:
    """建目录 → 写 → 追加 → 读 → 列 → 删，外加沙箱越界必须被拒。"""
    out = file_system.invoke({"action": "write", "path": "demo/a.txt", "content": "第一章\n"})
    assert out.startswith("[OK]"), out

    out = file_system.invoke({"action": "append", "path": "demo/a.txt", "content": "第二章\n"})
    assert out.startswith("[OK]"), out

    out = file_system.invoke({"action": "read", "path": "demo/a.txt"})
    assert "第一章" in out and "第二章" in out, out

    out = file_system.invoke({"action": "list", "path": "demo"})
    assert "a.txt" in out, out

    out = file_system.invoke({"action": "read", "path": "/etc/passwd"})
    assert out.startswith("[ERROR]") and "沙箱" in out, f"越界路径没被拦住：{out}"

    out = file_system.invoke({"action": "delete", "path": "demo"})
    assert out.startswith("[OK]"), out
    assert not (SANDBOX_ROOT / "demo").exists(), "删除后目录还在"
    return "读写追加列举删除 + 越界拦截 均正常"


def test_code_interpreter() -> str:
    """正常输出、异常捕获、pip 三件事。"""
    out = code_interpreter.invoke({"action": "run", "code": "print(6 * 7)"})
    assert "42" in out, out

    out = code_interpreter.invoke({"action": "run", "code": "1 / 0"})
    assert out.startswith("[ERROR]") and "ZeroDivisionError" in out, f"异常没被捕获：{out}"

    # 装一个已经装好的包，验证 pip 通道通，又不触发大下载
    out = code_interpreter.invoke({"action": "pip", "package": "tiktoken"})
    assert out.startswith("[OK]") or "already satisfied" in out, out

    # 缺模块时要能识别出来并尝试装（这个包在 PyPI 上不存在，安装必然失败，
    # 但走一遍就能证明"检测缺模块 → 触发安装 → 装不上就如实汇报"这条链是通的）
    out = code_interpreter.invoke({"action": "run", "code": "import zzz_no_such_pkg_9z"})
    assert "No module named" in out, out
    assert "[自动修复] 安装 zzz_no_such_pkg_9z" in out, f"没有触发自动修复：{out[:300]}"
    return "代码执行、异常捕获、pip 安装、缺模块自动修复 均正常"


def test_web_search() -> str:
    """有 Key 就真搜一次，没 Key 必须优雅报错而不是抛异常。"""
    if not env("TAVILY_API_KEY"):
        out = web_search.invoke({"query": "langgraph"})
        assert out.startswith("[ERROR]") and "TAVILY_API_KEY" in out, out
        raise Skipped("未配置 TAVILY_API_KEY（已验证缺 Key 时优雅报错）")

    out = web_search.invoke({"query": "langgraph 教程", "max_results": 3})
    assert out.startswith("[OK]"), out
    assert "链接：" in out, f"结果里没有 url：{out[:300]}"
    return "Tavily 搜索返回正常"


def test_web_scrape() -> str:
    """抓 example.com 的静态正文。"""
    try:
        out = web_scrape.invoke({"url": "https://example.com", "action": "static"})
    except Exception as e:  # noqa: BLE001
        raise Skipped(f"网络不可达：{type(e).__name__}") from e
    if out.startswith("[ERROR]"):
        raise Skipped(f"抓不到（网络或站点问题）：{out[:160]}")
    assert "Example Domain" in out, f"正文不对：{out[:300]}"
    return "静态页面抓取正常"


def test_web_scrape_dynamic() -> str:
    """Playwright 动态抓取。浏览器没装就跳过，不算失败。"""
    try:
        out = web_scrape.invoke({"url": "https://example.com", "action": "dynamic"})
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
            raise Skipped("未安装 Playwright 浏览器，执行 playwright install chromium 后可用") from e
        raise Skipped(f"动态抓取不可用：{type(e).__name__}: {e}") from e
    if out.startswith("[ERROR]"):
        raise Skipped(f"动态抓取失败：{out[:160]}")
    assert "抓取方式：dynamic" in out, out
    return "Playwright 动态抓取正常（浏览器已就绪）"


def test_db_query_sqlite() -> str:
    """建表 → 插入 → 查询 → 看表结构。"""
    db_query.invoke({"action": "schema",
                     "sql": "DROP TABLE IF EXISTS novel"})
    out = db_query.invoke({"action": "schema",
                           "sql": "CREATE TABLE novel(id INTEGER PRIMARY KEY, title TEXT, chapter INTEGER)"})
    assert out.startswith("[OK]"), out

    out = db_query.invoke({"action": "insert",
                           "sql": "INSERT INTO novel(title, chapter) VALUES('测试书名', 1)",
                           "table": "novel"})
    assert out.startswith("[OK]") and "测试书名" in out, out

    out = db_query.invoke({"action": "query", "sql": "SELECT * FROM novel"})
    assert out.startswith("[OK]") and "测试书名" in out, out

    out = db_query.invoke({"action": "tables"})
    assert "novel" in out and "CREATE TABLE" in out, out
    return "SQLite 建表/插入/查询/看结构 均正常"


def test_db_query_faiss() -> str:
    """灌两段文本进向量库，再用问句检索，第一条必须命中正确那段。"""
    out = db_query.invoke({"action": "vector_add",
                           "text": "报到需要录取通知书和身份证原件。\n---\n宿舍安排在东区五号楼。"})
    assert out.startswith("[OK]"), out

    out = db_query.invoke({"action": "vector_info"})
    assert "2 个片段" in out, out

    out = db_query.invoke({"action": "vector_search", "text": "报到要带什么材料", "top_k": 2})
    assert out.startswith("[OK]"), out
    first = out.split("[2]")[0]
    assert "通知书" in first, f"第一条没命中报到那段：{out[:400]}"

    embedder_note = "在线模型" if env("EMBEDDING_API_KEY") else "本地哈希降级向量"
    return f"FAISS 写入与检索正常（{embedder_note}）"


def test_git() -> str:
    """auto 一次提交，改一下再提交，日志里要有两条。"""
    (SANDBOX_ROOT / "repo").mkdir(parents=True, exist_ok=True)
    (SANDBOX_ROOT / "repo" / "main.py").write_text("print('v1')\n", encoding="utf-8")

    out = git.invoke({"action": "auto", "path": "repo", "message": "feat: 第一版"})
    assert "[OK]" in out, out

    (SANDBOX_ROOT / "repo" / "main.py").write_text("print('v2')\n", encoding="utf-8")
    out = git.invoke({"action": "auto", "path": "repo", "message": "feat: 第二版"})
    assert "[OK]" in out, out

    out = git.invoke({"action": "log", "path": "repo"})
    assert "第一版" in out and "第二版" in out, out

    out = git.invoke({"action": "status", "path": "repo"})
    assert "干净" in out, f"提交后工作区应干净：{out}"
    return "git init/add/commit/log 均正常"


def test_git_outer_repo() -> str:
    """沙箱被套在一个外层 Git 仓库里时，不能把外层仓库整个提交掉。

    这是部署到 /workspace 下才暴露出来的坑：sandbox/ 自己没有 .git，
    git 就沿目录树向上找到了 /workspace 这个课程仓库，auto 一下把
    49 个文件（含 20MB 的 rustup、core dump、课件目录）全提交了。
    """
    import shutil
    import subprocess

    outer = SANDBOX_ROOT.parent          # 临时目录本身，即 sandbox 的上一级
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True,
                   capture_output=True, text=True)
    (outer / "outer_file.txt").write_text("外层仓库的文件\n", encoding="utf-8")
    try:
        out = git.invoke({"action": "auto", "path": ".", "message": "test: 不该提交到外层"})
        assert "[OK]" in out, out

        # 1. 外层仓库里不能出现任何提交
        proc = subprocess.run(["git", "log", "--oneline"], cwd=outer,
                              capture_output=True, text=True)
        assert proc.returncode != 0 or not proc.stdout.strip(), \
            f"外层仓库被提交了：{proc.stdout.strip()}"

        # 2. 仓库根必须落在沙箱里，而不是外层
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=SANDBOX_ROOT,
            capture_output=True, text=True,
        ).stdout.strip()
        assert Path(top).resolve() == SANDBOX_ROOT.resolve(), f"仓库根跑到沙箱外了：{top}"
    finally:
        shutil.rmtree(outer / ".git", ignore_errors=True)
        (outer / "outer_file.txt").unlink(missing_ok=True)
    return "沙箱套在外层仓库里时只在沙箱内提交，外层仓库无提交"


# ---------------------------------------------------------------- 图

class _ScriptedModel(SimpleChatModel):
    """离线假模型：第一轮点名调 file_system，第二轮给结论。

    用它可以在没有 API_KEY 的情况下验证"图真的会转起来"：
    agent 节点产出 tool_calls → tools 节点执行 → 回到 agent → 结束。
    """

    script: list[BaseMessage] = []

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def _call(self, messages, stop=None, run_manager=None, **kwargs) -> str:
        raise NotImplementedError

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        step = self.script.pop(0) if self.script else AIMessage(content="自测收工")
        return ChatResult(generations=[ChatGeneration(message=step)])

    def bind_tools(self, tools, **kwargs):  # noqa: D102 create_react_agent 会调它
        return self


def test_graph_offline() -> str:
    """用假模型把 ReAct 图跑一圈，验证工具确实被图调用执行了。"""
    from langgraph.prebuilt import create_react_agent

    (SANDBOX_ROOT / "graph").mkdir(parents=True, exist_ok=True)
    (SANDBOX_ROOT / "graph" / "note.txt").write_text("hello graph\n", encoding="utf-8")

    model = _ScriptedModel()
    model.script = [
        AIMessage(content="", tool_calls=[{
            "name": "file_system",
            "args": {"action": "read", "path": "graph/note.txt"},
            "id": "call_selftest_1",
        }]),
        AIMessage(content="我读到了文件内容。"),
    ]
    graph = create_react_agent(model, tools=ALL_TOOLS, prompt="自测用系统提示")
    state = graph.invoke({"messages": [HumanMessage(content="读一下 note.txt")]})

    kinds = [type(m).__name__ for m in state["messages"]]
    assert "ToolMessage" in kinds, f"图没有执行任何工具，消息序列：{kinds}"
    tool_msg = [m for m in state["messages"] if type(m).__name__ == "ToolMessage"][0]
    assert "hello graph" in str(tool_msg.content), f"工具执行结果不对：{tool_msg.content}"
    return "LangGraph 图按 agent→tools→agent 转了一圈，工具确实被执行"


# ---------------------------------------------------------------- 执行

TESTS = [
    ("工具注册", test_tool_registry),
    ("FileSystem", test_filesystem),
    ("CodeInterpreter", test_code_interpreter),
    ("WebSearch", test_web_search),
    ("WebScrape 静态", test_web_scrape),
    ("WebScrape 动态", test_web_scrape_dynamic),
    ("DBQuery-SQLite", test_db_query_sqlite),
    ("DBQuery-FAISS", test_db_query_faiss),
    ("Git", test_git),
    ("Git-外层仓库越界防护", test_git_outer_repo),
    ("LangGraph 图", test_graph_offline),
]


def main() -> int:
    keyword = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    results = []

    print(f"自测开始，临时沙箱：{SANDBOX_ROOT}\n")
    for name, fn in TESTS:
        if keyword and keyword not in name.lower():
            continue
        try:
            status, detail = PASS, fn()
        except Skipped as e:
            status, detail = SKIP, str(e)
        except AssertionError as e:
            status, detail = FAIL, str(e) or "断言失败"
        except Exception as e:  # noqa: BLE001 用例自己炸了也要记下来
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        results.append((name, status, detail))
        print(f"{status:4s} | {name:16s} | {detail.splitlines()[0] if detail else ''}")
        log.info("%s %s %s", status, name, detail)

    failed = [r for r in results if r[1] == FAIL]
    skipped = [r for r in results if r[1] == SKIP]
    print(f"\n合计 {len(results)} 项：通过 {len(results) - len(failed) - len(skipped)}，"
          f"跳过 {len(skipped)}，失败 {len(failed)}")
    print(f"临时沙箱保留在：{_TMP}")
    if failed:
        print("\n失败详情：")
        for name, _, detail in failed:
            print(f"· {name}：{detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
