"""分层误报校准的示例评估批次。

场景设定（与业务验收口径对齐）：
- 评估批次 EVAL-2026-001 挂在模型 MODE-0001 下，分两个流量层：
  * 大流量层 100 个已复核样本，流量占比 0.9；
  * 小流量层 10 个已复核样本，流量占比 0.1。
- 大流量层里 A-C001 被两位复核员给出过不同标签（最新结论为负例），
  用于验证同一校准批次里冲突样本的权重只能进入分母一次。
- 大流量层里 A-N001 权重为 2、分数 0.93，是后续“复核更正标签”的目标样本：
  更正前阈值 0.91 会因它误报率超限，更正为正例后试用阈值下界下移到 0.91。

这里只依赖标准库，保证种子数据可重复生成。
"""
from __future__ import annotations

from typing import Any


def _review(label: str, reviewer: str, reviewed_at: str, note: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "label": label,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
    }
    if note:
        record["note"] = note
    return record


def _sample(
    sample_id: str,
    layer: str,
    score: float,
    label: str,
    *,
    weight: float = 1.0,
    reviews: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "layer": layer,
        "score": round(float(score), 4),
        "weight": float(weight),
        # 复核更正前 corrected_label 为空，以最新一次复核结论为准。
        "corrected_label": None,
        "reviews": reviews if reviews is not None else [_review(label, "复核员甲", "2026-11-18T09:00:00")],
    }


def _build_large_layer() -> list[dict[str, Any]]:
    """大流量层：100 个样本，负例有效权重 97（含一个权重 2 的样本与一个冲突样本）。"""
    layer = "大流量层"
    samples: list[dict[str, Any]] = []

    # 正例最高分控制在 0.93 以下，保证试用阈值上界由负例分数断点决定。
    positive_scores = [0.88, 0.82, 0.66, 0.44]
    for index, score in enumerate(positive_scores, start=1):
        samples.append(_sample(f"A-P{index:03d}", layer, score, "正例"))

    # N001 权重 2、分数 0.93：更正前是抬高 0.91/0.92 阈值误报率的关键样本。
    samples.append(_sample("A-N001", layer, 0.93, "负例", weight=2.0))
    # N002 卡在 0.92，N003..N006 共 4 个负例卡在 0.91。
    samples.append(_sample("A-N002", layer, 0.92, "负例"))
    for index in range(3, 7):
        samples.append(_sample(f"A-N{index:03d}", layer, 0.91, "负例"))
    # N007、N008 卡在 0.90：让 0.90 断点误报率越过 5%，使试用下界停在 0.91/0.92。
    for index in range(7, 9):
        samples.append(_sample(f"A-N{index:03d}", layer, 0.90, "负例"))
    # N009..N095 共 87 个负例，分数从 0.70 有序下移，全部低于 0.90。
    for index in range(9, 96):
        score = 0.70 - (index - 9) * 0.007
        samples.append(_sample(f"A-N{index:03d}", layer, score, "负例"))

    # 冲突样本：两次复核标签不一致，最新结论为负例，权重只能计一次。
    samples.append(
        _sample(
            "A-C001",
            layer,
            0.58,
            "负例",
            reviews=[
                _review("正例", "复核员甲", "2026-11-18T09:10:00", note="首轮复核判为正例"),
                _review("负例", "复核员乙", "2026-11-18T10:20:00", note="二次复核改判负例"),
            ],
        )
    )
    return samples


def _build_small_layer() -> list[dict[str, Any]]:
    """小流量层：10 个样本，仅 8 个负例，有效负例权重不足以单独试用。"""
    layer = "小流量层"
    samples: list[dict[str, Any]] = []
    samples.append(_sample("B-P001", layer, 0.80, "正例"))
    samples.append(_sample("B-P002", layer, 0.47, "正例"))
    negative_scores = [0.62, 0.55, 0.44, 0.38, 0.31, 0.24, 0.16, 0.08]
    for index, score in enumerate(negative_scores, start=1):
        samples.append(_sample(f"B-N{index:03d}", layer, score, "负例"))
    return samples


def build_eval_batches() -> list[dict[str, Any]]:
    """生成挂在分析模型下的已复核评估批次种子数据。"""
    samples = _build_large_layer() + _build_small_layer()
    return [
        {
            "id": 1,
            "status": "已复核",
            "pending": False,
            "abnormal": False,
            "评估批次编号": "EVAL-2026-001",
            "模型编号": "MODE-0001",
            "批次名称": "十一月分层误报评估批次",
            "评估时间": "2026-11-18",
            "流量分层": [
                {"layer": "大流量层", "traffic_share": 0.9, "min_negative_weight": 90.0},
                {"layer": "小流量层", "traffic_share": 0.1, "min_negative_weight": 30.0},
            ],
            "samples": samples,
        }
    ]
