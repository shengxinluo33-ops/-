"""爬虫采集模块：搜榜单 → 抓榜单页 → 从页面里解出多本小说 → 逐本补全简介。

链路：

    Tavily 搜"XX小说排行榜 / 书单"  →  若干个榜单页 URL
        ↓  逐页：robots 检查 → 睡 1~3 秒 → 静态抓（太短换 Playwright）
        ↓  从正文里解出《书名》条目（榜单页一页能出好几本）
        ↓  每本书再单独搜一次，把简介/作者/评分补全
        ↓  广告、备案号、举报电话等噪声清掉；失败只记日志不中断

**为什么不是"抓详情页"？** 实测踩过：搜"武侠小说排行榜"回来的全是榜单页
（起点分类页、知乎专栏、起点问答），把它们当一本书存进去，书名就成了
"最新武侠小说排行榜前十名"，简介里全是营业执照和举报电话。榜单页的价值是
**一页里有好几本书**，所以要按《书名》往出解，而不是整页当一条记录。

两条底线：

1. **不暴力爬。** 请求之间随机睡 1~3 秒（config.CRAWL_MIN_DELAY/MAX_DELAY），
   单页超时 20 秒，先看 robots.txt 再抓。
2. **不脆。** 单条 URL 挂了只往 logs/failed_urls.log 记一行，继续跑下一条。
"""

from __future__ import annotations

import random
import re
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from config import (
    CRAWL_MAX_DELAY,
    CRAWL_MIN_DELAY,
    CRAWL_MIN_TEXT,
    CRAWL_TIMEOUT,
    CRAWL_UA,
    env,
    get_logger,
    log_failed_url,
)
from titlematch import normalize

log = get_logger("crawler")

# 各分类的搜索词。多备几条，一条搜不到还有下一条。
CATEGORY_QUERIES: dict[str, list[str]] = {
    "武侠": ["武侠小说排行榜 书单 金庸 古龙", "经典武侠小说推荐 书名 作者"],
    "言情": ["言情小说排行榜 书单 经典", "好看的言情小说推荐 书名 作者"],
    "科幻": ["科幻小说排行榜 书单 刘慈欣", "经典科幻小说推荐 书名 作者"],
    "悬疑": ["悬疑推理小说排行榜 书单", "好看的悬疑小说推荐 书名 作者"],
    "历史": ["历史小说排行榜 书单 经典", "好看的历史小说推荐 书名 作者"],
    "玄幻": ["玄幻小说排行榜 书单 经典", "好看的玄幻小说推荐 书名 作者"],
    "都市": ["都市小说排行榜 书单 经典", "好看的都市小说推荐 书名 作者"],
}

# 广告/导航/备案噪声：简介里出现这些就砍掉那句话
_AD_PATTERNS = [
    r"手机版|手机阅读|wap\.|扫码下载|下载APP|内存占用",
    r"最新章节|章节列表|全文阅读|免费阅读|在线阅读|txt下载|加入书架|收藏本书",
    r"Copyright|版权所有|侵权举报|免责声明|举报电话|举报投诉",
    r"网络出版服务许可证|网出证|营业执照|互联网宗教信息服务|京公网安备|ICP备|沪ICP",
    r"国家互联网信息管理办法|色情小说|一经发现|即作删除",
    r"广告|推广|赞助|点击这里|立即阅读|立即登录|点我注册",
    r"本书由.*?整理|由网友.*?上传|本站不承担任何责任",
    r"^\s*(?:总机|地址|联系方式|登录|首页|我的书架)\b",
    # 百科/维基的界面词和表格行：混进简介里非常脏
    r"订阅更新|查看历史|上传文件|固定链接|引用此页|获取短链接|链入页面|相关更改",
    r"打印/导出|下载为PDF|打印页面|页面信息|在其他项目中|维基共享资源|维基数据项目",
    r"自由的百科全书|本页使用了标题或全文手工转换|可打印版本",
    r"^\s*(?:目录|衍生作品|作者简介|内容简介|创作背景|作品目录|参考资料|外部链接)\s*$",
    # 自媒体/公众号话术：整段是"推荐指数+个人感悟"的模板，不是简介
    r"点赞|关注我|收藏本|求票|月票|双击屏幕|长按识别|扫码关注|第一时间看",
    # 维基/百科页面上的"[编辑]"标记
    r"\[\s*编辑\s*\]|\[\s*Edit\s*\]",
]
_AD_RE = re.compile("|".join(_AD_PATTERNS), re.I | re.M)
# 表格行：一行里两个以上竖杠，基本就是百科 infobox
_TABLE_ROW_RE = re.compile(r"\|.*\|.*\|")
# 乱码：替换字符、不可见控制符
_GARBLED_RE = re.compile(r"[\uFFFD\u0000-\u0008\u000b\u000c\u000e-\u001f]")

