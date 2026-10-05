"""退役电芯成组放行 API。

流程：录入电芯 -> 预选（只读，返回候选与检测版本）-> 确认（单事务重查版本与
占用状态，原子占用整组并写入不可改写的检测快照）。
"""
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import db
from .schemas import CellCreate, ConfirmRequest, PreselectRequest, TestRevision
from .selection import choose_group

app = FastAPI(title="退役电芯成组放行 API", version="1.0.0")


def _row_to_cell(row) -> dict:
    return {
        "cell_id": row["cell_id"],
        "chemistry": row["chemistry"],
        "capacity": row["capacity"],
        "resistance": row["resistance"],
        "test_version": row["test_version"],
        "occupied": bool(row["occupied"]),
        "group_id": row["group_id"],
    }


# ---------- 录入与复测 ----------

@app.post("/cells", status_code=201)
def create_cell(body: CellCreate):
    """录入一节待选电芯（最多 12 节）。"""
    conn = db.get_conn()
    with db.write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM cells").fetchone()["n"]
            if count >= db.MAX_CELLS:
                raise HTTPException(409, f"待选电芯已达上限 {db.MAX_CELLS} 节，无法继续录入")
            try:
                conn.execute(
                    "INSERT INTO cells (cell_id, chemistry, capacity, resistance, test_version)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (body.cell_id, body.chemistry, body.capacity, body.resistance,
                     body.test_version),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise HTTPException(409, f"电芯 {body.cell_id} 已存在")
                raise
            conn.commit()
            # 回读放在写锁内：写连接仅在锁内使用，避免读到其他写事务的中间状态
            row = conn.execute(
                "SELECT * FROM cells WHERE cell_id = ?", (body.cell_id,)
            ).fetchone()
            return _row_to_cell(row)
        except Exception:
            conn.rollback()
            raise


@app.get("/cells")
def list_cells():
    with db.read_connection() as conn:
        rows = conn.execute("SELECT * FROM cells ORDER BY cell_id").fetchall()
        return {"count": len(rows), "cells": [_row_to_cell(r) for r in rows]}


