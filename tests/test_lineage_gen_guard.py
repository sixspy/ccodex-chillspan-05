"""跨代伪连三断言（失败测甲/乙/争用）+ 纪要偏航 + 五页同源对账。

夹具走本地文件库：monkeypatch db.DB_PATH 到 tmp_path/t.db，init_db() 种子。
种子自带已封批批次 TB-CK-HD-01-002（G2）与未封批批次，可直接演跨代伪连。
口径定稿见 docs/lineage_memo.md（甲路径，当班封死）。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _sealed_and_open(c: TestClient, h: dict):
    batches = c.get("/api/batches", headers=h).json()["items"]
    sealed = next(b for b in batches if b["status"] == "已封批")
    opened = next(b for b in batches if b["status"] != "已封批")
    return sealed, opened


def _formal_edges(c: TestClient, h: dict) -> list:
    items = c.get("/api/lineage", headers=h).json()["items"]
    return [e for e in items if e.get("status", "正式") == "正式"]


def test_cross_gen_target_sealed_zero_add_reject():
    """失败测甲：目标端已封代 → 正式血缘边零增并拒收，跨代正式边不得存在。"""
    c = TestClient(app)
    h = _auth(c)
    sealed, opened = _sealed_and_open(c, h)
    before = _formal_edges(c, h)
    r = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": opened["id"],
            "to_batch_id": sealed["id"],
            "edge_type": "分装衍生",
            "detail": "跨代伪连夹具：未封代→已封代",
        },
    )
    assert r.status_code == 409
    assert "已封代" in r.json()["detail"]
    after = _formal_edges(c, h)
    assert len(after) == len(before)  # 正式边零增
    assert not any(
        e["from_batch_id"] == opened["id"] and e["to_batch_id"] == sealed["id"]
        for e in after
    )
    # 来源端已封代同律
    r2 = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": sealed["id"],
            "to_batch_id": opened["id"],
            "edge_type": "分装衍生",
            "detail": "跨代伪连夹具：已封代→未封代",
        },
    )
    assert r2.status_code == 409
    assert "已封代" in r2.json()["detail"]
    assert len(_formal_edges(c, h)) == len(before)


def test_shadow_unpromoted_never_counts_formal():
    """失败测乙：未获接纳的跨代连边（影子）不得计入正式血统。"""
    c = TestClient(app)
    h = _auth(c)
    sealed, opened = _sealed_and_open(c, h)
    before = _formal_edges(c, h)
    r = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": opened["id"],
            "to_batch_id": sealed["id"],
            "edge_type": "调拨挂靠",
            "detail": "影子边夹具",
        },
    )
    assert r.status_code == 409  # 未晋升（甲路径：拒收）
    formal = _formal_edges(c, h)
    assert len(formal) == len(before)  # 正式血统零改
    inbound = c.get(f"/api/lineage/batch/{sealed['id']}", headers=h).json()["inbound"]
    assert not any(e["from_batch_id"] == opened["id"] for e in inbound)
    detail = c.get(f"/api/batches/{sealed['id']}", headers=h).json()
    assert not any(
        e["from_batch_id"] == opened["id"] for e in detail["lineage"]["inbound"]
    )


def test_lineage_lock_contention_second_window_no_edge():
    """争用失败测：双窗同对已封代端点抢增边，后到留争用痕不落边。"""
    c = TestClient(app)
    h1 = _auth(c, "admin")
    h2 = _auth(c, "ops")
    sealed, opened = _sealed_and_open(c, h1)
    lock_key = f"lineage:{opened['id']}:{sealed['id']}"
    r1 = c.post("/api/locks/acquire", headers=h1, json={"lock_key": lock_key})
    assert r1.status_code == 200  # 窗口一持血缘锁
    before = c.get("/api/lineage", headers=h1).json()["items"]
    r2 = c.post(
        "/api/lineage",
        headers=h2,
        json={
            "from_batch_id": opened["id"],
            "to_batch_id": sealed["id"],
            "edge_type": "分装衍生",
            "detail": "双窗抢增边",
        },
    )
    assert r2.status_code == 409  # 后到被拒
    cont = c.get("/api/contention", headers=h1).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "ops" for x in cont
    )  # 留争用痕
    after = c.get("/api/lineage", headers=h1).json()["items"]
    assert len(after) == len(before)  # 不落边


def test_memo_deviation_marked_and_write_blocked():
    """偏离纪要记血缘偏航并拒写：纪要拆封或改第三路，合法边也拒。"""
    c = TestClient(app)
    h = _auth(c)
    memo = c.get("/api/lineage/memo", headers=h)
    assert memo.status_code == 200
    body = memo.json()
    assert body["path"] == "甲"
    assert body["sealed"] == 1
    batches = c.get("/api/batches", headers=h).json()["items"]
    opens = [b for b in batches if b["status"] != "已封批"]
    payload = {
        "from_batch_id": opens[0]["id"],
        "to_batch_id": opens[1]["id"],
        "edge_type": "分装衍生",
        "detail": "偏航探针",
    }
    for tamper in (
        "UPDATE lineage_memo SET sealed=0",
        "UPDATE lineage_memo SET sealed=1, path='丙'",
    ):
        with connect() as conn:
            conn.execute(tamper)
            conn.commit()
        r = c.post("/api/lineage", headers=h, json=payload)
        assert r.status_code == 409
        assert "偏航" in r.json()["detail"]
    dev = c.get("/api/lineage/deviations", headers=h).json()["items"]
    assert len(dev) >= 2
    assert not any(
        e["detail"] == "偏航探针"
        for e in c.get("/api/lineage", headers=h).json()["items"]
    )


def test_five_pages_share_one_source():
    """五页同源：血缘/批次/封批/温谱/仓间同库同口径。"""
    c = TestClient(app)
    h = _auth(c)
    batches = c.get("/api/batches", headers=h).json()["items"]
    opens = [b for b in batches if b["status"] != "已封批"]
    b1, b2 = opens[0], opens[1]
    r = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": b1["id"],
            "to_batch_id": b2["id"],
            "edge_type": "分装衍生",
            "detail": "同源对账边",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "正式"
    assert r.json()["from_generation"] == b1["generation"]
    assert r.json()["to_generation"] == b2["generation"]
    edge_id = r.json()["id"]
    # 血缘页数据
    edge = next(
        e
        for e in c.get("/api/lineage", headers=h).json()["items"]
        if e["id"] == edge_id
    )
    assert edge["status"] == "正式"
    # 批次页同源：详情血缘与血缘页逐字段一致
    d1 = c.get(f"/api/batches/{b1['id']}", headers=h).json()
    out_e = next(e for e in d1["lineage"]["outbound"] if e["id"] == edge_id)
    for k in (
        "from_batch_id",
        "to_batch_id",
        "status",
        "from_generation",
        "to_generation",
        "edge_type",
    ):
        assert out_e[k] == edge[k]
    d2 = c.get(f"/api/batches/{b2['id']}", headers=h).json()
    assert any(e["id"] == edge_id for e in d2["lineage"]["inbound"])
    # 新批走封批→温谱：封批页/温谱页/批次页代一致
    chambers = c.get("/api/chambers", headers=h).json()["items"]
    ch = next(x for x in chambers if x["chamber_code"] == "CK-HN-03")
    probes = c.get(f"/api/probes?chamber_id={ch['id']}", headers=h).json()["items"]
    nb = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probes[0]["id"],
            "cargo_ref": "同源对账货载",
            "temp_points": [{"t": "10:00", "c": 3.0}, {"t": "11:00", "c": 4.0}],
            "batch_code": "TB-SAME-SRC-001",
        },
    ).json()
    seal = c.post("/api/seals", headers=h, json={"batch_id": nb["id"]})
    assert seal.status_code == 200
    gen = seal.json()["generation"]
    sp = c.post("/api/spectrum/export", headers=h, json={"batch_id": nb["id"]})
    assert sp.status_code == 200
    assert sp.json()["export_ref"].endswith(f"G{gen}")
    assert any(
        x["export_ref"] == sp.json()["export_ref"]
        for x in c.get("/api/spectrum", headers=h).json()["items"]
    )
    detail = c.get(f"/api/batches/{nb['id']}", headers=h).json()["batch"]
    assert detail["generation"] == gen
    seals = c.get("/api/seals", headers=h).json()["items"]
    mani = next(s for s in seals if s["batch_code"] == "TB-SAME-SRC-001")
    snap = json.loads(mani["manifest"])
    assert mani["batch_generation"] == gen
    assert "lineage" in snap
    assert "inbound" in snap["lineage"] and "outbound" in snap["lineage"]
    # 仓间页对账平衡
    recon = c.get("/api/reconcile", headers=h).json()
    assert recon["summary"]["unbalanced"] == 0
    # 血缘对账无伪连
    rec = c.get("/api/lineage/reconcile", headers=h).json()
    assert rec["clean"] is True
    assert rec["fakes"] == []


def test_wrapup_reconcile_confirms_no_fake_links():
    """收工对账：连番伪连尝试后，血缘/批次/封批复核无伪连。"""
    c = TestClient(app)
    h = _auth(c)
    sealed, opened = _sealed_and_open(c, h)
    for frm, to in ((opened["id"], sealed["id"]), (sealed["id"], opened["id"])):
        r = c.post(
            "/api/lineage",
            headers=h,
            json={
                "from_batch_id": frm,
                "to_batch_id": to,
                "edge_type": "分装衍生",
                "detail": "收工对账探针",
            },
        )
        assert r.status_code == 409
    rec = c.get("/api/lineage/reconcile", headers=h).json()
    assert rec["clean"] is True
    assert rec["fakes"] == []
    with connect() as conn:
        rows = conn.execute(
            """SELECT e.id FROM lineage_edges e
               JOIN temp_batches a ON a.id=e.from_batch_id
               LEFT JOIN temp_batches b ON b.id=e.to_batch_id
               WHERE e.status='正式' AND (
                 (a.status='已封批' AND a.sealed_at IS NOT NULL
                  AND e.created_at > a.sealed_at) OR
                 (b.status='已封批' AND b.sealed_at IS NOT NULL
                  AND e.created_at > b.sealed_at))"""
        ).fetchall()
        assert rows == []
        gens = conn.execute(
            """SELECT b.id FROM temp_batches b
               WHERE b.status='已封批' AND b.generation != (
                 SELECT MAX(s.batch_generation) FROM seal_snapshots s
                 WHERE s.batch_id=b.id)"""
        ).fetchall()
        assert gens == []