# 维基/百科页面上的"[编辑]"标记
_EDIT_TAG_RE = re.compile(r"\[\s*(?:编辑|Edit)\s*\]", re.I)
# 邮箱：被当成简介抓进来的（如"邮箱：zhongdu@lifeweek.com.cn"）。
# 单独一条而不是塞进 _AD_PATTERNS——后者是整行丢弃，单行简介里带个邮箱就全没了。
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.\w{2,}")

# 自媒体模板标签：📖【书名】💡【作者】🍃【出版社】
# **删标签本身，留后面的文字**——标签是排版，后面的词才是内容。
# 实测 198 本里有 22 本简介是这个格式，整段看着像简介其实全是书目字段。
_TEMPLATE_TAG_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2700-\u27BF]*\s*【[^】]{0,16}】")
# emoji 与变体选择符：混在正文里对语义检索是纯噪声
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]")
# 百科/维基页面的 markdown 标题："# 雪山飞狐"、"## 目录"、"### 创作背景"。
# 这类页面解出来的"简介"整段就是目录结构（28 本中招），必须砍掉。
#
# **不能加 `^` 行首锚点**：`clean_text` 最后会把换行压成空格，库里存的简介是
# 单行，标题变成"行内"的 `# 目录 ## 创作背景`。加了锚点就只对采集时的多行原文
# 有效，清洗已有数据时一条都删不掉（实测《雪山飞狐》300 字 → 299 字，纹丝不动）。
# **标题内容也不能贪**：写成 `[^#]{0,24}` 会连吃 24 个字符，把标题后面的正文
# 一起吃掉（《布衣神相》整段只剩"）《殺人的心跳》"）。标题词本身没有空格和句读，
# 所以卡到第一个空格/标点为止。
# 另外它必须在 `[编辑]` 之后跑，顺序反了会吃掉 `[`，剩下的 `编辑]` 就配不上了。
_MARKDOWN_HEAD_RE = re.compile(r"#{1,6}[ \t]*[^\s#。，；！？、]{0,16}")

# 书名：《》是中文书单页里最可靠的信号
_BOOK_RE = re.compile(r"《([^《》\s][^《》]{0,29})》")
# 退化情况：榜单里不带书名号，写成 "1、射雕英雄传 —— 金庸"
_NUMBERED_RE = re.compile(
    r"^\s*(\d{1,2})\s*[.、．)）]\s*([^\s，。；、]{2,25})(?:[\s—\-–:：]+([^\s，。；、]{2,15}))?"
)

_SCORE_RE = re.compile(
    r"(?:评分|分数|豆瓣评分|读者评分|推荐指数)[^0-9]{0,8}(\d(?:\.\d)?)\s*分?"
    r"|(\d\.\d)\s*分",
)
# 百科 infobox 里的 "| 作者 | 金庸 |" 这种最准，优先用它
_AUTHOR_INFOBOX_RE = re.compile(r"作者\s*[|｜]\s*([一-龥·]{2,12})")
_AUTHOR_RE = re.compile(r"(?:作者|作\s*者)\s*[：:]\s*([一-龥·]{2,12})")
# 这些词会被宽松的正则当成"作者"抓回来，实测踩过（作者=表示 / 作者=简介）
_AUTHOR_STOPWORDS = {
    "表示", "简介", "详情", "未知", "不详", "佚名", "无", "相关", "其他", "其他信息",
    "信息", "说明", "备注", "主页", "专栏", "专栏文章", "本文", "本书", "推荐",
    "正序浏览", "倒序浏览", "正序", "倒序", "浏览",
}
# 阅读器界面上的词，被当成作者抓回来过（"作者：正序浏览"）
_AUTHOR_UI_RE = re.compile(r"浏览|目录|章节|下载|阅读|收藏|书架|评论|打分|登录|注册")

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


# ---------------------------------------------------------------- 工具

def polite_sleep() -> None:
    """请求之间随机睡 1~3 秒。这是爬虫的礼貌，也是别把 IP 搞封的保命手段。"""
    time.sleep(random.uniform(CRAWL_MIN_DELAY, CRAWL_MAX_DELAY))


