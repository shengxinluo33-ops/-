# 考研择校爬虫 · 双 Agent 协作版

一个用 **DeepSeek + Kimi 两个大模型联动** 完成考研分数线爬取的项目：

- **DeepSeek** 当「开发工程师」，负责生成爬虫配置；
- **Kimi** 当「评审」，负责挑配置的毛病；
- 两者通过 API 来回多轮，最终产出配置，交给通用爬虫框架抓数据存 CSV。

## 目录结构

```
multiagent-kaoyan-scraper/
├── config.py            # API Key 配置（从环境变量读取）
├── agents.py            # 封装 DeepSeek / Kimi 两个 Agent
├── orchestrator.py      # 主循环：写 → 审 → 改 → 落地执行
├── generic_scraper.py   # 通用爬虫框架（填 URL + CSS 选择器即可）
├── example_config.json  # 爬虫配置示例
├── requirements.txt
└── README.md
```

## 一、安装依赖

```powershell
pip install -r requirements.txt
```

## 二、设置 API Key（从环境变量读，别写进代码）

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."   # platform.deepseek.com 申请
$env:KIMI_API_KEY      = "sk-..."   # platform.moonshot.ai 申请
```

> 永久保存：把这两行加到「系统属性 → 环境变量」里。

## 三、两种使用方式

### 方式 A：双 Agent 协作（完整流程）

```powershell
python orchestrator.py
```

流程：DeepSeek 生成配置 → Kimi 评审 → DeepSeek 按意见改 → 评审通过后自动抓取存 CSV。

### 方式 B：只用爬虫框架（手写配置，不走模型）

你自己写好 `config.json`（参照 `example_config.json`），然后：

```powershell
python generic_scraper.py config.json
```

## 四、爬虫配置格式

```json
{
  "url": "目标网页地址",
  "container": "每条记录的 CSS 选择器",
  "fields": {"字段名": "该字段的 CSS 选择器（相对 container）"},
  "output": "输出 CSV 文件名"
}
```

## 五、重要提醒

1. **模型生成的选择器是「猜测」**：DeepSeek 没见过目标网页的真实 HTML，生成的选择器可能不准。正确姿势是先按 F12 看目标网页结构，把选择器核对/修正后再抓。
2. **遵守 robots.txt 与网站条款**：只抓公开、允许抓取的数据；控制请求频率（别高频轰炸服务器）。
3. **Key 安全**：key 只放环境变量，代码里不出现明文；别把 key 提交到 git。
4. **费用**：每多一轮协作都在消耗 token，`max_rounds=3` 已是够用上限，别调太大。

## 六、扩展方向（写进简历的加分项）

- 给两个 Agent 加 `tools`，让它们能自己读网页、验证选择器（从「文本生成」升级为「真 Agent」）。
- 加第三个「仲裁」Agent 处理两者意见冲突。
- 用 `asyncio` 并行抓取多个学校，再让 Kimi 汇总成报告。
