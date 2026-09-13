"""天气 Agent 网页版 —— 后端

工具调用与 Agent Loop 直接沿用
`ch1-intro-and-toolcalling/notebooks/03_工具调用与Agent.ipynb` 的实现，
只是把 print 换成 SSE 事件推给浏览器。

    observe -> think -> act -> observe
    think 阶段用 stream=True，所以浏览器能逐 token 看到最终回答。

运行：
    python app.py            # 默认 http://0.0.0.0:8000
    python app.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

# ---------------------------------------------------------------- 配置

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE.parent / "ch1-intro-and-toolcalling" / "notebooks" / ".env"

load_dotenv(ENV_FILE, override=True)

API_KEY = os.getenv("API_KEY")
BASE_URL = os.getenv("BASE_URL")
MODEL_ID = os.getenv("MODEL_ID")

if not (API_KEY and BASE_URL and MODEL_ID):
    raise RuntimeError(f"没从 {ENV_FILE} 读到 API_KEY / BASE_URL / MODEL_ID，先把 02 的密钥单元跑一遍")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

MAX_TURNS = 6          # Agent Loop 最多转几圈，防止死循环烧 token
MAX_RETRIES = 4        # 网络抖动 / 限流 429 的重试次数

SYSTEM_PROMPT = (
    "你是一个会用工具的天气助手，回答用简洁的中文。"
    "凡是涉及实时天气、或需要天气数据才能回答的问题（比如穿什么、适合去哪玩、"
    "两个城市哪里更暖和），都必须先调用工具拿到真实数据，绝对不要凭记忆编造数值。"
    "需要多步就自己排顺序：先拿到前置工具的结果，再调用依赖它的工具。"
    "拿到工具结果后再组织成最终回答；工具返回错误时，向用户说明情况或用常识降级回答。"
)

# ---------------------------------------------------------------- 工具

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


def get_weather(city: str) -> str:
    """查询指定城市的实时天气。"""
    url = f"https://wttr.in/{city}?format=j1"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        cur = r.json()["current_condition"][0]
        return (
            f"{city}当前天气：{cur['weatherDesc'][0]['value']}，"
            f"气温 {cur['temp_C']}°C，体感 {cur['FeelsLikeC']}°C，"
            f"湿度 {cur['humidity']}%，风速 {cur['windspeedKmph']}km/h"
        )
    except requests.exceptions.RequestException as e:
        # 出错返回字符串、不抛异常：让模型看到错误后自己决定怎么办
        return f"错误：查询天气时遇到网络问题 - {e}"
    except (KeyError, IndexError) as e:
        return f"错误：解析天气数据失败，可能是城市名无效 - {e}"


def get_attraction(city: str, weather: str) -> str:
    """根据城市和当前天气，推荐合适的旅游景点。"""
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        spots = _FALLBACK_SPOTS.get(city)
        if spots:
            return (
                "注意：未配置 TAVILY_API_KEY，没有做实时搜索。以下是内置兜底数据："
                f"{city} 常见景点有 {'、'.join(spots)}。请结合「{weather}」自行取舍。"
            )
        return (
            f"错误：未配置 TAVILY_API_KEY，且内置兜底数据里没有 {city}。"
            f"已知城市：{'、'.join(_FALLBACK_SPOTS)}。"
        )
    try:
        from tavily import TavilyClient

        data = TavilyClient(api_key=key).search(
            query=f"{city} 在 {weather} 天气下值得去的旅游景点推荐",
            search_depth="basic",
            include_answer=True,
            country="china",
        )
        if data.get("answer"):
            return data["answer"]
        return "\n".join(
            f"- {r['title']}: {r['content'][:100]}" for r in data.get("results", [])[:3]
        ) or "没有找到相关景点。"
    except Exception as e:
        return f"错误：搜索时出问题 - {e}"


TOOL_MAP = {"get_weather": get_weather, "get_attraction": get_attraction}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的实时天气（天气状况、气温、体感、湿度、风速）",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称，如 昆明、北京"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_attraction",
            "description": "根据城市和当前天气，推荐合适的旅游景点",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称"},
                    "weather": {"type": "string", "description": "当前天气状况，来自 get_weather 的结果"},
                },
                "required": ["city", "weather"],
            },
        },
    },
]

# ---------------------------------------------------------------- Agent


def stream_turn(messages: list, out: dict):
    """问模型一次。边流式产出文本增量，结束时把结果写进 out。

    out["content"] 本轮文本；out["calls"] 本轮要调的工具（[{'id','name','args'}]）。
    """
    for attempt in range(MAX_RETRIES):
        try:
            stream = client.chat.completions.create(
                model=MODEL_ID, messages=messages, tools=TOOLS, stream=True
            )
            break
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                yield ("error", f"重试 {MAX_RETRIES} 次仍失败：{type(e).__name__} —— 检查网络、Key 或换渠道")
                return
            is_limited = "RateLimit" in type(e).__name__ or "429" in str(e)
            wait = (10, 15, 20)[attempt] if is_limited else 2 ** (attempt + 1)
            yield ("status", f"[第 {attempt + 1} 次失败] {type(e).__name__}，{wait} 秒后重试")
            time.sleep(wait)

    content: list[str] = []
    acc: dict[int, dict] = {}

    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            content.append(delta.content)
            yield ("token", delta.content)
        for tc in delta.tool_calls or []:
            slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function.arguments:
                    slot["args"] += tc.function.arguments

    out["content"] = "".join(content)
    out["calls"] = [
        {"id": v["id"], "name": v["name"],
         "args": json.loads(v["args"]) if v["args"] else {}}
        for v in acc.values()
    ]


def run_agent(history: list):
    """Agent Loop：yield (事件类型, 负载)，同时把每一步写回 history。"""
    for turn in range(MAX_TURNS):
        out: dict = {}
        yield ("status", f"第 {turn + 1} 轮 · 思考中")
        yield from stream_turn(history, out)
        if "calls" not in out:                      # 上面已经 yield 了 error
            return

        calls = out["calls"]
        content = out["content"]

        # 不再调工具 = 任务完成，最终回答已经在上一步流式吐出去了
        if not calls:
            history.append({"role": "assistant", "content": content})
            yield ("done", "")
            return

        history.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"],
                              "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                for c in calls
            ],
        })

        for c in calls:
            yield ("tool_call", {"name": c["name"], "args": c["args"]})
            fn = TOOL_MAP.get(c["name"])
            # 工具不存在也返回字符串 —— 把错误当数据，让模型自己纠正
            result = (fn(**c["args"]) if fn
                      else f"错误：没有名为 {c['name']} 的工具，可用的有 {list(TOOL_MAP)}")
            yield ("tool_result", {"name": c["name"], "result": result})
            history.append({"role": "tool", "tool_call_id": c["id"], "content": result})

    yield ("error", f"达到最大轮数 {MAX_TURNS}，未能完成任务")


# ---------------------------------------------------------------- 服务

app = FastAPI()
sessions: dict[str, list] = {}      # 内存里存多轮历史，重启即清空


@app.get("/api/info")
def info():
    return {
        "model": MODEL_ID,
        "base_url": BASE_URL,
        "tools": list(TOOL_MAP),
        "tavily": bool(os.getenv("TAVILY_API_KEY")),
    }


@app.post("/api/reset")
def reset(body: dict):
    sessions.pop(body.get("session_id", "default"), None)
    return {"ok": True}


@app.post("/api/chat")
def chat(body: dict):
    session_id = body.get("session_id") or "default"
    text = (body.get("message") or "").strip()
    if not text:
        return {"error": "消息为空"}

    history = sessions.setdefault(session_id, [{"role": "system", "content": SYSTEM_PROMPT}])
    history.append({"role": "user", "content": text})

    def events():
        # 出错时把半成品历史回滚掉，避免下一轮带着残缺的 tool 消息请求
        snapshot = json.dumps(history, ensure_ascii=False)
        for kind, payload in run_agent(history):
            if kind == "error":
                history[:] = json.loads(snapshot)[:-1]
            yield f"data: {json.dumps({'type': kind, 'data': payload}, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"__end__\"}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    print(f"\n  天气 Agent 已启动 →  http://localhost:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
