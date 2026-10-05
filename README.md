# 退役电芯成组放行 API

基于 FastAPI + SQLite 的退役电芯成组放行服务：录入待选电芯（最多 12 节），按串联成组
约束预选最优组合，确认时在单个 SQLite 事务中原子占用整组并保存不可改写的检测快照。

## 快速启动

### Docker（一条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口：<http://localhost:8000>（交互文档 <http://localhost:8000/docs>）。
SQLite 数据持久化在 `battery-data` 卷中。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

## 业务规则

- **录入**：最多 12 节待选电芯，字段为 ID、化学体系、容量(Ah)、内阻(mΩ)、检测版本。
- **复测修订**：`PUT /cells/{id}/test` 更新容量/内阻并提升检测版本。确认前复测会使
  已预选候选过期；确认后复测只更新电芯档案，不改变已确认组的快照。
- **预选**（只读，不占用）：从同一化学体系、未占用、容量不低于最低单芯容量的电芯中
  选出 `series_count`（3~6）节，要求组内容量差、内阻差均不超过上限。目标：**使组内
  最小容量最大**；同值时按电芯 ID 排序序列的字典序取最小者。返回候选 ID、受限容量
  （= 组内最小容量）及各芯检测版本；无可行组合时返回原因。
- **确认**：在**一次 SQLite 事务**（`BEGIN IMMEDIATE`）中重查所有电芯的检测版本与
  未占用状态，全部一致才原子地占用整组、写入组记录与检测快照；任何一节版本已变化
  或已被别组占用，则**整组拒绝、回滚，不留部分占用**。确认事务期间，`GET /groups`、
  `GET /groups/{group_id}`、`GET /cells`、`POST /preselect` 经由独立只读连接读取，
  **只能看到已提交的完整快照**：未提交或被回滚的确认一律不可见（按组编号查询仍按
  既有规则返回 `404`）；只有确认成功提交后，各查询接口才统一看到整组占用。数据库开启
  WAL，只读请求不会阻塞确认事务，并发确认则由写锁串行化（后到者整组拒绝）。
- **快照不可改写**：`groups` / `group_cells` 表由触发器禁止 UPDATE/DELETE，可按组
  编号 `GET /groups/{group_id}` 查询，后续检测修订不影响已确认组。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cells` | 录入电芯（最多 12 节） |
| GET | `/cells` / `/cells/{id}` | 查询电芯 |
| PUT | `/cells/{id}/test` | 复测修订（更新容量/内阻/检测版本） |
| POST | `/preselect` | 预选成组（只读） |
| POST | `/groups/confirm` | 确认候选（单事务原子占用 + 快照） |
| GET | `/groups` / `/groups/{group_id}` | 查询已确认组及检测快照 |

## 调用示例（curl）

### 1. 录入电芯

```bash
for i in 1 2 3 4 5 6; do
  curl -s -X POST http://localhost:8000/cells \
    -H 'Content-Type: application/json' \
    -d "{\"cell_id\":\"C00$i\",\"chemistry\":\"LFP\",\"capacity\":$((101-i)).0,\"resistance\":2.$i,\"test_version\":\"T1\"}"
  echo
done
```

### 2. 预选（4 节串联，最低单芯容量 90Ah，容量差 ≤5Ah，内阻差 ≤0.5mΩ）

```bash
curl -s -X POST http://localhost:8000/preselect \
  -H 'Content-Type: application/json' \
  -d '{"series_count":4,"min_capacity":90.0,"max_capacity_diff":5.0,"max_resistance_diff":0.5,"chemistry":"LFP"}'
```

响应（候选 ID、受限容量、各芯检测版本）：

```json
{
  "feasible": true,
  "chemistry": "LFP",
  "series_count": 4,
  "limited_capacity": 97.0,
  "cell_ids": ["C001", "C002", "C003", "C004"],
  "cells": [
    {"cell_id": "C001", "capacity": 100.0, "resistance": 2.1, "test_version": "T1"},
    {"cell_id": "C002", "capacity": 99.0,  "resistance": 2.2, "test_version": "T1"},
    {"cell_id": "C003", "capacity": 98.0,  "resistance": 2.3, "test_version": "T1"},
    {"cell_id": "C004", "capacity": 97.0,  "resistance": 2.4, "test_version": "T1"}
  ]
}
```

无可行组合时：

```json
{"feasible": false, "reason": "同一化学体系下容量达标的未占用电芯不足 4 节（体系 LFP 最多 2 节）"}
```

### 3. 确认候选（带上预选返回的检测版本）

```bash
curl -s -X POST http://localhost:8000/groups/confirm \
  -H 'Content-Type: application/json' \
  -d '{"cells":[
        {"cell_id":"C001","test_version":"T1"},
        {"cell_id":"C002","test_version":"T1"},
        {"cell_id":"C003","test_version":"T1"},
        {"cell_id":"C004","test_version":"T1"}]}'
