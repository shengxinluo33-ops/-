#!/bin/bash
# 小说系统常驻启动脚本：关掉终端/关掉 AI 会话后继续跑，进程意外退出会被拉起。
#
#   ./start_web.sh            # 前台跑（调试用，Ctrl+C 就停）
#   ./start_web.sh --daemon   # 后台常驻，关掉终端也不停（推荐）
#
# 配套：./webctl.sh status|stop|restart

set -u
ROOT=/workspace/novel_crawl_recommend
PORT=5001
LOG="$ROOT/logs/web.log"
PIDFILE="$ROOT/logs/web.pid"

# 已经在跑就别起第二个，否则两个进程抢同一个端口，后起的直接退出。
# --serve 是守护进程自己，必须跳过这道检查，不然它把自己判成"已运行"然后退出
if [ "${1:-}" != "--serve" ] \
   && curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/status"; then
    echo "端口 $PORT 上已经有一个在跑了。要重启请执行 ./webctl.sh restart"
    exit 0
fi

serve() {
    cd "$ROOT" || exit 1
    # 由守护进程自己写 PID：$! 拿到的是 setsid 的进程号，setsid 可能 exec 也可能
    # fork，那个号不一定是活到最后的那一个，所以这里写自己的 $$
    echo $$ >"$PIDFILE"
    while true; do
        # 端口上已经有别人在服务了，就别起了。之前两个守护进程同时启动，后一个
        # 的 app.py 一直 "Address already in use"，这儿就变成每 5 秒刷两行日志的
        # 死循环。这不是"我们崩了"，是"活儿已经有人干了"，退让等着。
        if curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/status"; then
            echo "[$(date '+%F %T')] 端口 $PORT 已被别的服务占用，本次不启动，60 秒后再看" >>"$LOG"
            sleep 60
            continue
        fi
        echo "[$(date '+%F %T')] 启动 app.py --host 0.0.0.0 --port $PORT" >>"$LOG"
        python3 app.py --host 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1
        # 走到这儿说明进程没了。正常 Ctrl+C 退出码是 130，给个不重启的口子
        echo "[$(date '+%F %T')] app.py 退出（$?），5 秒后自动重启" >>"$LOG"
        sleep 5
    done
}

if [ "${1:-}" = "--daemon" ]; then
    # CloudStudio 空间长时间没活动会休眠，服务跟着一起停。配了访问令牌就用
    # /workspace/nosleep 包一层，它每 5 秒打一次心跳（令牌在 ~/.cs_token 或环境变量）
    token="${CS_HEALTHZ_TOKEN:-}"
    [ -z "$token" ] && [ -f "$HOME/.cs_token" ] && token="$(tr -d ' \n\r' <"$HOME/.cs_token")"
    wrap=""
    # 用 bash 显式调用，不靠 nosleep 的可执行位——那个文件是 644，
    # 拿 -x 判断会永远为假，心跳就静默地没启用
    if [ -n "$token" ] && [ -f /workspace/nosleep ]; then
        export CS_HEALTHZ_TOKEN="$token"
        wrap="bash /workspace/nosleep"
        echo "已启用防休眠心跳（令牌 ${#token} 字节）"
    else
        echo "提示：$HOME/.cs_token 没读到，空间休眠后服务会停。配法见 README「长期在线」"
    fi

    # setsid 脱离开当前终端，nohup 忽略挂断信号 —— 少了任一个，关掉终端服务就跟着没了
    setsid nohup $wrap bash "$0" --serve >>"$LOG" 2>&1 </dev/null &
    sleep 4
    if curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/status"; then
        echo "已启动（PID $(cat "$PIDFILE")），端口 $PORT"
        echo "公网地址：https://894a6547050c42f48c0a4e475294176c--$PORT.ap-shanghai2.cloudstudio.club"
    else
        echo "启动失败，看日志：tail -50 $LOG"
        exit 1
    fi
else
    serve
fi
