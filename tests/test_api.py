import os
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["DB_PATH"] = path
    from app import db
    db.reset()
    from app.main import app
    with TestClient(app) as c:
        yield c
    db.reset()
    Path(path).unlink(missing_ok=True)


def add_cell(client, cell_id, chemistry="LFP", capacity=100.0, resistance=2.0, version="T1"):
    r = client.post("/cells", json={
        "cell_id": cell_id, "chemistry": chemistry, "capacity": capacity,
        "resistance": resistance, "test_version": version,
    })
    assert r.status_code == 201, r.text
    return r


def seed_six(client):
    # 6 节 LFP：容量两两差 <= 5，内阻差 <= 0.5
    data = [
        ("C001", 100.0, 2.0), ("C002", 99.0, 2.1), ("C003", 98.0, 2.2),
        ("C004", 97.0, 2.3), ("C005", 96.0, 2.4), ("C006", 95.5, 2.5),
    ]
    for cid, cap, res in data:
        add_cell(client, cid, capacity=cap, resistance=res)


def test_register_limit_12(client):
    for i in range(12):
        add_cell(client, f"C{i:03d}")
    r = client.post("/cells", json={
        "cell_id": "C999", "chemistry": "LFP", "capacity": 1.0,
        "resistance": 1.0, "test_version": "T1",
    })
    assert r.status_code == 409
    assert "12" in r.json()["detail"]


def test_duplicate_cell_rejected(client):
    add_cell(client, "C001")
    r = client.post("/cells", json={
        "cell_id": "C001", "chemistry": "LFP", "capacity": 1.0,
        "resistance": 1.0, "test_version": "T1",
    })
    assert r.status_code == 409


def test_preselect_maximizes_min_capacity(client):
    seed_six(client)
    r = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    body = r.json()
    assert body["feasible"] is True
    # 最小容量最大的 4 节组合是 C001..C004（min=97）
    assert body["cell_ids"] == ["C001", "C002", "C003", "C004"]
    assert body["limited_capacity"] == 97.0
    assert all(c["test_version"] == "T1" for c in body["cells"])


def test_preselect_tie_break_lexicographic(client):
    # 4 节中任取 3 节最小容量都是 99：同值时按 ID 排序序列字典序取最小
    add_cell(client, "B001", capacity=100.0, resistance=2.0)
    add_cell(client, "B002", capacity=99.0, resistance=2.0)
    add_cell(client, "A001", capacity=100.0, resistance=2.0)
    add_cell(client, "A002", capacity=99.0, resistance=2.0)
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 1.0, "max_resistance_diff": 0.5,
    })
    body = r.json()
    assert body["feasible"] is True
    assert body["limited_capacity"] == 99.0
    assert body["cell_ids"] == ["A001", "A002", "B001"]


def test_preselect_chemistry_isolated(client):
    seed_six(client)
    add_cell(client, "N001", chemistry="NCM", capacity=100.0, resistance=2.0)
    add_cell(client, "N002", chemistry="NCM", capacity=99.0, resistance=2.0)
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5, "chemistry": "NCM",
    })
    body = r.json()
    assert body["feasible"] is False
    assert "不足 3 节" in body["reason"]


def test_preselect_infeasible_reasons(client):
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert r.json()["feasible"] is False
    assert "没有可用的未占用电芯" in r.json()["reason"]

    # 容量全部不达标
    add_cell(client, "C001", capacity=50.0)
    add_cell(client, "C002", capacity=50.0)
    add_cell(client, "C003", capacity=50.0)
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert "最低单芯容量" in r.json()["reason"]

    # 数量够但差值约束不满足
    add_cell(client, "C004", capacity=200.0, resistance=9.0)
    add_cell(client, "C005", capacity=100.0, resistance=2.0)
    add_cell(client, "C006", capacity=100.0, resistance=2.0)
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert "容量差上限" in r.json()["reason"] or "内阻差上限" in r.json()["reason"]


