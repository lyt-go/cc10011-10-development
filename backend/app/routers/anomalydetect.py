"""异常检测接口：维护检测模型，并承接训练、部署快照与异常事件数据集的数据流。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.anomalydetect import (
    STAGE_ORDER,
    SUPPORTED_ALGORITHMS,
    AnomalydetectError,
    service,
)

router = APIRouter(prefix="/api/anomalydetect", tags=["异常检测"])

LIST_FIELDS = ["模型编号", "监控指标", "检测算法", "训练窗口天", "灵敏度参数", "季节周期", "节假日因子", "模型更新时间"]
STATUSES = ["草稿", "模型训练中", "已部署", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模型编号检索"),
    status: str | None = Query(default=None, description="草稿、模型训练中、已部署、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模型编号与状态过滤异常检测列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/options")
def list_options() -> dict[str, Any]:
    """页面下拉选项与阶段口径，避免前端自己维护一份状态枚举。"""
    return {
        "statuses": STATUSES,
        "stage_order": STAGE_ORDER,
        "algorithms": sorted(SUPPORTED_ALGORITHMS),
    }


@router.get("/snapshots")
def list_snapshots(
    model_code: str | None = Query(default=None, description="按模型编号检索"),
    status: str | None = Query(default=None, description="生效中、已停用"),
) -> dict[str, Any]:
    """检测模型部署快照：每次部署生成一条，带上部署版本与模型版本。"""
    items = service.list_snapshots(model_code=model_code, status=status)
    return {"items": items, "total": len(items)}


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int) -> dict[str, Any]:
    """读取单条部署快照，用于追溯异常事件来自哪次训练。"""
    snapshot = service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"部署快照 {snapshot_id} 不存在或已归档")
    return snapshot


@router.get("/datasets")
def list_datasets(
    batch: str | None = Query(default=None, description="按业务批次检索"),
    status: str | None = Query(default=None, description="待训练、模型训练中、快照已生成、已完成"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """异常检测事件数据集列表：一个业务批次只有一份数据集。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_datasets(batch=batch, status=status, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.post("/datasets", response_model=ActionResult)
def generate_dataset(payload: EntryPayload) -> ActionResult:
    """按部署快照生成异常检测事件数据集；同一批次重复提交不会产生第二份。"""
    try:
        dataset, message = service.generate_dataset(payload.values)
    except AnomalydetectError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=dataset is not None, message=message, entry=dataset)


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: int) -> dict[str, Any]:
    """读取数据集明细与它包含的异常事件。"""
    dataset = service.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"异常检测事件数据集 {dataset_id} 不存在或已归档")
    return dataset


@router.get("/events")
def list_events(
    dataset_id: int | None = Query(default=None, description="按数据集过滤"),
    batch: str | None = Query(default=None, description="按业务批次过滤"),
    model_code: str | None = Query(default=None, description="按模型编号过滤"),
    only_anomaly: bool = Query(default=False, description="只看异常事件"),
) -> dict[str, Any]:
    """异常事件明细：告警历史等页面复用同一份记录，不另存一套。"""
    items = service.query_events(
        dataset_id=dataset_id, batch=batch, model_code=model_code, only_anomaly=only_anomaly
    )
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出异常检测清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "anomalydetect", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条检测模型明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"检测模型 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条检测模型，缺字段时说明原因而不是静默丢弃。"""
    try:
        entry, missing = service.create_entry(payload.values)
    except AnomalydetectError as error:
        return ActionResult(ok=False, message=str(error))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="检测模型已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交训练、部署模型、停用模型；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(entry_id, action, payload.values)
    except AnomalydetectError as error:
        return ActionResult(ok=False, message=str(error))
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
