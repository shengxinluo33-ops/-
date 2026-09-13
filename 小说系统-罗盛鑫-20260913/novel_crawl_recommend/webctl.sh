#!/bin/bash
# 小说系统服务控制：./webctl.sh status|start|stop|restart
#
# 用 pgrep 找进程而不是靠 pid 文件——真正的服务进程是 app.py，
# 看门狗是 start_web.sh --serve，两个都得停，光停一个会被看门狗重新拉起来。

set -u
ROOT=/workspace/novel_crawl_recommend
PORT=5001
LOG="$ROOT/logs/web.log"

procs() {
    # 光靠 pgrep -f 会连执行这条命令的 shell 自己一起匹配上（整条命令行里含
    # 同样的字符串），一 kill 就把自己干掉了。用 /proc 里的进程名过滤掉。
    for p in $(pgrep -f "$1" 2>/dev/null); do
        case "$(cat /proc/$p/comm 2>/dev/null)" in
            python3|bash) echo "$p" ;;
        esac
    done
}

pids() { procs "app.py --host|start_web.sh --serve"; }

# nosleep 会在自己内部 fork 一个心跳子 shell，所以配了令牌时匹配到的是两个：
# 父进程 + 心跳。只有一个说明心跳没起来，一个都没有说明压根没启用。
heartbeat_status() {
    n=$(procs "/workspace/nosleep" | wc -l)
    if [ "$n" -ge 2 ]; then
        echo "防休眠心跳：运行中"
    elif [ "$n" -eq 1 ]; then
        echo "防休眠心跳：异常（父进程在，心跳子 shell 没了）"
    else
        echo "防休眠心跳：未启用（没配 CS_HEALTHZ_TOKEN，空间会休眠）"
        return
    fi
    # 心跳成功是静默的，只有失败才往 stderr 打一行，所以日志里有失败就等于令牌没生效
    bad=$(tail -300 "$LOG" 2>/dev/null | grep -c "心跳请求失败")
    [ "$bad" -gt 0 ] && echo "  警告：最近日志里 $bad 条「心跳请求失败」，令牌可能无效或没勾权限"
    return 0
}

case "${1:-status}" in
    status)
        ps=$(pids | tr '\n' ' ')
        if [ -n "${ps// /}" ] && curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/status"; then
            echo "运行中（PID: $ps），端口 $PORT"
            curl -s "http://127.0.0.1:$PORT/api/status" \
                | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"库存 {d['novels']} 本，{len(d['categories'])} 个分类\")" 2>/dev/null
            heartbeat_status
        else
            echo "未运行"
            exit 1
        fi
        ;;
    start)
        exec "$ROOT/start_web.sh" --daemon
        ;;
    stop)
        ps=$(pids | tr '\n' ' ')
        [ -z "${ps// /}" ] && { echo "本来就没在跑"; exit 0; }
        kill $ps 2>/dev/null
        sleep 2
        kill -9 $(pids) 2>/dev/null
        rm -f "$ROOT/logs/web.pid"
        echo "已停止"
        ;;
    restart)
        "$0" stop
        sleep 1
        exec "$ROOT/start_web.sh" --daemon
        ;;
    log)
        exec tail -f "$LOG"
        ;;
    *)
        echo "用法：$0 status|start|stop|restart|log"
        exit 1
        ;;
esac