def robots_allows(url: str) -> bool:
    """看 robots.txt 让不让抓。取不到 robots 文件时按"允许"处理。"""
    parts = urlparse(url)
    host = f"{parts.scheme}://{parts.netloc}"
    if host not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{host}/robots.txt")
        try:
            rp.read()
        except Exception as e:  # noqa: BLE001 取不到就当没限制
            log.debug("读 robots.txt 失败（%s）：%s", host, e)
            rp = None
        _robots_cache[host] = rp
    rp = _robots_cache[host]
    if rp is None:
        return True
    try:
        return rp.can_fetch(CRAWL_UA, url)
    except Exception:  # noqa: BLE001
        return True


def strip_inline_noise(text: str) -> str:
    """片段级清洗：**只删噪声片段，不整段丢弃**。

    和 `clean_text` 的区别很关键：那个会逐行丢弃命中广告词的整行，
    对**多行原文**没问题，但库里存的简介是压过空白的**单行**——
    随便命中一个"广告"就会把整条简介删光。实测踩过：《没钱修什么仙？》
    258 字的真实剧情简介、《离婚后，落户西北当神农》349 字，
    都是因为全文里带了"广告"两个字被整行丢掉，洗完剩 0 字。

    所以清洗已有数据只能用这个版本，采集时的多行原文继续用 `clean_text`。
    """
    if not text:
        return ""
    text = _GARBLED_RE.sub("", text)
    text = _TEMPLATE_TAG_RE.sub(" ", text)      # 删【标签】，留后面的文字
    text = _EMOJI_RE.sub("", text)
    # 先删 [编辑]，再删 markdown 标题：反过来的话标题那条正则的 [^#]{0,24}
    # 会贪婪吃掉 "["，剩下的 "编辑]" 就再也匹配不上了（实测《布衣神相》）
    text = _EDIT_TAG_RE.sub("", text)           # 维基的 [编辑] 标记
    text = _MARKDOWN_HEAD_RE.sub("", text)      # "# 目录" 整段砍掉
    text = _EMAIL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_text(text: str, max_len: int = 400) -> str:
    """清洗：片段级去噪（`strip_inline_noise`）→ 逐句砍广告/备案 → 压空白 → 截断。

    片段级那三步是后来补的（2026-09-03）。原来只砍广告，结果百科页面解出来的
    "简介"整段是 `## 目录 ## 创作背景 ### 文学来源`，自媒体榜单页解出来的是
    `📖【书名】💡【作者】🍃【出版社】`——**格式上像简介，内容上什么都没说**，
    而且这些噪声会进向量库，直接污染语义检索。
    """
    if not text:
        return ""
    text = strip_inline_noise(text)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _AD_RE.search(line):
            continue
        if _TABLE_ROW_RE.search(line):
            continue      # 百科 infobox 的一行，不是正文
        if len(line) < 6 and not re.search(r"[\u4e00-\u9fff]", line):
            continue      # 太短的碎片多半是导航/按钮
        lines.append(line)
    out = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return out[:max_len]


# ---------------------------------------------------------------- 搜索

