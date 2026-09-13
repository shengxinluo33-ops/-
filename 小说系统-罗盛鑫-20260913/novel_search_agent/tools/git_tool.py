"""【工具6】Git：在沙箱里建仓库、暂存、提交、看日志。

动作：init / add / commit / log / status / diff / auto

auto 是"代码变更自动提交版本"那条要求的落点：一条命令把
"没有仓库就 init → 有改动就 add → 有内容就 commit" 走完，
Agent 每改完一轮代码调一次，沙箱里就多一个可回滚的版本。

commit 时用 `git -c user.name=... -c user.email=...` 传身份，
不去改用户机器上的全局 git 配置 —— 这是工具该有的边界。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.tools import tool

from config import SANDBOX_ROOT, get_logger
from tools.common import fail, ok, resolve_in_sandbox, truncate

log = get_logger("tool.git")

ACTIONS = ("init", "add", "commit", "log", "status", "diff", "auto")
_IDENTITY = ["-c", "user.name=novel-search-agent", "-c", "user.email=agent@localhost"]


@tool
def git(action: str, path: str = ".", message: str = "", max_entries: int = 10) -> str:
    """Git 版本管理，作用范围只能是沙箱内的工作目录。

    Args:
        action: init=初始化仓库；add=暂存改动；commit=提交；log=查看提交历史；
            status=看有哪些改动；diff=看具体改了什么；auto=一次完成 init+add+commit
        path: 沙箱内的仓库路径，默认 "." 表示沙箱根目录
        message: commit / auto 的提交说明，留空会自动生成一条
        max_entries: log 显示几条历史，默认 10
    """
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return fail("Git", f"未知动作 {action!r}，可选：{'、'.join(ACTIONS)}")

    try:
        repo = resolve_in_sandbox(path)
    except Exception as e:  # noqa: BLE001
        return fail("Git 解析路径", e)
    if not repo.is_dir():
        return fail("Git", f"{repo} 不是目录")

    log.debug("git %s in %s", action, repo)
    try:
        return {
            "init": _init, "add": _add, "commit": _commit, "log": _log,
            "status": _status, "diff": _diff, "auto": _auto,
        }[action](repo, message, max_entries)
    except Exception as e:  # noqa: BLE001
        log.warning("git %s 失败：%s", action, e)
        return fail(f"Git {action}", e)


# ---------------------------------------------------------------- 内部


def _run(repo, args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *_IDENTITY, *args], cwd=str(repo),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _repo_root(repo):
    """git 真正会操作的那个仓库的根目录（沙箱里没有 .git 时会向上回溯）。"""
    code, out = _run(repo, ["rev-parse", "--show-toplevel"])
    return Path(out.strip()).resolve() if code == 0 else None


def _inside_sandbox(root) -> bool:
    return root is not None and (root == SANDBOX_ROOT or SANDBOX_ROOT in root.parents)


def _guard(repo) -> str:
    """挡住"沙箱里没仓库、git 自己找到外层大仓库去"的情况。

    resolve_in_sandbox 只校验了传入的路径参数，管不住 git 沿目录树向上回溯。
    沙箱里没有 .git 时，git 会一路找到 /workspace 这种外层仓库，
    于是 add -A / commit 提交的是整个外层仓库 —— 实测踩过一次。
    """
    root = _repo_root(repo)
    if root is None or _inside_sandbox(root):
        return ""
    return (f"{repo} 不在沙箱内的 Git 仓库里。git 沿目录树向上找到的是沙箱外的 "
            f"{root}，继续操作会把外层仓库整个提交。请先在沙箱内执行 action=init 建仓库。")


def _is_repo(repo) -> bool:
    code, _ = _run(repo, ["rev-parse", "--is-inside-work-tree"])
    return code == 0


def _init(repo, _msg, _n) -> str:
    # 注意不能只看 _is_repo：外层仓库也会让它返回 True。
    # 只要当前仓库根不在沙箱内，就在本目录 init 一个沙箱内的仓库。
    if _inside_sandbox(_repo_root(repo)):
        return ok(f"{repo.name} 已经是沙箱内的 Git 仓库了，不用重复 init")
    code, out = _run(repo, ["init"])
    if code != 0:
        return fail("Git init", out)
    return ok(out or "已在沙箱内创建仓库")


def _add(repo, _msg, _n) -> str:
    if err := _guard(repo):
        return fail("Git add", err)
    if not _is_repo(repo):
        return fail("Git add", "这里还不是 Git 仓库，先执行 action=init 或 action=auto")
    code, out = _run(repo, ["add", "-A"])
    if code != 0:
        return fail("Git add", out)
    _, status = _run(repo, ["status", "--short"])
    return ok(f"已暂存全部改动。\n当前状态：\n{status or '（无改动）'}")


def _commit(repo, message, _n) -> str:
    if err := _guard(repo):
        return fail("Git commit", err)
    if not _is_repo(repo):
        return fail("Git commit", "还不是 Git 仓库，先 init")
    message = (message or "").strip() or "chore: agent 自动提交"
    code, out = _run(repo, ["commit", "-m", message])
    if code == 0:
        return ok(truncate(out, label="提交信息"))
    if "nothing to commit" in out:
        return ok("没有可提交的改动，工作区是干净的。")
    return fail("Git commit", out)


def _auto(repo, message, _n) -> str:
    """一条命令走完 init → add → commit，这就是"自动提交版本"。"""
    steps = []
    # 沙箱内没有仓库（或仓库在外层）时先 init，否则 add/commit 会打到外层仓库上
    if not _inside_sandbox(_repo_root(repo)):
        steps.append(_init(repo, "", 0))
        if err := _guard(repo):
            return fail("Git auto", err)
    steps.append(_add(repo, "", 0))
    steps.append(_commit(repo, message, 0))
    return "自动提交流程：\n" + "\n".join(steps)


def _log(repo, _msg, max_entries) -> str:
    if err := _guard(repo):
        return fail("Git log", err)
    if not _is_repo(repo):
        return fail("Git log", "还不是 Git 仓库")
    n = max(1, min(int(max_entries or 10), 100))
    code, out = _run(repo, ["log", f"-{n}", "--stat", "--date=short",
                            "--pretty=format:%h | %ad | %s"])
    if code != 0:
        return fail("Git log", out)
    return ok(f"最近提交（{repo.name}）：\n{truncate(out or '（还没有任何提交）', label='提交历史')}")


def _status(repo, _msg, _n) -> str:
    if err := _guard(repo):
        return fail("Git status", err)
    code, out = _run(repo, ["status", "--short"])
    if code != 0:
        return fail("Git status", out)
    return ok(f"工作区状态：\n{out or '（干净，没有改动）'}")


def _diff(repo, _msg, _n) -> str:
    if err := _guard(repo):
        return fail("Git diff", err)
    code, out = _run(repo, ["diff", "--cached"])
    if code != 0:
        return fail("Git diff", out)
    return ok(f"已暂存的改动：\n{truncate(out or '（无）', label='diff')}")


if __name__ == "__main__":
    demo = SANDBOX_ROOT / "_gitdemo"
    demo.mkdir(parents=True, exist_ok=True)
    (demo / "a.py").write_text("print('v1')\n", encoding="utf-8")
    print(git.invoke({"action": "auto", "path": "_gitdemo", "message": "feat: 第一版"}))
    (demo / "a.py").write_text("print('v2')\n", encoding="utf-8")
    print(git.invoke({"action": "auto", "path": "_gitdemo", "message": "feat: 第二版"}))
    print(git.invoke({"action": "log", "path": "_gitdemo"}))
