"""失败重试接口：维护重试策略，并支撑失败批次的重试续做。

固定路径（/dataset、/queue、/batches、/export）必须声明在 /{entry_id} 之前，
否则会被动态段吞掉（之前 /export 命中 {entry_id} 直接 422）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.retry import RetryService

router = APIRouter(prefix="/api/retry", tags=["失败重试"])

service = RetryService()

LIST_FIELDS = ["策略编号", "关联端点", "最大重试", "退避倍数", "初始间隔秒", "最大间隔秒", "重试条件", "降级端点", "策略版本"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]
DATASET_STATUSES = ["待重试", "重试中", "已成功", "已失败", "冲突待裁决"]


# ------------------------------------------------------------------
# 重试数据集与失败批次
# ------------------------------------------------------------------
@router.get("/dataset", response_model=PageResult[dict])
def list_dataset(
    keyword: str | None = Query(default=None, description="按请求编号检索"),
    batch: str | None = Query(default=None, description="按批次编号过滤"),
    status: str | None = Query(default=None, description="待重试、重试中、已成功、已失败、冲突待裁决"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """重试数据集列表：每行是一个原始请求，退避快照随请求固定。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_dataset(keyword=keyword, batch=batch, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/dataset/{dataset_id}", response_model=dict)
def get_dataset_detail(dataset_id: int) -> dict:
    """重试详情：数据集行串起完整尝试序列。"""
    detail = service.get_dataset_detail(dataset_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"重试数据集记录 {dataset_id} 不存在")
    return detail


@router.post("/dataset/{dataset_id}/resume", response_model=ActionResult)
def resume_request(dataset_id: int, payload: EntryPayload) -> ActionResult:
    """续做单条请求：只处理未完成尝试，已成功的绝不重发。"""
    outcome = str(payload.values.get("结果") or "成功").strip()
    interrupted = bool(payload.values.get("中断"))
    entry, message = service.resume_request(dataset_id, outcome, interrupted)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/dataset/{dataset_id}/report", response_model=ActionResult)
def report_late_failure(dataset_id: int, payload: EntryPayload) -> ActionResult:
    """补报失败：批次结束后收到的迟到失败上报，先撤销成功再挂起待裁决。"""
    result = str(payload.values.get("结果") or "失败").strip()
    if result != "失败":
        return ActionResult(ok=False, message="补报只接受失败结果，成功补报请走续做流程")
    entry, message = service.report_late_failure(dataset_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/dataset/{dataset_id}/adjudicate", response_model=ActionResult)
def adjudicate(dataset_id: int, payload: EntryPayload) -> ActionResult:
    """裁决冲突：重试回到待处理队列，终止置为终态失败；保证只留一个成功版本。"""
    decision = str(payload.values.get("裁决") or "").strip()
    entry, message = service.adjudicate(dataset_id, decision)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/queue", response_model=PageResult[dict])
def list_queue(
    batch: str | None = Query(default=None, description="按批次编号过滤"),
    status: str | None = Query(default=None, description="待处理、重试中、已完成、已终结"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """失败批次待处理队列：编入每次尝试结果。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_queue(batch=batch, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches", response_model=dict)
def list_batches() -> dict[str, Any]:
    """批次汇总：状态由数据集推导，残留未完成请求的批次不会显示成成功。"""
    summaries = service.list_batches()
    return {"items": summaries, "total": len(summaries)}


@router.post("/batches", response_model=ActionResult)
def register_batch(payload: EntryPayload) -> ActionResult:
    """登记失败批次：建立重试数据集，未完成请求写入待处理队列。"""
    summary, message = service.register_batch(payload.values)
    if summary is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=summary)


@router.post("/batches/{batch_code}/resume", response_model=ActionResult)
def resume_batch(batch_code: str, payload: EntryPayload) -> ActionResult:
    """续做整个失败批次：只处理未完成请求，跳过终态。"""
    outcome = str(payload.values.get("结果") or "成功").strip()
    interrupted = bool(payload.values.get("中断"))
    summary, message = service.resume_batch(batch_code, outcome, interrupted)
    if summary is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=summary)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出失败重试清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "retry", "total": total, "items": items}


# ------------------------------------------------------------------
# 重试策略（原有能力）
# ------------------------------------------------------------------
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤失败重试列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条重试策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="重试策略已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条重试策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重试策略 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条重试策略执行提交校验、启用策略、停用策略；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