def test_confirm_success_and_snapshot(client):
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    r = client.post("/groups/confirm", json=payload)
    assert r.status_code == 201, r.text
    group = r.json()
    assert group["group_id"] == 1
    assert group["limited_capacity"] == 97.0
    assert group["series_count"] == 4

    # 电芯被占用
    cells = client.get("/cells").json()["cells"]
    used = {c["cell_id"]: c for c in cells if c["occupied"]}
    assert set(used) == set(pre["cell_ids"])
    assert all(c["group_id"] == 1 for c in used.values())

    # 快照可按组编号查询
    snap = client.get("/groups/1").json()
    assert snap["cells"] == group["cells"]


def test_confirm_stale_version_rejected_atomically(client):
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    # 预选后复测其中一节 -> 候选过期
    client.put("/cells/C002/test", json={
        "capacity": 99.0, "resistance": 2.1, "test_version": "T2"})
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    r = client.post("/groups/confirm", json=payload)
    assert r.status_code == 409
    assert "C002" in r.json()["detail"]
    # 整组拒绝：无任何部分占用，无组产生
    assert all(not c["occupied"] for c in client.get("/cells").json()["cells"])
    assert client.get("/groups").json()["count"] == 0


def test_confirm_occupied_by_other_group_rejected(client):
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    assert client.post("/groups/confirm", json=payload).status_code == 201
    # 别组已抢先占用 -> 再次确认同一候选被拒
    r = client.post("/groups/confirm", json=payload)
    assert r.status_code == 409
    assert "占用" in r.json()["detail"]
    assert client.get("/groups").json()["count"] == 1


def test_revision_after_confirm_does_not_change_snapshot(client):
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    group = client.post("/groups/confirm", json=payload).json()
    # 确认后复测已入组电芯：允许修订档案，但快照不变
    client.put("/cells/C001/test", json={
        "capacity": 88.0, "resistance": 3.0, "test_version": "T9"})
    snap = client.get(f"/groups/{group['group_id']}").json()
    c001 = next(c for c in snap["cells"] if c["cell_id"] == "C001")
    assert c001["capacity"] == 100.0
    assert c001["test_version"] == "T1"
    assert snap["limited_capacity"] == 97.0


def test_confirm_mixed_chemistry_rejected(client):
    add_cell(client, "C001", chemistry="LFP")
    add_cell(client, "C002", chemistry="LFP")
    add_cell(client, "C003", chemistry="NCM")
    r = client.post("/groups/confirm", json={"cells": [
        {"cell_id": "C001", "test_version": "T1"},
        {"cell_id": "C002", "test_version": "T1"},
        {"cell_id": "C003", "test_version": "T1"},
    ]})
    assert r.status_code == 409
    assert "化学体系" in r.json()["detail"]
    assert all(not c["occupied"] for c in client.get("/cells").json()["cells"])


