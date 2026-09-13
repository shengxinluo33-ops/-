"""智能体交互模型 · 网页版后端

把 agent_core 产出的事件流，用 SSE 一行行推给浏览器。
后端不做任何渲染逻辑 —— 前端拿到事件自己画，两边只认事件协议。

    python app.py            # http://0.0.0.0:8000
    python app.py --port 8080

会话历史落在 .sessions/<session_id>.json，所以重启服务、刷新页面都还在。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from agent_core import EV_END, EV_ERROR, AgentSession
from tools import build_registry, system_prompt

HERE = Path(__file__).resolve().parent
# 先找自己目录下的 .env，没有再复用第一章 notebook 里配好的那份 ——
# 这样单独把 agent-lab/ 拷出去也能跑，不用改代码
ENV_FILES = [
    HERE / ".env",
    HERE.parent / "ch1-intro-and-toolcalling" / "notebooks" / ".env",
]

for _f in ENV_FILES:
    if _f.exists():
        load_dotenv(_f, override=True)
        break
ENV_FILE = next((str(f) for f in ENV_FILES if f.exists()), str(ENV_FILES[0]))

API_KEY = os.getenv("API_KEY")
BASE_URL = os.getenv("BASE_URL")
MODEL_ID = os.getenv("MODEL_ID")

REGISTRY = build_registry()

# 联网查资料往往要"搜一次 → 读一两个页面 → 再回答"，6 轮容易不够用
MAX_TURNS = 8

# 会话历史落盘目录。一个会话一个 JSON，就是 messages 数组本身。
SESSIONS_DIR = HERE / ".sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

app = FastAPI()
sessions: dict[str, AgentSession] = {}     # 内存缓存，落盘才是真相

# 中断用的两把钥匙，每个会话一套：
#   stop —— 用户点了"停止"，循环每一步都会查它
#   idle —— 当前没有请求在跑。"重新生成"要等它，否则会和上一轮的收尾打架
stop_flags: dict[str, threading.Event] = {}
idle_flags: dict[str, threading.Event] = {}


def flags_for(sid: str) -> tuple[threading.Event, threading.Event]:
    stop = stop_flags.setdefault(sid, threading.Event())
    idle = idle_flags.setdefault(sid, threading.Event())
    idle.set()                             # 初始状态 = 空闲
    return stop, idle


def get_client() -> OpenAI:
    if not (API_KEY and BASE_URL and MODEL_ID):
        raise RuntimeError(f"没从 {ENV_FILE} 读到 API_KEY / BASE_URL / MODEL_ID")
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def safe_sid(raw: str) -> str:
    """把 session_id 压成安全的文件名。

    session_id 来自浏览器，不能直接拼进路径 —— 否则 `../../..` 就能写到任意位置。
    不合规的一律换成哈希，等于自动洗白。
    """
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw or ""):
        return raw
    return "h-" + hashlib.sha256((raw or "").encode()).hexdigest()[:16]


def history_path(sid: str) -> Path:
    return SESSIONS_DIR / f"{safe_sid(sid)}.json"


def load_history(sid: str) -> list[dict]:
    p = history_path(sid)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []                       # 文件坏了就当没历史，不要拖垮启动
    return data if isinstance(data, list) else []


def save_history(sid: str, messages: list[dict]) -> None:
    """落盘。这里绝不能抛 —— 抛了会掀掉整条 SSE 流，前端就永远等不到收尾。

    每次都确认目录存在：目录被手动删掉、或者服务跑在只读之外的任何意外情况下，
    写不进去最多是"这次没存上"，不能变成"页面卡死"。
    """
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = history_path(sid)
        tmp = path.with_suffix(".tmp")
        # 先写临时文件再原子替换：写一半崩了也不会留下半个坏 JSON
        tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        print(f"[warn] 会话 {sid} 落盘失败，本次不持久化：{e}")


def get_session(sid: str) -> AgentSession:
    """按 session_id 取会话。内存没有就从磁盘捞回来。"""
    s = sessions.get(sid)
    if s is None:
        stop, _ = flags_for(sid)
        s = AgentSession(
            client=get_client(), model=MODEL_ID, tools=REGISTRY,
            system_prompt=system_prompt(), session_id=sid,
            max_turns=MAX_TURNS,
            should_stop=stop.is_set,        # 循环里查的就是这个
        )
        saved = load_history(sid)
        if saved:
            s.messages = saved
        sessions[sid] = s
    return s


@app.get("/api/info")
def info() -> dict:
    ready = bool(API_KEY and BASE_URL and MODEL_ID)
    return {
        "ready": ready,
        "model": MODEL_ID,
        "base_url": BASE_URL,
        "tools": [
            {"name": t.name, "description": t.description,
             "params": list(t.parameters.get("properties", {}))}
            for t in REGISTRY
        ],
        "tavily": bool(os.getenv("TAVILY_API_KEY")),
        "max_turns": MAX_TURNS,
        "search": "web_search / fetch_url",
    }


@app.get("/api/history")
def history(session_id: str = "") -> dict:
    """把落盘的历史给前端，用来在刷新后把对话重画出来。"""
    sid = safe_sid(session_id)
    msgs = load_history(sid)
    return {
        "session_id": sid,
        "messages": [m for m in msgs if m.get("role") != "system"],   # 提示词不发给浏览器
    }


@app.post("/api/reset")
def reset(body: dict) -> dict:
    """清空会话：内存和磁盘一起删。"""
    sid = safe_sid(str(body.get("session_id") or ""))
    sessions.pop(sid, None)
    history_path(sid).unlink(missing_ok=True)
    stop_flags.pop(sid, None)
    idle_flags.pop(sid, None)
    return {"ok": True}


@app.post("/api/stop")
def stop(body: dict) -> dict:
    """中断当前这轮思考。

    只是置一个标志位，真正的收手发生在循环里：
    每收到一个 token 增量、每个工具调用之前都会查它。
    """
    sid = safe_sid(str(body.get("session_id") or ""))
    flags_for(sid)[0].set()
    return {"ok": True}


@app.post("/api/retry")
def retry(body: dict):
    """重新问一遍最后那个问题：先把上一轮从历史里删掉，再重跑。

    和 /api/chat 的区别只有前面这步 drop_last_turn()。
    """
    sid = safe_sid(str(body.get("session_id") or uuid.uuid4().hex[:8]))
    text = (body.get("message") or "").strip()
    if not text:
        return {"error": "消息为空"}

    try:
        session = get_session(sid)
    except RuntimeError as e:
        return {"error": str(e)}

    idle = flags_for(sid)[1]
    if not idle.wait(timeout=8):           # 等上一轮收尾，最多等 8 秒
        return {"error": "上一轮还没停下来，稍等一下再点"}

    dropped = session.drop_last_turn()
    save_history(sid, session.messages)
    return sse_run(sid, session, text, dropped=dropped)


def sse_run(sid: str, session: AgentSession, text: str, dropped: int = 0) -> StreamingResponse:
    """把一轮交互跑成 SSE。/api/chat 和 /api/retry 共用这一份。"""
    stop, idle = flags_for(sid)
    stop.clear()                           # 新一轮开始，先把上一轮的停止标志清掉
    idle.clear()

    def events():
        if dropped:
            yield f"data: {json.dumps({'type': 'history', 'data': f'已撤销上一轮（{dropped} 条消息），重新生成'}, ensure_ascii=False)}\n\n"
        try:
            for ev in session.run(text):
                yield ev.sse()
        except Exception as e:                       # noqa: BLE001 兜底：别让流断在半路
            yield f"data: {json.dumps({'type': EV_ERROR, 'data': f'服务端异常：{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
        finally:
            # 放 finally：被中断、网络断了也要落盘，聊过的内容不丢
            save_history(sid, session.messages)
            idle.set()                     # 无论怎么结束都要放开，否则 /api/retry 会一直等
            yield f"data: {json.dumps({'type': EV_END, 'data': None})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat")
def chat(body: dict):
    sid = safe_sid(str(body.get("session_id") or uuid.uuid4().hex[:8]))
    text = (body.get("message") or "").strip()
    if not text:
        return {"error": "消息为空"}

    try:
        session = get_session(sid)
    except RuntimeError as e:
        # 先把消息存进局部变量：except 块结束时 Python 会删掉 e，
        # 而生成器是"之后"才执行的，直接引用 e 会 NameError。
        msg = str(e)

        def no_key():
            yield f"data: {json.dumps({'type': EV_ERROR, 'data': msg}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': EV_END, 'data': None})}\n\n"
        return StreamingResponse(no_key(), media_type="text/event-stream")

    return sse_run(sid, session, text)


app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    print(f"\n  智能体交互模型已启动 →  http://localhost:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
