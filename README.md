# 跑道离场尾流间隔校验 API

基于 **Python + FastAPI + SQLite** 的跑道离场尾流（wake turbulence）间隔校验服务。
客户端按跑道提交航班计划与前机—后机最小间隔矩阵，并可为每条跑道配置**临时关闭窗口**
与**滚动容量约束**；航班可选 **`locked=true`（不可移动航班）**。服务端完成四件事：

1. **间隔校验（validate）**：按计划离场时刻、航班号稳定排序，列出同跑道相邻航班的间隔违规、
   计划离场时刻落入关闭窗口的航班及其命中窗口，以及滚动容量约束的超限航班、触发窗口与
   最早合规时刻。**锁定不影响校验结论**——仍按原计划逐项校验，仅额外回显该跑道的
   `locked_flights`（位次、航班号、计划时刻、尾流等级）与批次级 `total_locked`。
2. **试排（trial）**：保持原航班顺序，逐项把航班推迟到**同时满足尾流间隔、滚动容量约束且
   避开关闭窗口**的最早安全时刻；容量/关闭导致的延误沿队列向后级联，
   容量推迟后若落入关闭窗口会继续迭代避让，**原计划时刻绝不改写**。
   返回调整后时刻、单项延误、触发前机、容量与关闭窗口命中情况。
   **锁定航班必须保持 `scheduled_off`**：稳定顺序计算中，若尾流、关闭窗口或容量要求会把
   首个锁定航班推迟，则**不挪动它、也不生成违反约束的时刻**，而在该跑道返回
   `blocked`（首个不可行航班、`constraint` 冲突约束、`required_earliest_off` 所需最早时刻
   及对应约束明细），该航班及其后的航班**不再给出建议**，`items` 只含可行前缀；
   可行前缀照常持久化、可用批次 ID 回查。
3. **相邻交换最优试排（best-adjacent-swap）**：在试排基础上，对每条跑道以稳定排序为基线，
   枚举「不交换」与每一对相邻航班交换后的顺序（`scheduled_off` 不改），分别按同一套
   尾流/关闭窗口/滚动容量规则从头试排并择优（总延误 → 最大单航班延误 → 交换位置），
   返回基线指标、选中的交换、优化后完整结果与节省秒数；请求含任意 `locked=true`
   时返回 `422`。
4. **相交跑道联合试排（joint）**：请求可额外定义若干**互斥跑道组**及组内两次离场的
   **最小占用间隔**（相交跑道不能同时占用）。先按 `(计划时刻, 航班号)` 确定**跨跑道
   全局稳定顺序**，再逐项求不早于原计划、同时满足**本跑道**尾流间隔、关闭窗口避让、
   滚动容量与**互斥组间隔**的最早时刻；任一约束推迟后全部相关约束重新检查、循环至
   收敛。返回每项的首个触发约束、触发前序航班与延误。锁定航班若需后移则在该航班
   （全局首个阻塞）处停止：返回阻塞航班、首个触发约束、最终最早合规时刻及**各约束
   分别要求的时刻**（`required_times`），其后不再生成建议，仅持久化可行全局前缀。
5. **候选插入查询（slots/insert）**：针对**单条跑道**评估一架候选航班能否插入现有
   计划。现有计划本身违反尾流间隔、关闭窗口或滚动容量时返回 `422`；否则**保持现有
   航班时刻不变**，按 `(离场时刻, 航班号)` 确定插入位次，在候选区间
   `[earliest_off, latest_off]`（含两端、精确到整秒）内求**最早可行的 UTC 时刻**——
   需同时满足插入前后相邻尾流间隔、关闭窗口避让与插入后全序列的滚动容量；找不到时
   返回明确的无空档结果。该接口为**只读查询**，不改写计划、不产生批次。
6. **离场冲突分组（conflict-groups）**：对已保存的 **validate 批次**整理需要联动
   处理的离场冲突。按跑道从既有航班与违规明细构图：尾流违规连接前后机、容量超限把
   其触发窗口内的同跑道航班连成一组、关闭窗口冲突将命中航班标为单点冲突；含冲突的
   **连通分量**即冲突组（交叠的容量窗口与尾流边经共享航班合并，互不关联的冲突不会
   仅因计划顺序相邻而合并）。每组返回按 `(计划时刻, 航班号)` 排序的航班及所含
   尾流/容量/关闭冲突数量，组按最早航班排序，未涉及冲突的航班不输出。
   该接口为**只读查询**：批次不存在返回 `404`，非 validate 批次返回 `422`。

**兼容形状**：未在任何航班上传 `locked` 字段时，validate/trial/批次回查的响应与旧版本
完全一致（不出现 `locked`、`locked_flights`、`blocked`、`total_locked` 字段）；显式传了
`locked`（即使都是 `false`）才回显锁定相关字段。

