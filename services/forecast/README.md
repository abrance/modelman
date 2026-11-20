# modelman-forecast

自托管的时序预测服务。模型是 **TimesFM 3.0**（Google 的 decoder-only 时序基础模型，
330M 参数），PyTorch CPU 前向，权重随镜像交付。

对外给一个业务端点 `POST /v1/forecast`：喂历史序列，出中位数点预测与
0.1–0.9 分位区间。

## 许可证：先把这件事说清楚

`google/timesfm-3.0-pytorch` 的**代码是 Apache-2.0，权重不是**。权重是
`timesfm-non-commercial-license-v1.0`，非商业用途。镜像里带着权重，
等于随镜像再分发，所以：

- 自用、评估、非商业研究：可以用。
- 商业或生产用途：不行，换 TimesFM 2.5（Apache-2.0）或其它可商用模型。
  接口形状不变，换的是 `registry/forecast-digests.json` 里的 repo 与 revision。
- 要商用但又想留在 3.0 的架构上，得先向 Google 取得授权。

细节登记在 `registry/forecast.yaml` 的 `license` 一节。

## 接口

仓库约定的端点全都有：

| 端点 | 语义 |
|---|---|
| `GET /` | 自带页面（`src/static/`，免鉴权） |
| `GET /livez` | 进程存活 |
| `GET /healthz` | 存活 + 已加载模型 + 运行时长 |
| `GET /readyz` | 权重已常驻才 200，否则 503 并给出原因 |
| `GET /version` | 版本、提交、构建时间、生效配置 |
| `GET /models` | 档位、加载状态、权重摘要是否核过、分位水平 |
| `GET /metrics` | Prometheus 文本（需鉴权） |
| `POST /v1/forecast` | 业务端点（需鉴权） |

### `POST /v1/forecast`

```json
{
  "series": [
    {"id": "sensor-a", "values": [100, 104, 99, 110, 121]},
    {"values": [[1.0, 1.1, 1.2], [2.0, 2.1, 2.2]]}
  ],
  "horizon": 12,
  "quantiles": [0.1, 0.5, 0.9]
}
```

`values` 两种形状：数字数组是单变量；数组的数组是多变量（每个内层列表是一个变量，
长度必须一致，且同一次请求里所有序列的变量数必须相同）。`id` 可选，给了就原样回填。
`quantiles` 可选，缺省返回模型自带的全部九个分位。

```json
{
  "success": true,
  "model": "timesfm-3.0",
  "horizon": 12,
  "quantiles": [0.1, 0.5, 0.9],
  "forecasts": [
    {
      "id": "sensor-a",
      "point": [121.4, 122.1, "...（12 个值，0.5 分位）"],
      "quantiles": {"0.1": ["..."], "0.5": ["..."], "0.9": ["..."]}
    }
  ],
  "context_lengths": [5],
  "usage": {"series": 1, "context_points": 5, "horizon_points": 12, "quantiles": 3},
  "time_ms": 812.4,
  "truncated": false
}
```

契约要点：`point` 恒等于 `quantiles["0.5"]`；同一时刻分位越高预测值不会越小；
`horizon` 个点，顺序是"最近的未来"在前。错误响应带可读的 `error`，
状态码区分 400（请求非法）/ 401（鉴权）/ 503（过载或权重未就绪）/ 500（推理失败）。

## 配置

只在一个模块里读环境变量（`src/config.py`），其余代码只接受配置结构体。

