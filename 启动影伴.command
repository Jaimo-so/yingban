#!/bin/zsh

set -u

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
APP_URL="http://127.0.0.1:8765/"
HEALTH_URL="${APP_URL}api/health"
export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${PATH}"
export PORT="8765"
export YINGBAN_HOST="127.0.0.1"
export YINGBAN_PORT="8765"
export UV_CACHE_DIR="${HOME}/Library/Caches/yingban/uv"

watcher_pid=""

stop_health_watcher() {
  if [[ -n "$watcher_pid" ]]; then
    local watcher_parent
    watcher_parent="$(ps -o ppid= -p "$watcher_pid" 2>/dev/null | tr -d ' ')"
    if [[ "$watcher_parent" == "$$" ]]; then
      kill "$watcher_pid" 2>/dev/null || true
      wait "$watcher_pid" 2>/dev/null || true
    fi
  fi
}

trap stop_health_watcher EXIT

pause_after_error() {
  local message="$1"
  print -u2 ""
  print -u2 "启动失败：${message}"
  print -u2 "请按任意键关闭窗口。"
  read -k 1 2>/dev/null || true
  print
  exit 1
}

health_ready() {
  local response
  response="$(curl --fail --silent --show-error --max-time 2 "$HEALTH_URL" 2>/dev/null)" || return 1
  [[ "$response" == *'"ok":true'* || "$response" == *'"ok": true'* ]]
}

cd "$PROJECT_DIR" || pause_after_error "无法进入影伴项目目录。"

if health_ready; then
  print "影伴已在运行，正在打开浏览器……"
  open "$APP_URL" || pause_after_error "无法打开默认浏览器。"
  exit 0
fi

command -v uv >/dev/null 2>&1 || pause_after_error "未找到 uv，请先安装 uv。"
command -v curl >/dev/null 2>&1 || pause_after_error "未找到 curl。"
command -v open >/dev/null 2>&1 || pause_after_error "未找到 macOS 的 open 命令。"

print "正在检查首次启动配置……"
uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python local_bootstrap.py || pause_after_error "首次启动配置失败。"

FRONTEND_EXPORT="frontend/out/index.html"
FRONTEND_NEEDS_BUILD=0
if [[ ! -f "$FRONTEND_EXPORT" ]]; then
  FRONTEND_NEEDS_BUILD=1
elif [[ -n "$(find \
  frontend/app \
  frontend/components \
  frontend/lib \
  frontend/scripts \
  frontend/package.json \
  frontend/package-lock.json \
  frontend/next.config.ts \
  web/index.html \
  web/admin.html \
  web/share.html \
  -newer "$FRONTEND_EXPORT" -print -quit 2>/dev/null)" ]]; then
  FRONTEND_NEEDS_BUILD=1
fi

if (( FRONTEND_NEEDS_BUILD )); then
  command -v npm >/dev/null 2>&1 || pause_after_error "前端需要构建，但未找到 npm。"
  print "正在准备影伴页面，首次可能需要几分钟……"
  if [[ ! -d "frontend/node_modules" ]]; then
    npm --prefix frontend ci || pause_after_error "前端依赖安装失败。"
  fi
  npm --prefix frontend run build || pause_after_error "前端页面构建失败。"
fi

print "正在启动影伴……"
(
  integer attempt
  for attempt in {1..90}; do
    if health_ready; then
      print "影伴已启动，正在打开浏览器……"
      open "$APP_URL"
      exit 0
    fi
    sleep 1
  done
  print -u2 "等待启动超时，请查看本窗口中的服务日志。"
) &
watcher_pid=$!

uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python app.py serve
exit_code=$?

if (( exit_code != 0 && exit_code != 130 && exit_code != 143 )); then
  pause_after_error "影伴服务已退出（状态码 ${exit_code}）。"
fi
