# 判定服务（jev）：设计、实测与取舍

这个服务给"让模型回答一个结构化问题"这类调用提供自托管后端：给一段 state 和若干
带类型的提问（choice / noul / score），一次前向返回全部答案与概率，不生成文本。

动机很具体：这类判定原先要调用托管 API（TypeSafe Jev），而它的直连访问在
early access 白名单后面，且按输入 token 计费。判定本身是几十毫秒级的窄任务，
放在自己的主机上更可控，也省掉一个外部依赖。

## 为什么是这一条路

选型时的候选与落选理由：

| 方案 | 结论 |
|---|---|
| 直接部署上游 `laya-serve` | 只提供 `/v1/systemone` 与 `/health`，不满足本仓库的服务契约（探活、`/version`、`/models`、`/metrics`、自带页面、体积上限、并发上界），也没有鉴权开关以外的运维面 |
| 自己写推理（读 logits） | 决策头、校准温度、选项编码都在上游包里，重写等于重做一份研究实现 |
| 用更大的 LLM 包装成同协议 | 每次请求都要生成 token，比 Jev 慢一到两个数量级，还把成本从"电费"变回"按 token 计费"，等于绕回原点 |
| **内嵌 `laya` 作为库，自带契约端点** | 采用。权重、推理、决策头都由上游维护，本服务只负责契约、限额、可观测与判定协议 |

判定协议刻意与 TypeSafe Jev 同形状（`POST /v1/systemone`，`answers[id]` 带
`choice` / `noul` / `score` 与 `confidence`），这样既有调用方（例如 `pi-jev`）
只要把 base url 指过来就能用，不需要改代码。

对上游结果做了两处**只增不改**的补齐，因为调用方读的键名不同：

- choice 答案补 `distribution`（上游放在 `probabilities`，而 Jev 客户端读
  `distribution`），两个键同时保留；
- `usage` 补 `totalTokens`（上游是 `input_tokens` / `output_tokens`）。

## 权重怎么交付

**权重不在仓库里。** 这是与另外两个服务唯一的交付差异，原因很直接：这是公开
仓库，单文件上限 100 MB，而 multilingual 档位的权重是 644 MB；LFS 免费额度
1 GB，每次 CI 还要再拉一遍，长期不成立。

替代做法是把"可复现"落在不可变 revision 加逐文件 sha256 上：

1. `registry/jev-digests.json` 是摘要的唯一事实来源，里面记仓库、subfolder、
   提交 sha 与每个文件的 sha256；
2. 构建期 `services/jev/tools/fetch_weights.py` 按该 revision 取回、逐文件校验，不符即失败；
3. 服务启动时`Agent(expected_sha256=...)` 再校验一次，**在解析任何权重之前**，
   所以被替换过的权重不会进入运行时；
4. 契约测试断言基线与摘要文件里的 revision、文件摘要完全一致。

也就是说，"这个容器跑的是哪个权重"由摘要回答，而不是由"文件在不在仓库里"回答。

## 实测数据

两台机器都跑过同一份基准脚本，结果**逐位一致**（工具路由 6 例同样对 4 例、
门禁 noul 同样 0.934 与 0.309），说明判定是确定性的。下面是各自的耗时与内存。

| 指标 | 本机（Ryzen 5 7600，6 核 12 线程） | cloud3（Xeon Gold 6133，16 vCPU） |
|---|---|---|
| 权重体积（5 个文件） | 678 MB | 678 MB |
| 加载耗时（权重已在本地） | 3.3 s | 9.7 s |
| 常驻内存（加载后） | 1.70 GB | 1.72 GB |
| 峰值内存（含加载） | 2.30 GB | 2.29 GB |
| 单问 p50（4 线程） | 99 ms | 207 ms |
| 三问 p50（4 线程） | 492 ms | 890 ms |
| 单问 p50（8 线程） | — | 151 ms |
| 三问 p50（8 线程） | — | 564 ms |
| 单问 p50（16 线程） | — | **2165 ms** |

结论分四条：

