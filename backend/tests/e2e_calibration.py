"""端到端 HTTP 验收：通过真实 FastAPI 路由验证分层误报校准全链路。

运行：python3 tests/e2e_calibration.py
"""
from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8000"
failures: list[str] = []


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def check(name: str, condition: bool, detail: object = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


def main() -> int:
    # 1. 模型编号关联评估批次
    batches = call("GET", "/api/calibration/eval-batches?model_id=MODE-0001")
    check("按模型编号查到评估批次", batches["total"] == 1, batches)
    eval_batch = batches["items"][0]
    check("评估批次含 110 个已复核样本", len(eval_batch["samples"]) == 110)
    check("评估批次带两个流量分层", [x["layer"] for x in eval_batch["流量分层"]] == ["大流量层", "小流量层"])

    # 2. 首次提交校准
    first = call("POST", "/api/calibration/eval-batches/1/submit",
                 {"max_fpr": 0.05, "min_negative_weight": 100, "request_id": "e2e-1"})
    check("首次提交确认成功", first["ok"] and first["entry"]["duplicated"] is False, first)

    # 3. 并发重复提交（同一 request_id 与不同 request_id 各一组）
    import concurrent.futures

    def submit(rid: str) -> dict:
        return call("POST", "/api/calibration/eval-batches/1/submit",
                    {"max_fpr": 0.05, "min_negative_weight": 100, "request_id": rid})

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(submit, [f"e2e-{i}" for i in range(2, 8)]))
    check("并发提交全部返回 ok", all(item["ok"] for item in responses))
    check("并发提交全部命中幂等去重", all(item["entry"]["duplicated"] for item in responses))
    listed = call("GET", "/api/calibration/batches?eval_batch_id=1")
    check("并发后只有 1 个校准批次", listed["total"] == 1, listed["total"])
    check("已确认样本权重只记一次", listed["items"][0]["已确认样本权重"]["总体"] == 111.0)

    # 4. 首版试用资格核对
    result = listed["items"][0]["result"]
    trial = result["trial"]
    check("首版可试用阈值 [0.92, 0.93]",
          trial["threshold_min"] == 0.92 and trial["threshold_max"] == 0.93, trial)
    check("首版覆盖大流量层、流量 0.9",
          trial["covered_layers"] == ["大流量层"] and trial["covered_traffic_share"] == 0.9)
    check("小流量层在不可试用清单且待补负例权重 22",
          trial["uncovered_layers"][0]["layer"] == "小流量层"
          and trial["uncovered_layers"][0]["required_extra_negative_weight"] == 22.0)

    # 5. 复核更正大层 A-N001 标签后重算
    correction = call("POST", "/api/calibration/eval-batches/1/samples/A-N001/correct",
                      {"new_label": "正例", "operator": "值班员丙", "reason": "端到端复核更正"})
    check("更正成功", correction["ok"], correction)
    check("生成 002 版本", correction["entry"]["校准批次编号"] == "CALIB-0001-002")
    after = correction["entry"]["result"]
    check("更正后大层负例权重 95", after["layers"][0]["negative_weight"] == 95.0)
    check("更正后小层负例权重仍为 8", after["layers"][1]["negative_weight"] == 8.0)
    check("更正后总体负例权重 103", after["overall"]["negative_weight"] == 103.0)
    check("更正后试用阈值扩大到 [0.91, 0.93]",
          after["trial"]["threshold_min"] == 0.91 and after["trial"]["threshold_max"] == 0.93)

    # 6. 智能告警消费视图
    consumption = call("GET", "/api/alert/calibration-consumption")
    check("告警消费表有 1 条最新校准", len(consumption["items"]) == 1, consumption)
    item = consumption["items"][0]
    check("告警侧可试用阈值范围", item["可试用阈值范围"] == {"min": 0.91, "max": 0.93})
    check("告警侧不可试用流量清单", [x["layer"] for x in item["不可试用流量清单"]] == ["小流量层"])
    check("告警侧附校准批次编号", item["判定依据"]["校准批次编号"] == "CALIB-0001-002")
    layer_basis = {x["layer"]: x["effective_negative_weight"]
                   for x in item["判定依据"]["分层有效负例权重"]}
    check("告警侧附各层有效负例权重", layer_basis == {"大流量层": 95.0, "小流量层": 8.0}, layer_basis)
    check("告警侧附阈值下界处各层误报率",
          all(x["false_alarm_rate_at_min_threshold"] is not None
              for x in item["判定依据"]["分层有效负例权重"]))

    # 7. 同规则再提交仍幂等，不产生第三个版本
    again = call("POST", "/api/calibration/eval-batches/1/submit",
                 {"max_fpr": 0.05, "min_negative_weight": 100, "request_id": "e2e-again"})
    check("更正后同规则提交仍去重", again["entry"]["duplicated"] is True)
    listed2 = call("GET", "/api/calibration/batches?eval_batch_id=1")
    check("校准批次仍为 2 个版本", listed2["total"] == 2, listed2["total"])

    print(f"\n端到端验收结果：{len(failures)} 项失败")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
