# 判定服务

给"让模型回答一个结构化问题"提供自托管后端：给一段 state 和若干带类型的提问
（`choice` / `noul` / `score`），**一次前向**返回全部答案、概率与置信度，不生成文本。

对外协议与 TypeSafe Jev 同形状（`POST /v1/systemone`），所以既有调用方
（例如 `pi-jev`）把 base url 指过来就能用，不需要改代码。设计依据、实测数据与
精度边界见 `docs/jev.md`。

## 与仓库里其它服务的差别

| 项 | 取舍 |
|---|---|
| 权重体积 | 678 MB，超出公开仓库单文件 100 MB 上限，**因此不在仓库里**：构建期按不可变 revision 取回并逐文件校验 sha256，摘要见 `registry/jev-digests.json` |
| 内存占用 | 常驻 1.7 GB、加载峰值 2.3 GB，是三个服务里最重的；部署侧 `limits` 必须给到 3 Gi |
| 参数敏感 | 判定结果与 `MAX_LEN` / `HEAD_MAX_LEN` / 线程数一起决定，所以参数指纹进契约基线，改动即触发基线重生成 |
| 无状态 | 与 OCR 一样，重启不影响行为，可以随时换 tag 回滚 |

## 接口

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET | `/` | 否 | 自带判定台：填 state 与 questions → 看答案与选项概率分布 |
| GET | `/app.css`、`/app.js` | 否 | 页面的样式与脚本（随镜像交付，启动时读进内存） |
| GET | `/livez` | 否 | 进程存活 |
| GET | `/healthz`、`/health` | 否 | 存活 + 已加载档位 + 运行时长 |
| GET | `/readyz` | 否 | 权重已常驻；否则 503 |
| GET | `/version` | 否 | 版本、提交、构建时间、生效配置 |
| GET | `/models` | 否 | 档位、权重摘要校验结果、加载耗时与失败原因 |
| GET | `/metrics` | 是 | Prometheus 文本 |
| GET | `/openapi.json` | 是 | OpenAPI schema（交互式 `/docs` 刻意关闭） |
| POST | `/v1/systemone` | 是 | 一次前向完成所有判定 |

`/v1/systemone` 请求体：

```json
{
  "state": { "prompt": "查一下 k8s 里 pod 为什么一直 pending" },
  "questions": {
    "route": {
      "type": "choice",
      "instructions": "Which single tool should handle this request?",
      "criteria": { "bash": "shell commands", "kubectl_describe": "kubernetes resource inspection" }
    },
    "needed": { "type": "noul", "instructions": "Is any listed tool needed?" },
    "complexity": {
      "type": "score",
      "instructions": "How complex is this request?",
      "criteria": ["trivial", "moderate", "complex"]
    }
  }
}
```

响应（`answers` 的字段名对齐 Jev，只增不改）：

```json
{
  "success": true,
  "model": "multilingual",
  "answers": {
    "route": {
      "type": "choice",
      "choice": "kubectl_describe",
      "probabilities": { "bash": 0.05, "kubectl_describe": 0.95 },
      "distribution": { "bash": 0.05, "kubectl_describe": 0.95 },
      "confidence": 0.86
    },
    "needed": { "type": "noul", "noul": 0.76, "confidence": 0.76 },
    "complexity": { "type": "score", "score": 1.4, "confidence": 0.55 }
  },
  "usage": { "input_tokens": 210, "output_tokens": 0, "totalTokens": 210 },
  "time_ms": 207.4,
  "truncated": false
}
```

两处对上游结果的补齐，都是"只增不改"：

- `distribution` 是 `probabilities` 的副本——上游把选项概率放在 `probabilities`，
  而 Jev 客户端（含 `pi-jev`）读的是 `distribution`；
- `usage.totalTokens` 是 `input_tokens + output_tokens`，上游没这个键。

`truncated` 为真表示 state 没被完整读入（token 预算不够），此时答案可能不可信，
调用方应当据此拒绝采用，而不是继续读 `answers`。