- **延迟不是问题。** cloud3 上 4 线程单问 207 ms、三问 890 ms，交互式调用够用。
- **内存才是成本。** 1.7 GB 常驻、2.3 GB 峰值，是这三个服务里最重的一个。
  部署时的 `limits` 必须给到 3 Gi，否则加载阶段会被 OOM 杀掉。
- **线程数不能给满。** cloud3 是共享 vCPU，`INFER_THREADS=16` 时单问从 207 ms
  涨到 2165 ms——线程互相踩的代价比并行收益大一个数量级。默认值是 4，
  想更快就给 8（564 ms 三问），不要给满核数。
- **加载耗时与请求无关。** 权重常驻（`PRELOAD=true`）时首请求没有额外开销；
  关掉预加载则每个冷启动会多 9.7 s（cloud3）。

两台机器的算力差异主要来自 CPU 代际：cloud3 的 Skylake-SP 有 AVX512 地基
（`avx512f` / `bw` / `dq` / `vl`）但没有 `avx512_bf16`、`avx512_vnni` 与 `amx`，
而权重是 bf16，所以走模拟路径。纯 fp32 矩阵乘两台机器基本持平
（2048×2048 实测 31.0 ms 对 30.9 ms），差距集中在模型本身的 bf16 运算上。

## 部署环境的一个坑：vCPU 型号

判定服务对 CPU 指令集有硬要求，而这是**镜像与配置都改不了**的一类问题。
cloud3 最初的 vCPU 是 QEMU 老基线（`QEMU Virtual CPU version 2.5+`，36 个 flag，
没有 `sse4_2`、`avx`、`avx2`），在那上面：

- numpy ≥ 2.4 的 manylinux 轮子连 import 都会失败：
  `RuntimeError: NumPy was built with baseline optimizations: (X86_V2) but your
  machine doesn't support: (X86_V2).`
- PyTorch 官方 CPU 轮子在缺 AVX2 的 CPU 上是 SIGILL，上游有多次复现。

换到 Xeon Gold 6133 之后两者都正常（见上表）。所以部署判定服务之前先确认目标
主机的 vCPU 至少满足 x86-64-v2；`numpy` 的报错是这类问题里最好认的信号。

另一个与网络有关的不对称：本机直连 `huggingface.co` 不通、要走 `hf-mirror.com`，
而 cloud3 与 GitHub 托管 runner 相反，直连可用（实测 10 MB/s）而镜像源返回跳转。
取权重的源因此是个构建参数（`HF_ENDPOINT`），不是写死的常量。

torch 的 CPU 轮子同理，而且这里还有一段事故记录：一开始把阿里云的目录型镜像
（`pytorch-wheels`）写成唯一源，本机能用，但 GitHub 托管 runner 拉它极慢，
CI 在 `make test` 的依赖安装上卡满 30 分钟超时，而 `--quiet` 把进度也盖住了，
日志里只能看到一条命令行。教训是两面的：默认值要选 CI 能过的那个（本地卡住几秒
就能看出，CI 卡住要等满超时），以及不要为了静默而丢掉进度输出。
现在 `TORCH_INDEX` 与 `HF_ENDPOINT` 都是可覆盖变量，默认值面向 CI 与 cloud3。

## 精度：这个服务能做什么、不能做什么

这是必须写清楚的部分。multilingual 档位是 mmBERT-base 上的校准决策模型，它
在**类别少、描述清晰**的判定上表现稳定，在**细粒度工具选择**上不稳。

自测两个场景：

| 场景 | 样本 | 结果 |
|---|---|---|
| 门禁二值判断（noul："所有函数都带类型注解"） | 满足 / 不满足各一例 | 0.934 与 0.309，方向正确、间隔明显 |
| 工具路由（choice，5 个工具选一） | 6 例 | 4 例正确（0.667）；错的两例是把"读文件某段"判成无关、把"全仓库搜索"判成读文件 |

因此：

- **不要**把它的工具路由结果按 `JEV_THRESHOLD=0.65` 直接当成无人值守的自动激活
  依据。`pi-jev` 的自动路由要开，应当先把阈值调高到误判代价可接受的位置，或者
  先在自己的样本上评测 `typed-decisions` 档位乃至微调。
