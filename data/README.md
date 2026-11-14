# 合成数据说明

本目录下所有 YAML 都是**完全合成的离线演示数据**，由
`swapdesk/fixtures/generator.py` 确定性生成：

- 数据源名称为 `src-alpha`、`src-beta`、`上游源-A` 等占位名；
- 所有 `endpoint_host` 都是回环占位 `127.0.0.1`，端口为随机选取的本地占位值；
- 不包含真实密钥、真实公司名、真实域名、外网 IP 或个人信息；
- 任务、下游对象、口径、血缘和窗口均为示例业务语义。

## 文件

| 文件 | 内容 |
|---|---|
| `sources.yaml` | 7 个合成数据源（含两个替代源和一个字段不完整替代源） |
| `tasks.yaml` | 5 个采集任务，其中 `task-orders-enrich` 是双源任务 |
| `downstream.yaml` | 8 个下游对象，含直接源依赖、链式下游与报表 |
| `lineage.yaml` | 10 条字段级血缘边 |
| `metrics.yaml` | 5 个口径，其中 4 个标记为必需 |
| `swap_requests/req-001.yaml` | 可执行样例：替换订单源与事件源，带窗口重叠 warning |
| `swap_requests/req-002.yaml` | 阻断样例：缺失替代源 + 字段不完整，计划必须被拒绝 |
| `swap_requests/req-003.yaml` | 候选档样例：首选精简源、保底全量源，字段不足的消费方自动降档 |

## 重新生成

在本项目根目录执行：

```bash
python -c "from swapdesk.fixtures.generator import write_docs; write_docs()"
```

同一 seed（默认 11）生成逐字节相同的内容。测试使用临时目录生成同样的数据，
不会写入本目录或用户 home。