@app.get("/cells/{cell_id}")
def get_cell(cell_id: str):
    with db.read_connection() as conn:
        row = conn.execute("SELECT * FROM cells WHERE cell_id = ?", (cell_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"电芯 {cell_id} 不存在")
        return _row_to_cell(row)


@app.put("/cells/{cell_id}/test")
def revise_test(cell_id: str, body: TestRevision):
    """复测修订：更新容量/内阻并提升检测版本。

    未确认的电芯：修订后旧的预选候选即过期（确认时版本对不上会被整组拒绝）。
    已确认的电芯：修订只影响电芯当前档案，已确认组的检测快照保持不变。
    """
    conn = db.get_conn()
    with db.write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM cells WHERE cell_id = ?", (cell_id,)).fetchone()
            if row is None:
                raise HTTPException(404, f"电芯 {cell_id} 不存在")
            conn.execute(
                "UPDATE cells SET capacity = ?, resistance = ?, test_version = ?"
                " WHERE cell_id = ?",
                (body.capacity, body.resistance, body.test_version, cell_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM cells WHERE cell_id = ?", (cell_id,)
            ).fetchone()
            return _row_to_cell(row)
        except Exception:
            conn.rollback()
            raise


# ---------- 预选 ----------

@app.post("/preselect")
def preselect(body: PreselectRequest):
    """按串联要求预选一组同体系、未占用电芯（只读，不占用）。"""
    with db.read_connection() as conn:
        rows = conn.execute("SELECT * FROM cells WHERE occupied = 0").fetchall()
        free = [_row_to_cell(r) for r in rows]
    if body.chemistry is not None:
        free = [c for c in free if c["chemistry"] == body.chemistry]

    # 按化学体系分组择优
    by_chem: dict[str, list[dict]] = {}
    for c in free:
        by_chem.setdefault(c["chemistry"], []).append(c)

    best = None  # (limited_capacity, ids_tuple, combo, chemistry)
    for chem, cells in by_chem.items():
        combo, limited = choose_group(
            cells, body.series_count, body.min_capacity,
            body.max_capacity_diff, body.max_resistance_diff,
        )
        if combo is None:
            continue
        ids = tuple(sorted(c["cell_id"] for c in combo))
        if (best is None
                or limited > best[0]
                or (limited == best[0] and ids < best[1])):
            best = (limited, ids, combo, chem)

    if best is None:
        return JSONResponse({"feasible": False, "reason": _infeasible_reason(free, body)})

    limited, ids, combo, chem = best
    combo_sorted = sorted(combo, key=lambda c: c["cell_id"])
    return {
        "feasible": True,
        "chemistry": chem,
        "series_count": body.series_count,
        "limited_capacity": limited,
        "cell_ids": [c["cell_id"] for c in combo_sorted],
        "cells": [
            {
                "cell_id": c["cell_id"],
                "capacity": c["capacity"],
                "resistance": c["resistance"],
                "test_version": c["test_version"],
            }
            for c in combo_sorted
        ],
    }


def _infeasible_reason(free: list[dict], body: PreselectRequest) -> str:
    if not free:
        if body.chemistry:
            return f"没有化学体系为 {body.chemistry} 的未占用电芯"
        return "没有可用的未占用电芯"
    by_chem: dict[str, list[dict]] = {}
    for c in free:
        if c["capacity"] >= body.min_capacity:
            by_chem.setdefault(c["chemistry"], []).append(c)
    if not by_chem:
        return (f"没有容量不低于最低单芯容量 {body.min_capacity} 的未占用电芯"
                f"（未占用共 {len(free)} 节）")
    max_n = max(len(v) for v in by_chem.values())
    if max_n < body.series_count:
        chem = max(by_chem, key=lambda k: len(by_chem[k]))
        return (f"同一化学体系下容量达标的未占用电芯不足 {body.series_count} 节"
                f"（体系 {chem} 最多 {max_n} 节）")
    return (f"候选电芯数量足够，但任意 {body.series_count} 节同体系组合均超出"
            f"容量差上限 {body.max_capacity_diff} 或内阻差上限 {body.max_resistance_diff}")


# ---------- 确认与快照查询 ----------

@app.post("/groups/confirm", status_code=201)
def confirm_group(body: ConfirmRequest):
    """确认候选：单事务内重查检测版本与占用状态，全部一致才原子占用整组并写快照。

    任何一节版本已变化（确认前被复测）或已被别组占用 -> 整组拒绝，不留部分占用。
    """
    ids = [c.cell_id for c in body.cells]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "确认列表中存在重复电芯 ID")
    expected = {c.cell_id: c.test_version for c in body.cells}

    conn = db.get_conn()
    with db.write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                f"SELECT * FROM cells WHERE cell_id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
            found = {r["cell_id"]: r for r in rows}

            missing = [i for i in ids if i not in found]
            if missing:
                raise HTTPException(409, f"电芯不存在：{missing}")
            occupied = [i for i in ids if found[i]["occupied"]]
            if occupied:
                raise HTTPException(409, f"电芯已被其他组占用，候选过期：{occupied}")
            stale = [i for i in ids if found[i]["test_version"] != expected[i]]
            if stale:
                raise HTTPException(
                    409,
                    "检测版本已变化（确认前发生复测），候选过期："
                    + str([{i: {"expected": expected[i], "actual": found[i]["test_version"]}}
                           for i in stale]),
                )
            chemistries = {found[i]["chemistry"] for i in ids}
            if len(chemistries) != 1:
                raise HTTPException(409, f"化学体系不一致，无法成组：{sorted(chemistries)}")

            chemistry = chemistries.pop()
            limited = min(found[i]["capacity"] for i in ids)
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO groups (chemistry, series_count, limited_capacity, created_at)"
                " VALUES (?, ?, ?, ?)",
                (chemistry, len(ids), limited, now),
            )
            group_id = cur.lastrowid
            for i in ids:
                r = found[i]
                conn.execute(
                    "INSERT INTO group_cells (group_id, cell_id, capacity, resistance, test_version)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (group_id, i, r["capacity"], r["resistance"], r["test_version"]),
                )
                conn.execute(
                    "UPDATE cells SET occupied = 1, group_id = ? WHERE cell_id = ?",
                    (group_id, i),
                )
            conn.commit()
            # 提交后仍在写锁内回读完整快照；读路径用独立连接，提交后才能看到本组
            return _group_payload(conn, group_id)
        except Exception:
            conn.rollback()  # 整组拒绝：不留下任何部分占用
            raise


def _group_payload(conn, group_id: int) -> dict:
    g = conn.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM group_cells WHERE group_id = ? ORDER BY cell_id", (group_id,)
    ).fetchall()
    return {
        "group_id": g["group_id"],
        "chemistry": g["chemistry"],
        "series_count": g["series_count"],
        "limited_capacity": g["limited_capacity"],
        "created_at": g["created_at"],
        "cells": [
            {
                "cell_id": r["cell_id"],
                "capacity": r["capacity"],
                "resistance": r["resistance"],
                "test_version": r["test_version"],
            }
            for r in rows
        ],
    }


@app.get("/groups")
def list_groups():
    """只列出已提交的完整组：未提交/已回滚的确认事务对读连接不可见。"""
    with db.read_connection() as conn:
        rows = conn.execute("SELECT group_id FROM groups ORDER BY group_id").fetchall()
        return {"count": len(rows),
                "groups": [_group_payload(conn, r["group_id"]) for r in rows]}


@app.get("/groups/{group_id}")
def get_group(group_id: int):
    """按组编号查询不可改写的检测快照（仅已提交组可见，否则按既有规则 404）。"""
    with db.read_connection() as conn:
        g = conn.execute("SELECT 1 FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if g is None:
            raise HTTPException(404, f"组 {group_id} 不存在")
        return _group_payload(conn, group_id)