## 档位与精度

只交付 `multilingual` 档位（mmBERT-base 322M，覆盖 100+ 语言含英文）。**它的精度
边界必须清楚**：

| 场景 | 表现 |
|---|---|
| 门禁式二值判断（`noul`） | 方向可靠：满足判据 0.934、不满足 0.309 |
| 细粒度工具选择（`choice`，5 选项） | 自测 6 例对 4 例（0.667），会把"读文件某段"判成无关 |

因此不要把工具路由结果按 0.65 阈值直接当成无人值守的自动激活依据；判定调用方
应当读概率自己做阈值决策，并在低置信时回退。档位选型与落选理由见 `docs/jev.md`。

## 构建与运行

```bash
make build SERVICE=jev    # 建 venv、装依赖（含 CPU 版 torch）、取回并校验权重
make test  SERVICE=jev    # 单元 + 契约 + HTTP 测试，会真实加载模型推理
make run   SERVICE=jev    # 前台启动，默认 0.0.0.0:8080
make image SERVICE=jev    # 构建镜像（权重在构建期取回并校验）
make smoke SERVICE=jev    # 起容器、等就绪、打一次真实判定
```

首次 `make build` 要下载 184 MB 的 torch CPU 轮子与 678 MB 权重。权重只在本地
缺失或摘要不符时才下载；国内网络直连 `huggingface.co` 常常不通，用
`HF_ENDPOINT=https://hf-mirror.com make build SERVICE=jev` 换源（cloud3 上相反，
直连可用、镜像源跳转，见 `docs/jev.md`）。

## 配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `MODELS_DIR` | `models` | 权重根目录，实际读 `<MODELS_DIR>/<MODEL_TIER>` |
| `MODEL_TIER` | `multilingual` | 档位，只交付这一个 |
| `INFER_THREADS` | `4` | torch intra-op 线程数。**必须显式钉住**：实测 16 线程时单问延迟从 207 ms 涨到 2165 ms（共享 vCPU 上互相踩） |
| `PRELOAD` | `true` | 启动即加载权重（加载实测 9.7 s on cloud3） |
| `MAX_LEN` / `HEAD_MAX_LEN` | `1024` / `256` | state 与选项的 token 预算 |
| `MAX_STATE_CHARS` | `8000` | state 字符上限（对象会先序列化） |
| `MAX_QUESTIONS` | `32` | 单请求问题数上限 |
| `MAX_OPTIONS` | `100` | 单问题选项数上限，与上游一致 |
| `MAX_BYTES` | `1048576` | 请求体上限，在 ASGI 层读完再判 |
| `MAX_CONCURRENCY` / `QUEUE_TIMEOUT_SECS` | `2` / `30` | 推理槽位与排队超时，等不到返回 503 |
| `AUTH_TOKEN` | 空 | 有值则业务端点要求 `Authorization: Bearer` 或 `X-Auth-Token` |

`AUTH_TOKEN` 与另外两个服务同一口径：有值就校验，空值就开放，是否启用由部署仓库
决定。判定服务的对外形态是公网入口，启用它可以挡住"打开域名的人顺手调接口"。

## 运维要点

- **`/readyz` 才是健康门**：权重没加载完时 `/healthz` 仍返回 200，只有 `/readyz`
  会 503。容器的 `HEALTHCHECK` 探的就是它。
- **权重摘要**：`/models` 的 `state.digest_verified` 为真才说明镜像里的权重通过了
  sha256 校验；为假表示清单缺失，此时服务仍然启动，但这条信息不该出现在生产里。
- **常驻内存**：判定的成本主要在内存而不是 CPU。`limits` 3 Gi、`requests` 1.5 Gi，
  低于 2.3 GB 会在加载阶段被 OOM 杀掉。
- **别名端点**：`/health` 与 `/healthz` 同义，保留是为了与另外两个服务一致。
