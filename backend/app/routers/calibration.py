"""分层误报校准接口。

数据链路：分析模型编号 -> 关联评估批次（已复核样本标签/分数/权重/流量分层）
-> 校准批次（分层误报率、有效样本量、试用阈值范围）-> 智能告警消费视图。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, CalibrationSubmit, LabelCorrection, PageResult
from app.services.calibration import calibration_service

router = APIRouter(prefix="/api/calibration", tags=["分层误报校准"])


@router.get("/eval-batches", response_model=PageResult[dict])
def list_eval_batches(
    model_id: str | None = Query(default=None, description="按模型编号过滤评估批次"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """列出模型关联的评估批次及其流量分层、样本与复核记录。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = calibration_service.list_eval_batches(model_id)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.get("/eval-batches/{eval_batch_id}")
def get_eval_batch(eval_batch_id: int) -> dict[str, Any]:
    """读取单个评估批次明细。"""
    row = calibration_service.get_eval_batch(eval_batch_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"评估批次 {eval_batch_id} 不存在")
    return row


@router.post("/eval-batches/{eval_batch_id}/submit", response_model=ActionResult)
def submit_calibration(eval_batch_id: int, payload: CalibrationSubmit) -> ActionResult:
    """基于已复核评估批次确认一个校准批次；重复/并发提交只确认一次权重。"""
    entry, message, duplicated = calibration_service.submit_calibration(
        eval_batch_id,
        max_fpr=payload.max_fpr,
        min_negative_weight=payload.min_negative_weight,
        request_id=payload.request_id,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"duplicated": duplicated, **entry})


@router.post("/eval-batches/{eval_batch_id}/samples/{sample_id}/correct", response_model=ActionResult)
def correct_sample_label(
    eval_batch_id: int, sample_id: str, payload: LabelCorrection
) -> ActionResult:
    """复核更正样本标签并按新标签重算，生成新版本校准批次。"""
    entry, message, change = calibration_service.correct_label(
        eval_batch_id,
        sample_id=sample_id,
        new_label=payload.new_label,
        operator=payload.operator,
        reason=payload.reason,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"change": change, **entry})


@router.get("/batches", response_model=PageResult[dict])
def list_calibration_batches(
    model_id: str | None = Query(default=None),
    eval_batch_id: int | None = Query(default=None),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """列出已确认的校准批次（含各版本）。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = calibration_service.list_batches(model_id=model_id, eval_batch_id=eval_batch_id)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.get("/batches/{batch_id}")
def get_calibration_batch(batch_id: int) -> dict[str, Any]:
    """读取单个校准批次的完整分层指标。"""
    row = calibration_service.get_batch(batch_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"校准批次 {batch_id} 不存在")
    return row


@router.get("/trial-consumption")
def trial_consumption(
    model_id: str | None = Query(default=None, description="可选，按模型编号过滤"),
) -> dict[str, Any]:
    """智能告警消费的分层误报校准表：试用阈值范围、不可试用流量清单与判定依据。"""
    return calibration_service.trial_consumption(model_id=model_id)
