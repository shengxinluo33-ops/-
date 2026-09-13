"""智能体交互模型 · 核心

这个模块定义"用户 ↔ 智能体 ↔ 工具"三者之间的交互模型，一共四件事：

    1. 状态（Phase）     一次交互在任一时刻处于哪一个阶段
    2. 事件（Event）     前后端之间唯一的契约，后端只发事件，前端只画事件
    3. 循环（Loop）      observe → think → act → observe，直到不再要工具
    4. 工具（Tool）      声明即注册；出错不抛异常，把错误当数据交回模型

设计上刻意守着三条规矩：

    · 错误当数据       工具失败、工具名写错，都返回字符串给模型，让它自己纠正。
                       只有"重试到上限""超过最大轮数"才算真正的失败。
    · 模型看不到 UI    核心只产事件，不关心是浏览器、终端还是 notebook 在消费。
    · 历史可回滚       一圈跑砸了就把这一轮写进去的半成品消息弹掉，
                       避免下一轮带着残缺的 tool 消息去请求（OpenAI 会 400）。

单独跑这个文件可以看到一次假的交互事件流：
    python agent_core.py
"""

from __future__ import annotations

import ast
import inspect
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, get_type_hints

# ---------------------------------------------------------------- 状态

IDLE = "idle"           # 空闲：等待用户输入
OBSERVE = "observe"     # 观察：把用户的话 / 工具的结果放进上下文
THINK = "think"         # 思考：等模型产出（文本增量 or 工具调用）
ACT = "act"             # 行动：执行工具
DONE = "done"           # 完成：模型不再要工具，已给出最终回答
STOPPED = "stopped"     # 中断：用户点了停止，本轮跑到一半收手
FAILED = "failed"       # 失败：重试耗尽或超过最大轮数

PHASES = [IDLE, OBSERVE, THINK, ACT, DONE, STOPPED, FAILED]

PHASE_LABEL = {
    IDLE: "空闲",
    OBSERVE: "观察",
    THINK: "思考",
    ACT: "行动",
    DONE: "完成",
    STOPPED: "中断",
    FAILED: "失败",
}

# ---------------------------------------------------------------- 事件

# 事件类型。前端按 type 分派，不解析自然语言，所以加功能只需要加事件。
EV_SESSION = "session"        # 会话建立：模型、可用工具
EV_STATE = "state"            # 状态迁移：{phase, turn}
EV_TOKEN = "token"            # 思考的流式文本增量
EV_TOOL_CALL = "tool_call"    # 决定调工具：{name, args, call_id}
EV_TOOL_RESULT = "tool_result"  # 工具回来了：{name, ok, result, elapsed, call_id}
EV_ERROR = "error"            # 真失败：{message}
EV_STOPPED = "stopped"        # 被用户中断：{turns, tool_calls, elapsed}
EV_DONE = "done"              # 正常结束：{turns, tool_calls, elapsed}
EV_END = "__end__"            # 流结束哨兵


@dataclass
class Event:
    """一次交互里的一个最小事实。"""

    type: str
    data: Any = None
    ts: float = field(default_factory=time.time)

    def sse(self) -> str:
        return f"data: {json.dumps({'type': self.type, 'data': self.data}, ensure_ascii=False)}\n\n"

    def __str__(self) -> str:
        return f"[{self.type}] {self.data}"


