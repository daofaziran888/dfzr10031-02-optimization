"""分层误报校准业务规则。

口径要点（对应需求逐条落地）：
1. 数据来自模型编号关联的评估批次，只接收“已复核”的样本标签、分数、权重与流量分层。
2. 同一样本被两次复核给出不同标签时，按样本编号去重，权重在同一校准批次里只进入分母一次，
   同时登记冲突清单，以最新一次复核结论作为已确认标签。
3. 缺少负例的分层误报率置为 None（不可计算），绝不按零误报处理，直接判为不可试用。
4. 同一校准批次并发/重复提交通过锁 + 批次幂等键控制，只记一次已确认样本权重。
5. 小流量层与总体试用条件冲突时，分别给出可覆盖流量与暂未覆盖分层的待复核要求。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

EVAL_MODULE = "eval_batch"
BATCH_MODULE = "calibration_batch"
POSITIVE_LABEL = "正例"
NEGATIVE_LABEL = "负例"
LABELS = (POSITIVE_LABEL, NEGATIVE_LABEL)

DEFAULT_MAX_FPR = 0.05
DEFAULT_OVERALL_MIN_NEGATIVE_WEIGHT = 100.0

_submit_lock = threading.Lock()


class CalibrationService:
    # ------------------------------------------------------------------ 查询
    def list_eval_batches(self, model_id: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(EVAL_MODULE)
        if model_id:
            rows = [row for row in rows if row.get("模型编号") == model_id]
        return rows

    def get_eval_batch(self, eval_batch_id: int) -> dict[str, Any] | None:
        return store.find(EVAL_MODULE, eval_batch_id)

    def list_batches(
        self,
        *,
        model_id: str | None = None,
        eval_batch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(BATCH_MODULE))
        if model_id:
            rows = [row for row in rows if row.get("模型编号") == model_id]
        if eval_batch_id is not None:
            rows = [row for row in rows if row.get("评估批次编号") == self._eval_code(eval_batch_id)]
        return rows

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    # ------------------------------------------------------------ 样本确认
    def _confirmed_samples(self, eval_batch: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把评估批次的样本收敛成已确认样本：去重、识别复核冲突、剔除未复核样本。"""
        confirmed: list[dict[str, Any]] = []
        unreviewed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for sample in eval_batch.get("samples", []):
            sample_id = str(sample.get("sample_id") or "")
            if sample_id in seen:
                # 同一批次里同一样本只允许进入分母一次，重复登记直接忽略。
                continue
            reviews = list(sample.get("reviews") or [])
            if not reviews:
                unreviewed.append({"sample_id": sample_id, "layer": sample.get("layer")})
                continue
            review_labels = [str(item.get("label") or "") for item in reviews]
            corrected = sample.get("corrected_label")
            final_label = corrected if corrected else reviews[-1].get("label")
            confirmed.append(
                {
                    "sample_id": sample_id,
                    "layer": sample.get("layer"),
                    "score": float(sample.get("score") or 0.0),
                    "weight": float(sample.get("weight") or 1.0),
                    "final_label": final_label,
                    "conflict": len(set(review_labels)) > 1,
                    "review_labels": review_labels,
                    "review_count": len(reviews),
                }
            )
            seen.add(sample_id)
        return confirmed, unreviewed

    # ------------------------------------------------------------ 指标计算
    @staticmethod
    def _fpr_at(samples: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
        negatives = [item for item in samples if item["final_label"] == NEGATIVE_LABEL]
        neg_weight = round(sum(item["weight"] for item in negatives), 6)
        fp = [item for item in negatives if item["score"] >= threshold]
        fp_weight = round(sum(item["weight"] for item in fp), 6)
        fpr = round(fp_weight / neg_weight, 6) if neg_weight > 0 else None
        return {
            "threshold": threshold,
            "effective_negative_weight": neg_weight,
            "false_positive_weight": fp_weight,
            "false_positive_count": len(fp),
            "false_alarm_rate": fpr,
        }

    @staticmethod
    def _eligibility(point: dict[str, Any], min_negative_weight: float, max_fpr: float) -> dict[str, Any]:
        neg_weight = point["effective_negative_weight"]
        if neg_weight <= 0:
            eligible, reason = False, "缺少已确认负例，误报率不可计算，不能按零误报试用"
        elif neg_weight < min_negative_weight:
            eligible, reason = False, f"有效负例权重 {neg_weight:g} 低于最低要求 {min_negative_weight:g}"
        elif point["false_alarm_rate"] is None:
            eligible, reason = False, "缺少已确认负例，误报率不可计算"
        elif point["false_alarm_rate"] > max_fpr:
            eligible, reason = False, (
                f"误报率 {point['false_alarm_rate']:.4f} 超过上限 {max_fpr:.4f}"
            )
        else:
            eligible, reason = True, "满足试用条件"
        point = dict(point)
        point["eligible"] = eligible
        point["reason"] = reason
        point["min_negative_weight"] = min_negative_weight
        return point

    def _scope_metrics(
        self,
        scope: str,
        samples: list[dict[str, Any]],
        min_negative_weight: float,
        max_fpr: float,
        traffic_share: float | None,
        unreviewed_count: int,
    ) -> dict[str, Any]:
        negatives = [item for item in samples if item["final_label"] == NEGATIVE_LABEL]
        positives = [item for item in samples if item["final_label"] == POSITIVE_LABEL]
        total_weight = round(sum(item["weight"] for item in samples), 6)
        neg_weight = round(sum(item["weight"] for item in negatives), 6)
        pos_weight = round(sum(item["weight"] for item in positives), 6)
        conflicts = [item for item in samples if item["conflict"]]

        grid = sorted({item["score"] for item in samples}, reverse=True)
        thresholds = [
            self._eligibility(self._fpr_at(samples, threshold), min_negative_weight, max_fpr)
            for threshold in grid
        ]
        return {
            "scope": scope,
            "traffic_share": traffic_share,
            "min_negative_weight": min_negative_weight,
            "effective_sample_count": len(samples),
            "effective_sample_weight": total_weight,
            "positive_weight": pos_weight,
            "negative_weight": neg_weight,
            "negative_missing": neg_weight <= 0,
            "review_count": sum(item["review_count"] for item in samples),
            "conflict_count": len(conflicts),
            "conflict_sample_ids": [item["sample_id"] for item in conflicts],
            "unreviewed_count": unreviewed_count,
            "thresholds": thresholds,
        }

    def compute_calibration(
        self,
        eval_batch: dict[str, Any],
        *,
        max_fpr: float = DEFAULT_MAX_FPR,
        min_negative_weight: float = DEFAULT_OVERALL_MIN_NEGATIVE_WEIGHT,
    ) -> dict[str, Any]:
        confirmed, unreviewed = self._confirmed_samples(eval_batch)
        layer_meta = {
            item["layer"]: item for item in eval_batch.get("流量分层", [])
        }
        layer_names = list(dict.fromkeys(item["layer"] for item in confirmed))
        # 评估批次声明过、但当前没有已确认样本的分层也要显式暴露，防止被静默忽略。
        for name in layer_meta:
            if name not in layer_names:
                layer_names.append(name)

        unreviewed_by_layer: dict[str, int] = {}
        for item in unreviewed:
            unreviewed_by_layer[item["layer"]] = unreviewed_by_layer.get(item["layer"], 0) + 1

        layers: list[dict[str, Any]] = []
        for layer in layer_names:
            meta = layer_meta.get(layer, {})
            layer_samples = [item for item in confirmed if item["layer"] == layer]
            layers.append(
                self._scope_metrics(
                    layer,
                    layer_samples,
                    float(meta.get("min_negative_weight", min_negative_weight)),
                    max_fpr,
                    meta.get("traffic_share"),
                    unreviewed_by_layer.get(layer, 0),
                )
            )

        overall = self._scope_metrics(
            "总体",
            confirmed,
            min_negative_weight,
            max_fpr,
            1.0,
            len(unreviewed),
        )
        trial = self._build_trial(overall, layers, max_fpr, confirmed)
        conflicts = [
            {
                "sample_id": item["sample_id"],
                "layer": item["layer"],
                "weight": item["weight"],
                "review_labels": item["review_labels"],
                "final_label": item["final_label"],
                "conflict_type": "两次复核标签不一致，权重仅计入分母一次",
            }
            for item in confirmed
            if item["conflict"]
        ]
        return {
            "评估批次编号": eval_batch.get("评估批次编号"),
            "模型编号": eval_batch.get("模型编号"),
            "校准规则": {"max_fpr": max_fpr, "min_negative_weight": min_negative_weight},
            "layers": layers,
            "overall": overall,
            "trial": trial,
            "conflicts": conflicts,
            "unreviewed": unreviewed,
        }

    def _build_trial(
        self,
        overall: dict[str, Any],
        layers: list[dict[str, Any]],
        max_fpr: float,
        confirmed: list[dict[str, Any]],
    ) -> dict[str, Any]:
        eligible_points = [point for point in overall["thresholds"] if point["eligible"]]
        trialable = bool(eligible_points)
        threshold_min = min(point["threshold"] for point in eligible_points) if eligible_points else None
        threshold_max = max(point["threshold"] for point in eligible_points) if eligible_points else None

        covered: list[str] = []
        covered_guidance: list[dict[str, Any]] = []
        uncovered: list[dict[str, Any]] = []
        for layer in layers:
            reason = None
            if layer["negative_missing"]:
                reason = "缺少已确认负例，误报率不可计算，不能按零误报试用"
            elif layer["negative_weight"] < layer["min_negative_weight"]:
                reason = (
                    f"有效负例权重 {layer['negative_weight']:g} 低于分层最低要求 "
                    f"{layer['min_negative_weight']:g}"
                )
            elif trialable:
                # 小流量层与总体条件可能冲突：总体试用下界处该分层未必安全，
                # 因此以“分层在总体可试用区间内是否存在自身合格的阈值区间”为覆盖口径。
                safe_points = [
                    point for point in layer["thresholds"]
                    if point["eligible"]
                    and threshold_min <= point["threshold"] <= threshold_max
                ]
                if safe_points:
                    covered.append(layer["scope"])
                    covered_guidance.append(
                        {
                            "layer": layer["scope"],
                            "safe_threshold_min": min(point["threshold"] for point in safe_points),
                            "safe_threshold_max": max(point["threshold"] for point in safe_points),
                        }
                    )
                else:
                    reason = (
                        f"总体可试用区间 [{threshold_min:g}, {threshold_max:g}] 内该分层误报率均超上限 "
                        f"{max_fpr:g}，需要继续复核分层样本"
                    )
            else:
                reason = "总体无满足条件的阈值，分层无法随总体试用"
            if reason is not None:
                uncovered.append(
                    {
                        "layer": layer["scope"],
                        "traffic_share": layer.get("traffic_share"),
                        "effective_negative_weight": layer["negative_weight"],
                        "unreviewed_count": layer["unreviewed_count"],
                        "required_extra_negative_weight": round(
                            max(0.0, layer["min_negative_weight"] - layer["negative_weight"]), 6
                        ),
                        "reason": reason,
                    }
                )

        shares = {item["scope"]: item.get("traffic_share") or 0.0 for item in layers}
        return {
            "trialable": trialable,
            "max_fpr": max_fpr,
            "threshold_min": threshold_min,
            "threshold_max": threshold_max,
            "covered_layers": covered,
            "covered_layer_guidance": covered_guidance,
            "covered_traffic_share": round(sum(shares.get(name, 0.0) for name in covered), 6),
            "uncovered_layers": uncovered,
        }

    # ------------------------------------------------------------ 提交（幂等）
    def submit_calibration(
        self,
        eval_batch_id: int,
        *,
        max_fpr: float = DEFAULT_MAX_FPR,
        min_negative_weight: float = DEFAULT_OVERALL_MIN_NEGATIVE_WEIGHT,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """提交校准批次。并发提交同一评估批次+同一规则时只落一次已确认样本权重。"""
        with _submit_lock:
            eval_batch = self.get_eval_batch(eval_batch_id)
            if eval_batch is None:
                return None, f"评估批次 {eval_batch_id} 不存在", False
            if eval_batch.get("status") != "已复核":
                return None, f"评估批次 {eval_batch.get('评估批次编号')} 尚未完成复核，不能提交校准", False

            rows = store.rows(BATCH_MODULE)
            for row in rows:
                same_request = request_id and row.get("request_id") == request_id
                same_rule = (
                    row.get("评估批次编号") == eval_batch.get("评估批次编号")
                    and float(row["校准规则"]["max_fpr"]) == float(max_fpr)
                    and float(row["校准规则"]["min_negative_weight"]) == float(min_negative_weight)
                )
                if same_request or same_rule:
                    return row, "相同校准批次已确认，重复/并发提交未重复计入样本权重", True

            result = self.compute_calibration(
                eval_batch,
                max_fpr=max_fpr,
                min_negative_weight=min_negative_weight,
            )
            version = (
                max(
                    (int(row.get("版本号", 0)) for row in rows
                     if row.get("评估批次编号") == eval_batch.get("评估批次编号")),
                    default=0,
                )
                + 1
            )
            seq = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            confirmed_weight = {
                layer["scope"]: layer["effective_sample_weight"] for layer in result["layers"]
            }
            confirmed_weight["总体"] = result["overall"]["effective_sample_weight"]
            entry = {
                "id": seq,
                "status": "已确认",
                "pending": False,
                "abnormal": False,
                "校准批次编号": f"CALIB-{eval_batch_id:04d}-{version:03d}",
                "评估批次编号": eval_batch.get("评估批次编号"),
                "模型编号": eval_batch.get("模型编号"),
                "版本号": version,
                "request_id": request_id,
                "校准规则": {"max_fpr": max_fpr, "min_negative_weight": min_negative_weight},
                "已确认样本权重": confirmed_weight,
                "result": result,
                "trial": result["trial"],
                "创建时间": datetime.now().isoformat(timespec="seconds"),
            }
            rows.append(entry)
            return entry, "校准批次已确认，样本权重仅计入一次", False

    # ------------------------------------------------------------ 复核更正重算
    def correct_label(
        self,
        eval_batch_id: int,
        sample_id: str,
        new_label: str,
        *,
        operator: str = "值班员",
        reason: str = "",
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        if new_label not in LABELS:
            return None, f"更正标签只能是 {LABELS[0]} 或 {LABELS[1]}", None
        eval_batch = self.get_eval_batch(eval_batch_id)
        if eval_batch is None:
            return None, f"评估批次 {eval_batch_id} 不存在", None
        target = next(
            (item for item in eval_batch.get("samples", []) if item.get("sample_id") == sample_id),
            None,
        )
        if target is None:
            return None, f"样本 {sample_id} 不在评估批次 {eval_batch.get('评估批次编号')} 中", None

        # 更正重算继承该评估批次最近一次确认的校准规则；尚未提交过则回落到默认口径。
        existing = [
            row for row in store.rows(BATCH_MODULE)
            if row.get("评估批次编号") == eval_batch.get("评估批次编号")
        ]
        if existing:
            latest_rule = max(existing, key=lambda row: int(row.get("版本号", 0)))["校准规则"]
            rule = {
                "max_fpr": float(latest_rule["max_fpr"]),
                "min_negative_weight": float(latest_rule["min_negative_weight"]),
            }
        else:
            rule = {
                "max_fpr": DEFAULT_MAX_FPR,
                "min_negative_weight": DEFAULT_OVERALL_MIN_NEGATIVE_WEIGHT,
            }

        before = self.compute_calibration(
            eval_batch,
            max_fpr=rule["max_fpr"],
            min_negative_weight=rule["min_negative_weight"],
        )
        old_label = target.get("corrected_label") or (target.get("reviews") or [{}])[-1].get("label")
        target["corrected_label"] = new_label
        target["corrected_by"] = operator
        target["corrected_at"] = datetime.now().isoformat(timespec="seconds")
        target["correction_reason"] = reason
        after = self.compute_calibration(
            eval_batch,
            max_fpr=rule["max_fpr"],
            min_negative_weight=rule["min_negative_weight"],
        )

        rows = store.rows(BATCH_MODULE)
        version = (
            max(
                (int(row.get("版本号", 0)) for row in rows
                 if row.get("评估批次编号") == eval_batch.get("评估批次编号")),
                default=0,
            )
            + 1
        )
        seq = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        confirmed_weight = {
            layer["scope"]: layer["effective_sample_weight"] for layer in after["layers"]
        }
        confirmed_weight["总体"] = after["overall"]["effective_sample_weight"]
        entry = {
            "id": seq,
            "status": "已确认",
            "pending": False,
            "abnormal": False,
            "校准批次编号": f"CALIB-{eval_batch_id:04d}-{version:03d}",
            "评估批次编号": eval_batch.get("评估批次编号"),
            "模型编号": eval_batch.get("模型编号"),
            "版本号": version,
            "request_id": None,
            "校准规则": after["校准规则"],
            "已确认样本权重": confirmed_weight,
            "更正记录": {
                "sample_id": sample_id,
                "old_label": old_label,
                "new_label": new_label,
                "operator": operator,
                "reason": reason,
            },
            "result": after,
            "trial": after["trial"],
            "创建时间": datetime.now().isoformat(timespec="seconds"),
        }
        rows.append(entry)
        change = {
            "sample_id": sample_id,
            "old_label": old_label,
            "new_label": new_label,
            "before_trial": before["trial"],
            "after_trial": after["trial"],
        }
        return entry, f"样本 {sample_id} 标签已更正为{new_label}，校准批次已重算", change

    # ------------------------------------------------------------ 告警消费视图
    def trial_consumption(self, model_id: str | None = None) -> dict[str, Any]:
        """给智能告警消费的分层误报校准表：可试用阈值范围 + 不可试用流量清单 + 判定依据。"""
        latest: dict[str, dict[str, Any]] = {}
        for row in store.rows(BATCH_MODULE):
            if model_id and row.get("模型编号") != model_id:
                continue
            key = str(row.get("评估批次编号"))
            if key not in latest or int(row.get("版本号", 0)) > int(latest[key].get("版本号", 0)):
                latest[key] = row

        items: list[dict[str, Any]] = []
        for row in latest.values():
            result = row["result"]
            trial = result["trial"]
            threshold_min = trial["threshold_min"]
            overall = result["overall"]
            overall_point = (
                self._fpr_at(
                    [
                        {
                            "final_label": item["final_label"],
                            "weight": item["weight"],
                            "score": item["score"],
                        }
                        for layer in result["layers"]
                        for item in self._flatten_layer_samples(result, layer["scope"])
                    ],
                    threshold_min,
                )
                if threshold_min is not None
                else None
            )
            layer_evidence = []
            for layer in result["layers"]:
                layer_point = (
                    self._fpr_at(self._flatten_layer_samples(result, layer["scope"]), threshold_min)
                    if threshold_min is not None
                    else None
                )
                layer_evidence.append(
                    {
                        "layer": layer["scope"],
                        "traffic_share": layer["traffic_share"],
                        "effective_negative_weight": layer["negative_weight"],
                        "effective_sample_weight": layer["effective_sample_weight"],
                        "effective_sample_count": layer["effective_sample_count"],
                        "false_alarm_rate_at_min_threshold": (
                            layer_point["false_alarm_rate"] if layer_point else None
                        ),
                        "negative_missing": layer["negative_missing"],
                        "conflict_count": layer["conflict_count"],
                    }
                )
            items.append(
                {
                    "模型编号": row.get("模型编号"),
                    "评估批次编号": row.get("评估批次编号"),
                    "校准批次编号": row.get("校准批次编号"),
                    "版本号": row.get("版本号"),
                    "可试用": trial["trialable"],
                    "可试用阈值范围": (
                        {"min": trial["threshold_min"], "max": trial["threshold_max"]}
                        if trial["trialable"]
                        else None
                    ),
                    "误报率上限": trial["max_fpr"],
                    "可覆盖流量占比": trial["covered_traffic_share"],
                    "覆盖分层": trial["covered_layers"],
                    "不可试用流量清单": trial["uncovered_layers"],
                    "判定依据": {
                        "校准批次编号": row.get("校准批次编号"),
                        "总体有效负例权重": overall["negative_weight"],
                        "总体有效样本权重": overall["effective_sample_weight"],
                        "总体误报率_阈值下界": (
                            overall_point["false_alarm_rate"] if overall_point else None
                        ),
                        "分层有效负例权重": layer_evidence,
                    },
                }
            )
        return {"generated_at": datetime.now().isoformat(timespec="seconds"), "items": items}

    def _flatten_layer_samples(self, result: dict[str, Any], layer: str) -> list[dict[str, Any]]:
        """从评估批次重新取出某分层的已确认样本，保证消费视图与最新口径一致。"""
        eval_code = result["评估批次编号"]
        eval_batch = next(
            (row for row in store.rows(EVAL_MODULE) if row.get("评估批次编号") == eval_code),
            None,
        )
        if eval_batch is None:
            return []
        confirmed, _ = self._confirmed_samples(eval_batch)
        return [item for item in confirmed if item["layer"] == layer]

    def _eval_code(self, eval_batch_id: int) -> str | None:
        row = self.get_eval_batch(eval_batch_id)
        return row.get("评估批次编号") if row else None


calibration_service = CalibrationService()
