# 规划

本页记录已完成的能力、下一步计划、明确不做的事，以及需要决策的未决问题。
设计与决策依据见 `design.md`。

## 已交付

| 能力 | 状态 | 证据 |
|---|---|---|
| OCR 服务（PP-OCRv6 small/tiny + v5，MNN，CPU） | 已上线 | `services/ocr`，容器 healthy |
| 标准镜像交付 | 已上线 | `ghcr.io/abrance/modelman-ocr:v0.1.0-e12648f`，公开包，匿名可拉 |
| 契约测试门禁 | 已生效 | 20 张样本 + 基线，逐样本比对行数与相似度 |
| 服务契约端点 | 已上线 | `/` `/livez` `/healthz` `/readyz` `/version` `/models` `/metrics` `/ocr` `/ocr/batch` |
| 访问日志 | 已上线 | `tower_http=debug`，形如 `finished processing request latency=28 ms status=200` |
| 部署链路 | 已闭环 | tag → CI → GHCR → cops 改 tag → 按单元部署（cloud3 k3s：kubectl apply + rollout + 健康门禁） |
| 资源限额 | 已生效 | k8s resources.limits：ocr 8 核/1500Mi、logcluster 1.5 核/256Mi、forecast 2 核/3Gi（口径见 cops 侧 k8s.yaml） |
| 多服务构建与 CI | 已落地 | 根 `Makefile` 只做分派，命令在 `services/<name>/service.mk`；CI 按目录发现服务（`docs/design.md` D12） |
| 日志聚类服务（Drain3 + FastAPI，Python） | 已上线 | `services/logcluster`；镜像 `v0.1.1-4c4f4e1` 跑在 cloud3（k3s），入口 `https://logcluster.xiaoyxq.top`，模板树落在 PVC `model-logcluster-state`；`cops` 侧单元 `apps/model-logcluster` |
| 日志聚类自带 Web 界面 | 已上线 | `GET /`：贴日志 → 看模板与本次变更，含只匹配、模板树面板、写入提示；三份静态资源随镜像交付，不启动新容器。设计见 `docs/logcluster-ui.md` |
| OCR 自带 Web 界面 | 已上线 | `GET /`（根路径，打开域名就是页面）：点选/拖入/粘贴/手机拍照 → 文字，含档位选择、位置框、批量、历史与导出；三份静态资源编译期嵌入二进制，不新增依赖与部署单元。镜像 `v0.1.3-6476ffb` 跑在 cloud3（k3s），入口 `https://ocr.xiaoyxq.top` 按根路径转发；真浏览器验收 19/19 是打线上入口跑的。设计见 `docs/ocr-ui.md` |
| 时序预测服务（TimesFM 3.0，330M 参数，PyTorch CPU，Python + FastAPI） | 已上线 | `services/forecast`；镜像 `v0.1.0-de7e751`（digest `sha256:010d180e…`）跑在 cloud3（k3s），入口 `https://forecast.xiaoyxq.top`；`cops` 侧单元 `apps/model-forecast` |
| 时序预测自带 Web 界面 | 已上线 | `GET /`（根路径，打开域名就是页面）：填或粘贴历史值 → 看中位数点预测与 0.1–0.9 分位区间，可多序列、多分位；三份静态资源随镜像交付，不启动新容器 |
| 判定服务（System One 风格 typed decisions） | 已实现，待发布 | `services/jev`：`POST /v1/systemone` 与 TypeSafe Jev 同形状，一次前向出 choice/noul/score 全部答案与概率；权重 678 MB 不进仓库，构建期按 revision 取回并逐文件校验 sha256（D18）。实测（cloud3）：加载 9.7 s、常驻 1.7 GB、峰值 2.3 GB、单问 207 ms、三问 890 ms（4 线程）。设计与精度边界见 `docs/jev.md` |

实测指标（口径：预热后 + 20 次真实请求后的稳态 RSS）：单档约 74 MB、两档约 95 MB；
`v6small` p50 6.5 ms、p95 16.9 ms（本机），云主机端到端 28–54 ms；
镜像 133 MB（压缩）/ 214 MB（落盘）。

## 下一步

按依赖顺序排列，前一项是后一项的前提或输入。

### 一、对外开放的鉴权口径（已定，不再是"下一步"）