def _tavily():
    key = env("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("缺少 TAVILY_API_KEY，无法搜索。")
    from tavily import TavilyClient

    return TavilyClient(api_key=key)


def search_candidates(category: str, max_results: int = 10) -> list[dict]:
    """搜该分类的榜单/书单页，返回候选（title/url/snippet）。"""
    client = _tavily()
    queries = CATEGORY_QUERIES.get(category) or [f"{category}小说排行榜 书单"]

    seen: set[str] = set()
    items: list[dict] = []
    for q in queries:
        if len(items) >= max_results:
            break
        log.info("搜索：%s", q)
        try:
            resp = client.search(query=q, max_results=max_results,
                                 search_depth="basic", include_answer=False)
        except Exception as e:  # noqa: BLE001 网络/鉴权/额度都可能炸
            log.warning("搜索失败（%s）：%s", q, e)
            continue
        for r in resp.get("results") or []:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            items.append({
                "title": (r.get("title") or "").strip(),
                "url": url,
                "snippet": (r.get("content") or "").strip(),
            })
        polite_sleep()
    return items[:max_results]


def search_one(query: str, max_results: int = 2) -> list[dict]:
    """为单本书补全信息时用。"""
    try:
        resp = _tavily().search(query=query, max_results=max_results,
                                search_depth="basic", include_answer=False)
    except Exception as e:  # noqa: BLE001
        log.warning("搜索失败（%s）：%s", query, e)
        return []
    return [{"title": (r.get("title") or "").strip(),
             "url": (r.get("url") or "").strip(),
             "snippet": (r.get("content") or "").strip()}
            for r in resp.get("results") or []]


# ---------------------------------------------------------------- 抓取

def fetch_page_text(url: str) -> tuple[str, str]:
    """抓一页正文。先静态，正文太短再上浏览器。返回 (正文, 方式)。"""
    text = ""
    try:
        resp = requests.get(url, headers={"User-Agent": CRAWL_UA}, timeout=CRAWL_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        text = clean_text(soup.get_text(separator="\n"), max_len=20000)
    except Exception as e:  # noqa: BLE001
        log.debug("静态抓取失败（%s）：%s", url, e)

    if len(text) >= CRAWL_MIN_TEXT:
        return text, "static"

    log.debug("静态正文只有 %d 字，改用浏览器：%s", len(text), url)
    polite_sleep()
    dynamic = ""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=CRAWL_UA)
                page.goto(url, timeout=CRAWL_TIMEOUT * 1000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                soup = BeautifulSoup(page.content(), "html.parser")
                for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
                    tag.decompose()
                dynamic = clean_text(soup.get_text(separator="\n"), max_len=20000)
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 超时、被墙、页面崩溃都算抓不到
        log.debug("动态抓取失败（%s）：%s", url, e)

    if dynamic:
        return dynamic, "dynamic"
    return text, "static" if text else "failed"


# ---------------------------------------------------------------- 解析

def _clean_title(raw: str) -> str:
    """书名：砍站点后缀，去掉"最新/排行榜/前十名"这类榜单词。"""
    if not raw:
        return ""
    title = re.split(r"\s*[_\-|｜–—]\s*", raw)[0].strip()
    title = re.sub(r"《|》", "", title)
    title = re.sub(r"(小说|最新章节|全文阅读|免费阅读|在线阅读)$", "", title).strip()
    return title[:60]


# 榜单页/站点词：书名里含这些基本就不是书，而是页面标题本身。
#
# **这里不能放分类名。** 原来这张表里躺着"武侠/言情/科幻/玄幻/都市/悬疑/历史"，
# 本意是拦"最新言情小说排行榜"这种页面标题，但代价是把《武侠世界》《都市妖奇谈》
# 这类真书名一起误伤了——拦页面标题靠"排行榜/书单/推荐"已经足够，分类名是净损失。
_NOISE_WORDS = (
    "排行榜", "榜单", "书单", "推荐", "大全", "前十名", "前十",
    "小说网", "中文网", "阅读", "下载", "免费", "首页", "分类", "完本", "排名",
)

# 榜单序号：**不能再拿裸的「第」做子串匹配**（原来就是这么写的），
# 那样《庆余年第一季》《第九个寡妇》全被判成噪声。序号后面得跟得上名次量词。
_RANK_NOISE_RE = re.compile(
    r"第\s*[0-9０-９一二三四五六七八九十百千]+\s*[名部位]|top\s*\d+", re.I)

# 看着像书名、其实不是书的东西。**按 titlematch.normalize() 归一后精确匹配**：
# 精确匹配能删掉《时代》周刊，又不会误伤《时代广场的蟑螂》这种真书名。
# 来源：榜单页作者写"入选《纽约时报》年度好书"时，《》正则把它当成书名摘了出来。
_NON_BOOK_TITLES = {
    # 报纸 / 媒体
    "纽约时报", "华尔街日报", "华盛顿邮报", "卫报", "泰晤士报", "洛杉矶时报",
    "光明日报", "人民日报", "中国青年报", "南方周末", "参考消息", "新闻周刊",
    "福布斯", "经济学人", "纽约客", "时代", "时代周刊", "读者文摘",
    # 书评机构 / 行业刊物
    "柯克斯书评", "出版人周刊", "图书馆杂志", "学校图书馆杂志", "书单杂志",
    # 文学杂志
    "今古传奇", "科幻世界", "人民文学", "收获", "当代", "十月", "钟山", "花城",
    "天涯", "小说月报", "译林", "世界文学", "外国文艺", "中华读书报", "文学报",
    "文艺报", "散文", "诗刊", "萌芽", "青年文摘", "故事会", "书城",
    # 榜单页导航词
    "新书", "热门", "精选", "必读", "好书", "经典",
}

# 人工确认过的人名条目：这些是作家，没有任何小说叫这个名字。
# 库里 91 个作者名是自动判定的（见 _known_author_names），这批是漏网的。
_PERSON_NOISE = {"刘慈欣"}

_AUTHOR_NAME_CACHE: set[str] | None = None


def _known_author_names() -> set[str]:
    """库里出现过的作者名（归一化后），用来识别"书名其实是个作者名"的条目。

    只查一次并缓存 —— 这个集合在逐条解析时会被反复用到，
    每条都查一遍库是纯浪费。
    """
    global _AUTHOR_NAME_CACHE
    if _AUTHOR_NAME_CACHE is None:
        try:
            from store import connect, init_db

            init_db()
            with connect() as conn:
                _AUTHOR_NAME_CACHE = {
                    normalize(r["author"])
                    for r in conn.execute(
                        "SELECT DISTINCT author FROM novels WHERE author != ''")
                }
        except Exception as e:  # noqa: BLE001 库还没建好时不能让解析挂掉
            log.debug("读作者名列表失败，跳过这条规则：%s", e)
            _AUTHOR_NAME_CACHE = set()
    return _AUTHOR_NAME_CACHE


def noise_reason(title: str) -> str:
    """这条书名为什么不是一本书。返回空字符串表示它是正常书名。

    判定原因要能说清楚：这份规则不只用于采集，还用于**从库里删数据**
    （`export_csv.py --clean`）。删错真书是找不回来的，
    所以判定依据得打印出来给人核一眼，而不是闷头删。
    """
    if len(title) < 2 or len(title) > 30:
        return "长度不像书名"
    for w in _NOISE_WORDS:
        if w in title:
            return f"含榜单/站点词「{w}」"
    if _RANK_NOISE_RE.search(title):
        return "榜单序号（第N名 / TOP10）"
    if title.isdigit():
        return "纯数字"

    key = normalize(title)
    if key in _NON_BOOK_TITLES:
        return "媒体名 / 杂志名 / 导航词"
    if key in _PERSON_NOISE:
        return "人名（人工确认）"
    if key in _known_author_names():
        return "书名等于库里某个作者名"
    return ""


def is_noise_title(title: str) -> bool:
    """看着像书名、其实不是书的东西。判定规则见 `noise_reason()`。"""
    return bool(noise_reason(title))


def extract_entries(text: str, source_url: str) -> list[dict]:
    """从一页正文里解出若干本小说。

    主信号是《书名》；书名号后面的那一段当简介。
    整个页面一个书名号都没有时，退化去认 "1、书名 —— 作者" 这种编号列表。
    """
    found: list[dict] = []
    matches = list(_BOOK_RE.finditer(text))
    for i, m in enumerate(matches):
        title = _clean_title(m.group(1))
        if not title or is_noise_title(title):
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), start + 400)
        desc = clean_text(re.sub(r"[。；;]\s*", "。 ", text[start:end]))[:300]
        found.append({"title": title, "description": desc, "source_url": source_url})

    if found:
        return found

    for line in text.splitlines():
        m = _NUMBERED_RE.match(line)
        if not m:
            continue
        title = _clean_title(m.group(2))
        if not title or is_noise_title(title):
            continue
        found.append({
            "title": title,
            "description": clean_text(line)[:300],
            "author": (m.group(3) or "").strip(),
            "source_url": source_url,
        })
    return found


