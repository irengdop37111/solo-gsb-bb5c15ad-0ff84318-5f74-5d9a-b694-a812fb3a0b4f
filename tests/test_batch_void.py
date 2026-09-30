"""批次作废测试。

覆盖：
- 作废：未知批次 404、空原因 400；重复作废幂等返回既有结果且不重算（changed=false）；
- 保留：原登记、裁决当前快照与历史版本、出货资格与作废原因均可查询，批次号不可复用；
- 传递：作废批次不再参与同线残留传递，其后“有效”批次按原顺序重算并追加版本
  （trigger=VOID_BATCH，即使结论不变也追加），作废批自身不产生新版本；
- 资格：作废批不参与当前资格统计，批号锁定不联动、不触碰冻结资格；
- 核验单：涉及作废批或被重算批的已确认单转待复核，历次确认快照不改；
  含作废批的旧单据可查询，但重新核验 409、确认 422（逐批 BATCH_VOIDED）；
  新核验单引用作废批 400；作废批不得更正（409）；
- 查询：批次列表/详情/历史、核验单详情标明作废状态；有效批次既有接口保持兼容；
- 原子性：校验失败整事务回滚，不留下部分作废或部分版本。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def void(client, batch_no, reason):
    return client.post(f"/api/batches/{batch_no}/void", json={"reason": reason})


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


# ---------------------------------------------------------------- 拒绝与幂等


def test_void_unknown_batch_and_empty_reason_rejected(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])

    resp = void(client, "NOPE", "误登记")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"

    for bad in ("", "   "):
        resp = void(client, "B1", bad)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"
    # 失败不留作废标记
    assert client.get("/api/batches/B1").json()["voided"] is False


def test_repeat_void_is_idempotent_and_does_not_recompute(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[])

    resp = void(client, "B1", "首次作废原因")
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert body["void"]["reason"] == "首次作废原因"
    # 作废 B1 后 B2 重算一次（v2）
    assert [s["batch_no"] for s in body["recomputed"]] == ["B2"]

    resp = void(client, "B1", "再次作废原因")
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert body["void"]["reason"] == "首次作废原因"  # 保留首次原因
    assert body["recomputed"] == []
    assert body["affected_verifications"] == []
    # 不重算：B2 裁决版本保持 v2
    assert client.get("/api/batches/B2").json()["adjudication"]["version"] == 2
    hist = client.get("/api/batches/B2/history").json()["history"]
    assert [v["version"] for v in hist] == [1, 2]
    assert [v["trigger"] for v in hist] == ["REGISTER", "VOID_BATCH"]


# ---------------------------------------------------------------- 保留与不可复用


def test_void_keeps_registration_history_and_blocks_reuse(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=[])
    client.put("/api/batches/B1", json={"declared_allergens": ["peanut"]})

    assert void(client, "B1", "误登记，实际未投产").status_code == 200

    detail = client.get("/api/batches/B1").json()
    assert detail["voided"] is True
    assert detail["void"]["reason"] == "误登记，实际未投产"
    assert detail["void"]["voided_at"]
    # 原登记内容保留
    assert detail["material_lots"] == ["RM-P"]
    assert detail["declared_allergens"] == ["peanut"]
    # 裁决当前快照冻结在作废时点（v2 RELEASE），不作废产生新版本
    assert detail["adjudication"]["version"] == 2
    assert detail["adjudication"]["disposition"] == "RELEASE"
    # 历史裁决版本全部保留
    hist = client.get("/api/batches/B1/history").json()
    assert hist["voided"] is True
    assert [v["version"] for v in hist["history"]] == [1, 2]
    # 出货资格保留
    assert detail["shipment_eligibility"]["version"] == 1
    elig = client.get("/api/batches/B1/eligibility").json()
    assert elig["voided"] is True and elig["eligibility"]["status"] == "SHIPPABLE"

    # 批次号不可复用
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": "B1",
            "line_id": "L1",
            "material_lots": [],
            "declared_allergens": [],
            "cleaned_allergens": [],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_BATCH"


def test_voided_batch_cannot_be_corrected(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    void(client, "B1", "误登记")

    resp = client.put("/api/batches/B1", json={"declared_allergens": ["egg"]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"
    resp = client.put("/api/batches/B1/cleaning", json={"cleaned_allergens": ["milk"]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"
    # 更正失败不改动冻结裁决
    assert client.get("/api/batches/B1").json()["adjudication"]["version"] == 1


# ---------------------------------------------------------------- 残留传递与重算


def test_void_removes_batch_from_carryover_chain(client):
    # B1 使用 peanut 且声明 -> RELEASE，产线留下 peanut 残留
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    # B2 误登记（无原料、未声明）：继承 peanut -> HOLD（实际未投产，稍后作废）
    add_batch(client, "B2", lots=[], declared=[])
    # B3 在 B2 之后登记：登记时同样继承 peanut -> HOLD
    b3 = add_batch(client, "B3", lots=[], declared=[])
    assert b3["adjudication"]["disposition"] == "HOLD"
    assert b3["adjudication"]["actual_allergens"] == ["peanut"]

    # 作废 B2：B3 重新接到 B1 之后；B1 仍留 peanut、B3 未清洁 -> 仍 HOLD（结论不变但追加版本）
    resp = void(client, "B2", "误登记，实际未投产")
    assert resp.status_code == 200
    recomputed = {s["batch_no"]: s for s in resp.json()["recomputed"]}
    assert list(recomputed) == ["B3"]
    assert recomputed["B3"]["version"] == 2
    assert recomputed["B3"]["disposition"] == "HOLD"
    assert recomputed["B3"]["actual"] == ["peanut"]
    # 作废批自身不产生新版本
    assert client.get("/api/batches/B2").json()["adjudication"]["version"] == 1

    # B3 之后再登记 B4：残留链只含有效批 B1 -> B3，B2 不参与
    b4 = add_batch(client, "B4", lots=[], declared=[], cleaned=["peanut"])
    assert b4["adjudication"]["disposition"] == "RELEASE"
    assert b4["adjudication"]["actual_allergens"] == []


def test_void_middle_batch_can_flip_downstream_disposition(client):
    # B1：milk 声明 -> RELEASE（留 milk）
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    # B2：误登记批，无原料，未清洁 -> HOLD（继承 milk）
    add_batch(client, "B2", lots=[], declared=[])
    # B3：清洁 milk 后无原料 -> 登记时 RELEASE
    add_batch(client, "B3", lots=[], declared=[], cleaned=["milk"])

    # 作废 B2 后：B3 的前一有效批变为 B1，B1 留 milk、B3 清洁 milk -> 仍 RELEASE
    rec = void(client, "B2", "误登记").json()["recomputed"]
    assert [s["batch_no"] for s in rec] == ["B3"]
    assert rec[0]["disposition"] == "RELEASE" and rec[0]["version"] == 2

    # 跨作废批连接（L2 独立验证）：C1 引入 egg（声明，RELEASE，留 egg 残留）；
    # C2 误登记：无原料、不清洁 -> HOLD（继承 egg）；C3 同样无原料、不清洁。
    add_lot(client, "RM-E", "蛋液", ["egg"])
    add_batch(client, "C1", line_id="L2", lots=["RM-E"], declared=["egg"])
    add_batch(client, "C2", line_id="L2", lots=[], declared=[])
    add_batch(client, "C3", line_id="L2", lots=[], declared=[])
    assert client.get("/api/batches/C3").json()["adjudication"]["disposition"] == "HOLD"
    # 作废 C2：C3 重算后直接接 C1，egg 残留仍来自 C1（跨过作废批）
    rec2 = {s["batch_no"]: s for s in void(client, "C2", "误登记").json()["recomputed"]}
    assert list(rec2) == ["C3"]
    assert rec2["C3"]["actual"] == ["egg"]
    assert rec2["C3"]["disposition"] == "HOLD"
    # 新登记 C4（不清洁）：残留沿 C1 -> C3 传递，C2 永不参与
    c4 = add_batch(client, "C4", line_id="L2", lots=[], declared=[])
    assert c4["adjudication"]["actual_allergens"] == ["egg"]


def test_void_last_batch_has_no_recompute(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])

    body = void(client, "B2", "误登记").json()
    assert body["changed"] is True
    assert body["recomputed"] == []
    # B1 完全不受影响
    assert client.get("/api/batches/B1").json()["adjudication"]["version"] == 1


def test_void_is_line_isolated(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "A1", line_id="L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "A2", line_id="L1", lots=[], declared=[])
    add_batch(client, "C1", line_id="L2", lots=[], declared=[])

    # 作废 L1 的 A1：仅同线 A2 重算；L2 的 C1 不动
    body = void(client, "A1", "误登记").json()
    assert [s["batch_no"] for s in body["recomputed"]] == ["A2"]
    assert client.get("/api/batches/C1").json()["adjudication"]["version"] == 1


def test_new_batch_seq_ignores_void_for_carryover_but_keeps_ordering(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=[])  # 误登记，继承 milk -> HOLD
    void(client, "B2", "误登记")
    # B3 序号续接为 3（seq 不复用），但残留从上一有效批 B1 带入
    b3 = add_batch(client, "B3", lots=[], declared=[], cleaned=["milk"])
    assert b3["seq"] == 3
    assert b3["adjudication"]["disposition"] == "RELEASE"


# ---------------------------------------------------------------- 当前资格与锁定


def test_voided_batch_excluded_from_current_eligibility_stats(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=["RM-P"], declared=["peanut"])
    void(client, "B2", "误登记")

    rows = client.get("/api/eligibilities").json()["eligibilities"]
    assert [e["batch_no"] for e in rows] == ["B1"]
    # 按产线过滤同样排除
    rows = client.get("/api/eligibilities", params={"line_id": "L1"}).json()["eligibilities"]
    assert [e["batch_no"] for e in rows] == ["B1"]
    # 单批查询仍可看到冻结资格
    elig = client.get("/api/batches/B2/eligibility").json()
    assert elig["voided"] is True
    assert elig["eligibility"]["version"] == 1


def test_lot_lock_does_not_touch_voided_batch_eligibility(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=["RM-P"], declared=["peanut"])
    void(client, "B2", "误登记")

    resp = client.post("/api/material-lots/RM-P/lock", json={"reason": "待检"})
    affected = {a["batch_no"] for a in resp.json()["affected_batches"]}
    assert affected == {"B1"}  # 作废批不联动

    frozen = client.get("/api/batches/B2").json()["shipment_eligibility"]
    assert frozen["version"] == 1 and frozen["status"] == "SHIPPABLE"
    # 有效批正常递增
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 2

    # 解除/再锁同样不触碰作废批
    client.post("/api/material-lots/RM-P/unlock", json={"reason": "解除"})
    assert client.get("/api/batches/B2").json()["shipment_eligibility"]["version"] == 1
    hist = client.get("/api/batches/B2/eligibility").json()["history"]
    assert [v["version"] for v in hist] == [1]


def test_voided_batch_excluded_from_lot_correction_recompute(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=["RM-P"], declared=["peanut"])
    void(client, "B2", "误登记")

    # 原料批号更正：作废批 B2 不重算（裁决冻结），仅 B1 重算
    resp = client.put("/api/material-lots/RM-P", json={"allergens": ["peanut", "sesame"]})
    assert resp.status_code == 200
    assert [s["batch_no"] for s in resp.json()["recomputed"]] == ["B1"]
    assert client.get("/api/batches/B2").json()["adjudication"]["version"] == 1


# ---------------------------------------------------------------- 核验单联动


def test_confirmed_verifications_flip_to_pending_review(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])
    # 已确认单：含将被重算的后续批 B2
    create_verification(client, "V1", ["B1", "B2"])
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    body = void(client, "B1", "误登记").json()
    affected = {v["verification_no"]: v["status"] for v in body["affected_verifications"]}
    assert affected == {"V1": "PENDING_REVIEW"}
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"


def test_verification_containing_voided_batch_queryable_but_reverify_rejected(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])
    # 作废前先建一张含 B2 的草稿单
    create_verification(client, "VD", ["B1", "B2"])
    void(client, "B2", "误登记")

    doc = client.get("/api/verifications/VD").json()
    assert doc["status"] == "DRAFT"  # 草稿不联动转态
    assert doc["has_voided_batches"] is True
    assert doc["voided_batches"] == ["B2"]
    entry = {b["batch_no"]: b for b in doc["batches"]}["B2"]
    assert entry["voided"] is True
    assert entry["void"]["reason"] == "误登记"
    # 作废批的当前裁决仍可显示（冻结快照）
    assert entry["current"]["version"] == 1

    # 重新核验拒绝
    resp = client.post("/api/verifications/VD/reverify")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_HAS_VOIDED_BATCH"


def test_confirm_rejected_for_verification_with_voided_batch(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])
    create_verification(client, "VD", ["B1", "B2"])
    void(client, "B2", "误登记")

    resp = client.post("/api/verifications/VD/confirm")
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert [d["reason_code"] for d in details] == ["BATCH_VOIDED"]
    assert details[0]["batch_no"] == "B2"
    assert details[0]["void_reason"] == "误登记"
    # 整单不确认、不留确认记录
    doc = client.get("/api/verifications/VD").json()
    assert doc["status"] == "DRAFT"
    assert doc["confirmations"] == []


def test_confirmed_snapshot_unchanged_when_batch_voided(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    create_verification(client, "VC", ["B1"])
    assert client.post("/api/verifications/VC/confirm").status_code == 200

    void(client, "B1", "误登记")
    doc = client.get("/api/verifications/VC").json()
    assert doc["status"] == "PENDING_REVIEW"
    # 历次确认快照保留、内容不改
    assert len(doc["confirmations"]) == 1
    snap = doc["confirmations"][0]["batches"][0]
    assert snap["batch_no"] == "B1"
    assert snap["version"] == 1
    assert snap["disposition"] == "RELEASE"
    # 重新核验/确认均拒绝
    assert client.post("/api/verifications/VC/reverify").status_code == 409
    assert client.post("/api/verifications/VC/confirm").status_code == 422
    # 不新增确认记录
    assert len(client.get("/api/verifications/VC").json()["confirmations"]) == 1


def test_verification_without_voided_batch_can_reverify_and_reconfirm(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=["milk"])  # 继承 milk 且已声明 -> RELEASE
    add_batch(client, "B3", lots=[], declared=["milk"])  # 继续继承 milk
    create_verification(client, "V1", ["B2", "B3"])
    client.post("/api/verifications/V1/confirm")

    # 作废 B1（不在单据中）：B2、B3 重算（结论仍 RELEASE），已确认单转待复核
    body = void(client, "B1", "误登记").json()
    assert {s["batch_no"] for s in body["recomputed"]} == {"B2", "B3"}
    affected = {v["verification_no"]: v["status"] for v in body["affected_verifications"]}
    assert affected == {"V1": "PENDING_REVIEW"}
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"

    # 单据不含作废批：重新核验后可再次确认，历次确认快照保留
    assert client.post("/api/verifications/V1/reverify").status_code == 200
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    assert len(resp.json()["confirmations"]) == 2
    # 第一次确认快照停留在 v1，第二次为重算后的 v2
    first = {b["batch_no"]: b for b in resp.json()["confirmations"][0]["batches"]}
    second = {b["batch_no"]: b for b in resp.json()["confirmations"][1]["batches"]}
    assert first["B2"]["version"] == 1
    assert second["B2"]["version"] == 2


def test_new_verification_rejects_voided_batch(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    void(client, "B1", "误登记")

    resp = create_verification(client, "V1", ["B1"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"
    # 失败不留单据
    assert client.get("/api/verifications/V1").status_code == 404
    assert client.get("/api/verifications").json()["verifications"] == []


def test_voided_batch_listed_in_batch_queries_with_status(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])
    void(client, "B2", "误登记")

    rows = client.get("/api/batches").json()["batches"]
    by_no = {b["batch_no"]: b for b in rows}
    assert set(by_no) == {"B1", "B2"}  # 作废批仍在列表
    assert by_no["B2"]["voided"] is True
    assert by_no["B2"]["void"]["reason"] == "误登记"
    assert by_no["B1"]["voided"] is False
    assert by_no["B1"]["void"] is None
    # 按产线过滤同样带状态
    rows = client.get("/api/batches", params={"line_id": "L1"}).json()["batches"]
    assert {b["batch_no"] for b in rows} == {"B1", "B2"}


def test_failed_void_is_atomic_with_no_partial_versions(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[])
    v_before = client.get("/api/batches/B2").json()["adjudication"]["version"]

    # 空原因：整笔回滚，无作废记录、无重算版本
    resp = void(client, "B1", "   ")
    assert resp.status_code == 400
    assert client.get("/api/batches/B1").json()["voided"] is False
    assert client.get("/api/batches/B2").json()["adjudication"]["version"] == v_before
    assert len(client.get("/api/batches/B2/history").json()["history"]) == v_before