**口径已定并落地：直接开，不启用鉴权**（`docs/design.md` D15–D17），已上线服务的入口都
已公网可达。统一鉴权在外层做（见中期表），届时重新评估服务侧 `AUTH_TOKEN` 的去留；
要提前收敛，cops 三处即可，见 `docs/deployment.md`。

### 二、资源规划决策（已定：升配主机，不做量化）

原判断是「目标主机只有约 2 GiB 空闲内存，OCR + 时序预测的 fp32 组合装不下」，三条候选
路径里选的是**路径 3：主机升配**，另外两条（只上轻量服务、时序预测走 ONNX int8）都不做。

- 升配顺带解掉一个更硬的阻塞：换型前 cloud3 是 QEMU 虚拟 CPU，只有 SSE2，
  `import torch` 直接 SIGILL——这一条量化救不了，只能换型。
- 换型后 16 vCPU（Xeon Gold 6133）/ 16 GiB，`x86-64-v4`。
- 不做量化的依据是实测数字而不是估算：TimesFM 3.0 的 fp32 常驻约 1.4 GiB、
  加载峰值约 2.8 GB、单次预测中位 95 ms，k8s 内存上限给 3Gi 就够；
  在这个余量下，ONNX int8 的导出与精度回归换不回对应的收益。
- 口径提醒：cops 各单元只写 `limits` 时 k8s 把 request 记成同一个值，16 核会被记掉
  14.7 核；forecast 显式写了 `requests: cpu 500m / memory 1Gi`，`limits: cpu 2 / memory 3Gi`。
- 判定服务同批落在同一台机器上：常驻 1.7 GB、峰值 2.3 GB，单问 207 ms、三问 890 ms
  （`INFER_THREADS=4`）。两个内存大户加起来仍在 16 GiB 的余量内，`limits` 都按 3Gi 配；
  线程数必须显式设置——实测判定服务给满 16 线程时单问从 207 ms 涨到 2165 ms。

### 三、时序预测服务（已上线）

- 代码在 `services/forecast/`：TimesFM 3.0（330M 参数）的 PyTorch CPU 前向，FastAPI 提供
  服务，`registry/forecast.yaml` 登记档位、revision 与许可证，
  `registry/forecast-digests.json` 登记逐文件 sha256。
- 选它而不是候选清单里其它模型，是因为它**直接输出分位**（0.1…0.9 九档）：接口上
  「一组数组 = 一个分位」，省掉「先采样再数分位」那一层。`point` 恒等于
  `quantiles["0.5"]`；契约测试断言输出形状、分位单调性与数值容差，不只比文本。
- **权重不进仓库**（1.32 GB，超过仓库 100 MB 的单文件上限）：构建期按不可变 `revision`
  取回并逐文件校验 sha256，运行期 `local_files_only`，不联网。取回走 `HF_ENDPOINT`
  （构建期用镜像站），并强制 `HF_HUB_DISABLE_XET=1`——xet 走镜像站必 401。
- **上下文截尾而不是报错**：序列长于 `MAX_CONTEXT` 时取最后一段，响应里 `truncated=true`
  并有指标计数；模型全局上下文上限是 15360，默认收到 4096。超步长、超序列数、
  不认识的分位一律 400 并带可读原因。
- 只常驻一个档位（3.0 与 2.5 同驻要双份内存），也不做结果缓存
  （缓存键要含整段上下文与步长，真实用法命中率低）。
- 端口 `9102`（容器内 8080）；镜像 `v0.1.0-de7e751`，`cops` 单元 `apps/model-forecast`，
  入口 `https://forecast.xiaoyxq.top`。发布与部署都是首探即过。
- 冷节点首次部署要给足等待：拉 2.5 GB 镜像约 5 分钟，而 `kubectl rollout status`
  从 apply 那一刻就计时，所以单元里 `HEALTH_TIMEOUT=900` 而不是默认的 180。
- **许可证**：代码 Apache-2.0，权重是 `timesfm-non-commercial-license-v1.0`（非商业）。
  自用与评估可以，商业与生产不行；要走到商业那一步就换整档权重（TimesFM 2.5 是
  Apache-2.0）。这一条写在 `registry/forecast.yaml` 的 `license` 段里。

### 四、日志聚类服务（已上线）

- 代码在 `services/logcluster/`：Drain3 在线模板挖掘，**没有神经网络权重**，
  所以 `registry/logcluster.yaml` 里登记的是聚类参数与状态格式，而不是权重。