```

成功返回 `201` 及组编号、受限容量与快照内容。

### 4. 按组编号查询检测快照

```bash
curl -s http://localhost:8000/groups/1
```

### 5. 候选过期场景（确认前复测 → 整组拒绝）

```bash
# 预选后、确认前对 C002 复测，检测版本变为 T2
curl -s -X PUT http://localhost:8000/cells/C002/test \
  -H 'Content-Type: application/json' \
  -d '{"capacity":99.0,"resistance":2.2,"test_version":"T2"}'

# 再用旧版本 T1 确认 → 409 整组拒绝，无任何部分占用
curl -s -X POST http://localhost:8000/groups/confirm \
  -H 'Content-Type: application/json' \
  -d '{"cells":[
        {"cell_id":"C001","test_version":"T1"},
        {"cell_id":"C002","test_version":"T1"},
        {"cell_id":"C003","test_version":"T1"},
        {"cell_id":"C004","test_version":"T1"}]}'
# {"detail":"检测版本已变化（确认前发生复测），候选过期：[{'C002': {'expected': 'T1', 'actual': 'T2'}}]"}
```

已被别组抢先占用的电芯再次确认时同样返回 `409` 并整组拒绝。确认后对电芯的复测
修订不会改变 `GET /groups/{group_id}` 返回的快照内容。

### 6. 并发查询：确认进行中只读已提交快照

确认接口在单事务内完成"写组记录 → 写每节快照 → 更新占用"。可以在确认进行的同时
并发发起查询：进行中的组不会出现在组列表中（按编号查询返回 `404`），电芯占用与
预选结果也保持上一个已提交状态；确认成功后所有接口立即统一看到整组结果。

一边确认、一边并发查询（shell）：

```bash
# 后台发起确认
curl -s -X POST http://localhost:8000/groups/confirm \
  -H 'Content-Type: application/json' \
  -d '{"cells":[
        {"cell_id":"C001","test_version":"T1"},
        {"cell_id":"C002","test_version":"T1"},
        {"cell_id":"C003","test_version":"T1"},
        {"cell_id":"C004","test_version":"T1"}]}' &

# 并发查询：确认未提交前 count 只会是 0（或此前已提交的组数），
# 提交后才变为 1；/groups/1 在提交前返回 404，提交后返回完整快照
for i in $(seq 1 10); do
  curl -s http://localhost:8000/groups
  echo
done
wait
```

并发发起两个重叠确认时，只有一个返回 `201`，另一个按"已被占用、候选过期"整组
拒绝（`409`），不会产生部分占用：

```python
from concurrent.futures import ThreadPoolExecutor
import httpx

payload = {"cells": [{"cell_id": cid, "test_version": "T1"}
                     for cid in ["C001", "C002", "C003", "C004"]]}

with httpx.Client(base_url="http://localhost:8000") as client, \
        ThreadPoolExecutor(max_workers=2) as pool:
    statuses = list(pool.map(
        lambda _: client.post("/groups/confirm", json=payload).status_code,
        range(2),
    ))
    print(sorted(statuses))  # [201, 409]：一个成功，一个整组拒绝
    print(client.get("/groups").json()["count"])  # 1：只产生一个完整组
```

## 项目结构

```
app/
  main.py       # FastAPI 路由：录入 / 复测 / 预选 / 确认 / 快照查询
  db.py         # SQLite 连接（单写连接+写锁 / 每请求只读连接、WAL、schema、不可改写触发器）
  selection.py  # 成组预选算法（穷举组合，最多 C(12,6)=924 种）
  schemas.py    # Pydantic 请求模型（串联节数 3~6 等校验）
tests/test_api.py
Dockerfile / docker-compose.yml
```
