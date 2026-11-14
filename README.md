# swapdesk — 数据源撤换与口径迁移台账

**一句话定位**：面向分布式数据采集集群的「数据源退役治理」引擎——输入本地登记表与撤换请求，
输出影响面、冲突与阻断证据、可执行迁移计划、可回滚执行台账和复核结论。

## 是 / 不是

**是：**

- 一个纯本地、完全离线、一条命令跑通的撤换影响分析引擎。
- 输入：五类 YAML 登记表（数据源、采集任务、下游对象、字段级血缘、口径字典）+ 撤换请求。
- 输出：`blocking / warning / info` 冲突清单、分批迁移计划、SQLite 台账、回滚脚本、
  归档证据、Markdown/HTML 报告。
- 决策规则驱动、确定性可复现；演示时间戳固定后，计划、分析、报告和台账核心字段逐字节一致。
- 每次「分析—计划—执行—回滚—复核」都可从本地文件与台账重放。

**不是：**

- 不是爬虫或采集执行器：不发起网络请求，不抓取任何数据。
- 不是血缘图谱或可视化产品：只有极简本地只读 HTML 报告，核心是引擎与台账。
- 不是指标口径校准/比对工具：只判断口径是否引用将被撤换的源，不做跨口径数值比对。
- 不是通用工作流引擎、调度器、数据湖维护工具或多用户平台。

## 合规与数据说明

- 仓库内全部登记表、任务、表、口径、请求与地址均为**本地合成占位数据**；
  数据源名形如 `src-alpha`、`上游源-A`，主机地址只用 `127.0.0.1`。
- 不包含真实密钥、真实公司名、真实域名或外网 IP。
- 运行期不发起任何网络请求；报告页面无外链、无 CDN、无外部字体。
- 所有输出写入 `./var/`（或 `--workdir` 指定目录），不写入用户 home 或系统目录。

## 快速上手

```bash
pip install pyyaml pytest
python -m swapdesk demo
python -m swapdesk serve
python -m pytest -q
```

- `demo`：生成合成数据，完成「分析 → 计划 → 执行 → 复核」，并写入报告与台账。
  默认工作目录是 `./var`；相同输入重复运行复用同一计划哈希与台账运行，结果保持幂等。
  `serve` 默认读取的 `./var/reports` 正是 demo 的报告目录。
- `serve`：在 `127.0.0.1:8765` 启动只读报告服务，只允许 GET。
- `pytest -q`：全部测试离线通过。

其他命令：

```bash
python -m swapdesk analyze --data-dir data --request req-001.yaml
python -m swapdesk plan    --data-dir data --request req-001.yaml
python -m swapdesk execute --data-dir data --request req-001.yaml --stop-after 3
python -m swapdesk execute --data-dir data --request req-001.yaml --resume
python -m swapdesk verify  --run-id <run-id>
python -m swapdesk rollback --run-id <run-id>
python -m swapdesk serve --port 8765
```

`--workdir` 可覆盖输出根目录。`execute/rollback` 支持 `--timestamp` 固定台账时间戳；
`demo` 固定使用 `2026-01-15T00:00:00Z`。

## 登记表字段

所有 ID 在五类实体间全局唯一。未知引用会带文件、记录 ID、字段定位。

### `sources.yaml`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 数据源 ID，如 `src-alpha` |
| `name` | string | 是 | 展示名，如 `上游源-A` |
| `kind` | enum | 是 | `database / api / file / stream` |
| `status` | enum | 是 | `active / retiring / retired` |
| `fields` | string[] | 是 | 可提供字段，非空、不重复 |
| `owner` | string | 否 | 负责团队 |
| `description` | string | 否 | 合成说明 |
| `endpoint_host` | string | 否 | 默认 `127.0.0.1`，仅作占位 |
| `endpoint_port` | int | 否 | 默认 `0` |

### `tasks.yaml`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 任务 ID，全局唯一 |
| `name` | string | 是 | 任务名 |
| `source_refs` | string[] | 是 | 引用 `sources[].id`，非空 |
| `output_fields` | string[] | 是 | 任务必需输出字段，非空 |
| `schedule` | string | 否 | 合成调度描述，如 `every 10m` |
| `status` | enum | 是 | `active / paused / retired` |
| `owner` | string | 否 | 负责团队 |