| 变量 | 默认 | 说明 |
|---|---|---|
| `LISTEN_ADDR` | `0.0.0.0:8080` | 监听地址 |
| `MODELS_DIR` | `models` | 权重根目录，实际权重在 `$(MODELS_DIR)/timesfm-3.0/` |
| `MODEL_TIER` | `timesfm-3.0` | 唯一档位 |
| `DEVICE` | `cpu` | `cpu` 或 `cuda` |
| `INFER_THREADS` | `min(4, 核数)` | torch intra-op 线程数 |
| `PRELOAD` | `true` | 启动即加载权重，否则首次请求时加载 |
| `VERIFY_WEIGHTS` | `true` | 加载前逐文件核 sha256 |
| `MAX_CONTEXT` | `4096` | 上下文上限，超过则取最后一段；模型自身上限 15360 |
| `MAX_HORIZON` | `512` | 预测步长上限 |
| `MAX_SERIES` | `16` | 单请求序列条数上限 |
| `MAX_BYTES` | `2097152` | 请求体上限 |
| `MAX_CONCURRENCY` | `2` | 同时进入模型的请求数上限 |
| `QUEUE_TIMEOUT_SECS` | `60` | 等并发槽位的超时，等不到就 503 |
| `LIMIT_CONCURRENCY` | `max(8, 2×MAX_CONCURRENCY)` | uvicorn 连接数上限 |
| `AUTH_TOKEN` | 空 | 空表示不鉴权（当前部署口径） |
| `HEALTHCHECK_PATH` | `/readyz` | 容器自探活路径 |

## 本地跑

```bash
make build SERVICE=forecast     # 建 venv、装钉死版本依赖、按摘要取回权重（约 1.4 GB）
make test  SERVICE=forecast     # 单元 + HTTP + 契约测试（会真实加载模型）
make run   SERVICE=forecast PORT=9102
```

`make build` 会把权重取到 `services/forecast/models/`（已在 `.gitignore` 里），
已存在且摘要一致时不重复下载。

两个源都可以覆盖，因为它们的类型不同、境内外快慢也相反：

```bash
# 国内网络：权重走镜像，torch 走阿里的目录列表（注意是 --find-links，不是 --index-url）
make build SERVICE=forecast \
  HF_ENDPOINT=https://hf-mirror.com \
  TORCH_SOURCE_ARGS="--find-links https://mirrors.aliyun.com/pytorch-wheels/cpu/"
```

默认值选的是"CI 能过"的那组：HuggingFace 直连 + PyTorch 官方 PEP 503 索引。

起容器冒烟（会验健康门、真实预测、分位单调性与错误码映射）：

```bash
make smoke SERVICE=forecast PORT=18082
```

## 三个刻意的取舍

**上下文截尾而不是报错。** 序列长于 `MAX_CONTEXT` 时取最后一段，并在响应里
把 `truncated` 置为 true、在指标里加计数器。时序里越近的点越重要，截尾比 400
有用；但截断这件事必须能被看见，否则调用方会以为模型看了全部历史。

**只常驻一个档位。** TimesFM 3.0 与 2.5 是两套权重与 API，同时常驻要双份内存
（3.0 单档位加载峰值 2.8 GB）。需要 Apache-2.0 权重时整档替换，不做运行时路由。

**不做结果缓存。** 同一条序列重复请求会重复计算。预测是确定性的，缓存看似划算，
但缓存键要包含整段上下文与步长，命中率在真实用法（每轮都有新点）下很低，
而内存成本是常数级的。真要提速，先把上下文长度收短。

## 实测数据（cloud3，Intel Xeon Gold 6133，4 线程）

| 项 | 值 |
|---|---|
| 加载（含 1.32 GB 权重的 sha256 校验，暖盘） | 1.8 秒（冷启动、空磁盘缓存时更久） |
| 常驻 RSS（4 线程，已服务过请求） | 约 1.5 GiB |
| 加载峰值 RSS（4 线程） | 约 2.8 GB（16 线程下 MKL 每线程 arena 会把它推得更高） |
| 单次预测（中位，分位全取） | 95 ms |
| 512 点上下文 × 48 步 | 1.3 s |
| 镜像体积 | 约 2.5 GB（torch CPU 轮子 + 1.32 GB 权重） |

线程数直接决定内存：同样的权重，16 线程下的峰值明显高于 4 线程（OpenMP/MKL 的
每线程 arena）。所以 `INFER_THREADS` 不只是个性能参数，它同时是内存上界的一部分——
调大它之前先看常驻内存。

容器内存上限至少给 3Gi；CPU request 给 1 即可，服务内部自己把并发收敛在 2、
线程数收敛在 4。