def _pick_best_description(text: str, title: str, max_len: int = 300) -> str:
    """从一段杂文本里挑最像"简介"的那几句。

    榜单页和百科页的正文里，真正的简介往往只有一两句话，周围全是
    infobox、目录、书评编号列表。逐句打分比整段截断干净得多。
    """
    text = clean_text(text, max_len=6000)
    if not text:
        return ""
    scored = []
    for s in re.split(r"(?<=[。！？!?])\s*", text):
        s = s.strip()
        if len(s) < 20:
            continue
        sc = 0
        if title and title in s:
            sc += 3
        if re.search(r"讲述|故事|主人公|主角|描写|创作背景|内容简介|是[^。]{0,12}(小说|作品)", s):
            sc += 2
        if re.search(r"^\s*[0-9０-９]{1,3}\s*[、.．]", s):
            sc -= 3                      # "31、温瑞安《…》" 这种编号书评
        if "|" in s:
            sc -= 3                      # 表格行
        if len(s) > 200:
            sc -= 1
        scored.append((sc, len(s), s))
    if not scored:
        return text[:max_len]
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return scored[0][2][:max_len]


def parse_score(text: str) -> float | None:
    m = _SCORE_RE.search(text or "")
    return _to_float(m.group(1) or m.group(2)) if m else None