### `downstream.yaml`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 下游对象 ID，全局唯一 |
| `name` | string | 是 | 展示名 |
| `kind` | enum | 是 | `table / view / model / report` |
| `upstream_refs` | string[] | 是 | 引用 source/task/downstream，非空 |
| `status` | enum | 是 | `active / retired` |
| `owner` | string | 否 | 负责团队 |

### `lineage.yaml`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 血缘边 ID，全局唯一 |
| `from_ref` | string | 是 | 已知实体 ID（source/task/downstream/metric） |
| `to_ref` | string | 是 | 已知实体 ID；不能与 `from_ref` 相同 |
| `fields` | string[] | 否 | 边携带的字段证据 |
| `relation` | string | 否 | 默认 `derived` |

### `metrics.yaml`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 口径 ID，全局唯一 |
| `name` | string | 是 | 口径名 |
| `source_refs` | string[] | 是 | 引用任意已知实体，非空 |
| `required_fields` | string[] | 是 | 口径必需字段；可为空数组 |
| `required` | bool | 是 | 是否关键口径 |
| `owner` | string | 否 | 负责团队 |
| `definition` | string | 否 | 合成定义说明 |

### 撤换请求 `swap_requests/*.yaml`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `request_id` | string | 是 | 请求 ID |
| `title` | string | 是 | 标题 |
| `created_at` | ISO-8601 string | 是 | 创建时间 |
| `retire` | list | 是 | 非空；每项含 `source`，以及 `replacement` 或 `candidates` 之一 |
| `retire[].source` | string | 是 | 必须已在 `sources.yaml` 登记 |
| `retire[].replacement` | string | 条件 | 单替代源旧写法；与 `candidates` 二选一；可暂未登记，由 F3 报 `C_REPLACEMENT_MISSING` |
| `retire[].candidates` | string[] | 条件 | 候选替代源列表，按优先级降序（首项为首选档，其余依次为保底档）；与 `replacement` 二选一 |
| `window.start/end` | ISO-8601 string | 是 | 撤换生效窗口，`start <= end` |
| `other_windows` | list | 否 | 每项 `id/start/end`，用于窗口冲突判断 |
| `max_parallel` | int | 否 | 默认 2，范围 `1..32` |
| `owner` | string | 否 | 请求负责人 |
| `notes` | string | 否 | 备注 |

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_FILE_MISSING` | 登记表/请求文件不存在或不可读 |
| `E_YAML_PARSE` | YAML 语法错误 |
| `E_SCHEMA_ROOT` | 文件根节点类型错误 |
| `E_SCHEMA_MISSING_FIELD` | 缺少必填字段 |
| `E_SCHEMA_TYPE` | 字段类型错误 |
| `E_SCHEMA_ENUM` | 枚举值或范围越界 |
| `E_SCHEMA_DUPLICATE_ID` | 记录 ID 重复 |
| `E_SCHEMA_EMPTY` | 必需集合为空 |
| `E_REF_UNKNOWN_SOURCE` | 引用未知数据源（含撤换请求待下线源） |
| `E_REF_UNKNOWN_TASK` | 保留错误码；当前未知引用统一由 `E_REF_UNKNOWN_ENTITY` 返回 |
| `E_REF_UNKNOWN_DOWNSTREAM` | 保留错误码；当前未知引用统一由 `E_REF_UNKNOWN_ENTITY` 返回 |
| `E_REF_UNKNOWN_ENTITY` | 下游/血缘/口径引用未知实体 |
| `E_REQUEST_INVALID` | 请求时间或窗口非法 |
| `E_PLAN_BLOCKED` | 存在 blocking 冲突，拒绝生成执行计划 |
| `E_PLAN_INVALID` | 计划结构、指纹或依赖非法 |
| `E_RUN_NOT_FOUND` | 台账中不存在该运行 |
| `E_RUN_CONFLICT` | 同一 run_id 对应了不同计划指纹 |
| `E_RUN_INCOMPLETE` | 运行未完成且未指定 `--resume` |
| `E_STATE_MISMATCH` | 执行前 mock 状态与步骤声明不一致 |
| `E_STEP_FAILED` | 步骤前置条件或后置断言失败 |
| `E_ROLLBACK_UNAVAILABLE` | 无可回滚运行、计划或状态产物 |
| `E_VERIFY_FAILED` | 复核差异（CLI 退出码 4） |
| `E_PATH_ESCAPE` | 报告服务路径越界 |
| `E_HTTP_METHOD` | 只读服务收到非 GET 请求 |
| `E_HTTP_NOT_FOUND` | 请求的报告不存在 |
| `E_INTERNAL` | CLI 边界的未分类错误 |

## 规则 ID

| 规则 ID | 分级 | 含义 |
|---|---|---|
| `W_LINEAGE_CYCLE` | warning | 依赖图检测到环；DFS 稳定枚举，扩散用已访问集合截断 |
| `C_SOURCE_UNKNOWN` | blocking | 撤换请求引用未登记源（API 级防护；CLI 更早报 `E_REF_UNKNOWN_SOURCE`） |
| `C_REPLACEMENT_MISSING` | blocking | 替代源为空、未登记或无法解析 |
| `C_REPLACEMENT_SELF` | blocking | 替代源就是待下线源自身 |
| `C_SOURCE_ALREADY_RETIRED` | warning | 待下线源已处于 `retired` |
| `C_KIND_CHANGED` | warning | 替代源 `kind` 与原源不同 |
| `C_LINEAGE_REVIEW` | info | 任务仅通过血缘间接受影响，需要语义复核 |
| `C_PARTIAL_REPOINT` | info | 多源任务只撤换部分源，保留其余源 |
| `C_FIELD_MISSING` | blocking / warning | 替代后有效源集合缺少任务字段（blocking）或非必需口径字段（warning） |
| `C_METRIC_REVIEW` | warning | 必需口径通过血缘间接受影响，需要复核定义 |
| `C_METRIC_BROKEN` | blocking | 必需口径字段无法在替代后的有效源上成立 |
| `C_WINDOW_OVERLAP` | warning | 撤换窗口与其他变更窗口重叠 |
| `C_WINDOW_ZERO` | info | 撤换窗口起止相同，执行缓冲为零 |

字段兼容判断使用「替代后有效源集合」：撤换源替换为替代源，保留未撤换源，
再取字段并集。因此多源任务不会因单个替代源字段较少而被误判。

### 候选档选档（candidates）

当 `retire[]` 使用 `candidates` 列表时，分析阶段会对每个受影响的 task/metric
单独执行选档：按该消费方自己的 `output_fields` / `required_fields` 计算字段覆盖，
从首选档开始尝试；同一消费方引用多个待下线源时，各源档位组合后合并判定覆盖。
选不出能全覆盖的组合时按递减档位取「缺失最少」组合，并在冲突证据中记下缺什么、
退到哪档。共享实体（下游对象、血缘边）没有字段需求，按全局映射改指：取所有
消费方选档中的最小档位，无消费方引用时取首选档；指向具体消费方的血缘端点跟随
该消费方的选档。使用 `candidates` 的计划在每个重指步骤的 `payload.selection`
中携带采用的档位与选档理由；旧 `replacement` 单字符串写法的计划输出保持不变。

## 目录结构

```
swapdesk/
  __main__.py  cli.py  pipeline.py  config.py  errors.py
  io/          yamlload.py jsonout.py fs.py
  model/       entities.py schema.py
  analysis/    impact.py conflicts.py classify.py
  plan/        planner.py rollback.py
  execute/     runner.py ledger.py state.py verify.py
  report/      markdown.py html.py server.py
  fixtures/    generator.py
  tests/       test_schema.py test_impact.py test_conflicts.py
               test_planner.py test_runner_ledger.py test_verify.py
               test_reports.py test_cli_smoke.py test_offline_guard.py
