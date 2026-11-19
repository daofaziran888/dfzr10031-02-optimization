"""分层误报校准验收脚本（只依赖标准库）。

运行：cd backend && python3 -m tests.test_calibration
"""
from __future__ import annotations

import threading

from app.calibration_seed import build_eval_batches
from app.services.calibration import (
    DEFAULT_MAX_FPR,
    DEFAULT_OVERALL_MIN_NEGATIVE_WEIGHT,
    CalibrationService,
)
from app.store import store

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def scope(result: dict, scope: str) -> dict:
    return next(layer for layer in result["layers"] if layer["scope"] == scope)


def main() -> int:
    # 用一份独立的服务实例跑验收，避免与服务运行态相互污染。
    service = CalibrationService()
    eval_batch_id = 1
    eval_batch = service.get_eval_batch(eval_batch_id)
    check("评估批次已挂载到模型 MODE-0001", eval_batch["模型编号"] == "MODE-0001", eval_batch["模型编号"])

    # ---------- 场景一：一层 100 人、一层 10 人，生成试用资格 ----------
    entry, message, duplicated = service.submit_calibration(
        eval_batch_id,
        max_fpr=DEFAULT_MAX_FPR,
        min_negative_weight=DEFAULT_OVERALL_MIN_NEGATIVE_WEIGHT,
        request_id="req-1",
    )
    check("首次提交确认成功", entry is not None and not duplicated, message)
    check("校准批次编号为 CALIB-0001-001", entry["校准批次编号"] == "CALIB-0001-001", entry["校准批次编号"])

    result = entry["result"]
    large = scope(result, "大流量层")
    small = scope(result, "小流量层")

    # 大流量层 100 个样本：冲突样本 A-C001 的两次复核只能计一次。
    check("大流量层去重后样本数 100", large["effective_sample_count"] == 100, large["effective_sample_count"])
    check(
        "大流量层有效样本权重 101（A-N001 权重 2，冲突样本不重复计入）",
        large["effective_sample_weight"] == 101.0,
        large["effective_sample_weight"],
    )
    # 负例：95 个普通负例（N001 权重 2）+ 冲突样本最新结论为负例 -> 负例权重 97。
    check("大流量层有效负例权重 97", large["negative_weight"] == 97.0, large["negative_weight"])
    check("冲突样本被登记且只出现一次", large["conflict_count"] == 1 and large["conflict_sample_ids"] == ["A-C001"])
    check(
        "总体有效样本权重 111（101+10，冲突权重未重复进分母）",
        result["overall"]["effective_sample_weight"] == 111.0,
        result["overall"]["effective_sample_weight"],
    )
    check(
        "总体有效负例权重 105（97+8）",
        result["overall"]["negative_weight"] == 105.0,
        result["overall"]["negative_weight"],
    )
    check("小流量层 10 个样本", small["effective_sample_count"] == 10, small["effective_sample_count"])
    check("小流量层负例权重 8", small["negative_weight"] == 8.0, small["negative_weight"])

    # 阈值口径：更正前 0.93/0.92 合格、0.91 超限。
    def point(metrics: dict, threshold: float):
        return next(p for p in metrics["thresholds"] if abs(p["threshold"] - threshold) < 1e-6)

    def fpr(metrics: dict, threshold: float):
        return point(metrics, threshold)

    overall = result["overall"]
    check("总体阈值 0.93 误报率 2/105 且合格", fpr(overall, 0.93)["eligible"]
          and abs(fpr(overall, 0.93)["false_alarm_rate"] - (2.0 / 105.0)) < 1e-6)
    check("总体阈值 0.91 误报率 7/105 超过 5% 不可试用", not fpr(overall, 0.91)["eligible"]
          and abs(fpr(overall, 0.91)["false_alarm_rate"] - (7.0 / 105.0)) < 1e-6)
    check(
        "可试用阈值下界 0.92、上界 0.93",
        result["trial"]["threshold_min"] == 0.92 and result["trial"]["threshold_max"] == 0.93,
        result["trial"],
    )
    check(
        "大流量层安全阈值指引为 [0.92, 0.93]",
        result["trial"]["covered_layer_guidance"] == [
            {"layer": "大流量层", "safe_threshold_min": 0.92, "safe_threshold_max": 0.93}
        ],
        result["trial"]["covered_layer_guidance"],
    )

    # 小流量层：不是零误报逻辑，而是负例权重不足 -> 不可试用流量清单。
    uncovered = {item["layer"]: item for item in result["trial"]["uncovered_layers"]}
    check("小流量层进入不可试用清单", "小流量层" in uncovered, result["trial"])
    check(
        "小流量层待补负例权重 22（30-8）",
        uncovered["小流量层"]["required_extra_negative_weight"] == 22.0,
        uncovered.get("小流量层"),
    )
    check("可覆盖流量占比 0.9（仅大流量层）", result["trial"]["covered_traffic_share"] == 0.9)

    # ---------- 场景二：并发/重复提交只确认一次 ----------
    responses: list[tuple] = []

    def worker(rid: str) -> None:
        responses.append(
            service.submit_calibration(
                eval_batch_id,
                max_fpr=DEFAULT_MAX_FPR,
                min_negative_weight=DEFAULT_OVERALL_MIN_NEGATIVE_WEIGHT,
                request_id=rid,
            )
        )

    threads = [threading.Thread(target=worker, args=(f"req-{i}",)) for i in range(2, 8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    batch_rows = [row for row in store.rows("calibration_batch")
                  if row["评估批次编号"] == "EVAL-2026-001"]
    check("并发提交后仍只有 1 个校准批次", len(batch_rows) == 1, len(batch_rows))
    check("所有并发响应都返回去重提示", all(item[2] for item in responses), responses)
    check(
        "已确认样本权重只记一次（总体 111）",
        batch_rows[0]["已确认样本权重"]["总体"] == 111.0,
        batch_rows[0]["已确认样本权重"],
    )

    # ---------- 场景三：缺少负例的分层不能当成零误报 ----------
    no_negative = CalibrationService()
    batch = {
        "评估批次编号": "EVAL-NO-NEG",
        "模型编号": "MODE-TEST",
        "status": "已复核",
        "流量分层": [
            {"layer": "无负例层", "traffic_share": 0.3, "min_negative_weight": 10.0},
            {"layer": "正常层", "traffic_share": 0.7, "min_negative_weight": 10.0},
        ],
        "samples": [
            {"sample_id": "X1", "layer": "无负例层", "score": 0.9, "weight": 1.0,
             "corrected_label": None, "reviews": [{"label": "正例", "reviewer": "甲"}]},
            *[
                {"sample_id": f"N{i}", "layer": "正常层", "score": 0.2 + i * 0.01, "weight": 1.0,
                 "corrected_label": None, "reviews": [{"label": "负例", "reviewer": "甲"}]}
                for i in range(15)
            ],
        ],
    }
    no_neg_result = no_negative.compute_calibration(batch)
    no_neg_layer = scope(no_neg_result, "无负例层")
    check("缺负例层误报率为 None 而不是 0",
          all(point["false_alarm_rate"] is None for point in no_neg_layer["thresholds"]))
    check("缺负例层所有阈值均不可试用",
          all(not point["eligible"] for point in no_neg_layer["thresholds"]))
    uncovered_no_neg = {item["layer"]: item for item in no_neg_result["trial"]["uncovered_layers"]}
    check("缺负例层进入不可试用流量清单并说明原因",
          "无负例层" in uncovered_no_neg and "不能按零误报" in uncovered_no_neg["无负例层"]["reason"])

    # ---------- 场景四：复核更正大层一条标签后重新计算 ----------
    new_entry, new_message, change = service.correct_label(
        eval_batch_id, "A-N001", "正例", operator="值班员乙", reason="复核确认该样本为真实正样本"
    )
    check("更正成功并生成新版本", new_entry["版本号"] == 2, new_message)
    after = new_entry["result"]
    after_large = scope(after, "大流量层")
    after_small = scope(after, "小流量层")

    # 分母核对：A-N001 权重 2 从负例移到正例，样本总数不变。
    check("更正后大流量层样本数仍为 100（去重口径不变）",
          after_large["effective_sample_count"] == 100, after_large["effective_sample_count"])
    check("更正后大流量层负例权重 95（97-2）",
          after_large["negative_weight"] == 95.0, after_large["negative_weight"])
    check("更正后总体负例权重 103（105-2）",
          after["overall"]["negative_weight"] == 103.0, after["overall"]["negative_weight"])
    check("小流量层分母不受影响仍为 8",
          after_small["negative_weight"] == 8.0, after_small["negative_weight"])

    # 阈值核对：0.93 处不再误报，0.91 处误报 5/103=4.85%，下界下移到 0.91。
    check("更正后总体阈值 0.93 误报率为 0",
          fpr(after["overall"], 0.93)["false_alarm_rate"] == 0.0)
    check(
        "更正后总体阈值 0.91 误报率 5/103 且合格",
        fpr(after["overall"], 0.91)["eligible"]
        and abs(fpr(after["overall"], 0.91)["false_alarm_rate"] - (5.0 / 103.0)) < 1e-6,
    )
    check(
        "可试用阈值范围扩大为 [0.91, 0.93]",
        after["trial"]["threshold_min"] == 0.91 and after["trial"]["threshold_max"] == 0.93,
        after["trial"],
    )
    check("试用覆盖仍只有大流量层，覆盖流量 0.9",
          after["trial"]["covered_layers"] == ["大流量层"]
          and after["trial"]["covered_traffic_share"] == 0.9)
    check(
        "总体下界 0.91 处大流量层自身超限，分层安全指引仍为 [0.92, 0.93]",
        after["trial"]["covered_layer_guidance"] == [
            {"layer": "大流量层", "safe_threshold_min": 0.92, "safe_threshold_max": 0.93}
        ],
        after["trial"]["covered_layer_guidance"],
    )

    # ---------- 场景五：智能告警消费分层误报校准表 ----------
    consumption = service.trial_consumption()
    check("消费表只取每个评估批次的最新版本",
          len(consumption["items"]) == 1 and consumption["items"][0]["版本号"] == 2)
    item = consumption["items"][0]
    check("消费表给出可试用阈值范围", item["可试用阈值范围"] == {"min": 0.91, "max": 0.93})
    check("消费表附带不可试用流量清单",
          [row["layer"] for row in item["不可试用流量清单"]] == ["小流量层"])
    basis = item["判定依据"]
    check("判定依据附校准批次编号", basis["校准批次编号"] == "CALIB-0001-002")
    check("判定依据附总体有效负例权重 103", basis["总体有效负例权重"] == 103.0)
    layer_weights = {row["layer"]: row["effective_negative_weight"] for row in basis["分层有效负例权重"]}
    check("判定依据附各层有效负例权重 95/8", layer_weights == {"大流量层": 95.0, "小流量层": 8.0}, layer_weights)
    check("判定依据各层附阈值下界处误报率",
          all(row["false_alarm_rate_at_min_threshold"] is not None for row in basis["分层有效负例权重"]))

    print("\n评估批次种子规模：", {row["评估批次编号"]: len(row["samples"]) for row in build_eval_batches()})
    print(f"\n验收结果：{len(FAILURES)} 项失败")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
