# -
通过多个agent'协作抓取考研官网信息并整理出来

---

## 仓库内容

| 目录 | 项目 | 说明 |
|---|---|---|
| `multiagent-kaoyan-scraper/` | 考研信息多智能体抓取 | 多个 agent 协作抓取考研官网信息并整理 |
| `novel_crawl_recommend/` + `novel_search_agent/` | **项目一：爬虫驱动的小说检索推荐系统** | 见下 |
| `agent-lab/` + `weather-agent-web/` | **项目二：智能体交互模型与天气智能体** | 见下 |

---

## 项目一：爬虫驱动的小说检索推荐系统

`novel_crawl_recommend`（采集 + 检索 + 推荐 + Web）与 `novel_search_agent`（对话式检索 Agent）。

输入一个小说类型（武侠 / 言情 / 科幻…）或一句自然语言描述（"女主重生复仇古言"），
返回带排名、评分、书名、作者、简介、来源链接的榜单。

- **采集**：Tavily 搜索 + requests/bs4 静态解析 + Playwright 动态渲染，
  起点中文网 / 微信读书 / 国家图书馆三个来源互补元数据
- **存储**：元数据进 SQLite，简介 embedding 进 FAISS（BAAI/bge-m3，1024 维）
- **检索**：关键词 / 语义 / 混合三种模式，`/api/agent` 支持自然语言意图理解后推荐
- **规模**：8062 本，22 个分类
- 详细说明见 `novel_crawl_recommend/README.md`

## 项目二：智能体交互模型与天气智能体

`agent-lab`（可复用 agent 骨架）与 `weather-agent-web`（天气助手实例）。

把"用户 ↔ 智能体 ↔ 工具"的交互过程定成一套状态机 + 一套事件协议，
前端通过 SSE 实时把每个阶段画出来。

- **状态机**：`idle → observe → think → act → done / failed / stopped`
- **工具解耦**：工具以注册表方式接入，新增工具不改主循环
- **实时可视化**：事件流通过 SSE 推送到页面
- 详细说明见 `agent-lab/README.md`

---

## 运行

各项目目录下均有独立的 `README.md` 与 `requirements.txt`。
密钥相关配置参考各自的 `.env.example`，`.env` 不提交进仓库。