每次请求（含输入排序结果、关闭窗口、容量配置、违规/冲突/试排结论）都会持久化到 SQLite，
可用批次 ID 回查。

---

## 一条命令启动

```bash
docker compose up -d --build
```

启动后访问 **http://localhost:8000** ：

- Swagger 交互文档：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health
- 默认间隔矩阵：http://localhost:8000/matrix/default

停止：`docker compose down`（数据保留在命名卷 `wake-data` 中；加 `-v` 可一并清除）。

## 必要配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WAKE_DB_PATH` | `/data/wake.db` | SQLite 数据库文件路径，容器内挂载在持久卷 `/data` |
| `WAKE_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `WAKE_PORT` | `8000` | uvicorn 监听端口（容器内固定暴露 8000） |

端口映射在 `docker-compose.yml` 的 `ports` 中配置（默认 `8000:8000`，宿主机端口被占用时改左侧即可）。

---

## 接口

### `POST /api/v1/validate` — 间隔违规校验

请求体：

```json
{
  "runways": [
    {
      "runway": "36L",
      "flights": [
        {"flight_no": "CA101", "scheduled_off": "2026-09-18T08:00:00+08:00", "wake": "J"},
        {"flight_no": "CA102", "scheduled_off": "2026-09-18T08:01:00+08:00", "wake": "M"},
        {"flight_no": "CA103", "scheduled_off": "2026-09-18T08:02:30+08:00", "wake": "M"}
      ],
      "closures": [
        {"start": "2026-09-18T08:02:30+08:00", "end": "2026-09-18T08:03:30+08:00"}
      ],
      "capacity": {"window_seconds": 600, "max_departures": 3}
    }
  ]
}
```

- `flight_no`：航班号，**整个请求内唯一**（跑道内、跑道间均不可重复）。
- `scheduled_off`：计划离场时刻，ISO 8601 且**必须带时区**（`Z` 或 `+08:00` 偏移），服务端统一按 UTC 处理。
- `wake`：尾流等级，四档 —— `L` 轻型 / `M` 中型 / `H` 重型 / `J` 超重型。
- `locked`（可选，**严格布尔值**，缺省 `false`）：是否为**不可移动航班**。
  validate 不改变任何校验逻辑，只在该跑道回显 `locked_flights`；trial 中锁定航班必须保持
  `scheduled_off`（详见下文 trial 的锁定语义）。该字段必须是真正的 JSON `true`/`false`，
  传 `"true"`、`1` 等会被拒绝（`422`）。
- 跑道按其标识独立排序与校验；排序规则为 `(scheduled_off 升序, flight_no 升序)` 的稳定排序。
- 仅检查排序后**相邻**两架航班（单跑道离场的运行惯例）：前机尾流等级 → 后机尾流等级查矩阵，
  实际间隔 < 要求间隔即违规，返回要求秒数、实际秒数与缺口秒数。

#### 不可移动航班回显（validate）

只要某跑道的请求里**任意航班显式携带了 `locked` 字段**，该跑道响应就带：

- `locked_flights`：稳定排序位次上所有 `locked=true` 航班的列表，每项含
  `position` / `flight_no` / `scheduled_off`（UTC）/ `wake`；
  没有锁定航班时为空列表 `[]`。

批次级在**任意航班显式携带 `locked` 时**汇总 `total_locked`（全批锁定航班总数，可为 0）。
校验仍完全基于原计划：被锁定的航班若本身违反尾流/关闭/容量约束，仍会照常出现在对应
违规/冲突/超限列表中——锁定只约束 trial，不豁免 validate。

#### 跑道临时关闭窗口（可选字段 `closures`）

每条跑道可携带多个关闭窗口，语义为 **`[start, end)` 左闭右开**：

- `start` / `end` 均为 ISO 8601 且**必须带时区**，服务端统一归一化为 UTC；
- `end` 必须**晚于** `start`（相等或倒置都会被拒绝，`422`）；
- 同一跑道内窗口**不得重叠**（`422`）；首尾相接（前一个的 `end` 等于后一个的 `start`）是允许的；
- 响应中 `closures` 按开始时刻升序、以 UTC 原样回显。

validate 额外返回**关闭冲突**：凡计划离场时刻落在某个窗口内（含 `start`、不含 `end`）的航班，
在对应跑道的 `closure_conflicts` 中列出，含 `flight_no`、`position`（排序后位次）、
`window_index`（命中窗口在回显 `closures` 中的 1 基序号）与窗口起止时刻；
跑道级与批次级分别汇总 `closure_conflict_count` / `total_closure_conflicts`。

#### 跑道滚动容量约束（可选字段 `capacity`）

每条跑道可携带至多一个容量约束：

```json
"capacity": {"window_seconds": 600, "max_departures": 3}
```

- 语义为**滚动半开区间**：任意 `[t, t + window_seconds)` 内该跑道的离场数不得超过
  `max_departures`；两个字段都必须是**正整数**（`0`、负数、小数、字符串、布尔均被拒绝，`422`），
  缺省或显式 `null` 表示该跑道不施加容量约束。
- 对稳定排序后的航班序列，判定等价于：航班 `p` 与其前方第 `max_departures` 架航班（锚机）的
  计划时刻之差必须 `>= window_seconds`；否则以锚机时刻为起点的窗口
  `[anchor_off, anchor_off + window_seconds)` 内至少有 `max_departures + 1` 架离场，
  该航班记一次超限（窗口随航班向前滑动，同一航班最多记一次）。
- validate 在对应跑道返回 `capacity`（原样回显配置）、`capacity_violation_count` 与
  `capacity_violations`，每条含：
  `flight_no` / `position` / `scheduled_off`、`window_seconds` / `max_departures`、
  `anchor_flight_no` / `anchor_position`（锚机及其位次）、
  `window_start` / `window_end`（触发窗口，UTC）、`departures_in_window`（该窗口内实际离场数）
  以及 **`earliest_compliant_off`（最早合规时刻 = 锚机时刻 + window_seconds）**；
  批次级在**有任意跑道配置 capacity 时**汇总 `total_capacity_violations`。
  列表按稳定排序位次升序。

### `POST /api/v1/trial` — 试排（级联推迟）

请求体与 validate 完全一致（含 `closures`、`capacity`）。算法：

- 顺序保持为稳定排序结果；航班**只会推迟、绝不提前**；
- 每架航班取满足以下全部条件的最早时刻：
  1. 不早于其原计划时刻；
  2. 不早于 `直接前机调整时刻 + 要求间隔`（首架航班无前机约束）；
  3. 不落在任何关闭窗口内 —— 若落入窗口则推迟到该窗口 `end`；窗口首尾相接时会**继续迭代**，
     直到完全离开关闭时段（尾流推迟后落入后续窗口的情形同样处理）；
  4. 满足滚动容量约束 —— 不早于 `前方第 max_departures 架航班调整时刻 + window_seconds`；
- 容量推迟可能恰好落入（之后的）关闭窗口、关闭推迟也必须重新满足容量，因此两类约束**交替迭代**
  直到同时成立，延误继续向后续航班级联；
- 每项返回 `adjusted_off`（调整后时刻，UTC）、`delay_seconds`（单项延误，非负）、
  `triggered_by`（触发本次推迟的直接前机航班号；未因尾流推迟为 `null`）、
  `closure_hit`（是否因关闭窗口产生了额外推迟）与
  `closure_window`（最后命中并导致推迟的窗口，未命中为 `null`）；
  该跑道显式携带 `locked` 时每项还返回 `locked`（该航班是否锁定；锁定且可行时
  `adjusted_off == scheduled_off`、`delay_seconds == 0`）；
  **配置了 capacity 的跑道**还返回 `capacity_hit`、`capacity_anchor`（容量窗口锚机航班号）、
  `capacity_window_seconds` / `capacity_max_departures` 与
  `capacity_window`（触发容量推迟的半开窗口 `[锚机调整时刻, +window_seconds)`，
  未因容量推迟为 `null`），跑道对象同时原样回显 `capacity`；
- 该接口只返回试排建议，`scheduled_off` 原值原样返回，数据库中也同时保存原计划。

#### 不可移动航班（trial 的 `locked` 语义）

按稳定顺序逐项计算时，每架航班仍取满足尾流/关闭/容量的最早时刻，但对锁定航班：

- **锁定航班必须保持 `scheduled_off`**；未锁定航班仍**只可后移、绝不提前**。
- 若尾流间隔、关闭窗口或容量要求会把**首个**锁定航班推迟，则立即停止该跑道：
  - **不挪动**该锁定航班，也**不生成**任何违反约束的 `adjusted_off`；
  - 跑道对象返回 `blocked`，描述该不可行航班；
  - 该航班及它之后的航班**不再给出试排建议**，`items` 只保留此前的**可行前缀**。
- `blocked` 字段：

  | 字段 | 含义 |
  | --- | --- |
  | `flight_no` / `wake` / `scheduled_off` / `position` | 首个不可行的锁定航班（位次为稳定排序位次） |
  | `constraint` | 首个要求推迟的约束：`wake`（尾流）/ `closure`（关闭窗口）/ `capacity`（容量）；判定优先级为尾流 → 关闭 → 容量 |
  | `required_earliest_off` | 三类约束交替迭代后该航班的**最早合规时刻**（UTC） |
  | `triggered_by` / `required_gap_seconds` | 尾流约束明细：要求保持间隔的直接前机与要求秒数；非尾流约束时为 `null` |
  | `closure_hit` / `closure_window` | 是否同时命中关闭窗口及最后一个导致推迟的窗口 |
  | `capacity_hit` / `capacity_anchor` / `capacity_window_seconds` / `capacity_max_departures` / `capacity_window` | 容量约束明细（仅该跑道配置了 capacity 时出现这些键，规则与 `items` 一致） |

- 跑道在显式携带 `locked` 时还回显 `locked_flights`（同 validate）与
  `blocked`（可行时为 `null`）；`total`/`order` 仍是**全部**航班，`items` 在阻塞时短于总数，
  `delayed_count` / `total_delay_seconds` / `max_delay_seconds` 只统计可行前缀。
- 批次级在显式携带 `locked` 时汇总 `total_locked`。
- 可行前缀与 `blocked` 都持久化，`GET /api/v1/batches/{batch_id}` 原样回查；
  后续跑道（其它 runway）互不影响，各自独立给出完整建议或阻塞结论。

> 注意：`constraint` 给出的是“第一个要求后移”的约束；当尾流把航班推进关闭窗口、或容量推迟
> 撞上关闭窗口时，`required_earliest_off` 是**所有约束迭代完毕**后的最终最早时刻，
> `closure_hit` / `capacity_hit` 同时反映其余命中情况。

### `POST` 边界示例

| 场景 | 计划 / 配置（窗口 60 秒、容量 2 架） | validate 结论 | trial 调整 |
| --- | --- | --- | --- |
| 恰好装满 | 00:00、00:30、01:00 | 无超限（锚机 00:00 的窗口 `[00:00,01:00)` 右端不含） | 全部不动 |
| 第三架落入 | 00:00、00:30、**00:59** | CA3 超限；触发窗口 `[00:00,01:00)`，`departures_in_window=3`，最早合规 `01:00` | CA3 → **01:00**（`capacity_hit=true`，锚机 CA1） |
| 容量推迟撞关闭 | 00:00、00:30、00:59；关闭 `[01:00,01:40)` | 同上 | CA3 → 01:00 撞窗口 → **01:40**（`capacity_hit` 与 `closure_hit` 均为 `true`） |
| 窗口滑动 | 00:00、00:40、01:20、02:00（窗口 90 秒、2 架） | 第 3 架（锚机第 1 架，跨度 80 < 90）、第 4 架（锚机第 2 架，跨度 80 < 90）各一次 | 逐项推迟到锚机 + 90 秒，向后级联 |
| 锁定可保持 | 00:00、**00:03:00(locked)**（J→M 180 秒） | 第 2 架无违规 | 锁定航班 00:03 已是合规时刻 → 保持不动 |
| 锁定不可后移 | 00:00、**00:01:00(locked)**（J→M 180 秒） | 第 2 架尾流违规（照常报告） | `blocked` 指向第 2 架，`constraint="wake"`，最早时刻 **00:03:00**；`items` 仅含第 1 架，其后不建议 |

### `POST /api/v1/trial/best-adjacent-swap` — 相邻交换最优试排

请求体与 trial 完全一致（含 `closures`、`capacity`、`matrix`）。**例外：请求中任意航班
`locked=true` 时整个请求被拒绝（`422`）**——交换顺序与「不可移动」语义冲突；显式
`locked=false` 不受影响，本接口响应也不回显锁定相关字段。

每条跑道**独立求解**：

1. 以稳定排序 `(scheduled_off, flight_no)` 为**基线顺序**；
2. 枚举候选：「不交换」（基线本身）以及每一对相邻航班交换后的顺序——只交换离场序列，
   所有航班的 `scheduled_off` 均不改写；
3. 每个候选都按 trial 的尾流间隔、关闭窗口与滚动容量规则**从头试排**
   （只推迟、不提前，延误向后级联）；
4. 候选按以下顺序择优：① **总延误秒数**最小 → ② **最大单航班延误**最小 →
   ③ **交换位置**最小；「不交换」视为位置 0，并列时恒优先——即交换仅在严格更优
   （前两项字典序）时才被选中。

每条跑道返回：

- `baseline_order` / `order`：基线顺序与优化后顺序（`items` 的 `position` 对应优化后顺序）；
- `baseline`：基线指标（`delayed_count` / `total_delay_seconds` / `max_delay_seconds`）；
- `chosen_swap`：选中的交换；**无改进时为 `null`**（此时 `order == baseline_order`、
  `saved_seconds == 0`）。否则含 `position`（被交换对在基线顺序中的第一个位次，1 基）与
  `first_flight_no` / `second_flight_no`（基线顺序下该位次及其后一位的航班，
  交换后二者顺序颠倒）；
- `saved_seconds`：基线总延误 − 优化后总延误（仅凭第二准则取胜时可能为 0）；
- `delayed_count` / `total_delay_seconds` / `max_delay_seconds` 与优化后完整 `items`
  （形状同 trial，含关闭窗口/容量命中明细；配置了 capacity 的跑道照常回显 `capacity`）；
- 批次级汇总 `total_baseline_delay_seconds` / `total_delay_seconds` / `total_saved_seconds`。

结果以 `mode="best_adjacent_swap"` 持久化（优化后顺序、基线指标、选中的交换与节省秒数
一并入库），可用 `GET /api/v1/batches/{batch_id}` 原样回查。

> 示例：`J@00:00`、`M@00:00:50`（J→M 需 180 秒，M→J 仅需 60 秒）。基线总延误 130 秒；
> 交换后 M 先走、J 推迟到 00:01:50，总延误 110 秒 —— `chosen_swap.position=1`，
> `saved_seconds=20`。

### `POST /api/v1/trial/joint` — 相交跑道联合试排

请求体在 validate/trial 同一结构（`runways` / `closures` / `capacity` / `matrix`）
之上增加可选的 **`groups`（互斥跑道组）**：

```json
{
  "runways": [
    {
      "runway": "36L",
      "flights": [
        {"flight_no": "CA1", "scheduled_off": "2026-09-18T08:00:00+08:00", "wake": "H"},
        {"flight_no": "CA2", "scheduled_off": "2026-09-18T08:02:00+08:00", "wake": "M"}
      ]
    },
    {
      "runway": "18R",
      "flights": [
        {"flight_no": "MU1", "scheduled_off": "2026-09-18T08:00:30+08:00", "wake": "H"}
      ]
    }
  ],
  "groups": [
    {"name": "cross", "runways": ["36L", "18R"], "min_interval_seconds": 90}
  ]
}
```

- `groups`：互斥跑道组列表（可省略，省略或为 `[]` 时各跑道独立试排，效果等同于
  分跑道 trial）。每项：
  - `name`：组名（可选），仅用于回显与结果标注；省略时按顺序命名为
    `group-1`、`group-2` …，显式重名返回 `422`；
  - `runways`：**至少 2 条**本请求中存在的跑道标识，组内不得重复；
    **同一条跑道至多属于一个组**（违反返回 `422`）；
  - `min_interval_seconds`：组内**任意跑道**两次相邻离场之间的最小占用间隔
    （正整数秒，`0`/负数/小数/字符串/布尔均被拒绝）。
- 不属于任何组的跑道照常按本跑道尾流/关闭/容量独立试排，不与组内跑道相互约束；
  不同组之间也互不影响。

#### 算法

1. **全局稳定顺序**：把所有跑道的航班合并，按 `(scheduled_off 升序, flight_no 升序)`
   排成唯一全局序列（航班号在整个请求内唯一，排序确定）。响应顶层 `order` 按该顺序
   给出 `{runway, flight_no}` 对；每项另有 `position`（全局位次）与
   `runway_position`（本跑道内稳定位次）。
2. **逐项求最早时刻**，从全局顺序首架开始，每架航班只推迟、不提前，时刻取满足以下
   **全部**条件的最早值：
   1. 不早于本航班原计划；
   2. 不早于**本跑道直接前机**调整时刻 + 尾流矩阵间隔；
   3. 不落在**本跑道**任何关闭窗口内（落入则推迟到窗口 `end`，首尾相接继续迭代）；
   4. 满足**本跑道**滚动容量（不早于前方第 `max_departures` 架调整时刻 + 窗口秒数）；
   5. 不早于**同组最近一架离场**（无论它用哪条组内跑道）的调整时刻 + 组间隔。
3. **收敛迭代**：尾流推迟可能落入关闭窗口、关闭/容量推迟可能破坏组间隔、组间隔推迟
   也可能撞上本跑道关闭窗口……因此每次任一约束把时刻推后，都**从头重新检查全部
   相关约束**，直到没有任何约束再要求推迟为止。延误沿全局顺序向后续航班（含其它
   跑道与同组航班）级联。
4. 每项返回 `adjusted_off` / `delay_seconds` 以及：
   - `constraint`：**首个**要求推迟的约束，取值 `wake` / `closure` / `capacity` /
     `mutex_group`，准点未动为 `null`（判定优先级同此顺序；最终时刻可能是多个约束
     交替迭代的结果，各命中标记同时给出）；
   - `triggered_by`：尾流直接前机航班号（未因尾流推迟为 `null`）；
   - `closure_hit` / `closure_window`：本跑道关闭窗口命中明细；
   - 本跑道配置了 capacity 时返回 `capacity_hit` / `capacity_anchor` /
     `capacity_window_seconds` / `capacity_max_departures` / `capacity_window`，
     跑道对象同时原样回显 `capacity`；
   - 跑道属于某个组时返回 `mutex_group_hit`（是否因组间隔推迟）、`mutex_group`
     （组名）、`mutex_group_anchor`（同组最近离场航班号）、
     `mutex_group_min_interval_seconds` 与 `mutex_group_window`
     （`[锚机调整时刻, +间隔)` 半开窗口），跑道对象同时回显 `mutex_group`。
5. 顶层汇总 `total_delayed` / `total_delay_seconds` / `max_delay_seconds`（仅统计
   可行前缀），并原样回显 `groups` 与全局 `order`。

#### 锁定航班（locked）的联合试排语义

- 锁定航班必须保持 `scheduled_off`，未锁定航班只后移、不提前。
- 若全局顺序中**首个**锁定航班在全部约束收敛后仍需后移，则**整个联合试排立即
  停止**：不挪动该航班、不生成违反约束的时刻，该航班及其后的所有航班（任何跑道）
  **都不再给出建议**；每条跑道的 `items` 只含可行全局前缀中属于该跑道的航班级联。
- 阻塞信息挂在阻塞航班所属跑道的 `blocked` 字段（其它跑道该字段为 `null`，且仅当
  请求显式携带 `locked` 时出现），除与 trial 一致的明细外还包含：
  - `runway` / `runway_position`：阻塞航班的跑道与跑道内位次；
  - `constraint`：首个要求后移的约束（`wake` → `closure` → `capacity` →
    `mutex_group` 优先级）；
  - `required_earliest_off`：四类约束迭代收敛后的最终最早合规时刻（UTC）；
  - **`required_times`**：各约束在收敛解下**分别**要求的时刻，固定四个键
    `wake` / `closure` / `capacity` / `mutex_group`，不适用者为 `null`：
    - `wake`：`{required_off, triggered_by, required_gap_seconds}`；
    - `closure`：`{required_off, window{start,end}}`（最后一个导致推迟的窗口）；
    - `capacity`：`{required_off, anchor_flight_no, window_seconds,
      max_departures, window{start,end}}`；
    - `mutex_group`：`{required_off, triggered_by, group,
      min_interval_seconds, window{start,end}}`。
- 跑道显式携带 `locked` 时照常回显 `locked_flights`，批次级汇总 `total_locked`。

结果以 `mode="joint_trial"` 持久化（输入计划、全局顺序、互斥组、可行前缀每项明细
与 blocker 的各约束所需时刻一并入库），可用批次 ID 原样回查。

### `POST /api/v1/slots/insert` — 单跑道候选插入查询

请求体（单条跑道 + 候选航班 + 可选矩阵）：

```json
{
  "runway": {
    "runway": "36L",
    "flights": [
      {"flight_no": "CA1", "scheduled_off": "2026-09-18T08:00:00+08:00", "wake": "J"},
      {"flight_no": "CA2", "scheduled_off": "2026-09-18T08:05:00+08:00", "wake": "M"}
    ],
    "closures": [
      {"start": "2026-09-18T08:10:00+08:00", "end": "2026-09-18T08:20:00+08:00"}
    ],
    "capacity": {"window_seconds": 600, "max_departures": 2}
  },
  "candidate": {
    "flight_no": "CA9",
    "wake": "M",
    "earliest_off": "2026-09-18T08:03:00+08:00",
    "latest_off": "2026-09-18T08:30:00+08:00"
  }
}
```

- `runway`：与 validate/trial 相同的单跑道结构（`flights` 至少 1 架，跑道内航班号
  唯一；`closures` / `capacity` 可选，规则相同）；`matrix` 语义与其它接口一致。
- `candidate`：`flight_no`（**不得与现有航班重复**，否则 `422`）、`wake`，以及带时区的
  `earliest_off` / `latest_off`——候选可离场区间为**闭区间**（含两端），
  `earliest_off` 不得晚于 `latest_off`。
- **本接口所有时刻（现有航班、关闭窗口、候选区间）必须精确到整秒**，携带毫秒/微秒
  会被拒绝（`422`）。
- 先校验现有计划：现有航班若违反相邻尾流间隔、落入关闭窗口或超出滚动容量，
  返回 `422` 及各类违规数量与首个明细，不做插入评估。

可行时（`feasible=true`）返回：

- `assigned_off`：区间内**最早可行的 UTC 离场时刻**（与 `earliest_feasible_off` 相同）；
- `insert_position`：插入位次（1 基，按 `(离场时刻, 航班号)` 稳定顺序确定）；
- `inserted_order`：插入后的完整离场序列（现有航班时刻不变）。

无空档时（`feasible=false`）返回明确的 `reason`，并给出
`earliest_feasible_off`——满足全部约束的**全局最早时刻**（晚于 `latest_off`），
`insert_position` / `assigned_off` / `inserted_order` 为 `null`。

判定规则：候选时刻须同时满足 ① 与前一架（按插入位次）的尾流间隔、② 与后一架的
尾流间隔、③ 不落入任何关闭窗口（`[start, end)`，可恰在 `end` 离场）、④ 插入后
**全序列**的滚动容量（任意 `[t, t+window_seconds)` 内离场数 ≤ `max_departures`，
含横跨候选的窗口）。该接口为**只读查询**：不写库、不产生 `batch_id`、不改写任何
计划或批次。

### `GET /api/v1/batches/{batch_id}` — 批次回查

所有 POST 接口（validate / trial / best-adjacent-swap / joint）的返回都带 `batch_id`，
可随时回查当时的输入顺序、生效矩阵、关闭窗口、容量配置与结论（validate 的违规/关闭
冲突/容量超限、trial 的试排结果、best-adjacent-swap 的基线指标/选中交换/优化后结果、
joint 的全局顺序/互斥组/可行前缀/阻塞明细），字段与对应 POST 响应一致：配置了
capacity 的跑道回显 `capacity` 及容量结论/试排明细，未配置的跑道**不出现**容量字段；
属于互斥组的跑道回显 `mutex_group` 与组间隔明细，不属于组的跑道不出现组字段，保持
旧响应形状。trial 的**可行前缀**（含每项 `locked`）与 `blocked`（若有）、joint 的
**可行全局前缀**与 blocker 的 `required_times` 一并回查；仅当原请求显式携带 `locked`
时，回查结果才含 `locked_flights` / `blocked` / `total_locked`。不存在的 ID 返回 `404`。

### `GET /api/v1/batches/{batch_id}/conflict-groups` — 离场冲突分组（只读）

针对**已保存的 validate 批次**，把需要联动处理的离场冲突按跑道整理成**冲突组**。
从批次持久化的航班与违规明细构图：

- **尾流违规**：每条违规在其前机与后机之间连一条边；
- **容量超限**：每条超限把其触发窗口 `[window_start, window_end)` 内（按计划离场
  时刻判定）的**全部同跑道航班**连成一组——窗口内未单独违规的航班也会进入该组；
- **关闭窗口冲突**：命中航班自身记为一个**单点冲突**（可经其它冲突与别的航班合并）。

对每条跑道求含冲突的**连通分量**：交叠的容量窗口与尾流边经共享航班合并成一组；
**互不关联的冲突不会仅因计划顺序相邻而合并**（中间隔着未涉及冲突的航班时亦然）。
未涉及任何冲突的航班不出现在结果中。

响应：

- 顶层：`batch_id` / `mode`（`validate`）/ `created_at` / `total_runways` /
  `total_groups`（全批冲突组数）；
- 每条跑道：`runway` / `group_count` / `groups`（无冲突的跑道照常列出，`groups`
  为空列表）；
- 每组：`group_index`（组内序号，1 基）、`flights`（按 `(计划时刻, 航班号)` 排序，
  每项含 `position` / `flight_no` / `wake` / `scheduled_off`）、
  `wake_conflict_count` / `capacity_conflict_count` / `closure_conflict_count`
  （组内三类冲突数量）；组按**最早航班**（计划时刻，再按航班号）排序。

该接口为**只读查询**，不改写任何数据；批次不存在返回 `404`，批次模式不是
`validate`（trial / best_adjacent_swap / joint_trial）返回 `422`。

### 间隔矩阵（可选字段 `matrix`）

请求可携带 `matrix` 覆盖默认矩阵：

```json
"matrix": [
  {"leader": "H", "follower": "M", "seconds": 120},
  {"leader": "J", "follower": "M", "seconds": 180}
]
```

- 省略 `matrix` 时使用内置的 ICAO 风格默认矩阵（可用 `GET /matrix/default` 查看）：

  | 前机 \ 后机 | L | M | H | J |
  | --- | --- | --- | --- | --- |
  | **L** | 100 | 60 | 60 | 60 |
  | **M** | 100 | 60 | 60 | 60 |
  | **H** | 100 | 120 | 100 | 60 |
  | **J** | 180 | 180 | 180 | 120 |

  （单位：秒，示例默认值，实际运行请以当地管制规定通过 `matrix` 覆盖。）

- **提供** `matrix` 时只有列出的组合生效，未列出的组合视为无最小间隔（0 秒）。
- 同一 `(leader, follower)` 组合重复、`seconds` 为负数均会被拒绝（`422`）。

### 错误约定

- `422`：请求体不合法（缺时区、航班号/跑道重复、尾流等级非法、矩阵条目非法、
  关闭窗口结束不晚于开始或彼此重叠、容量字段非正整数、`locked` 非布尔值、
  互斥组引用不存在的跑道/组内跑道重复/同一跑道属于多个组/组少于 2 条跑道/
  组名重复/组间隔非正整数等），或 best-adjacent-swap 请求含任意 `locked=true`
  航班，或 slots/insert 请求中候选号与现有航班重复、候选最早时刻晚于最晚时刻、
  时刻未精确到整秒、**现有计划本身违反尾流/关闭窗口/容量约束**，或对非
  validate 模式的批次请求 conflict-groups，响应体含中文原因。
- `404`：批次 ID 不存在。

---

## curl 示例

```bash
# 间隔校验
curl -s -X POST http://localhost:8000/api/v1/validate \
  -H 'Content-Type: application/json' \
  -d @sample.json

