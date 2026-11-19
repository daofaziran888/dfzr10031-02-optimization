"""智能告警接口：维护告警规则，覆盖保存配置、启用规则、停用规则等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert import AlertService
from app.services.calibration import calibration_service

router = APIRouter(prefix="/api/alert", tags=["智能告警"])

service = AlertService()

LIST_FIELDS = ["规则编号", "告警名称", "监控指标", "阈值条件", "告警级别", "通知方式", "静默时段", "规则状态"]
STATUSES = ["草稿", "已配置", "生效中", "已停用"]


@router.get("/calibration-consumption")
def calibration_consumption(
    model_id: str | None = Query(default=None, description="可选，按模型编号过滤校准表"),
) -> dict[str, Any]:
    """智能告警消费分层误报校准表：按规则生成可试用阈值范围与不可试用流量清单。

    每条记录都附带校准批次编号以及各层有效负例权重，作为告警判定依据。
    """
    consumption = calibration_service.trial_consumption(model_id=model_id)
    for item in consumption["items"]:
        item["判定依据"]["告警消费说明"] = (
            "仅当阈值落在可试用阈值范围内、且流量分层属于覆盖分层时才允许启用告警；"
            "不可试用流量清单中的分层需要继续复核后再纳入。"
        )
    return consumption


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出智能告警清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alert", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、已配置、生效中、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤智能告警列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    extra = {k: v for k, v in request.query_params.items() if k not in ("keyword", "status", "page", "size") and v.strip()}
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size, extra=extra)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条告警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条告警规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="告警规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条告警规则执行保存配置、启用规则、停用规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
