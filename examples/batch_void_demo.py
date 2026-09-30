#!/usr/bin/env python3
"""食品换线放行服务 —— 批次作废端到端调用示例（仅使用 Python 标准库）。

演示流程（独立产线 LVOID，避免与其他示例的批次冲突）：
  1. 登记原料批号 VB-RM-M（milk）与四个顺序批次：
       VB1 使用 milk 且声明 milk -> RELEASE，产线留下 milk 残留
       VB2 误登记、实际未投产：无原料，清洁 milk（登记时裁决 RELEASE，稍后作废）
       VB3 无原料、未清洁 -> 登记时上一有效批为 VB2（无残留）-> RELEASE
       VB4 无原料，清洁 milk -> 登记与作废后均 RELEASE
  2. 创建并确认核验单 VV-<时间戳>-1：[VB1, VB2]（含将被作废的批次）；
     另建并确认 VV-<时间戳>-2：[VB1, VB4]（只含作废后仍有效的批次）。
  3. 拒绝场景：未知批次作废 404、空原因 400。
  4. 作废 VB2（填写原因）：
       原登记/历史裁决/资格保留、批次号不可复用；
       VB3、VB4 按原顺序重算并追加版本（trigger=VOID_BATCH）：
       残留链跨过 VB2 接到 VB1，VB3 未清洁 -> 由 RELEASE 翻转为 HOLD；
       VB4 清洁 milk -> 结论不变（RELEASE）但仍追加版本；
       两张已确认核验单全部转 PENDING_REVIEW（历次确认快照不改）。
  5. 重复作废：changed=false，返回既有结果且不重算。
  6. 作废批查询：批次详情/列表/历史标明作废状态与原因；
     出货资格为冻结快照，不参与当前资格统计，批号锁定不联动。
  7. 作废批不得更正（409）、批次号不可复用（409）。
  8. 含作废批的旧单据 VV-1 可查询，但重新核验 409、确认 422（逐批 BATCH_VOIDED）；
     不含作废批的 VV-2 重新核验后可再次确认，历次确认快照保留。
  9. 新核验单引用作废批次被拒（400）。

用法：
    python examples/batch_void_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/batch_void_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, line_id: str, payload_extra: dict) -> None:
    payload = {"batch_no": batch_no, "line_id": line_id, **payload_extra}
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        print(
            f"  已登记批次 {batch_no}（{line_id}）=> 裁决 {adj['disposition']} v{adj['version']}，"
            f"实际 {adj['actual_allergens']}"
        )
    elif status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记批次失败: {body}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    line = "LVOID"
    lot = "VB-RM-M"

    section("1. 准备数据：原料批号 + 顺序批次 VB1..VB4")
    ensure_lot(lot, "作废示例全脂奶粉", ["milk"])
    ensure_batch("VB1", line, {
        "material_lots": [lot],
        "declared_allergens": ["milk"],
        "cleaned_allergens": [],
    })
    ensure_batch("VB2", line, {  # 误登记、实际未投产：清洁 milk，登记时 RELEASE
        "material_lots": [],
        "declared_allergens": [],
        "cleaned_allergens": ["milk"],
    })
    ensure_batch("VB3", line, {  # 登记时上一批 VB2 无残留 -> RELEASE
        "material_lots": [],
        "declared_allergens": [],
        "cleaned_allergens": [],
    })
    ensure_batch("VB4", line, {  # 清洁 milk -> RELEASE
        "material_lots": [],
        "declared_allergens": [],
        "cleaned_allergens": ["milk"],
    })

    doc1 = f"VV-{suffix}-1"
    doc2 = f"VV-{suffix}-2"
    section(f"2. 确认核验单 {doc1}:[VB1, VB2] 与 {doc2}:[VB1, VB4]")
    status, body = call("POST", "/api/verifications", {
        "verification_no": doc1, "batch_nos": ["VB1", "VB2"],
    })
    assert status == 201, body
    status, body = call("POST", f"/api/verifications/{doc1}/confirm")
    assert status == 200 and body["status"] == "CONFIRMED", body
    print(f"  {doc1} 已确认（含将被作废的误登记批 VB2）")

    status, body = call("POST", "/api/verifications", {
        "verification_no": doc2, "batch_nos": ["VB1", "VB4"],
    })
    assert status == 201, body
    status, body = call("POST", f"/api/verifications/{doc2}/confirm")
    assert status == 200 and body["status"] == "CONFIRMED", body
    print(f"  {doc2} 已确认（只含作废后仍有效的批次）")

    section("3. 拒绝场景：未知批次 404 / 空原因 400")
    status, body = call("POST", "/api/batches/NO-SUCH-BATCH/void", {"reason": "x"})
    print(f"[{status}] 作废未知批次: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "BATCH_NOT_FOUND", body
    status, body = call("POST", "/api/batches/VB1/void", {"reason": "   "})
    print(f"[{status}] 空原因作废: {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_PAYLOAD", body

    section("4. 作废误登记批 VB2（填写原因）：后续有效批按原顺序重算")
    status, voided = call("POST", "/api/batches/VB2/void", {
        "reason": "生产现场核实为误登记，该批次实际未投产",
    })
    show("作废结果（VB3/VB4 重算追加版本；两张已确认单转待复核）", status, voided)
    assert status == 200 and voided["changed"] is True, voided
    assert [s["batch_no"] for s in voided["recomputed"]] == ["VB3", "VB4"]
    rec = {s["batch_no"]: s for s in voided["recomputed"]}
    # VB3：跨过 VB2 接到 VB1，milk 残留未清洁 -> RELEASE 翻转为 HOLD（追加 v2）
    assert rec["VB3"]["disposition"] == "HOLD" and rec["VB3"]["actual"] == ["milk"]
    assert rec["VB3"]["version"] == 2
    # VB4：清洁掉带入的 milk -> 结论不变（RELEASE）但仍追加 v2
    assert rec["VB4"]["disposition"] == "RELEASE" and rec["VB4"]["actual"] == []
    assert rec["VB4"]["version"] == 2
    affected = {v["verification_no"]: v["status"] for v in voided["affected_verifications"]}
    assert affected == {doc1: "PENDING_REVIEW", doc2: "PENDING_REVIEW"}, affected
    # 作废批自身不产生新版本（冻结在 v1）
    assert call("GET", "/api/batches/VB2")[1]["adjudication"]["version"] == 1

    section("5. 重复作废：幂等返回既有结果，不重算")
    status, again = call("POST", "/api/batches/VB2/void", {"reason": "再次作废"})
    show("重复作废（预期 changed=false、recomputed 为空）", status, again)
    assert status == 200 and again["changed"] is False, again
    assert again["recomputed"] == []
    assert again["void"]["reason"] == "生产现场核实为误登记，该批次实际未投产"
    # 后续批版本不增加
    assert call("GET", "/api/batches/VB3")[1]["adjudication"]["version"] == 2

    section("6. 作废批保留可查、状态标明；资格冻结、不参与当前统计")
    status, vb2 = call("GET", "/api/batches/VB2")
    show("VB2 详情（void 负载 + 冻结裁决/资格）", status, vb2)
    assert vb2["voided"] is True and vb2["void"]["reason"]
    assert vb2["adjudication"]["version"] == 1          # 作废时点冻结
    assert vb2["shipment_eligibility"]["version"] == 1  # 资格冻结
    hist = call("GET", "/api/batches/VB2/history")[1]
    assert hist["voided"] is True and len(hist["history"]) == 1

    status, listing = call("GET", f"/api/batches?line_id={line}")
    print(f"[{status}] 批次列表（作废批仍在，带 voided 标记）:")
    for b in listing["batches"]:
        print(f"    {b['batch_no']}: voided={b['voided']} 裁决={b['adjudication']['disposition']}")
    assert {b["batch_no"] for b in listing["batches"]} == {"VB1", "VB2", "VB3", "VB4"}

    section(f"7. 含作废批旧单据 {doc1}：可查询，但重新核验/确认均拒绝")
    status, doc = call("GET", f"/api/verifications/{doc1}")
    show(f"{doc1} 详情（has_voided_batches=true，确认快照保留）", status, doc)
    assert doc["has_voided_batches"] is True and doc["voided_batches"] == ["VB2"]
    assert len(doc["confirmations"]) == 1  # 历次确认快照不改
    status, body = call("POST", f"/api/verifications/{doc1}/reverify")
    print(f"[{status}] 重新核验: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_HAS_VOIDED_BATCH", body
    status, body = call("POST", f"/api/verifications/{doc1}/confirm")
    show("确认（预期 422，逐批 BATCH_VOIDED）", status, body)
    assert status == 422, body
    assert [d["reason_code"] for d in body["error"]["details"]] == ["BATCH_VOIDED"]
    assert len(call("GET", f"/api/verifications/{doc1}")[1]["confirmations"]) == 1

    section(f"8. 不含作废批的 {doc2}：重新核验后再次确认，快照历史追加保留")
    status, body = call("POST", f"/api/verifications/{doc2}/reverify")
    assert status == 200 and body["status"] == "DRAFT", body
    # VB4 重算后仍 RELEASE；VB3 虽翻转为 HOLD 但不在单据内
    status, body = call("POST", f"/api/verifications/{doc2}/confirm")
    show(f"{doc2} 二次确认（confirmations 追加到 2 份）", status, body)
    assert status == 200 and len(body["confirmations"]) == 2, body
    first = {b["batch_no"]: b for b in body["confirmations"][0]["batches"]}
    second = {b["batch_no"]: b for b in body["confirmations"][1]["batches"]}
    assert first["VB4"]["version"] == 1 and second["VB4"]["version"] == 2

    section("9. 当前资格统计排除作废批；批号锁定不联动作废批")
    status, elig = call("GET", f"/api/eligibilities?line_id={line}")
    active = [e["batch_no"] for e in elig["eligibilities"]]
    print(f"[{status}] 当前出货资格统计批次: {active}（不含作废批 VB2）")
    assert "VB2" not in active and set(active) == {"VB1", "VB3", "VB4"}
    status, locked = call("POST", f"/api/material-lots/{lot}/lock", {"reason": "锁定联动验证"})
    assert {a["batch_no"] for a in locked["affected_batches"]} == {"VB1"}, locked
    frozen = call("GET", "/api/batches/VB2")[1]["shipment_eligibility"]
    assert frozen["version"] == 1 and frozen["status"] == "SHIPPABLE"
    print("  VB2 出货资格保持冻结 v1/SHIPPABLE，锁定未联动")
    call("POST", f"/api/material-lots/{lot}/unlock", {"reason": "联动验证完成，解除"})

    section("10. 作废批不得更正、批次号不可复用")
    status, body = call("PUT", "/api/batches/VB2", {"declared_allergens": ["milk"]})
    print(f"[{status}] 更正作废批: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "BATCH_VOIDED", body
    status, body = call("POST", "/api/batches", {
        "batch_no": "VB2", "line_id": line, "material_lots": [],
        "declared_allergens": [], "cleaned_allergens": [],
    })
    print(f"[{status}] 复用作废批次号: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "DUPLICATE_BATCH", body

    section("11. 新核验单引用作废批次被拒")
    status, body = call("POST", "/api/verifications", {
        "verification_no": f"VV-{suffix}-3", "batch_nos": ["VB1", "VB2"],
    })
    print(f"[{status}] 新建含作废批核验单: {body['error']['code']} — {body['error']['message']}")
    assert status == 400 and body["error"]["code"] == "BATCH_VOIDED", body

    section("完成")
    print(f"作废批 VB2 全程保留可查；{doc2} 已二次确认，{doc1} 含作废批不能重新核验/确认。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
