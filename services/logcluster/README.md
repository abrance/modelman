# 日志聚类服务

基于 [Drain3](https://github.com/logpai/Drain3) 在线模板挖掘的 HTTP 服务。
把一批日志行喂进来，得到每行所属的模板（簇）与簇 ID；模板树会累积并落盘，
重启后继续用。

它取代 NAS 上手工部署的 `log-cluster-service`：路由与响应字段保持一致，
调用方不需要改代码。设计依据见 `docs/design.md`，本服务的规划见
`docs/roadmap.md` 第四节。

## 与仓库里其它服务的差别

这个服务**没有模型权重**。"模型"是运行期累积出来的模板树，所以：

| 项 | 取舍 |
|---|---|
| `registry/logcluster.yaml` | 没有权重与版本，登记的是聚类参数与状态文件格式 |
| 镜像可回滚，**状态不可回滚** | 改 tag 回滚镜像不会回滚 `STATE_DIR`，回滚前要一起考虑状态格式 |
| 默认单副本 | 状态在本地卷里，横向扩容前必须先把状态外置 |

聚类参数（`SIM_TH` / `DEPTH` / `MAX_CHILDREN` / `MASK_RULES` /
`PARAMETRIZE_NUMERIC`）是状态文件的一部分。参数改了之后旧状态会被判定为不兼容，
服务会拒绝加载（见下面"状态"一节），不会拿旧模板去回答新配置的请求。

## 接口

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET | `/livez` | 否 | 进程存活 |
| GET | `/healthz`、`/health` | 否 | 存活 + 已加载档位 + 模板数与累计行数 |
| GET | `/readyz` | 否 | 状态可用；否则 503 |
| GET | `/version` | 否 | 版本、提交、构建时间、生效配置 |
| GET | `/models` | 否 | 档位（本服务只有 `default`）与状态文件情况、加载失败原因 |
| GET | `/metrics` | 是 | Prometheus 文本 |
| GET | `/openapi.json` | 是 | OpenAPI schema（交互式 `/docs` 刻意关闭） |
| POST | `/cluster` | 是 | 在线学习并返回每行的簇与模板 |
| POST | `/match` | 是 | 只读匹配，不新建簇 |
| GET | `/clusters` | 是 | 列出全部模板与出现次数 |

`/cluster` 请求体：

```json
{ "lines": ["user alice logged in from 10.0.0.1", "disk usage 95% on /dev/sda1"] }
```

响应（字段与旧实现一致，新增了 `error`）：

```json
{
  "success": true,
  "results": [
    { "line": "user alice logged in from 10.0.0.1",
      "cluster_id": 1,
      "template": "user alice logged in from 10.0.0.1",
      "change_type": "cluster_created",
      "parameters": [] },
    { "line": "disk usage 95% on /dev/sda1",
      "cluster_id": 2,
      "template": "disk usage 95% on /dev/sda1",
      "change_type": "cluster_created",
      "parameters": [] }
  ],
  "cluster_count": 2,
  "time_ms": 0.3,
  "error": null
}
```

两个容易踩的语义：

1. **第一条进入新簇的日志，`template` 就是它自己。** Drain3 只在发生合并时把
   变化的 token 换成 `<*>`，所以只有一个成员的簇不会出现占位符。
2. **`parameters` 只对已经出现占位符的模板有意义**，因此新建簇那一行通常是 `[]`。

`/match` 另外两个限制来自 drain3：匹配是**精确匹配**（内部 sim_th=1.0）并且
默认只走前缀树搜索，所以行首多一个时间戳、token 数变了就会匹配不上。
需要宽松匹配就先调 `SIM_TH` 用 `/cluster`，不要指望 `/match`。

错误码沿用仓库约定：请求体非法或参数越界 400，鉴权失败 401，过载或状态不可用
503，内部错误 500。错误响应同时带 `error`（本仓库约定）与 `detail`（旧实现字段名）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LISTEN_ADDR` | `0.0.0.0:8080` | 监听地址 |
| `STATE_DIR` | `.state`（镜像内 `/app/state`） | 状态文件所在目录，部署时挂卷 |
| `SIM_TH` | `0.4` | 相似度阈值，越低越容易合并 |
| `DEPTH` | `4` | Drain3 树深度，必须 >= 3 |
| `MAX_CHILDREN` | `100` | 内部节点最大子节点数 |
| `MASK_RULES` | 空 | 逗号分隔的脱敏规则名，可选 `ip`、`uuid`、`hex`、`path` |
| `PARAMETRIZE_NUMERIC` | `true` | 含数字的 token 视为参数。开着就基本不需要脱敏规则 |
| `SNAPSHOT_COMPRESS` | `true` | 状态文件是否 zlib + base64 |
| `SNAPSHOT_INTERVAL_MINUTES` | `5` | 模板没变化时的落盘间隔 |
| `MAX_CONCURRENCY` | `2` | 同时进入聚类引擎的请求数上界 |
| `QUEUE_TIMEOUT_SECS` | `30` | 等槽位的超时，超时返回 503 |
| `LIMIT_CONCURRENCY` | `max(8, 4×MAX_CONCURRENCY)` | uvicorn 连接级上限，超出直接 503 |
| `MAX_LINES` | `2000` | 单请求最大行数 |
| `MAX_LINE_CHARS` | `8192` | 单行最大字符数 |
| `MAX_BYTES` | `8388608` | 单请求体上限（8 MiB） |
| `HEALTHCHECK_PATH` | `/readyz` | 容器自探活探的路径 |
| `LOG_LEVEL` | `info` | 日志级别 |
| `AUTH_TOKEN` | 未设置 | 设置后业务端点与 `/metrics` 需要 `X-Auth-Token` 或 `Authorization: Bearer` |

## 状态

状态文件只有一个：`$STATE_DIR/drain_state.json`，外层是 JSON 信封：

```json
{
  "schema_version": 1,
  "profile": {"id": "default", "sim_th": 0.4, "depth": 4, "max_children": 100,
              "mask_rules": [], "parametrize_numeric": true, "snapshot_compress": true},
  "saved_at": "2026-09-23T10:00:00Z",
  "drain": "<base64 of drain3 序列化结果>"
}
```

- **写入**：临时文件 + `fsync` + `rename`。写一半被打断只会留下一个 `.tmp`，
  不会破坏已有状态。文件权限是 `0600`（临时文件的默认模式，`rename` 保留）：
  模板里可能嵌着未脱敏的日志片段，不必要对外可读。
- **落盘时机**：drain3 在模板发生变化时立即落盘；模板没变化时按
  `SNAPSHOT_INTERVAL_MINUTES` 周期落盘；进程退出时再存一次。
- **读取校验**：`schema_version` 或 `profile` 与当前配置不一致、或者 JSON 损坏，
  一律拒绝加载。

状态不可用时的行为是**降级而不是退出**：

| 表现 | 值 |
|---|---|
| 进程 | 正常启动并监听 |
| `/readyz` | 503，`error` 里说明哪一项不兼容 |
| 容器健康状态 | `unhealthy`（镜像的 HEALTHCHECK 探的就是 `/readyz`） |
| `/models` | `loaded: false`，`load_error` 给出原因 |
| `/cluster`、`/match`、`/clusters` | 503，不返回任何聚类结果 |
| 状态文件 | **不写入**，旧状态保持原样 |

这样部署链路会在健康门上失败（不会把坏状态推给调用方），同时还能用 HTTP 看到
原因。确认不再需要旧状态后，删除状态文件再重启即可从空树开始。

一个已知代价：模板频繁变化时每次变化都会压缩 + `fsync` 一次整个状态文件，
所以"新模板很多"的突发流量比"命中已有模板"的流量贵得多。
`SNAPSHOT_INTERVAL_MINUTES` 只影响模板不变时的周期落盘，不影响这个路径。

## 本地开发

```bash
make build    SERVICE=logcluster   # 建 venv 并装钉死版本的依赖
make test     SERVICE=logcluster   # 单元 + 契约 + HTTP 测试
make run      SERVICE=logcluster   # 在 0.0.0.0:8080 起服务，状态写到 ./.state
make smoke    SERVICE=logcluster   # 构建镜像 + 起容器 + 打一次真实聚类
make fixtures SERVICE=logcluster   # 重新生成契约测试基线（必须人工 review 差异）
```

手工试一次：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"lines":["user alice logged in","user bob logged in","disk full on /dev/sda1"]}' \
  http://127.0.0.1:8080/cluster
curl -s http://127.0.0.1:8080/clusters
```

## 契约测试

`tests/fixtures/case_NN.txt` 是**合成的**日志样本（不含任何真实数据），
`baseline.json` 与 `case_NN.expected.json` 是基线：

- 所有样本按固定顺序喂给同一棵从空开始的状态树（生产也是累积学习）；
- 逐行比对模板字符串、簇 ID、`change_type`，再比对每个样本结束时的模板数与累计行数；
- 最终模板清单、只读匹配探针结果逐项比对；
- 不变量：重启并重放同样的输入，不得产生新模板；
- 总耗时不得超过基线预算（实测的 10 倍，下限 2 秒），用来抓数量级退化。

`cluster_id` 依赖插入顺序，所以基线里连 ID 一起记下来——顺序变了要能被发现。
`drain3` 版本也必须与基线一致，换版本就得重跑基线。

## 明确不做

上线时确认过的四项，记录在 `docs/design.md` D14，避免以后反复讨论：

- **不迁移 NAS 旧实例已经学出来的模板。** drain3 的状态跨脱敏配置不通用，
  而且簇 ID 依赖插入顺序，迁过去也会重新编号；旧实例没有代码调用方。
  本服务从空树开始。
- **不启用 `AUTH_TOKEN`。** 只绑回环，与 OCR 服务现状一致。注意 `/cluster` 是写接口，
  一旦有入口对公网（或内网不可信网段）开放，必须先加 token，否则任何人都能污染模板。
- **不备份状态卷。** 模板可以从日志重新学出来，丢卷的代价是多跑一遍，不是丢数据。
- **不停掉 NAS 上的旧实例。** 暂时留着当对照。