- 技术栈 Python + FastAPI，路由与响应字段与 NAS 上的旧实现保持一致，
  调用方不需要改代码（失败响应多了一个 `error` 字段）。
- **有状态**：模板树落在 `STATE_DIR` 的卷里，默认单副本，横向扩容前必须先把状态外置。
  状态带 schema 与聚类参数校验，不兼容时降级为 `/readyz` 503、业务端点 503，
  并且不覆盖旧状态文件（见 `services/logcluster/README.md`）。
- 上线状态：tag `logcluster/v0.1.1`（提交 `4c4f4e1`）→ 镜像 `v0.1.1-4c4f4e1`
  → `cops` 单元 `apps/model-logcluster`，跑在 cloud3（k3s），入口
  `https://logcluster.xiaoyxq.top`，健康门禁首探即过。
- 实测：镜像 158 MB；稳态内存约 55 MB（空树 + 24 行聚类，读 `/proc/1/status`），24 行聚类 7–12 ms。
- 明确规定不做的事（见 `docs/design.md` D14）：不迁移 NAS 旧实例的历史模板、状态卷不做自动备份。
- NAS 上的旧实例**已停**（D16）：唯一的日志聚类服务在 cloud3 上，模板树从空树
  重新累积（cloud2 时代的卷没有迁过来，状态迁移步骤在 cops 的迁移设计里）。配套要注意两处外部引用已失效：
  `nas-log-cluster` 技能里的默认端点 `xiaoyxq.top:18083`、以及 kuba 仓库里那份
  `nas-log-cluster-deployment.md`。
- 鉴权口径与 OCR 同（D16）：不启用 `AUTH_TOKEN`，入口挂上即可用。**代价比 OCR 重** ——
  `/cluster` 是写接口，被滥用是在模板树里埋数据，而模板污染不会自动恢复，
  只能清空状态卷重学。
- 已知取舍：模板频繁变化时每次变化都会压缩 + `fsync` 整个状态文件，
  所以“新模板很多”的突发流量比“命中已有模板”贵得多。

### 五、日志聚类页面（已实现）

- 页面在根路径 `/`：贴日志文本或拖入日志文件 →「聚类」看每行归到哪个模板、本次
  新建/改写了哪些模板 →「只匹配」看命中 → 模板树面板看累积结果。设计与取舍见
  `docs/logcluster-ui.md`，鉴权口径见 D17。
- 与 OCR 页面共用的是形状不是代码：能复用的是 CSS 变量与卡片布局，`app.js` 重写。
  按 D12 的口径两个页面各留一份，等出现第三个页面再抽 `shared/`。
- **这一版把写接口摆到了浏览器前面**：页面上的「聚类」按钮直接改模板树。D16 早已接受
  这个口径，但页面让它变成"顺手一下"的事，所以界面上把「聚类」（写）与「只匹配」
  「刷新模板」（读）分开，写入按钮上写明代价。
- 沿用 OCR 那套资源约束：客户端按 `/version` 报的 `MAX_LINES` / `MAX_LINE_CHARS` /
  `MAX_BYTES` 先算一遍，超了直接禁用按钮；服务端那三道仍是最终闸门。

### 六、判定服务（已实现，待发布）

- 代码在 `services/jev/`：内嵌 Laya（`laya==0.3.22` + transformers）作库，自研 FastAPI
  服务提供契约端点与 `POST /v1/systemone`。选型与落选理由见 `docs/jev.md`。
- **权重不进仓库**（678 MB，超过仓库 100 MB 的单文件上限）：构建期按不可变 `revision`
  取回并逐文件校验 sha256，服务启动时再校验一次才加载（D18）。摘要的唯一事实来源是
  `registry/jev-digests.json`，契约测试会断言基线与它一致。
- **只交付 `multilingual` 一个档位**（D19）：英文档位与公开基准微调档位都不带，理由与
  代价写在 `registry/jev.yaml` 的 `excluded` 里。
- 实测（cloud3，Xeon Gold 6133 16 vCPU）：加载 9.7 s、常驻 1.70 GB、峰值 2.30 GB、
  单问 207 ms、三问 890 ms（`INFER_THREADS=4`）。内存是主要成本，CPU 是突发占用。