# ---------------------------------------------------------------- 工具


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable
    parameters: dict

    def schema(self) -> dict:
        """转成 OpenAI function calling 要求的形状。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具的注册表。

    工具函数的类型注解就是给模型看的 schema —— 改签名即改契约，
    不用再手抄一份 JSON，也就不会出现"说明和代码不一致"。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, fn: Callable | None = None, *, name: str | None = None,
                 description: str | None = None) -> Callable:
        def wrap(f: Callable) -> Callable:
            tool_name = name or f.__name__
            self._tools[tool_name] = Tool(
                name=tool_name,
                description=description or (inspect.getdoc(f) or "").strip().split("\n")[0],
                fn=f,
                parameters=self._params_of(f),
            )
            return f

        return wrap(fn) if fn is not None else wrap

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, args: dict) -> tuple[bool, str, float]:
        """执行工具。返回 (是否成功, 输出, 耗时秒)。

        不抛异常：找不到工具、参数不对、工具自己崩了，都变成 ok=False 的输出。
        —— 把错误交给模型去处理，是 Agent 能自愈的关键。
        """
        started = time.time()
        tool = self._tools.get(name)
        if tool is None:
            return False, f"错误：没有名为 {name} 的工具，可用的有 {'、'.join(self.names)}", 0.0
        try:
            out = tool.fn(**(args or {}))
        except TypeError as e:
            out = f"错误：调用 {name} 的参数不对 —— {e}"
            return False, str(out), time.time() - started
        except Exception as e:                      # noqa: BLE001 工具内部异常也要当数据
            return False, f"错误：{name} 执行失败 —— {type(e).__name__}: {e}", time.time() - started
        return True, out if isinstance(out, str) else json.dumps(out, ensure_ascii=False), time.time() - started

    @staticmethod
    def _params_of(fn: Callable) -> dict:
        """从签名 + 类型注解推导 JSON Schema。"""
        hints = get_type_hints(fn)
        sig = inspect.signature(fn)
        props: dict[str, dict] = {}
        required: list[str] = []

        for pname, param in sig.parameters.items():
            if pname in ("self", "cls"):
                continue
            json_type = ToolRegistry._json_type(hints.get(pname, str))
            props[pname] = {"type": json_type}
            doc = inspect.getdoc(fn) or ""
            hint = ToolRegistry._doc_param(doc, pname)
            if hint:
                props[pname]["description"] = hint
            if param.default is inspect.Parameter.empty:
                required.append(pname)

        return {"type": "object", "properties": props, "required": required}

    @staticmethod
    def _json_type(tp: Any) -> str:
        origin = getattr(tp, "__origin__", None)
        if origin is not None:
            return "array" if origin in (list, tuple) else "object"
        return {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }.get(tp, "string")

    @staticmethod
    def _doc_param(doc: str, name: str) -> str:
        """从 docstring 的 Args 段里抠某个参数的说明，抠不到就算了。"""
        lines = doc.split("\n")
        for i, line in enumerate(lines):
            if line.strip().lower().startswith(("args:", "参数:", "arguments:")):
                for after in lines[i + 1:]:
                    s = after.strip()
                    if not s:
                        break
                    if s.startswith(f"{name}:"):
                        return s.split(":", 1)[1].strip()
                    if s.startswith(f"{name} "):
                        return s[len(name):].strip()
                break
        return ""


# ---------------------------------------------------------------- 智能体会话


@dataclass
class TurnStats:
    turns: int = 0
    tool_calls: int = 0
    tokens: int = 0          # 文本增量个数，不是真实 token 数
    elapsed: float = 0.0


class AgentSession:
    """一个会话 = 一份 messages + 一个 ReAct 循环。

    run() 是生成器：每产出一个 Event，调用方立刻能推给前端，
    所以浏览器看到的不是"等 20 秒后哗一下全出来"，而是逐 token、逐步。
    """

    def __init__(
        self,
        client: Any,
        model: str,
        tools: ToolRegistry,
        system_prompt: str = "你是一个会用工具的助手，回答用简洁的中文。",
        max_turns: int = 6,
        max_retries: int = 4,
        max_tool_chars: int = 4000,
        session_id: str | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.max_retries = max_retries
        self.max_tool_chars = max_tool_chars
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self.should_stop = should_stop        # 用户点"停止"时这个回调变 True
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self.stats = TurnStats()

    # -- 对外 ----------------------------------------------------------

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.stats = TurnStats()

    def stopped(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def drop_last_turn(self) -> int:
        """删掉最后一轮（最后一条 user 及其之后的所有消息），返回删了几条。

        "重新生成"就是先调这个、再重跑同一条 user 消息。
        """
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                dropped = len(self.messages) - i
                del self.messages[i:]
                return dropped
        return 0

    def run(self, user_text: str) -> Iterator[Event]:
        """跑一轮完整交互。出错时回滚本轮写入的消息。"""
        started = time.time()
        self.stats = TurnStats()

        yield Event(EV_STATE, {"phase": OBSERVE, "turn": 0})
        self.messages.append({"role": "user", "content": user_text})

        snapshot = json.dumps(self.messages, ensure_ascii=False, default=str)
        rollback = False

        stopped_now = False

        for turn in range(1, self.max_turns + 1):
            self.stats.turns = turn

            # ---- think：问模型一次，流式收
            out: dict = {}
            yield Event(EV_STATE, {"phase": THINK, "turn": turn})
            for ev in self._stream_once(out):
                if ev.type == EV_ERROR:
                    rollback = True
                yield ev
            if "calls" not in out:
                self._fail(started)
                yield Event(EV_STATE, {"phase": FAILED, "turn": turn})
                break

            calls, content = out["calls"], out["content"]

            # 思考到一半被叫停：把已经吐出来的半截话留下，别扔——
            # 用户要看得见"它刚才说到哪了"，模型下轮也知道这事没聊完
            if out.get("stopped") or self.stopped():
                stopped_now = True
                if content:
                    self.messages.append({"role": "assistant", "content": content + "\n…（用户中断）"})
                break

            # ---- 不再要工具 = 想清楚了，收工
            if not calls:
                self.messages.append({"role": "assistant", "content": content})
                self.stats.elapsed = round(time.time() - started, 2)
                yield Event(EV_STATE, {"phase": DONE, "turn": turn})
                yield Event(EV_DONE, self.stats.__dict__.copy())
                break

            # ---- act：把"想调用工具"记进历史，再逐个执行
            self._append_tool_calls(content, calls)

            yield Event(EV_STATE, {"phase": ACT, "turn": turn})
            for idx, c in enumerate(calls):
                if self.stopped():
                    stopped_now = True
                    # 关键：assistant 已经带着 tool_calls 写进历史了，
                    # 剩下没执行的调用必须补占位结果，否则下一条请求
                    # 会因为"tool_calls 没有对应 tool 消息"被直接 400 拒掉。
                    for rest in calls[idx:]:
                        self.messages.append({
                            "role": "tool", "tool_call_id": rest["id"],
                            "content": "（用户中断，工具未返回结果）",
                        })
                    break

                yield Event(EV_TOOL_CALL, {"name": c["name"], "args": c["args"], "call_id": c["id"]})
                ok, result, elapsed = self.tools.call(c["name"], c["args"])
                self.stats.tool_calls += 1
                result = self._truncate(result)
                yield Event(EV_TOOL_RESULT, {
                    "name": c["name"], "ok": ok, "result": result,
                    "elapsed": round(elapsed, 2), "call_id": c["id"],
                })
                self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})

            if stopped_now:
                break
        else:
            rollback = True
            yield Event(EV_ERROR, f"达到最大轮数 {self.max_turns}，强制结束 —— 任务可能没完成")
            self._fail(started)
            yield Event(EV_STATE, {"phase": FAILED, "turn": self.max_turns})

        if stopped_now:
            self.stats.elapsed = round(time.time() - started, 2)
            yield Event(EV_STATE, {"phase": STOPPED, "turn": self.stats.turns})
            yield Event(EV_STOPPED, self.stats.__dict__.copy())

        if rollback and self.messages and self.messages[-1]["role"] == "user":
            self.messages[:] = json.loads(snapshot)[:-1]   # 弹掉这条 user，下一轮别带着残局

        yield Event(EV_END)

    # -- 内部 ----------------------------------------------------------

    def _fail(self, started: float) -> None:
        self.stats.elapsed = round(time.time() - started, 2)

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_tool_chars:
            return text
        return text[: self.max_tool_chars] + f"\n…（输出过长，已截断到 {self.max_tool_chars} 字）"

    def _append_tool_calls(self, content: str, calls: list[dict]) -> None:
        self.messages.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"],
                                 "arguments": json.dumps(c["args"], ensure_ascii=False)},
                }
                for c in calls
            ],
        })

    def _stream_once(self, out: dict) -> Iterator[Event]:
        """调一次模型：重试 + 流式收文本增量与工具调用，结果写进 out。"""
        for attempt in range(self.max_retries):
            try:
                stream = self.client.chat.completions.create(
                    model=self.model, messages=self.messages,
                    tools=self.tools.schemas(), stream=True,
                )
                break
            except Exception as e:                  # noqa: BLE001 渠道五花八门，只能兜底
                if attempt == self.max_retries - 1:
                    yield Event(EV_ERROR, f"重试 {self.max_retries} 次仍失败：{type(e).__name__} "
                                          f"—— 检查网络、Key 或换渠道（{e}）")
                    return
                limited = "RateLimit" in type(e).__name__ or "429" in str(e)
                wait = (10, 15, 20)[attempt] if limited else 2 ** (attempt + 1)
                yield Event(EV_TOKEN, f"[第 {attempt + 1} 次失败] {type(e).__name__}，{wait} 秒后重试\n")
                time.sleep(wait)

        content: list[str] = []
        acc: dict[int, dict] = {}

        for chunk in stream:
            # 每收到一个增量就查一次停止信号 —— 这是"中断思考"真正的落点，
            # 不用等这一轮模型吐完就能收手
            if self.stopped():
                out["stopped"] = True
                break
            delta = chunk.choices[0].delta
            if delta.content:
                content.append(delta.content)
                self.stats.tokens += 1
                yield Event(EV_TOKEN, delta.content)
            for tc in delta.tool_calls or []:
                slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["args"] += tc.function.arguments

        if out.get("stopped"):
            # 主动关掉连接，别让服务端还在给我们推已经没人要的 token
            try:
                stream.close()
            except Exception:                      # noqa: BLE001 有的渠道没有 close，无所谓
                pass

        out["content"] = "".join(content)

        calls = []
        for v in acc.values():
            if not v["name"]:
                continue                      # 只攒到一半的工具调用，直接丢
            try:
                args = json.loads(v["args"]) if v["args"] else {}
            except json.JSONDecodeError:
                # 中断时参数可能只传了一半。这里不抛，丢掉这个残缺调用即可
                if not out.get("stopped"):
                    continue
                args = {}
            calls.append({"id": v["id"], "name": v["name"], "args": args})
        out["calls"] = calls


# 一个顺手的工具：给需要"算个数"的场景，避免模型口算
_SAFE_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
               ast.FloorDiv, ast.USub, ast.UAdd)


def safe_eval(expr: str) -> float:
    """只做算术的求值器。非算术节点一律拒绝，杜绝 __import__ 之类的绕过。"""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            raise ValueError(f"不允许的语法：{type(node).__name__}")
    return eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, {})  # noqa: S307


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    # 假 client：第一轮要调工具，第二轮给最终回答。不联网、不花 token。
    class _Delta:
        def __init__(self, **kw): self.content = kw.get("content"); self.tool_calls = kw.get("tool_calls")

    class _Fn:
        def __init__(self, name, args): self.name = name; self.arguments = args

    class _TC:
        def __init__(self, i, name, args): self.index = i; self.id = f"call_{i}"; self.function = _Fn(name, args)

    class _Chunk:
        def __init__(self, d): self.choices = [type("C", (), {"delta": d})()]

    class _FakeClient:
        def __init__(self): self.n = 0
        @property
        def chat(self): return self
        @property
        def completions(self): return self
        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                return [_Chunk(_Delta(tool_calls=[_TC(0, "add", '{"a": 1, "b": 2}')]))]
            return [_Chunk(_Delta(content="1 + 2 = 3"))]

    reg = ToolRegistry()

    @reg.register
    def add(a: int, b: int) -> int:
        """把两个整数相加。

        Args:
            a: 第一个数
            b: 第二个数
        """
        return a + b

    print("工具 schema：", json.dumps(reg.schemas(), ensure_ascii=False))
    s = AgentSession(_FakeClient(), "fake-model", reg, max_turns=3)
    for ev in s.run("1 加 2 等于多少？"):
        print(ev)
    print("\n最终历史：")
    for m in s.messages:
        print(" ", m.get("role"), "|", str(m.get("content"))[:60])
