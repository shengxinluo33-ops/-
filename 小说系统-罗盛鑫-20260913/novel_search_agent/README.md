# novel_search_agent

用 **LangGraph + LangChain** 搭的一个会自己用工具的 Agent，挂载 6 个工具。
所有落盘操作都限制在 `sandbox/` 沙箱内，跑不跑得起来一条命令就能自测。

---

## 1. 一分钟跑起来

```bash
cd novel_search_agent
pip install -r requirements.txt
playwright install chromium        # 工具4的动态抓取要用；只做静态抓取可跳过

cp .env.example .env               # 填 API_KEY（必填）和 TAVILY_API_KEY（可选）

python selftest.py                 # 先自测，10 个用例
python agent.py                    # 再进交互模式
```

`python agent.py "帮我搜一下 langgraph 的 ReAct 用法"` 可以一句话模式跑完就退。

---

## 2. 6 个工具

| # | 工具名 | 干什么 | 动作参数 |
|---|---|---|---|
| 1 | `file_system` | 沙箱内读写文件 | `list / read / write / append / mkdir / delete` |
| 2 | `code_interpreter` | 执行 Python、装包 | `run / pip` |
| 3 | `web_search` | Tavily 联网搜索 | `query`, `max_results`, `search_depth` |
| 4 | `web_scrape` | 抓网页正文 | `auto / static / dynamic` |
| 5 | `db_query` | SQLite + FAISS | `tables / schema / insert / query / execute / vector_add / vector_search / vector_info` |
| 6 | `git` | 版本管理 | `init / add / commit / log / status / diff / auto` |

全部用 `@tool` 装饰器定义，schema 由类型注解 + docstring 自动生成，
不用手抄 JSON —— 改函数签名就等于改契约。

---

## 3. 目录结构

```
novel_search_agent/
├── agent.py              # 主入口：LangGraph ReAct 图 + CLI
├── selftest.py           # 自测：10 个用例，含离线跑图
├── config.py             # 环境变量与日志（唯一读 env 的地方）
├── tools/
│   ├── __init__.py       # 6 个工具的汇总出口 ALL_TOOLS
│   ├── common.py         # 沙箱路径校验 + 输出截断
│   ├── filesystem.py     # 工具1
│   ├── code_interpreter.py  # 工具2
│   ├── web_search.py     # 工具3
│   ├── web_scrape.py     # 工具4
│   ├── db_query.py       # 工具5
│   └── git_tool.py       # 工具6
├── sandbox/              # 工具能碰的唯一目录（自动建）
├── data/                 # agent.db 与 faiss.index（自动建）
├── logs/                 # agent.log 与修复记录
└── requirements.txt
```

每个工具文件末尾都有 `if __name__ == "__main__"` 的小演示，用模块方式跑：

```bash
python -m tools.filesystem        # 不能写成 python tools/filesystem.py，见第 7 节
python -m tools.db_query
```

---

## 4. 主 Agent 的图长什么样

`agent.py` 用 `create_react_agent` 生成，结构就是：

```
start → agent ──要调工具──▶ tools ──┐
          ▲                          │
          └──────────────────────────┘
          │ 不再要工具
          ▼
         end
```

`agent` 节点 = 一次 LLM 调用；`tools` 节点 = 执行它选中的工具。
循环由边条件控制（返回里有没有 `tool_calls`），代码里没有手写 while。

多轮任务推荐链路：
`web_search` / `web_scrape` 找资料 → `file_system` 落盘 → `db_query` 入库检索 → `git(action=auto)` 提交一版。

---

## 5. 自测覆盖了什么

`python selftest.py` 跑 10 个用例，判定 **PASS / SKIP / FAIL**，退出码非 0 表示有 FAIL：

```
PASS | 工具注册            6 个工具都注册了、都有 schema
PASS | FileSystem         写→追加→读→列→删，外加 /etc/passwd 越界被拦
PASS | CodeInterpreter    打印输出、ZeroDivisionError 捕获、pip、缺模块自动修复
SKIP | WebSearch          没配 TAVILY_API_KEY 时跳过（但会验证它优雅报错）
PASS | WebScrape 静态      requests + bs4
PASS | WebScrape 动态      Playwright 真浏览器
PASS | DBQuery-SQLite      建表→插入→查询→看结构
PASS | DBQuery-FAISS       灌 2 段文本，检索第一条必须命中正确那段
PASS | Git                 auto 提交两次，log 里有两条，工作区干净
PASS | LangGraph 图        用假模型把图转一圈，确认工具真的被执行
```

自测用 `tempfile.mkdtemp()` 建临时沙箱和数据目录（靠 `NOVEL_AGENT_SANDBOX` /
`NOVEL_AGENT_DATA` 两个环境变量重定向），**不会污染正式的 `sandbox/` 和 `data/`**。
`python selftest.py git` 可以只跑名字含 git 的用例。

---

## 6. 几个必须知道的限制

- **CodeInterpreter 能读写任意路径。** 这是代码解释器的本质，不是 bug。
  沙箱只约束 FileSystem/Git/DBQuery 三个工具。别在不可信的多租户环境直接暴露它。
- **FAISS 的向量化有两档。** 配了 `EMBEDDING_API_KEY` 走在线模型（真正的语义检索，
  现用硅基流动的 `BAAI/bge-m3`）；没配就降级为本地 `HashingEmbeddings`
  —— 本质是加权的词袋哈希，只能抓字面重合，用来跑通流程和自测，
  别拿它当语义检索的效果基准。两档的实测差别见 `logs/fixlog.md` 第 8 条。
- **换 embedding 模型后索引要重建。** 维度不一致时 `vector_add` 会检测到并自动重建，
  但 `vector_search` 会直接报维度不匹配并提示原因（详见 `logs/fixlog.md` 第 7 条）。
- **`auto` 抓取模式**先静态后动态，正文少于 200 字才动用浏览器，省时间。

---

## 7. 实现过程中踩过的坑

完整记录见 **`logs/fixlog.md`**，其中两条值得一提：

1. 装 `langgraph` 时 pip 把 `langchain-core` 升到了 1.6.1，直接和课程在用的
   `langchain 0.3.27` / `langchain-community 0.3.28` 冲突。已回退到
   `core 0.3.75 + langgraph 0.3.34`，`pip check` 现在只剩与你课程无关的老冲突。
2. `python tools/filesystem.py` 会报 `No module named 'config'`。
   因为包内模块互相 import 依赖项目根目录在 `sys.path` 上。
   统一改用 `python -m tools.filesystem`（`agent.py` 自己插了 `sys.path`，所以能直接跑）。
