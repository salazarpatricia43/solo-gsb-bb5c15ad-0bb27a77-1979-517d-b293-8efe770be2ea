# 可解释规则裁决 API（Explainable Rule Adjudication API）

基于 **Python + FastAPI + 内存存储** 的策略裁决服务。客户端提交由
`all`（与）、`any`（或）、`not`（非）以及比较运算组成的策略，每条规则带
**优先级**与**结论**；服务针对给定事实做裁决，并给出逐节点推导轨迹。

## 语义

- **三值逻辑（Kleene 强三值逻辑）**：每个表达式求值为 `true` / `false` / `unknown`。
  事实中缺失或为 `null` 的字段使对应比较表达式为 `unknown`；`all` / `any` 按
  三值逻辑传播（例如 `false AND unknown = false`、`true OR unknown = true`）。
- **优先级裁决**：规则按优先级从高到低分层层内裁决：
  - 层内**全部已知且至少一条为真**：真规则结论唯一 → `decided`（返回唯一结论）；
    真规则结论不一致 → `conflict`（冲突）。
  - 层内**全部为假**：跳过该层，继续下探更低优先级。
  - 层内**存在 unknown**：停止下探，返回 `unknown`，并给出
    **使该层可判定（层内每条规则都变已知）的所有包含关系最小的缺失字段集合**
    （`minimal_missing_field_sets`）以及逐节点推导轨迹。
  - 所有层均为假：返回 `no_match`。
- **包含关系最小**：集合族中删除冗余超集（如已存在 `{a}` 则删除 `{a,b}`）。
  同字段多个比较的不可能组合（如 `x>10 AND x<5`）会被可行性检查剔除。
- **可解释性**：每次裁决返回每个节点的 `true/false/unknown` 状态树、缺失字段
  对应的原子表（atom id / field / op / value）、每层裁决状态与跳过的优先级。

> 缺失字段集合是“存在一种赋值使该层可判定”的最小字段集。实现以比较原子为
> 布尔变量做最小 DNF 推导，再投影到字段并做逐字段具体可行性校验，因此能正确
> 处理同字段比较之间的关联（例如 `age>=18` 与 `age<18` 互补，只需提供 `age`）。

## 一条命令启动

```bash
docker compose up --build
```

依赖在**容器启动时**由 `entrypoint.sh` 执行 `pip install -r requirements.txt`
安装，随后启动 uvicorn。停止：`Ctrl+C` 或 `docker compose down`。

不使用容器时（需 Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT`（compose 宿主机映射端口） | `8000` | 宿主机暴露端口，如 `PORT=9000 docker compose up` |
| `HOST`（容器内） | `0.0.0.0` | uvicorn 绑定地址 |
| `PORT`（容器内） | `8000` | 容器内监听端口 |
| `LOG_LEVEL` | `info` | uvicorn 日志级别 |

无数据库等其他依赖；策略保存在进程内存中，重启即清空。

## API 地址与端口

- 默认地址：**http://localhost:8000**
- 健康检查：
  - `GET http://localhost:8000/health` — 存活检查，返回 `{"status":"ok"}`
  - `GET http://localhost:8000/health/ready` — 就绪检查（含内存策略数量）
- 交互式文档：**http://localhost:8000/docs** （Swagger UI）
- 接口前缀：`/api/v1`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/policies` | 创建策略（结构校验，返回生成的 id） |
| PUT | `/api/v1/policies/{id}` | 整体替换策略 |
| GET | `/api/v1/policies` | 列出策略摘要 |
| GET | `/api/v1/policies/{id}` | 获取策略全文 |
| DELETE | `/api/v1/policies/{id}` | 删除策略 |
| POST | `/api/v1/policies/{id}/evaluate` | 提交事实进行裁决 |
| POST | `/api/v1/policies/{id}/counterfactuals` | 反事实搜索：让裁决唯一变为目标结论的最小修改集 |

## 策略结构

```json
{
  "rules": [
    {
      "id": "adult-allow",                 // 可选，缺省自动生成 rule-1...
      "priority": 100,                     // 整数，越大越优先
      "conclusion": "allow",               // 非空字符串
      "expr": {
        "all": [
          { "field": "age", "op": "ge", "value": 18 },
          { "any": [
              { "field": "country", "op": "eq", "value": "NL" },
              { "not": { "field": "blocked", "op": "eq", "value": true } }
          ]}
        ]
      }
    }
  ]
}
```

- 比较叶节点：`{"field": <字符串>, "op": <运算符>, "value": <JSON字面量>}`
- 比较运算符：`eq`、`ne`、`gt`、`ge`、`lt`、`le`
- 逻辑节点：`{"all": [节点, ...]}`、`{"any": [节点, ...]}`、`{"not": 节点}`
- 类型严格：`true != 1`，数字与字符串间排序比较结果为 `false`（不会变 unknown）
- 结构非法时返回 `422`，响应体含 `errors` 列表，例如：

```json
{ "detail": "Policy structure validation failed",
  "errors": ["rules[0].expr.op: must be one of ['eq', 'ge', 'gt', 'le', 'lt', 'ne']"] }
```

## 裁决示例

```bash
PID=$(curl -s -X POST localhost:8000/api/v1/policies \
  -H 'Content-Type: application/json' \
  -d '{"rules":[
    {"id":"approve","priority":10,"conclusion":"approve",
     "expr":{"all":[{"field":"income","op":"ge","value":50000},
                    {"field":"debt_ratio","op":"lt","value":0.4}]}},
    {"id":"reject","priority":10,"conclusion":"reject",
     "expr":{"field":"debt_ratio","op":"ge","value":0.6}}]}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

