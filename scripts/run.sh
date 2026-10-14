#!/usr/bin/env bash
# ==============================================================================
# 「15 分钟生活圈」智能体检与规划助手 —— 一键启动（Linux / macOS）
#
#   ./scripts/run.sh --offline                 # 离线模式，零 API 配额（默认，评审方首选）
#   ./scripts/run.sh --live                    # 在线模式，调用真实百度地图 API
#   ./scripts/run.sh --offline --with-frontend # 同时起前端开发服务器
#   ./scripts/run.sh --help
#
# 脚本会：① 复用或创建 backend/.venv ② 校验/安装依赖 ③ 启动 uvicorn（可选起前端）
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="replay"
HOST="127.0.0.1"
PORT="8000"
WITH_FRONTEND="no"
INSTALL_DEPS="no"
SKIP_CHECK="no"

usage() {
  cat <<'USAGE'
「15 分钟生活圈」智能体检与规划助手 —— 一键启动（Linux / macOS）

用法：
  ./scripts/run.sh [选项]

示例：
  ./scripts/run.sh --offline                 # 离线模式，零 API 配额（默认，评审方首选）
  ./scripts/run.sh --live                    # 在线模式，调用真实百度地图 API
  ./scripts/run.sh --offline --with-frontend # 同时起前端开发服务器
  ./scripts/run.sh --help

脚本会：① 复用或创建 backend/.venv ② 校验/依赖安装 ③ 启动 uvicorn（可选起前端）
USAGE
  echo ""
  echo "可选参数："
  echo "  --offline | --replay    离线模式，读 data/seed 快照，零配额（默认）"
  echo "  --live                  在线模式，调用真实百度地图 API（需要 .env 填 AK/SK）"
  echo "  --hybrid                混合模式：缓存优先 → 未命中走 live → 熔断后回落 seed"
  echo "  --host <ip>             监听地址（默认 127.0.0.1）"
  echo "  --port <port>           监听端口（默认 8000）"
  echo "  --with-frontend         同时启动前端开发服务器"
  echo "  --install               显式允许安装/补装 Python 依赖"
  echo "  --install-frontend      允许在前端目录执行包管理器安装"
  echo "  --skip-check            跳过依赖完整性校验"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --offline|--replay) MODE="replay" ;;
    --live)             MODE="live" ;;
    --hybrid)           MODE="hybrid" ;;
    --host)             HOST="$2"; shift ;;
    --port)             PORT="$2"; shift ;;
    --with-frontend)    WITH_FRONTEND="yes" ;;
    --install)          INSTALL_DEPS="yes" ;;
    --install-frontend) INSTALL_FRONTEND="yes" ;;
    --skip-check)       SKIP_CHECK="yes" ;;
    -h|--help)          usage; exit 0 ;;
    *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
  esac
  shift
done
INSTALL_FRONTEND="${INSTALL_FRONTEND:-no}"

echo "=============================================================="
echo " 15 分钟生活圈 · 一键启动"
echo "   仓库根目录 : $ROOT"
echo "   数据模式   : $MODE"
echo "   监听地址   : http://$HOST:$PORT"
echo "   前端       : $WITH_FRONTEND"
echo "=============================================================="

# ---------------------------------------------------------------- Python 解释器
VENV_DIR="$ROOT/backend/.venv"
if [ -x "$VENV_DIR/bin/python" ]; then
  PY="$VENV_DIR/bin/python"
  echo "[1/4] 复用已有虚拟环境：$VENV_DIR"
elif [ -x "$ROOT/backend/.venv/Scripts/python.exe" ]; then
  PY="$ROOT/backend/.venv/Scripts/python.exe"
  echo "[1/4] 复用已有虚拟环境（Windows 布局）：$VENV_DIR"
elif command -v uv >/dev/null 2>&1; then
  echo "[1/4] 未发现虚拟环境，用 uv 创建 $VENV_DIR"
  uv venv "$VENV_DIR"
  PY="$VENV_DIR/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  echo "[1/4] 未发现虚拟环境，用 python3 -m venv 创建 $VENV_DIR"
  python3 -m venv "$VENV_DIR"
  PY="$VENV_DIR/bin/python"
else
  echo "[错误] 找不到 Python 3.11+，也找不到 uv。请先安装 Python。" >&2
  exit 1
fi

PYVER="$("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
echo "      Python 版本：$PYVER"