- **精度边界**：门禁式二值判定方向可靠（满足 0.934 / 不满足 0.309），但 5 选项的细粒度
  工具路由自测 6 例只对 4 例，因此不能按默认阈值当成无人值守的自动激活依据。
- 部署侧要注意：`limits` 给到 3Gi（低于 2.3 GB 会在加载阶段被 OOM），`INFER_THREADS`
  显式设置（实测 16 线程反而慢一个数量级）；构建期取权重的源可覆盖（`HF_ENDPOINT`、
  `TORCH_SOURCE_ARGS`），默认面向 CI，国内网络另行覆盖，见 `services/jev/README.md`。

## 中期

| 项 | 说明 | 触发条件 |
|---|---|---|
| 访问日志提到 INFO | 现在每个请求两行 DEBUG（`on_request` + `on_response`）。改成一条 INFO 级别需要改代码并发新版本，收益是日志量减半、级别语义更准 | 日志量成为问题，或下一次因其它原因发版时一并做 |
| Python 服务的静态检查与格式化 | CI 的 lint job 现在只覆盖 Rust（`fmt` + `clippy`）。三个 Python 服务（logcluster、forecast、jev）的代码都按 ruff 默认规则与格式整理过，但没有门禁，`make fmt-check` / `make clippy` 对它们仍是空操作 | 触发条件已满足（三个 Python 服务），待排期把 `ruff check` / `ruff format --check` 接进 CI |
| 提取基础镜像 | 把 torch / transformers / ONNX Runtime 固定在基础镜像层，服务镜像只叠代码与权重，缩短构建与拉取时间 | **触发条件已满足且代价已出现**：forecast 与 jev 都把 torch + transformers 烘进镜像，两个镜像各背一份（各约 1 GB 量级）。再做同技术栈的服务前应先抽基础层 |
| CI target 缓存治理 | 缓存命中后一次完整构建约 3.5 分钟；随服务数量增加需确认缓存体积不触顶 | 缓存体积接近上限时 |
| 统一鉴权 | 现在 ocr / logcluster / forecast / jev 都不启用 `AUTH_TOKEN`（D15/D16/D17），各自的 `AUTH_TOKEN` 是「要收敛时够用」的停手方案，不是终局。方向已定：在外层做统一鉴权，而不是每个服务各养一套密钥 | 开始做统一鉴权时；届时要重新定服务侧 `AUTH_TOKEN` 保留还是删掉 |
| 指标接入抓取 | 各服务的 `/metrics` 都已就绪（k3s 集群内可达），但还没有 Prometheus 抓取 | 需要在 Grafana 上看曲线时 |
| 镜像体积优化 | 当前 133 MB，主要是模型（39 MB）与运行时基础层 | 拉取时间成为瓶颈时 |
| 批量接口背压 | `/ocr/batch` 目前逐个串行处理，超长批次会长时间占用一个并发槽位 | 出现大批量调用方时 |
| OCR 是否开启 `MAX_SIDE` | 现在为 `0`（不缩放），解码位图只靠代码里 512 MiB 那道闸兜住。开启（如 `1920`）能压掉大图的内存峰值，代价是超大截图上的小字识别率可能下降 | 内存告警、出现 OOM 重启，或大尺寸截图成为主要输入时 |
| logcluster 状态卷的体积 | 模板树没有容量上限也没有回收机制，只随模板数增长（每个模板几百字节）。要处置只有清空重学，代价是丢掉累积的模板 | 模板数上千，或卷体积开始值得盯时 |

## 明确不做

- 多租户、账号体系、配额与计费。
- GPU 调度与显存管理（目标主机无 GPU）。
- 自研编排器或在本仓库内实现部署逻辑。
- 模型在线热更新：换模型走"出镜像 + 改 tag"，保持交付物不可变。
- 通用模型注册中心：当前只有 `registry/<name>.yaml` 这一层声明，够用即可。

## 未决问题

| 问题 | 影响 | 建议的决策时机 |
|---|---|---|
| 何时把本仓库拆多仓库 | 若技术栈分化为多条互不共享代码/CI 的流水线，或单个服务需要独立的发布节奏与权限隔离 | 出现上述信号时，而不是提前拆分 |
| 契约测试样本集如何扩充 | 现在的 20 张样本覆盖窄，仅能拦住"比基线更差" | 有真实业务样本时持续补充，扩充后必须重新生成并 review 基线 |