# 试排
curl -s -X POST http://localhost:8000/api/v1/trial \
  -H 'Content-Type: application/json' \
  -d @sample.json

# 相邻交换最优试排（注意：sample.json 含 locked=true，会被 422 拒绝；换用无锁定请求）
curl -s -X POST http://localhost:8000/api/v1/trial/best-adjacent-swap \
  -H 'Content-Type: application/json' \
  -d '{"runways": [{"runway": "36L", "flights": [
        {"flight_no": "CA1", "scheduled_off": "2026-09-18T00:00:00Z", "wake": "J"},
        {"flight_no": "CA2", "scheduled_off": "2026-09-18T00:00:50Z", "wake": "M"}
      ]}]}'

# 相交跑道联合试排（互斥组：36L 与 18R 两次离场至少间隔 90 秒）
curl -s -X POST http://localhost:8000/api/v1/trial/joint \
  -H 'Content-Type: application/json' \
  -d '{"runways": [
        {"runway": "36L", "flights": [
          {"flight_no": "CA1", "scheduled_off": "2026-09-18T00:00:00Z", "wake": "H"}]},
        {"runway": "18R", "flights": [
          {"flight_no": "MU1", "scheduled_off": "2026-09-18T00:00:30Z", "wake": "H"}]}],
       "groups": [{"runways": ["36L", "18R"], "min_interval_seconds": 90}]}'

