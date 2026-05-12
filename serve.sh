#!/usr/bin/env bash
# 实时翻译服务启停 — 后台带 --reload, 健康检查, 优雅退出
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8800}"
PIDFILE=".server.pid"
LOGFILE=".server.log"
UVICORN=".venv/bin/uvicorn"

green()  { printf "\033[32m%s\033[0m" "$1"; }
red()    { printf "\033[31m%s\033[0m" "$1"; }
yellow() { printf "\033[33m%s\033[0m" "$1"; }
gray()   { printf "\033[90m%s\033[0m" "$1"; }

[[ -x "$UVICORN" ]] || { red "✗ 未找到 $UVICORN — 请先 'uv sync'"; echo; exit 1; }

_pid_alive() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
_port_pid()  { lsof -ti:"$PORT" 2>/dev/null | head -1 || true; }

cmd_start() {
  if _pid_alive; then
    yellow "已在运行"; echo " — PID $(cat "$PIDFILE")"
    cmd_status
    return 0
  fi
  local occupant
  occupant=$(_port_pid)
  if [[ -n "$occupant" ]]; then
    red "✗ 端口 :$PORT 被 PID $occupant 占用 (非本脚本)"; echo
    echo "  先释放: kill $occupant"
    exit 1
  fi
  echo "→ 启动 server :$PORT (with --reload)"
  nohup "$UVICORN" server:app --host 0.0.0.0 --port "$PORT" --reload \
    >"$LOGFILE" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 20); do
    sleep 0.3
    if curl -fsS "http://127.0.0.1:$PORT/langs" >/dev/null 2>&1; then
      green "✓ 已启动"; echo " — PID $(cat "$PIDFILE")  http://localhost:$PORT"
      gray "  日志: ./serve.sh logs"; echo
      return 0
    fi
    if ! _pid_alive; then
      red "✗ 启动失败,见日志:"; echo
      tail -30 "$LOGFILE"
      rm -f "$PIDFILE"
      exit 1
    fi
  done
  yellow "⚠ 6 秒内未就绪 — 进程仍存活,可能是慢启动:"; echo
  tail -20 "$LOGFILE"
}

cmd_stop() {
  # 收集 PIDFILE + 端口上的所有占用进程 (uvicorn --reload 有 reloader+worker 两层)
  local pids=()
  if [[ -f "$PIDFILE" ]]; then
    local p
    p=$(cat "$PIDFILE")
    kill -0 "$p" 2>/dev/null && pids+=("$p")
  fi
  while IFS= read -r p; do
    [[ -n "$p" ]] && pids+=("$p")
  done < <(lsof -ti:"$PORT" 2>/dev/null || true)
  # 去重
  if [[ ${#pids[@]} -gt 0 ]]; then
    IFS=$'\n' read -r -d '' -a pids < <(printf "%s\n" "${pids[@]}" | sort -u && printf '\0')
  fi
  if [[ ${#pids[@]} -eq 0 ]]; then
    gray "未运行"; echo
    rm -f "$PIDFILE"
    return 0
  fi
  echo "→ 停止 PID(s): ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    local alive=0
    for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && alive=1; done
    [[ $alive -eq 0 ]] && break
    sleep 1
  done
  for p in "${pids[@]}"; do
    if kill -0 "$p" 2>/dev/null; then
      yellow "  PID $p 优雅退出超时 — SIGKILL"; echo
      kill -9 "$p" 2>/dev/null || true
    fi
  done
  rm -f "$PIDFILE"
  green "✓ 已停止"; echo
}

cmd_restart() { cmd_stop; sleep 0.5; cmd_start; }

cmd_status() {
  local pid="" src=""
  if _pid_alive; then
    pid=$(cat "$PIDFILE"); src="(pidfile)"
  else
    pid=$(_port_pid); src="(lsof)"
  fi
  if [[ -z "$pid" ]]; then
    gray "未运行"; echo
    return 1
  fi
  green "运行中"; echo " — PID $pid $src  端口 :$PORT"
  if curl -fsS "http://127.0.0.1:$PORT/langs" >/dev/null 2>&1; then
    green "  ✓ HTTP 健康检查通过"; echo
  else
    yellow "  ⚠ HTTP 无响应"; echo
  fi
}

cmd_logs() {
  [[ -f "$LOGFILE" ]] || { gray "(无日志)"; echo; return 0; }
  tail -f "$LOGFILE"
}

cmd_fg() {
  exec "$UVICORN" server:app --host 0.0.0.0 --port "$PORT" --reload
}

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  fg)      cmd_fg ;;
  *)
    cat <<EOF
实时翻译服务管理

用法: $0 {start|stop|restart|status|logs|fg}

  start    后台启动 (带 --reload, 日志 $LOGFILE)
  stop     优雅停止 (SIGTERM → 5s → SIGKILL)
  restart  stop + start
  status   PID / 端口 / HTTP 健康检查
  logs     tail -f $LOGFILE
  fg       前台启动 (Ctrl+C 退出, 适合临时调试)

环境变量:
  PORT     HTTP 端口 (默认 $PORT)
EOF
    exit 1
    ;;
esac