# 事实不足 → unknown + 最小缺失字段集合 + 轨迹
curl -s -X POST localhost:8000/api/v1/policies/$PID/evaluate \
  -H 'Content-Type: application/json' -d '{"facts":{}}'
```

unknown 响应（节选）：

```json
{
  "status": "unknown",
  "decided_priority": 10,
  "conclusion": null,
  "reason": "undecidable_level",
  "missing_fields": ["debt_ratio"],
  "minimal_missing_field_sets": [["debt_ratio"]],
  "pending_conclusions": ["approve", "reject"],
  "skipped_priorities": [],
  "level_trace": {
    "priority": 10,
    "state": "unknown",
    "rules": [ { "rule_id": "approve", "status": "unknown",
                 "expr": { "op": "all", "status": "unknown", "children": [ ... ] } },
               { "rule_id": "reject", "status": "unknown", "expr": { ... } } ],
    "atoms": [ { "id": 0, "field": "income", "op": "ge", "value": 50000 },
               { "id": 1, "field": "debt_ratio", "op": "lt", "value": 0.4 },
               { "id": 2, "field": "debt_ratio", "op": "ge", "value": 0.6 } ],
    "true_rule_ids": [],
    "minimal_missing_field_sets": [["debt_ratio"]]
  },
  "levels_trace": [ ... ]
}
```

事实充分时：

```bash
curl -s -X POST localhost:8000/api/v1/policies/$PID/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"facts":{"income":80000,"debt_ratio":0.2}}'
# {"status":"decided","conclusion":"approve","decided_priority":10,
#  "true_rule_ids":["approve"], ... ,"level_trace":{"state":"fired", ...}}
```

同优先级真规则结论相反：

```json
{ "status": "conflict", "reason": "opposite_conclusions",
  "conflicting_conclusions": ["no", "yes"], "decided_priority": 1 }
```

## 反事实修改示例（counterfactuals）

在给定事实基础上，按字段提供候选修改值，搜索能让裁决**唯一变为目标结论**
的所有**包含关系最小**修改集（同一字段一次只能选一个候选值）。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `facts` | object | 原始事实（缺省视为 `{}`，缺失/null 字段仍为 unknown） |
| `target_conclusion` | 非空字符串 | 希望最终唯一得到的结论 |
| `candidates` | 非空 object | 字段名 → 该字段可选的候选值**非空数组**（每个组合每字段至多取一个值） |
| `max_changes` | 整数 1–100 | 最多允许修改的字段数 |

非法请求（`candidates` 为空、某字段候选值为空、`max_changes` 非法等）返回
`422`，响应体含 `errors` 列表。

```bash
curl -s -X POST localhost:8000/api/v1/policies/$PID/counterfactuals \
  -H 'Content-Type: application/json' \
  -d '{
    "facts": {"income": 40000, "debt_ratio": 0.5},
    "target_conclusion": "approve",
    "candidates": {
      "income": [60000, 30000],
      "debt_ratio": [0.3, 0.7]
    },
    "max_changes": 2
  }'
```

响应（节选）：

```json
{
  "policy_id": "ab12...",
  "target_conclusion": "approve",
  "original_status": "no_match",
  "original_conclusion": null,
  "count": 1,
  "counterfactuals": [
    {
      "changes": {"debt_ratio": 0.3, "income": 60000},
      "verdict": {
        "status": "decided",
        "conclusion": "approve",
        "decided_priority": 10,
        "true_rule_ids": ["approve"],
        "level_trace": { "priority": 10, "state": "fired", "...": "..." },
        "levels_trace": [ "..." ]
      },
      "level_trace": { "priority": 10, "state": "fired", "...": "..." }
    }
  ]
}
```

搜索语义：

- 仅接受最终 `status` 为 `decided` 且结论等于 `target_conclusion` 的修改；
  修改后仍为 `unknown`、层内 `conflict` 或全部为假 `no_match` 的组合一律排除。
- **包含关系最小**针对“被修改的字段集合”：若某字段集已能达到目标，其任何超集
  都被剪枝；同一最小字段集上的不同取值若都能达到目标，则各自独立保留
  （它们互不包含）。
- 超过 `max_changes` 个字段的组合不会被枚举。
- 原始事实已经唯一得到目标结论时，直接返回**一个空修改集**
  （`"changes": {}`）。
- 结果稳定排序：先按修改字段数升序，再按字段名（`changes` 的键也按字段名
  排序输出），最后按候选值的规范 JSON 序（`json.dumps(..., sort_keys=True)`）。
- 为防止候选空间组合爆炸，求值组合数设有安全上限
  （`MAX_COUNTERFACTUAL_COMBINATIONS = 100000`）；触顶时返回 `422` 并提示
  收窄 `candidates` 或减小 `max_changes`，以保证结果始终完整。

## 项目结构

```
app/
  main.py            # FastAPI 路由、健康检查、请求模型
  validation.py      # 策略结构校验、反事实请求校验
  logic.py           # 三值逻辑、最小决定族、比较可行性
  engine.py          # 优先级分层、冲突/未知/无匹配裁决、轨迹
  counterfactual.py  # 反事实最小修改集枚举、剪枝与稳定排序
  store.py           # 加锁的内存策略存储
tests/               # 纯标准库 runner，也可用 pytest
Dockerfile
docker-compose.yml
entrypoint.sh        # 启动时安装依赖并运行 uvicorn
```

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
# 或无需 pytest（tests 内置 runner）：
python3 tests/test_logic.py && python3 tests/test_engine.py && \
python3 tests/test_api.py && python3 tests/test_counterfactual.py
```