- 门禁、分诊、打分这类粗粒度判定可以直接用，但要给阈值留余量——0.309 那个
  "不满足"离 0.5 并不远，样本一换就会翻面。
- 判定是**确定性**的：同一份权重、同一组参数、同样的输入，选项 argmax 与概率
  稳定复现（跨线程数会让浮点末位变化，故契约测试用 0.005 容差比对概率）。

## 输入与限额

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `MODELS_DIR` | `models` | 权重根目录，实际读 `<MODELS_DIR>/<MODEL_TIER>` |
| `MODEL_TIER` | `multilingual` | 档位，只交付这一个 |
| `INFER_THREADS` | `4` | torch intra-op 线程数，直接决定延迟与对邻居的挤压。**必须显式设置**：共享 vCPU 上给满核数会适得其反（cloud3 实测 16 线程时单问 2165 ms，4 线程 207 ms） |
| `PRELOAD` | `true` | 启动即加载权重 |
| `MAX_LEN` / `HEAD_MAX_LEN` | `1024` / `256` | state 与选项的 token 预算 |
| `MAX_STATE_CHARS` | `8000` | state 字符上限（对象会先序列化） |
| `MAX_QUESTIONS` | `32` | 单请求问题数上限 |
| `MAX_OPTIONS` | `100` | 单问题选项数上限，与上游一致 |
| `MAX_BYTES` | `1048576` | 请求体上限，在 ASGI 层读完再判 |
| `MAX_CONCURRENCY` / `QUEUE_TIMEOUT_SECS` | `2` / `30` | 推理槽位与排队超时，等不到就 503 |
| `AUTH_TOKEN` | 空 | 有值则业务端点要求 `Authorization: Bearer` 或 `X-Auth-Token` |

输入超限一律 400 并给出可读原因；`usage.truncated` 为真表示 state 没被完整读入，
调用方应当把它当成"这次答案可能不可信"的信号，而不是继续用。

部署侧的资源取值由上面的实测推出，不是拍的：

| 项 | 取值 | 依据 |
|---|---|---|
| `resources.requests.memory` | `1.5Gi` | 常驻 1.7 GB，request 略低于它让调度不被虚高占用卡住 |
| `resources.limits.memory` | `3Gi` | 加载峰值 2.29–2.30 GB，低于这个值会在加载阶段被 OOM 杀掉 |
| `resources.requests.cpu` | `500m` | 空闲不占 CPU，只有请求时吃线程 |
| `resources.limits.cpu` | `4` | 与 `INFER_THREADS=4` 对齐；给到 8 则三问降到 564 ms |
| `HEALTHCHECK` 的 `start-period` | `120s` | 加载实测 9.7 s（cloud3），留足一个数量级的余量 |

## 只交付一个档位

上游还有 `english`（ModernBERT-large，421M）与 `typed-decisions` 两个档位，都不带：

- `english`：中英输入混排时 `Router` 会在两个 checkpoint 之间切换，两个都常驻
  会让常驻内存从 1.7 GB 涨到 2.4 GB 以上，而收益只在纯英文场景
  （MASSIVE intent 英文 0.783 对 multilingual 的 0.657）；
- `typed-decisions`：它的高分来自在公开基准的训练集上微调，与本仓库自己的样本
  分布不一致，要用应当先在自己的样本上评测。

两个档位的启用方式都留在代码里（`config.py` 的 `TIER_SUBDIRS`），加档位要同时
改 `registry/jev.yaml`、摘要文件与契约基线。

## 与调用方的接线

`pi-jev` 这类 TypeSafe SDK 的客户端不需要改代码：SDK 会把请求打到
`<base url>/v1/systemone`，并且恒定发送 `Authorization: Bearer` 头，而本服务的
鉴权接受这个头。也就是说启用 `AUTH_TOKEN` 之后，"客户端带 key"这件事是现成的。

## 相关文件

- `services/jev/README.md`：怎么构建、怎么跑、有哪些端点
- `registry/jev.yaml`：档位、revision、契约测试口径
- `registry/jev-digests.json`：权重摘要的唯一事实来源