# 单跑道候选插入查询（只读；候选 CA9 最早 08:03、最晚 08:30）
curl -s -X POST http://localhost:8000/api/v1/slots/insert \
  -H 'Content-Type: application/json' \
  -d '{"runway": {"runway": "36L", "flights": [
          {"flight_no": "CA1", "scheduled_off": "2026-09-18T08:00:00+08:00", "wake": "J"},
          {"flight_no": "CA2", "scheduled_off": "2026-09-18T08:05:00+08:00", "wake": "M"}]},
       "candidate": {"flight_no": "CA9", "wake": "M",
                     "earliest_off": "2026-09-18T08:03:00+08:00",
                     "latest_off": "2026-09-18T08:30:00+08:00"}}'

# 回查批次
curl -s http://localhost:8000/api/v1/batches/<batch_id>

# 离场冲突分组（只读；仅 validate 批次，其他模式 422，批次不存在 404）
curl -s http://localhost:8000/api/v1/batches/<batch_id>/conflict-groups
```

`sample.json` 已随仓库提供（36L 跑道含关闭窗口与「10 分钟内最多 3 架」滚动容量约束，
首航 CA101 标记为 `locked`；18R 跑道含关闭窗口），可直接使用。

---

## 本地开发（不用 Docker）

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
export WAKE_DB_PATH=./wake.db            # 可选，默认 /data/wake.db
uvicorn app.main:app --reload --port 8000
```