# ---------------------------------------------------------------- 依赖
REQ_FILE="$ROOT/backend/requirements.txt"
MISSING="$("$PY" - <<'PYEOF'
import importlib.util
mods = ["fastapi","uvicorn","pydantic","pydantic_settings","httpx","numpy","scipy","shapely","contourpy","pytest"]
print(",".join(m for m in mods if importlib.util.find_spec(m) is None))
PYEOF
)"

if [ -z "$MISSING" ] && [ "$SKIP_CHECK" = "yes" ]; then
  echo "[2/4] 跳过依赖校验（--skip-check）"
elif [ -z "$MISSING" ]; then
  echo "[2/4] 依赖完整，无需安装"
else
  echo "[2/4] 缺失依赖：$MISSING"
  if [ "$INSTALL_DEPS" != "yes" ]; then
    echo "      为避免擅自改动环境，需显式加 --install 才会安装。例如："
    echo "        ./scripts/run.sh --offline --install"
    echo "      （也可手动执行：uv pip install --python \"$PY\" -r $REQ_FILE）"
    exit 3
  fi
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -r "$REQ_FILE"
  elif "$PY" -m pip --version >/dev/null 2>&1; then
    "$PY" -m pip install -r "$REQ_FILE"
  else
    echo "[错误] 该虚拟环境里没有 pip。请安装 uv 后重试：uv pip install --python \"$PY\" -r $REQ_FILE" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------- ASGI 入口探测
ENTRY_MODULE=""
for cand in app.main:app app.api.main:app app.asgi:app app.server:app main:app; do
  mod="${cand%%:*}"
  if ( cd "$ROOT/backend" && "$PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$mod') else 1)" ) 2>/dev/null; then
    ENTRY_MODULE="$cand"; break
  fi
done

if [ -z "$ENTRY_MODULE" ]; then
  echo ""
  echo "[3/4] ⚠️  未在 backend/ 下探测到 FastAPI 应用入口。"
  echo "      已尝试：app.main:app / app.api.main:app / app.asgi:app / app.server:app / main:app"
  echo "      说明当前代码快照还没有 API 层（由并行子任务产出）。"
  echo "      现在可以先用算法自测与基准验证："
  echo "        cd backend && \"$PY\" scripts/selftest.py"
  echo "        cd backend && \"$PY\" bench/benchmark_isochrone.py"
  exit 4
fi
echo "[3/4] ASGI 入口：$ENTRY_MODULE"

# ---------------------------------------------------------------- 启动
export DATA_MODE="$MODE"
export ENABLE_OFFLINE_REPLAY="$([ "$MODE" = "live" ] && echo false || echo true)"
export BACKEND_HOST="$HOST"
export BACKEND_PORT="$PORT"
export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"

if [ "$MODE" = "live" ] && [ ! -f "$ROOT/.env" ]; then
  echo "[警告] 在线模式但未发现 .env —— 请先 cp .env.example .env 并填入服务端 AK/SK。" >&2
fi

echo "[4/4] 启动后端：uvicorn $ENTRY_MODULE --app-dir $ROOT/backend"
echo "      打开浏览器访问 http://$HOST:$PORT   （Ctrl+C 停止）"

if [ "$WITH_FRONTEND" = "yes" ]; then
  if [ -f "$ROOT/frontend/package.json" ]; then
    echo "      同时启动前端开发服务器（另开一个终端更清晰，这里后台启动）"
    ( cd "$ROOT/frontend" && \
      if [ ! -d node_modules ]; then
        if [ "$INSTALL_FRONTEND" = "yes" ]; then
          if [ -f pnpm-lock.yaml ]; then pnpm install; else npm install; fi
        else
          echo "[警告] frontend/node_modules 不存在，跳过前端（加 --install-frontend 允许安装）" >&2
          exit 0
        fi
      fi; \
      if [ -f pnpm-lock.yaml ]; then pnpm run dev; else npm run dev; fi ) &
  else
    echo "[警告] 未发现 frontend/package.json，跳过前端。" >&2
  fi
fi

# 端口占用预检：避免 uvicorn 静默失败（address already in use）
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[错误] 端口 $PORT 已被占用。换一个端口即可，例如：./scripts/run.sh --offline --port 8001" >&2
  echo "       查看占用进程：lsof -nP -iTCP:$PORT -sTCP:LISTEN" >&2
  exit 5
fi

cd "$ROOT/backend"
exec "$PY" -m uvicorn "$ENTRY_MODULE" --app-dir "$ROOT/backend" --host "$HOST" --port "$PORT" --proxy-headers
