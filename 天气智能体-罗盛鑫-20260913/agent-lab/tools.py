"""内置工具集 —— 交互模型的"行动"能力从哪来

加一个新工具只需要三行：写个函数、加类型注解、写首行 docstring。
schema 由 ToolRegistry 从签名自动生成，所以**注解就是契约**：

    @registry.register
    def 我的工具(城市: str) -> str:
        \"\"\"一句话说明这个工具干什么 —— 这句话模型会看到。\"\"\"
        return "..."

两条经验：

· 首行 docstring 决定模型会不会在正确的时候调你。写得含糊，模型就乱调。
· 出错不要往外冒。想让前端标红就抛（注册器会兜住，转成 ok=False）；
  想让模型自己降级就返回"错误：……"字符串。两种都不会打断循环。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from datetime import datetime
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import requests

from agent_core import ToolRegistry, safe_eval

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# 没配 TAVILY_API_KEY 时的兜底数据，仅用于本地演示，不是实时搜索结果
_FALLBACK_SPOTS = {
    "昆明": ["石林", "滇池海埂公园", "翠湖公园", "云南民族村", "云南省博物馆"],
    "北京": ["故宫", "颐和园", "天坛公园", "什刹海", "中国国家博物馆"],
    "上海": ["外滩", "豫园", "上海博物馆", "武康路", "迪士尼乐园"],
    "广州": ["广州塔", "陈家祠", "沙面岛", "广东省博物馆", "白云山"],
    "成都": ["宽窄巷子", "大熊猫繁育研究基地", "武侯祠", "四川博物院", "人民公园"],
    "杭州": ["西湖", "灵隐寺", "西溪湿地", "浙江省博物馆", "宋城"],
    "西安": ["兵马俑", "城墙", "陕西历史博物馆", "大雁塔", "回民街"],
    "哈尔滨": ["中央大街", "冰雪大世界", "圣索菲亚教堂", "太阳岛", "黑龙江省博物馆"],
}


def _is_safe_url(url: str) -> tuple[bool, str]:
    """只放 http/https，且拒绝内网地址 —— 别让模型拿这个工具去扫内网。"""
    try:
        p = urlparse(url)
    except ValueError as e:
        return False, f"URL 解析失败：{e}"
    if p.scheme not in ("http", "https"):
        return False, f"只支持 http/https，不收 {p.scheme or '空'}://"
    host = p.hostname
    if not host:
        return False, "URL 里没有主机名"
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (socket.gaierror, ValueError) as e:
        return False, f"域名解析失败：{e}"
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return False, f"内网 / 本机地址不接受：{host}"
    return True, ""


def _sniff_encoding(r: requests.Response) -> str:
    """猜网页编码。

    很多站点（尤其国内站）不在 Content-Type 里带 charset，requests 会按
    ISO-8859-1 解码，中文就成乱码。优先看 header，再看 <meta charset>，最后才猜。
    """
    ctype = r.headers.get("Content-Type", "").lower()
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        return m.group(1)

    head = r.content[:4096]
    m = re.search(rb'(?i)<meta[^>]+charset=["\']?\s*([\w-]+)', head)
    if m:
        return m.group(1).decode("ascii", "ignore")

    if r.apparent_encoding:                 # 需要 charset_normalizer / chardet
        return r.apparent_encoding
    return "utf-8"                          # 绝大部分页面是 UTF-8


def _html_to_text(html: str, max_chars: int) -> str:
    """把 HTML 压成能读的纯文本。bs4 没装就退回正则。"""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form", "svg"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
    except Exception:                       # noqa: BLE001 没装 bs4 / 解析炸了都能降级
        text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
        import html as _h
        text = _h.unescape(text)

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    out = "\n".join(lines)
    return out[:max_chars] + (f"\n…（正文过长，已截断到 {max_chars} 字）" if len(out) > max_chars else "")


def _bing_rss(query: str, count: int) -> str:
    """Bing 的 RSS 输出不需要 key，返回结构化的搜索结果。"""
    # mkt=zh-CN 很关键：不带它，Bing 回的是一堆官网首页，带上才给出精确的结果页链接
    url = (f"https://cn.bing.com/search?q={quote_plus(query)}"
           f"&format=rss&count={count}&mkt=zh-CN")
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        r.raise_for_status()
        xml = r.text
    except requests.exceptions.RequestException as e:
        return f"错误：搜索请求失败 —— {e}"

    def clean(tag_text: str) -> str:
        return re.sub(r"\s+", " ", tag_text or "").strip()

    items: list[tuple[str, str, str, str]] = []
    try:
        from bs4 import BeautifulSoup

        for it in BeautifulSoup(xml, "xml").find_all("item"):   # RSS 是 XML，得用 xml 解析器
            def pick(tag):
                node = it.find(tag)
                return clean(node.get_text(strip=True)) if node else ""
            items.append((pick("title"), pick("link"), pick("description"), pick("pubDate")))
    except Exception:                       # noqa: BLE001 bs4 不可用时用正则兜底
        for block in re.findall(r"(?s)<item>(.*?)</item>", xml):
            def pick(tag):
                m = re.search(rf"(?s)<{tag}>(.*?)</{tag}>", block)
                return clean(re.sub(r"(?s)<!\[CDATA\[|\]\]>", "", m.group(1))) if m else ""
            items.append((pick("title"), pick("link"), pick("description"), pick("pubDate")))

    items = [(t, l, d, p) for t, l, d, p in items if l][:count]
    if not items:
        return f"没搜到「{query}」的结果（Bing RSS 没返回条目）。换个说法试试。"
    # 带上发布日期：问"最近/最新"时，模型得能判断这条是不是新的
    return "\n".join(
        f"{i}. {t}" + (f"（{_fmt_date(p)}）" if _fmt_date(p) else "") + f"\n   {l}\n   {d}"
        for i, (t, l, d, p) in enumerate(items, 1)
    )


def _fmt_date(raw: str) -> str:
    """Bing 的 pubDate 是「周日, 30 8月 2026 16:22:00 GMT」，规整成 2026-08-30。"""
    m = re.search(r"(\d{1,2})\s*(\d{1,2})月\s*(\d{4})", raw or "")
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw or "")
    return m.group(0) if m else ""


def build_registry() -> ToolRegistry:
    """造一个装好内置工具的注册表。"""
    reg = ToolRegistry()

    @reg.register
    def get_weather(city: str) -> str:
        """查询指定城市的实时天气（天气状况、气温、体感、湿度、风速）。

        Args:
            city: 城市名称，如 昆明、北京
        """
        try:
            r = requests.get(f"https://wttr.in/{city}?format=j1", timeout=20)
            r.raise_for_status()
            cur = r.json()["current_condition"][0]
            return (
                f"{city}当前天气：{cur['weatherDesc'][0]['value']}，"
                f"气温 {cur['temp_C']}°C，体感 {cur['FeelsLikeC']}°C，"
                f"湿度 {cur['humidity']}%，风速 {cur['windspeedKmph']}km/h"
            )
        except requests.exceptions.RequestException as e:
            return f"错误：查询天气时遇到网络问题 —— {e}"
        except (KeyError, IndexError) as e:
            return f"错误：解析天气数据失败，可能是城市名无效 —— {e}"

    @reg.register
    def get_attraction(city: str, weather: str) -> str:
        """根据城市和当前天气，推荐合适的旅游景点。

        Args:
            city: 城市名称
            weather: 当前天气状况，来自 get_weather 的结果
        """
        key = os.getenv("TAVILY_API_KEY")
        if not key:
            spots = _FALLBACK_SPOTS.get(city)
            if spots:
                return ("注意：未配置 TAVILY_API_KEY，没有做实时搜索。以下是内置兜底数据："
                        f"{city} 常见景点有 {'、'.join(spots)}。请结合「{weather}」自行取舍。")
            return (f"错误：未配置 TAVILY_API_KEY，且内置兜底数据里没有 {city}。"
                    f"已知城市：{'、'.join(_FALLBACK_SPOTS)}。")
        try:
            from tavily import TavilyClient

            data = TavilyClient(api_key=key).search(
                query=f"{city} 在 {weather} 天气下值得去的旅游景点推荐",
                search_depth="basic", include_answer=True, country="china",
            )
            if data.get("answer"):
                return data["answer"]
            return "\n".join(f"- {r['title']}: {r['content'][:100]}"
                             for r in data.get("results", [])[:3]) or "没有找到相关景点。"
        except Exception as e:                      # noqa: BLE001
            return f"错误：搜索时出问题 —— {e}"

    @reg.register
    def web_search(query: str, max_results: int = 5) -> str:
        """在互联网上搜索，返回标题、链接和摘要。不知道或有时效性的问题时先用它。

        Args:
            query: 搜索关键词，用最可能出现在网页里的说法
            max_results: 返回几条，默认 5
        """
        max_results = max(1, min(int(max_results or 5), 10))
        key = os.getenv("TAVILY_API_KEY")
        if key:                              # 配了 Tavily 就用它，质量更好
            try:
                from tavily import TavilyClient

                data = TavilyClient(api_key=key).search(
                    query=query, search_depth="basic",
                    include_answer=True, max_results=max_results,
                )
                if data.get("answer"):
                    return f"搜索摘要：{data['answer']}\n\n" + "\n".join(
                        f"{i}. {r.get('title','')}\n   {r.get('url','')}\n   {(r.get('content') or '')[:200]}"
                        for i, r in enumerate(data.get("results", [])[:max_results], 1))
            except Exception as e:           # noqa: BLE001 Tavily 挂了就退回 Bing
                bing = _bing_rss(query, max_results)
                return f"（Tavily 出错 {type(e).__name__}，已改用 Bing）\n{bing}"
        return _bing_rss(query, max_results)

    @reg.register
    def fetch_url(url: str, max_chars: int = 3000) -> str:
        """打开一个网页，把正文抓成纯文本。搜到链接后用它读详情。

        Args:
            url: 完整网址，必须是 http/https 开头，得来自 web_search 的结果
            max_chars: 最多返回多少字，默认 3000
        """
        ok, why = _is_safe_url(url)
        if not ok:
            return f"错误：{why}"

        max_chars = max(300, min(int(max_chars or 3000), 12000))

        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": UA})
            if r.status_code >= 400 and url.startswith("http://"):
                # 搜索结果里 http 链接不少已经迁到 https，顺手试一次
                r = requests.get("https://" + url[7:], timeout=25, headers={"User-Agent": UA})
            r.raise_for_status()
            r.encoding = _sniff_encoding(r)
        except requests.exceptions.RequestException as e:
            return f"错误：打不开 {url} —— {e}"

        ctype = r.headers.get("Content-Type", "")
        if not any(k in ctype.lower() for k in ("html", "xml", "text", "json")):
            return f"错误：{url} 返回的不是文本（Content-Type: {ctype}）"

        body = _html_to_text(r.text, max_chars)
        return f"[{url}]\n{body}" if body.strip() else f"错误：{url} 没抓到正文（可能是纯 JS 渲染的页面）"

    @reg.register
    def calculator(expression: str) -> str:
        """计算一个数学表达式，只支持加减乘除、乘方和取余。

        Args:
            expression: 表达式，如 (12 + 8) * 3 / 2
        """
        # 这里故意抛而不是返回"错误：…"—— 注册器会把异常转成 ok=False 的结果，
        # 模型照样拿到可读的错误信息，但前端能把这张卡片标红。
        return str(safe_eval(expression))

    @reg.register
    def current_time() -> str:
        """获取当前时间（北京时间）。"""
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        return now.strftime("%Y-%m-%d %H:%M:%S %A")

    return reg


_PROMPT_BASE = (
    "你是一个会上网查资料的助手，回答用简洁的中文。"
    "凡是涉及实时信息、新闻、最新进展、你不确定的事实，或需要数据才能回答的问题，"
    "都必须先用工具查到真实依据，绝对不要凭记忆编造——编造比说不知道糟糕得多。\n"
    "查资料的标准动作：\n"
    "1. web_search 找到候选结果（拿到标题、链接、摘要）\n"
    "2. 挑最相关的 1~2 个链接用 fetch_url 读正文，摘要不够就别急着答\n"
    "3. 用读到的内容组织回答，并在末尾列出参考来源（标题 + 链接）\n"
    "多步任务自己排顺序：先拿到前置工具的结果，再调用依赖它的工具。\n"
    "工具预算：整个过程工具调用加起来大约 4~5 次就够，别在重试上空转——"
    "某个链接打不开就换搜索结果里的下一条，重搜不要超过两次。\n"
    "读不到正文也别空手回来：把搜索结果里的标题和摘要整理成答案，"
    "并明确标注「这些来自搜索摘要、没能打开原文核对」。"
    "确实什么都查不到，就直说查不到，并说明你试过什么。"
)


def system_prompt() -> str:
    """在系统提示词里补上今天的日期。

    模型的知识有截止日，不告诉它今天几号，它搜"最近的新闻"会写出
    "2024年"这种关键词，搜出来的东西全是过期的。
    """
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y年%m月%d日")
    return (
        _PROMPT_BASE
        + f"\n今天是 {today}。问到「最近 / 最新 / 今天」时，"
        + f"搜索词里必须带上年月（比如「AI 新闻 {today[:5]}」），否则搜到的都是旧内容。"
    )
