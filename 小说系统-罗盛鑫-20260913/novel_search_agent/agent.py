"""LangGraph 主 Agent 入口：把 6 个工具挂到一个 ReAct 图上。

图的结构（create_react_agent 生成的就是它，画出来是这样）：

        ┌─────────┐
        │  start  │
        └────┬────┘
             ▼
        ┌─────────┐   要调工具   ┌────────┐
        │  agent  ├────────────▶│ tools  │
        └────┬────┘◀────────────┴────────┘
             │ 不再要工具
             ▼
        ┌─────────┐
        │   end   │
        └─────────┘

"agent" 节点就是一次 LLM 调用，"tools" 节点执行它选中的工具。
循环由 LangGraph 的边条件控制：模型返回里带 tool_calls 就走 tools，
否则直接结束。我们自己不需要写 while 循环。

跑法：
    python agent.py                      # 交互式，一句一句问
    python agent.py "帮我搜一下 X 并总结"   # 一句话模式，跑完就退
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 保证 python agent.py 也能 import

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from config import DEFAULT_BASE_URL, DEFAULT_MODEL_ID, SANDBOX_ROOT, env, get_logger, require
from tools import ALL_TOOLS, TOOL_NAMES

log = get_logger("agent")

SYSTEM_PROMPT = f"""你是一个会用工具的中文助手，名字叫 novel-search-agent。

可用的 6 个工具：
1. file_system      读写沙箱里的文件（沙箱根目录：{SANDBOX_ROOT}）
2. code_interpreter 执行 Python 代码、pip 装包，报错会自动补一次
3. web_search       Tavily 联网搜索
4. web_scrape       抓取网页正文（静态页用 static，渲染型页面用 dynamic）
5. db_query         SQLite 存取 + FAISS 语义检索
6. git              版本管理，action=auto 可以一条命令完成提交

干活的四条规矩：
· 先想清楚要用哪个工具，再动手；能一步做完的别绕两步。
· 工具报错是正常信号，看清错误改参数重试，不要一遍遍重复同样的调用。
· 涉及文件路径一律用相对路径，沙箱外的路径工具会直接拒绝。
· 最终回答用中文，说明你用了哪些工具、得到了什么；拿不到就直说拿不到，不要编。

多步任务的推荐顺序：web_search/web_scrape 找资料 → file_system 落盘
→ db_query 入库做检索 → git(action=auto) 提交一版。"""


def build_llm(temperature: float = 0.0) -> ChatOpenAI:
    """建聊天模型。Key 缺了才在这里问，import 阶段绝不打断。"""
    api_key = require("API_KEY", "聊天模型 API_KEY：")
    return ChatOpenAI(
        model=env("MODEL_ID", DEFAULT_MODEL_ID),
        api_key=api_key,
        base_url=env("BASE_URL", DEFAULT_BASE_URL),
        temperature=temperature,
        timeout=120,
        max_retries=2,
    )


def build_agent(temperature: float = 0.0):
    """编译出可调用的 LangGraph 图。"""
    llm = build_llm(temperature)
    agent = create_react_agent(llm, tools=ALL_TOOLS, prompt=SYSTEM_PROMPT)
    log.info("ReAct 图已编译，挂载工具：%s", "、".join(TOOL_NAMES))
    return agent


# ---------------------------------------------------------------- 调用


def _describe(msg: BaseMessage) -> str | None:
    """把一条消息转成给人看的一行。返回 None 表示不用打印。"""
    if isinstance(msg, HumanMessage):
        return None
    if isinstance(msg, AIMessage):
        if msg.tool_calls:
            calls = "，".join(
                f"{c['name']}({', '.join(f'{k}={v!r}' for k, v in c['args'].items())})"
                for c in msg.tool_calls
            )
            return f"▶ 调用工具：{calls}"
        return f"\n答：{msg.content}" if msg.content else None
    if isinstance(msg, ToolMessage):
        text = str(msg.content)
        short = text if len(text) <= 400 else text[:400] + f" …（共 {len(text)} 字）"
        return f"◀ 工具返回：{short}"
    return None


def run(agent, user_text: str, history: list[BaseMessage]) -> list[BaseMessage]:
    """跑一轮，打印过程，把更新后的历史还回来。"""
    history = list(history) + [HumanMessage(content=user_text)]
    state = agent.invoke({"messages": history})

    for msg in state["messages"][len(history):]:
        line = _describe(msg)
        if line:
            print(line)

    log.info("本轮结束，历史共 %d 条消息", len(state["messages"]))
    return state["messages"]


def main() -> None:
    args = [a for a in sys.argv[1:] if a.strip()]
    agent = build_agent()
    print(f"novel-search-agent 就绪，挂载 {len(TOOL_NAMES)} 个工具：{'、'.join(TOOL_NAMES)}")
    print(f"沙箱目录：{SANDBOX_ROOT}")

    history: list[BaseMessage] = []

    if args:                                   # 一句话模式
        run(agent, " ".join(args), history)
        return

    print("输入问题开始对话，输入 exit / quit 退出，输入 clear 清空历史。\n")
    while True:
        try:
            user = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if user.lower() in ("exit", "quit"):
            print("再见。")
            break
        if user.lower() == "clear":
            history = []
            print("（历史已清空）")
            continue
        if not user:
            continue
        history = run(agent, user, history)


if __name__ == "__main__":
    main()