运行测试（含 159 个领域逻辑与 API 端到端用例，使用临时 SQLite 文件）：

```bash
python3 -m pip install pytest httpx
python3 -m pytest -q
```

## 项目结构

```
app/
  main.py         # FastAPI 路由（validate / trial / best-adjacent-swap / trial/joint / slots/insert / batches / batches/{id}/conflict-groups / 元信息）
  schemas.py      # Pydantic 请求与响应模型、输入校验（含关闭窗口/容量/锁定/互斥组/候选插入/冲突分组合法性与兼容序列化）
  service.py      # 请求 -> 领域对象 -> 模式计算（含插入查询的现有计划 422 预检）
  joint.py        # 联合试排：全局稳定顺序、尾流/关闭/容量/互斥组间隔不动点迭代、锁定阻塞与各约束所需时刻
  separation.py   # 核心领域逻辑：稳定排序、相邻违规、关闭冲突、滚动容量校验、避窗/容量级联试排、锁定航班可行性（blocker）、相邻交换枚举择优、候选插入空档搜索、离场冲突连通分组、默认矩阵
  timeutils.py    # 时区归一化（统一 UTC，整数秒运算）
  database.py     # SQLite 连接、建表与旧库迁移（含 batches.mode CHECK 重建、互斥组表与联合试排列）
  repository.py   # 批次/航班/关闭窗口/容量/冲突/违规/试排/交换/联合试排（含互斥组与各约束所需时刻）的持久化与回查、冲突分组数据加载
  config.py       # 环境变量配置
tests/            # pytest 领域单测 + TestClient API 测试
Dockerfile
docker-compose.yml
sample.json       # 含关闭窗口与滚动容量约束的示例请求
requirements.txt
```
