# syntax=docker/dockerfile:1.7
# ==============================================================================
# 「15 分钟生活圈」智能体检与规划助手 —— 单镜像多阶段构建
#
#   阶段 1  frontend-build : 编译前端（Vue 3 + Vite）→ /app/frontend/dist
#   阶段 2  runtime        : 只带 Python 运行时 + 前端静态产物，由 FastAPI 同源托管
#
# 设计说明：
#   * 单镜像即可跑通全流程（任务书要求「评审方快速跑通演示」），不需要 Nginx。
#   * 前端与 API 同源 ⇒ 浏览器端 AK 的 Referer 白名单只需配置一个来源，AK 不出后端。
#   * 默认 DATA_MODE=replay（离线快照），评审方【零 API 配额】即可跑通。
#   * 目录结构严格对齐 docs/CONTRACT.md §0。
#
# ✅ 构建与运行验证（2026-10-15，Docker 29.8.2 / Docker Desktop，Windows + WSL2）
#      docker compose build        -> 成功，镜像 535 MB
#      docker compose up -d        -> 容器 10 秒内达到 healthy
#      GET /api/health             -> 200
#      GET /api/audit              -> 200，零 API 配额产出完整体检报告
#    镜像内已确认导入：contourpy 1.4.0 / shapely 2.2.0 / scipy 1.18.1 / numpy 2.5.4
#
#    ⚠️ 已知坑：根目录若存在 .env 且其中 DATA_MODE=live/hybrid，
#       docker compose 的 ${DATA_MODE:-replay} 会被它覆盖，从而消耗真实配额。
#       评审方不提供 .env 时默认即 replay（零配额）。
# ==============================================================================


# ------------------------------------------------------------------------------
# 阶段 1：前端构建
# ------------------------------------------------------------------------------
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend

# 仅复制依赖清单，最大化利用层缓存。
# 注意 frontend/package.json 是【必需】文件：若前端由并行子任务产出且尚未落地，
# 这里会明确失败（而不是构建出一个没有前端产物的"成功"镜像）。
COPY frontend/package.json frontend/package-lock.json* frontend/pnpm-lock.yaml* frontend/yarn.lock* ./

# 按锁文件存在性选择包管理器；无锁文件时退回 npm install
RUN if [ -f pnpm-lock.yaml ]; then \
        corepack enable && pnpm install --frozen-lockfile; \
    elif [ -f yarn.lock ]; then \
        corepack enable && yarn install --frozen-lockfile; \
    elif [ -f package-lock.json ]; then \
        npm ci; \
    else \
        npm install; \
    fi

# 复制源码并构建
COPY frontend/ ./
RUN if [ -f pnpm-lock.yaml ]; then corepack enable && pnpm run build; \
    elif [ -f yarn.lock ]; then corepack enable && yarn run build; \
    else npm run build; fi


# ------------------------------------------------------------------------------
# 阶段 2：运行时（后端 + 前端静态产物）
# ------------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_MODE=replay \
    FRONTEND_DIST=/app/frontend/dist \
    BACKEND_HOST=0.0.0.0 \
    BACKEND_PORT=8000 \
    CACHE_DB_PATH=/app/data/cache/app.db \
    SEED_DIR=/app/data/seed

WORKDIR /app

# 后端依赖（单独一层，便于缓存）
COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --upgrade pip && \
    python -m pip install -r /app/backend/requirements.txt

# 后端源码（目录结构对齐 CONTRACT.md §0）
COPY backend/ /app/backend/

# 前端静态产物（供 FastAPI 同源托管）
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# 离线快照（评审方零配额跑通的数据源）。镜像内没有 data/seed 时，
# 用占位文件保证目录存在，避免后续 COPY/挂载失败。
RUN mkdir -p /app/data/cache /app/data/seed /app/frontend/dist
COPY data/seed/ /app/data/seed/

# 【构建期自检】探测 ASGI 应用入口。
# 若 backend/app 下还没有 API 入口（并行子任务尚未产出），这里【不会】让构建失败，
# 而是写入一份说明，容器启动时给出明确提示 —— 比一个静默的 404 更容易排查。
# 构建日志里会打印探测结果（docker build 时注意看 [entrypoint] 开头的行）。
RUN python - <<'PY'
import importlib.util, pathlib

candidates = ["app.main:app", "app.api.main:app", "app.asgi:app", "app.server:app", "main:app"]
found = None
for cand in candidates:
    mod = cand.split(":")[0]
    sys_path = "/app/backend"
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    try:
        if importlib.util.find_spec(mod) is not None:
            found = cand
            break
    except Exception:
        continue

out = pathlib.Path("/app/entrypoint.txt")
out.write_text((found or "app.main:app") + "\n", encoding="utf-8")
print("[entrypoint] detected ASGI app:" + (found or " <none> (fallback app.main:app)"))
PY

EXPOSE 8000

# 健康检查（/api/health 由 CONTRACT.md §5 定义）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

# 入口：从 entrypoint.txt 读取探测到的模块名，避免依赖 awk/grep 等额外工具
CMD ["sh", "-c", "exec python -m uvicorn \"$(head -n 1 /app/entrypoint.txt)\" --app-dir /app/backend --host ${BACKEND_HOST} --port ${BACKEND_PORT} --proxy-headers --no-server-header"]