def parse_author(text: str) -> str:
    """抽作者。先认 infobox，再认"作者："，命中停用词就当没抽到。

    实测踩过：宽松正则把维基图例的"● 表示"和目录项的"作者简介"抓成了作者名，
    所以这里收得很紧——只要 2~12 个中文/间隔号，且不在停用词表里。
    """
    text = text or ""
    for pattern in (_AUTHOR_INFOBOX_RE, _AUTHOR_RE):
        m = pattern.search(text)
        if m:
            name = m.group(1).strip(" ·、,，。")
            if name and name not in _AUTHOR_STOPWORDS and not _AUTHOR_UI_RE.search(name):
                return name
    return ""


def _to_float(raw: str | None) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= 10 else None


# ---------------------------------------------------------------- 主流程

def crawl_category(category: str, limit: int = 8, enrich: bool = True) -> list[dict]:
    """采集一个分类的小说元数据，字段对齐 novels 表。

    流程：搜榜单页 → 逐页解析出《书名》条目 → 去重 → 逐本搜索补全简介/作者/评分
    → 按出现顺序编 rank_num。失败的 URL 记进 failed_urls.log。
    """
    category = (category or "").strip()
    if not category:
        raise ValueError("category 不能为空")

    candidates = search_candidates(category, max_results=10)
    log.info("分类「%s」搜到 %d 个榜单页候选", category, len(candidates))

    entries: list[dict] = []
    seen_titles: set[str] = set()

    for item in candidates:
        if len(entries) >= limit:
            break
        url = item["url"]
        polite_sleep()

        text = ""
        if robots_allows(url):
            text, how = fetch_page_text(url)
            log.debug("抓到 %d 字（%s）：%s", len(text), how, url)
        else:
            reason = "robots.txt 不允许抓取"
            log.info("跳过（%s），改用搜索摘要：%s", reason, url)
            log_failed_url(url, reason)
            text = clean_text(item["snippet"])       # 不让抓页面，就只吃搜索摘要

        page_entries = extract_entries(text, url) if text else []
        # 一个书名号都没解出来时，至少把这条搜索结果本身当候选试试
        if not page_entries:
            title = _clean_title(item["title"])
            if title and not is_noise_title(title):
                page_entries = [{"title": title,
                                 "description": clean_text(item["snippet"]),
                                 "source_url": url}]

        for e in page_entries:
            if len(entries) >= limit:
                break
            key = e["title"]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            entries.append(e)

    log.info("解析出 %d 本去重后的书：%s", len(entries), "、".join(e["title"] for e in entries))

    novels: list[dict] = []
    for i, e in enumerate(entries, 1):
        title, desc = e["title"], e.get("description", "")
        author, score = e.get("author", ""), parse_score(desc)

        # 页面里解出来的正文杂，先挑一遍最像简介的句子
        if len(desc) > 120:
            desc = _pick_best_description(desc, title)

        # 简介太短就读不出语义，单独搜一次这本书补上
        if enrich and len(desc) < 60:
            polite_sleep()
            blobs: list[str] = []
            for r in search_one(f"《{title}》 小说 作者 简介", max_results=2):
                blob = f"{r['title']} {r['snippet']}"
                blobs.append(blob)
                if not author:
                    author = parse_author(blob)
                if score is None:
                    score = parse_score(blob)
            picked = _pick_best_description(" ".join(blobs), title)
            if len(picked) > len(desc):
                desc = picked
            if len(desc) < 60:
                log.debug("「%s」简介仍然偏短（%d 字）", title, len(desc))

        # 简介太短就没有语义可检索，宁可给一句完整的话，也不要留半截噪声
        if len(desc) < 40:
            who = f"{author}创作的" if author else ""
            desc = f"《{title}》是{who}{category}类小说。"

        novels.append({
            "title": title,
            "author": author[:40],
            "category": category,
            "score": score,
            "rank_num": i,
            "description": desc,
            "source_url": e["source_url"],
        })
        log.info("[%d/%d] 《%s》｜作者 %s｜评分 %s｜简介 %d 字",
                 i, len(entries), title, author or "（未知）",
                 score or "（无）", len(desc))

    return novels


if __name__ == "__main__":
    import json
    import sys

    cat = sys.argv[1] if len(sys.argv) > 1 else "武侠"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    print(json.dumps(crawl_category(cat, n), ensure_ascii=False, indent=2))