data/          sources.yaml tasks.yaml downstream.yaml lineage.yaml
               metrics.yaml swap_requests/req-001.yaml req-002.yaml
docs/          DECISIONS.md
```

## 影响扩散语义（F2）

从撤换请求的待下线源排序后同时作为起点，沿以下有向边做稳定 BFS：

1. `source -> task`（`task.source_refs`）
2. `source/task/downstream -> downstream`（`downstream.upstream_refs`）
3. `lineage.from_ref -> lineage.to_ref`
4. `source/task/downstream/metric -> metric`（`metric.source_refs`）

记录每个目标第一次被访问的最短路径。直接影响力路径长度为 1 条边；更长为间接影响。
多源任务保留 `remaining_sources` 与 `partial`。依赖环由排序 DFS 检测，环的起点旋转到字典序
最小节点以稳定去重；BFS 的 visited 集合保证不无限递归。

## 执行与台账语义（F5）

- `run_id = run-<plan_hash 前 16 位>`，同一计划重复执行命中同一台账运行。
- 计划步骤按依赖等级：任务引用 → 下游/血缘 → 口径 → 退役源；每个等级一个 batch，
  内部按 `max_parallel` 切成 `sub_batches`。
- 每步写：`before_hash`、`after_hash`、尝试次数、产物路径、状态。状态哈希覆盖
  source 状态与字段、task 引用、downstream 引用、lineage 端点、metric 引用。
- 步骤产物位于 `runs/<run_id>/steps/<step_id>.json`，台账保存相对路径和 SHA-256。
- `--stop-after N` 表示本次调用最多推进 N 个新步骤，中断后运行状态为 `stopped`。
  不带 `--resume` 再次执行会报 `E_RUN_INCOMPLETE`；带 `--resume` 跳过台账中已成功的步骤。
- 已完成的运行再次执行是幂等的：不新增 step、不增加 attempts、不重复副作用。
- 台账表：`runs`、`steps`、`artifacts`，使用标准库 `sqlite3`。

## 回滚语义（F6）

- `rollback --run-id` 只回退该运行台账中 `succeeded` 的步骤，按 ordinal 倒序生成反向步骤。
- 反向动作：引用类恢复 `before`；`retire_source` 变为 `restore_source`（恢复原状态）。
- 回滚写入独立运行 `rb-<source-run-id>`，带 `parent_run_id`，不覆盖原运行台账。
- 回滚脚本持久化为 `plans/<rollback_id>.json` 与 `runs/<rb-run>/rollback_plan.json`。
- 回滚完成后比较最终状态哈希与原运行执行前哈希；`restored=true` 表示完全恢复。
  若未来引入不可逆动作，可通过 `restore_reason` 报告不可完全回退原因；本期动作均可逆。

## 复核判定（F7）

`verify --run-id` 输出 `PASS` 或 `FAIL` 与差异明细：

1. 计划的每个步骤都存在且状态为 `succeeded`，动作/目标与计划一致。
2. 步骤产物存在，SHA-256 与台账一致；产物中的 `state_after` 哈希等于台账 `after_hash`。
3. 计划声明的后置断言在产物状态上全部成立。
4. 台账无计划外步骤；运行状态为 `complete`。
5. 最终状态文件哈希等于台账 `final_hash`。
6. 无悬空引用、无被退役源仍被引用、无 active 任务源引用为空。
7. 必需口径的 `required_fields` 能由直接源字段并集覆盖。
8. 计划要求退役的源状态必须是 `retired`。

CLI 退出码：PASS `0`，FAIL `4`；blocking 计划拒绝 `3`；schema/参数错误 `2`。

## 离线保证

- 运行期不使用 HTTP 客户端、DNS、CDN、外部字体或远程资源。
- HTML/CSS 全部内联；报告服务由标准库 `http.server` 实现，固定监听 `127.0.0.1`。
- 文件服务只允许 GET，拒绝 POST/PUT/PATCH/DELETE/HEAD/OPTIONS，拒绝 `..`、反斜杠和绝对路径。
- `test_offline_guard.py` 将 `socket.connect`、`connect_ex`、`create_connection`、`getaddrinfo`
  全部替换为抛错函数，仍断言 `demo` 成功。

## 确定性

- JSON 使用 `sort_keys=True`、固定缩进和 UTF-8 换行；浮点做有限精度归一化。
- 分析与计划不含运行时钟字段；计划指纹基于规范化 JSON 的 SHA-256。
- demo 固定输入、固定时间戳与固定工作目录；两次 demo 的 plan/analysis/报告与台账核心字段一致。
- 台账时间戳属于显式字段，可经 `--timestamp` 或 `SWAPDESK_TIMESTAMP` 固定。

## 已知限制

- 本期不读取真实数据仓库/元数据系统，只读本地 YAML 登记表。
- 不实现真实采集、网络抓取、外部 API 调用。
- 不实现血缘图谱可视化、图数据库或前端 SPA。
- 不做跨口径数值比对与指标计算；仅判断引用与必需字段覆盖。
- 不支持分布式执行、消息队列、cron 调度、多用户权限。
- 不提供自适应/学习型迁移建议；全部规则驱动、确定性。
- 不发送通知、告警、邮件或 webhook。
- 一个源在一次撤换中可登记多个候选替代源（`candidates` 按优先级降序），消费方按字段覆盖自动选档；旧 `replacement` 单字符串写法继续可用。
- 回滚假设本地 mock 世界未被计划外修改；真实外部副作用不在本期范围内。
- 报告服务无 TLS/认证，仅因绑定 loopback 而限制为本机只读使用。
