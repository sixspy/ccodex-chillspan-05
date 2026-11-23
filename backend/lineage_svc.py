"""冷链血缘边登记（跨代血缘纪要·甲路径当班封死）。

纪要定稿见 docs/lineage_memo.md：
- 甲路径：任一端点已封代，正式血缘边零增并拒收；不落地影子草稿，第三路不许。
- 同对端点增边抢血缘锁，后到留争用痕不落边。
- 偏离纪要记血缘偏航并拒写。
- 正式边落库定格两端当时代，审计按快照追溯。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, try_acquire
from models import LineageIn

router = APIRouter(tags=["lineage"])

MEMO_PATH = "甲"
MEMO_RULE = (
    "甲路径：任一端点已封代（目标端/来源端），正式血缘边零增并拒收；"
    "跨代不落地影子草稿，第三路不许；同对端点增边抢血缘锁，后到留争用痕不落边；"
    "偏离本纪要记血缘偏航并拒写。"
)

_GOVERNANCE_DDL = (
    """CREATE TABLE IF NOT EXISTS lineage_memo(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      path TEXT NOT NULL,
      rule TEXT NOT NULL,
      decided_by TEXT NOT NULL,
      decided_at TEXT NOT NULL,
      sealed INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS lineage_deviations(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      actor TEXT NOT NULL,
      detail TEXT NOT NULL,
      created_at TEXT NOT NULL
    )""",
)


def ensure_lineage_governance(conn) -> None:
    """治理表与代快照列幂等落地；纪要仅首次建表时定稿写入并当班封死。"""
    fresh = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lineage_memo'"
        ).fetchone()
        is None
    )
    for ddl in _GOVERNANCE_DDL:
        conn.execute(ddl)
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(lineage_edges)").fetchall()
    }
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE lineage_edges ADD COLUMN status TEXT NOT NULL DEFAULT '正式'"
        )
    if "from_generation" not in cols:
        conn.execute(
            "ALTER TABLE lineage_edges ADD COLUMN from_generation INTEGER"
        )
    if "to_generation" not in cols:
        conn.execute("ALTER TABLE lineage_edges ADD COLUMN to_generation INTEGER")
    if fresh:
        conn.execute(
            """INSERT INTO lineage_memo(path,rule,decided_by,decided_at,sealed)
               VALUES (?,?,?,?,1)""",
            (MEMO_PATH, MEMO_RULE, "系统定稿", now_iso()),
        )


def _memo_row(conn):
    return conn.execute(
        "SELECT * FROM lineage_memo ORDER BY id LIMIT 1"
    ).fetchone()


def _memo_healthy(row) -> bool:
    return (
        row is not None
        and row["path"] == MEMO_PATH
        and int(row["sealed"]) == 1
    )


def _record_deviation(conn, actor: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO lineage_deviations(actor,detail,created_at) VALUES (?,?,?)",
        (actor, detail, now_iso()),
    )
    write_audit(conn, actor, "血缘偏航", "lineage_memo", {"detail": detail})


def _sealed_gen(batch) -> bool:
    return bool(batch) and batch["status"] == "已封批"


_EDGE_SELECT = """
SELECT e.*, a.batch_code from_code, b.batch_code to_code
FROM lineage_edges e
JOIN temp_batches a ON a.id=e.from_batch_id
LEFT JOIN temp_batches b ON b.id=e.to_batch_id
"""


def _formal_edges(conn, where: str = "", params: tuple = ()) -> list[dict]:
    sql = f"{_EDGE_SELECT} WHERE e.status='正式' {where} ORDER BY e.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def formal_lineage_for_batch(conn, batch_id: int) -> dict:
    """批次页/封批清单/血缘页共用的正式血缘同源口径。"""
    return {
        "outbound": _formal_edges(conn, "AND e.from_batch_id=?", (batch_id,)),
        "inbound": _formal_edges(conn, "AND e.to_batch_id=?", (batch_id,)),
    }


_FAKE_WHERE = """
e.status='正式' AND (
  (a.status='已封批' AND a.sealed_at IS NOT NULL AND e.created_at > a.sealed_at) OR
  (b.status='已封批' AND b.sealed_at IS NOT NULL AND e.created_at > b.sealed_at)
)
"""


def fake_connections(conn, batch_id: int | None = None) -> list[dict]:
    """跨代伪连扫描：正式边任一端在落边时刻已封代即伪连。"""
    sql = f"""SELECT e.id, e.from_batch_id, e.to_batch_id, e.edge_type, e.created_at,
             a.batch_code from_code, b.batch_code to_code,
             a.status from_status, b.status to_status
             FROM lineage_edges e
             JOIN temp_batches a ON a.id=e.from_batch_id
             LEFT JOIN temp_batches b ON b.id=e.to_batch_id
             WHERE {_FAKE_WHERE}"""
    params: tuple = ()
    if batch_id is not None:
        sql += " AND (e.from_batch_id=? OR e.to_batch_id=?)"
        params = (batch_id, batch_id)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fake_connections_for_batch(conn, batch_id: int) -> list[dict]:
    return fake_connections(conn, batch_id)


@router.get("/api/lineage")
def list_edges(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ensure_lineage_governance(conn)
        return {"items": _formal_edges(conn)}


@router.post("/api/lineage")
def create_edge(body: LineageIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"lineage:{body.from_batch_id}:{body.to_batch_id}"
    with connect() as conn:
        ensure_lineage_governance(conn)
        if not try_acquire(conn, lock_key, user):
            write_audit(
                conn,
                user,
                "lineage_contention",
                lock_key,
                {"from_batch_id": body.from_batch_id, "to_batch_id": body.to_batch_id},
            )
            conn.commit()  # 争用痕必须落库
            raise HTTPException(409, "争用：血缘锁被占用，后到留争用痕不落边")
        memo = _memo_row(conn)
        if not _memo_healthy(memo):
            _record_deviation(
                conn, user, "纪要缺失、未当班封死或路径被改，落边偏离甲路径纪要"
            )
            release(conn, lock_key, user)
            conn.commit()
            raise HTTPException(409, "血缘偏航：偏离跨代血缘纪要，拒绝写入")
        src = conn.execute(
            "SELECT * FROM temp_batches WHERE id=?", (body.from_batch_id,)
        ).fetchone()
        if not src:
            release(conn, lock_key, user)
            conn.commit()
            raise HTTPException(404, "来源批次不存在")
        dst = None
        if body.to_batch_id:
            dst = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (body.to_batch_id,)
            ).fetchone()
            if not dst:
                release(conn, lock_key, user)
                conn.commit()
                raise HTTPException(404, "目标批次不存在")
        if _sealed_gen(dst) or _sealed_gen(src):
            end = "目标端" if _sealed_gen(dst) else "来源端"
            write_audit(
                conn,
                user,
                "lineage_reject",
                str(body.from_batch_id),
                {"reason": f"{end}已封代", "to_batch_id": body.to_batch_id},
            )
            release(conn, lock_key, user)
            conn.commit()
            raise HTTPException(409, f"{end}已封代：正式血缘边零增并拒收（纪要甲路径）")
        cur = conn.execute(
            """INSERT INTO lineage_edges(from_batch_id,to_batch_id,edge_type,detail,
               status,from_generation,to_generation,created_at)
               VALUES (?,?,?,?,'正式',?,?,?)""",
            (
                body.from_batch_id,
                body.to_batch_id,
                body.edge_type,
                body.detail,
                int(src["generation"]),
                int(dst["generation"]) if dst else None,
                now_iso(),
            ),
        )
        write_audit(
            conn,
            user,
            "lineage_add",
            str(body.from_batch_id),
            {
                "edge_id": cur.lastrowid,
                "type": body.edge_type,
                "from_generation": int(src["generation"]),
                "to_generation": int(dst["generation"]) if dst else None,
            },
        )
        release(conn, lock_key, user)
        conn.commit()
        return {
            "id": cur.lastrowid,
            "edge_type": body.edge_type,
            "status": "正式",
            "from_generation": int(src["generation"]),
            "to_generation": int(dst["generation"]) if dst else None,
        }


@router.get("/api/lineage/batch/{batch_id}")
def batch_lineage(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ensure_lineage_governance(conn)
        return formal_lineage_for_batch(conn, batch_id)


@router.get("/api/lineage/memo")
def get_memo(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ensure_lineage_governance(conn)
        row = _memo_row(conn)
        if not row:
            raise HTTPException(404, "跨代血缘纪要缺失")
        return dict(row)


@router.get("/api/lineage/deviations")
def list_deviations(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ensure_lineage_governance(conn)
        rows = conn.execute(
            "SELECT * FROM lineage_deviations ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/lineage/reconcile")
def reconcile_lineage(authorization: AuthHeader = None):
    """收工对账：全量扫描跨代伪连。"""
    require_user(authorization)
    with connect() as conn:
        ensure_lineage_governance(conn)
        fakes = fake_connections(conn)
        total = conn.execute(
            "SELECT COUNT(*) c FROM lineage_edges WHERE status='正式'"
        ).fetchone()["c"]
        return {"clean": not fakes, "fakes": fakes, "formal_edges": total}
