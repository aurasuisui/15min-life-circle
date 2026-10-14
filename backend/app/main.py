"""FastAPI 应用入口。

  * 开发期 CORS 允许 `http://localhost:5173`（Vite 默认端口）；
  * 若 `frontend/dist` 存在则托管静态站点（同源，前端不接触服务端 AK）；
  * 统一错误响应 `{"error": {"code","message"}}`。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .api import errors as api_errors
from .config import ROOT

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

DESCRIPTION = """\
**「15 分钟生活圈」智能体检与规划助手 —— 后端 API**

* 等时圈：36 射线 × Δr=200 m + 阈值附近加密，一次采样出 5/10/15 分钟三档，
  IDW 重建耗时场 + marching squares 提等值线
* POI：2 组多关键字并集检索覆盖 8 类民生设施（按【请求】计费），
  6 级清洗流水线（归一化 → 去重 → 漂移校正 → 类型纠正 → 圈内裁剪）
* 盲区：解析耗时场零配额预筛 + 边界带真实算路校正 → 连通域聚合
* 降级：live → 缓存 → 快照 → 解析模型，四道防线 + 一张配额账本，**绝不静默**
"""

app = FastAPI(
    title="15min-life-circle API",
    description=DESCRIPTION,
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

_allowed = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://127.0.0.1:4173"]
_extra = [o.strip() for o in (os.environ.get("CORS_EXTRA_ORIGINS") or "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed + _extra,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

api_errors.install(app)
app.include_router(api_router)

# ---- 静态站点（frontend/dist），存在才挂载 ----
DIST = Path(ROOT) / "frontend" / "dist"
if DIST.exists() and (DIST / "index.html").exists():
    if (DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    async def _index():  # noqa: ANN202
        return FileResponse(str(DIST / "index.html"))

    @app.get("/{path:path}", include_in_schema=False)
    async def _spa(path: str):  # noqa: ANN202
        target = DIST / path
        if target.is_file():
            return FileResponse(str(target))
        return FileResponse(str(DIST / "index.html"))
else:
    @app.get("/", include_in_schema=False)
    async def _root():  # noqa: ANN202
        return JSONResponse({
            "service": "15min-life-circle",
            "note": "前端产物 frontend/dist 不存在；API 见 /api/docs",
            "endpoints": ["/api/health", "/api/geocode", "/api/audit", "/api/isochrone", "/api/seed/communities"],
        })


@app.on_event("shutdown")
def _shutdown() -> None:  # pragma: no cover
    try:
        from .baidu.client import get_client

        get_client().close()
    except Exception:
        pass