def test_series_count_validation(client):
    r = client.post("/preselect", json={
        "series_count": 2, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert r.status_code == 422
    r = client.post("/preselect", json={
        "series_count": 7, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert r.status_code == 422


# ---------- 并发：确认事务进行中的跨请求读取 ----------

def _hold_uncommitted_confirm(client, cell_ids):
    """在写连接上开启确认事务但不提交：组记录已插入、快照/占用写了一半的状态。

    返回 (event_release, thread)：event 置位后事务回滚（模拟确认被拒）。
    """
    from app import db
    released = threading.Event()

    def worker():
        conn = db.get_conn()
        with db.write_lock:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO groups (chemistry, series_count, limited_capacity, created_at)"
                " VALUES ('LFP', ?, 97.0, '2026-10-05T00:00:00+00:00')",
                (len(cell_ids),),
            )
            group_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # 只写第一节的快照：故意停在“快照尚未写齐”的中间状态
            conn.execute(
                "INSERT INTO group_cells (group_id, cell_id, capacity, resistance,"
                " test_version) VALUES (?, 'C001', 100.0, 2.0, 'T1')",
                (group_id,),
            )
            conn.execute("UPDATE cells SET occupied = 1, group_id = ?"
                         " WHERE cell_id = 'C001'", (group_id,))
            released.wait()
            conn.rollback()

    t = threading.Thread(target=worker)
    t.start()
    # 等待事务实际开始（写锁被持有即代表 INSERT 已执行）
    while not db.write_lock.locked():
        pass
    return released, t


def test_uncommitted_confirm_invisible_to_group_reads(client):
    """确认未提交时：组列表/按编号查询只能看到已提交的完整快照。"""
    seed_six(client)
    released, t = _hold_uncommitted_confirm(client, ["C001", "C002", "C003", "C004"])
    try:
        assert client.get("/groups").json()["count"] == 0
        r = client.get("/groups/1")
        assert r.status_code == 404  # 未提交组按现有规则不可查
        # 单节电芯查询同样看不到临时占用
        assert client.get("/cells/C001").json()["occupied"] is False
    finally:
        released.set()
        t.join()
    # 回滚后：组彻底不存在，占用消失，仿佛确认从未发生
    assert client.get("/groups").json()["count"] == 0
    assert client.get("/groups/1").status_code == 404
    assert all(not c["occupied"]
               for c in client.get("/cells").json()["cells"])


def test_uncommitted_occupancy_invisible_to_preselect(client):
    """确认未提交时：预选不得提前看到临时占用，仍按已提交状态选 C001..C004。"""
    seed_six(client)
    released, t = _hold_uncommitted_confirm(client, ["C001", "C002", "C003", "C004"])
    try:
        body = client.post("/preselect", json={
            "series_count": 4, "min_capacity": 90.0,
            "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
        }).json()
        assert body["feasible"] is True
        assert body["cell_ids"] == ["C001", "C002", "C003", "C004"]
    finally:
        released.set()
        t.join()


def test_committed_confirm_visible_atomically(client):
    """确认提交后：组列表/按编号/电芯占用/预选统一看到整组结果。"""
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    assert client.post("/groups/confirm", json=payload).status_code == 201

    groups = client.get("/groups").json()
    assert groups["count"] == 1
    assert groups["groups"][0]["cells"] == client.get("/groups/1").json()["cells"]
    occupied = {c["cell_id"] for c in client.get("/cells").json()["cells"]
                if c["occupied"]}
    assert occupied == set(pre["cell_ids"])
    # 已占用 4 节后，预选只能从剩余 2 节里选 -> 不足 4 节
    body = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    assert body["feasible"] is False


def test_concurrent_confirms_do_not_interfere(client):
    """并发请求不得干扰确认事务：两个重叠确认只有一个成功，另一个整组拒绝。"""
    seed_six(client)
    payload = {"cells": [
        {"cell_id": cid, "test_version": "T1"}
        for cid in ["C001", "C002", "C003", "C004"]
    ]}
    results = []

    def confirm():
        results.append(client.post("/groups/confirm", json=payload).status_code)

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409]
    # 只有一个组、无部分占用
    assert client.get("/groups").json()["count"] == 1
    occupied = [c for c in client.get("/cells").json()["cells"] if c["occupied"]]
    assert len(occupied) == 4
    assert all(c["group_id"] == 1 for c in occupied)


def test_reads_during_real_confirm_never_see_partial_state(client):
    """真实确认进行中并发读取：任何时刻组要么完整存在，要么完全不存在。"""
    seed_six(client)
    payload = {"cells": [
        {"cell_id": cid, "test_version": "T1"}
        for cid in ["C001", "C002", "C003", "C004"]
    ]}
    errors = []

    def reader():
        for _ in range(50):
            groups = client.get("/groups").json()["groups"]
            for g in groups:
                # 不允许出现“组记录已在但快照写了一半”的半成品
                if len(g["cells"]) != g["series_count"]:
                    errors.append(f"partial group {g['group_id']}: "
                                  f"{len(g['cells'])}/{g['series_count']}")
            # 占用状态必须以整组为单位：要么 0 节，要么 4 节
            n = sum(1 for c in client.get("/cells").json()["cells"]
                    if c["occupied"])
            if n not in (0, 4):
                errors.append(f"partial occupancy: {n}")

    rthread = threading.Thread(target=reader)
    rthread.start()
    assert client.post("/groups/confirm", json=payload).status_code == 201
    rthread.join()
    assert errors == []
