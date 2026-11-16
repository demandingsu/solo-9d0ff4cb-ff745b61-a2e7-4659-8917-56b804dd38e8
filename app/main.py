"""FastAPI 应用：输入版本提交、异步生成、结果查询。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query
from fastapi.responses import RedirectResponse
from sqlite3 import Connection

from . import allocation as alloc
from . import db
from .config import DB_PATH, WORKER_THREADS
from .models import (
    AttributionOut,
    CurrentResultOut,
    GenerationOut,
    HealthOut,
    NewDistrictSubmission,
    OldGridSubmission,
    VersionOut,
)

# 单线程工作池：生成任务按提交顺序串行执行与发布
_executor: ThreadPoolExecutor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _executor
    db.init_db()
    _executor = ThreadPoolExecutor(
        max_workers=WORKER_THREADS, thread_name_prefix="generation"
    )
    yield
    _executor.shutdown(wait=True)
    _executor = None


app = FastAPI(
    title="城市统计片区重划分摊服务",
    description=(
        "整版提交旧网格（编号、整数矩形、户数）与新片区（编号、整数矩形），"
        "按重叠面积以最大余数法逐格分摊户数并守恒总户数。"
        "生成任务固定提交时刻的版本快照，生成期间可继续替换任一输入。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_db() -> Connection:
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


def _items_from_old(payload: OldGridSubmission) -> list[dict]:
    return [
        {
            "id": it.id,
            "rect": it.rect.as_tuple(),
            "households": it.households,
        }
        for it in payload.items
    ]


def _items_from_new(payload: NewDistrictSubmission) -> list[dict]:
    return [{"id": it.id, "rect": it.rect.as_tuple()} for it in payload.items]


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthOut, tags=["运维"])
def health(conn: Connection = Depends(get_db)):
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "db": str(DB_PATH)}


@app.post("/old-grids", response_model=VersionOut, status_code=201, tags=["输入版本"])
def submit_old_grid(payload: OldGridSubmission, conn: Connection = Depends(get_db)):
    """整版提交旧网格；替换旧输入即产生新版本（历史版本保留）。"""
    try:
        with conn:
            version_id = db.insert_version("old", _items_from_old(payload), conn)
    except db.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return db.get_version(version_id, conn)


@app.post(
    "/new-districts", response_model=VersionOut, status_code=201, tags=["输入版本"]
)
def submit_new_districts(
    payload: NewDistrictSubmission, conn: Connection = Depends(get_db)
):
    """整版提交新片区；替换旧输入即产生新版本（历史版本保留）。"""
    try:
        with conn:
            version_id = db.insert_version("new", _items_from_new(payload), conn)
    except db.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return db.get_version(version_id, conn)


@app.get("/versions", response_model=list[VersionOut], tags=["输入版本"])
def list_versions(
    kind: str | None = Query(None, pattern="^(old|new)$"),
    conn: Connection = Depends(get_db),
):
    """列出输入版本，可按 kind=old/new 过滤。"""
    return db.list_versions(kind, conn)


@app.get("/versions/{version_id}", response_model=VersionOut, tags=["输入版本"])
def get_version(
    version_id: int = PathParam(..., ge=1), conn: Connection = Depends(get_db)
):
    try:
        return db.get_version(version_id, conn)
    except db.NoVersionError:
        raise HTTPException(status_code=404, detail=f"版本 {version_id} 不存在")


@app.post(
    "/generations",
    response_model=GenerationOut,
    status_code=202,
    tags=["生成任务"],
)
def create_generation(conn: Connection = Depends(get_db)):
    """开始一次生成：同步固定当前最新 old/new 版本快照，计算在后台串行执行。

    快照在本请求的事务内确定；此后替换任一输入不影响本任务。
    """
    try:
        with conn:
            gen_id = db.create_generation(conn)
    except db.NoVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    assert _executor is not None
    _executor.submit(db.run_generation, gen_id, DB_PATH)
    return db.get_generation(gen_id, conn)


@app.get("/generations", response_model=list[GenerationOut], tags=["生成任务"])
def list_generations(conn: Connection = Depends(get_db)):
    return db.list_generations(conn)


@app.get("/generations/{gen_id}", response_model=GenerationOut, tags=["生成任务"])
def get_generation(
    gen_id: int = PathParam(..., ge=1), conn: Connection = Depends(get_db)
):
    row = db.get_generation(gen_id, conn)
    if row is None:
        raise HTTPException(status_code=404, detail=f"生成任务 {gen_id} 不存在")
    return row


@app.get("/result", response_model=CurrentResultOut, tags=["发布结果"])
def get_current_result(conn: Connection = Depends(get_db)):
    """当前已发布结果（片区汇总 + 两份输入版本）。

    新结果完整发布前，这里始终返回上一份已发布结果；从未发布过时返回 404。
    """
    result = db.get_current_result(conn)
    if result is None:
        raise HTTPException(status_code=404, detail="尚无已发布的生成结果")
    return result


@app.get("/attribution", response_model=AttributionOut, tags=["结果归因"])
def get_attribution(
    from_generation: int = Query(
        ..., ge=1, alias="from", description="前一期生成任务编号"
    ),
    to_generation: int = Query(
        ..., ge=1, alias="to", description="后一期生成任务编号"
    ),
    conn: Connection = Depends(get_db),
):
    """按两个成功生成任务编号，归因各片区户数变动。

    - 仅比较片区编号集合完全相同的两次生成，否则 422；
    - `from` 作为前一期、`to` 作为后一期；两者须为不同任务，交换后各项贡献取反；
    - 沿用各任务创建时固定的旧网格 / 新片区版本，不读取“当前版本”；
    - 分别计算“先换旧网格”与“先换新片区”两条折算路径，
      以两条路径贡献的平均值给出旧网格修订 / 新片区边界调整两项（允许半户）；
    - 任一交叉折算缺少完整覆盖时拒绝（422）；
    - 只读查询，不发布结果，也不受查询期间新版本发布影响。
    """
    if from_generation == to_generation:
        raise HTTPException(
            status_code=422, detail="需要两个不同的生成任务编号"
        )
    try:
        return db.attribute_generations(from_generation, to_generation, conn)
    except db.GenerationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except db.GenerationNotComparableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (
        alloc.AttributionDistrictMismatchError,
        alloc.AttributionCoverageError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except alloc.AttributionIntegrityError as exc:
        # 内部对账失败属于服务端缺陷，宁可 500 也不返回不可信数字
        raise HTTPException(
            status_code=500, detail=f"归因对账校验失败：{exc}"
        )
