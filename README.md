# modelman - 自托管小模型服务

给小模型准备的一条尽量无脑的交付链路：一个服务一个镜像，CI 构建并发布，
部署主机拉取，上线前有健康门禁。

当前有三个服务，技术栈不同、交付约定相同：

| 服务 | 任务 | 技术栈 | 权重 | 运行形态 |
|---|---|---|---|---|
| [ocr](services/ocr/README.md) | PP-OCR 文字识别 | Rust + MNN | `.mnn` 随镜像交付 | 无状态 |
| [logcluster](services/logcluster/README.md) | Drain3 日志模板聚类 | Python + FastAPI | 无神经网络权重，参数即"模型" | **有状态**，模板树落在状态卷（cops 侧为 PVC） |
| [forecast](services/forecast/README.md) | TimesFM 3.0 时序预测（点预测 + 分位） | Python + FastAPI + PyTorch | 1.32 GB，构建期按 revision + sha256 取回，**不进仓库也不进构建上下文** | 无状态 |

三者共用同一套交付约定：一个服务一个镜像、契约测试门禁、同一组探活与状态端点、
`make <目标> SERVICE=<name>` 分派。差异只在容器内部。设计依据见 `docs/design.md`。

许可证口径不一，用之前先看 `registry/<服务名>.yaml`：`ocr` 与 `logcluster` 不涉及
第三方权重许可；`forecast` 的**代码**是 Apache-2.0，但 **TimesFM 3.0 权重是非商业许可**
（`timesfm-non-commercial-license-v1.0`），自用与评估可以，商业与生产不行。

## 目录结构

```
modelman/
├── Cargo.toml                  # cargo workspace（只含 Rust 服务）
├── Makefile                    # 构建 / 测试 / 镜像入口，按 service.mk 分派
├── registry/                   # 模型注册表：每个服务实际跑哪个模型（或哪套参数）
├── services/
│   ├── ocr/                    # PP-OCR 检测 + 识别服务
│   │   ├── models/             # MNN 模型文件与字符字典
│   │   ├── src/                # 服务代码
│   │   ├── tests/fixtures/     # 契约测试样本与基线
│   │   ├── service.mk          # 本服务的构建入口，根 Makefile 据此分派
│   │   ├── smoke.sh            # 容器冒烟检查，CI 与发布共用
│   │   └── Dockerfile
│   └── logcluster/             # Drain3 日志聚类服务（Python，不入 cargo workspace）
│       ├── src/                # 服务代码；src/static/ 是自带页面（随镜像交付）
│       ├── tests/fixtures/     # 合成样本 + 契约基线
│       ├── tools/              # 契约基线生成器、页面验收脚本
│       ├── service.mk
│       ├── smoke.sh
│       └── Dockerfile
│   └── forecast/               # TimesFM 3.0 时序预测服务（Python，不入 cargo workspace）
│       ├── src/                # 服务代码；src/static/ 是自带页面（随镜像交付）
│       ├── tests/fixtures/     # 契约用例 + 基线
│       ├── tools/              # 权重取回（revision + sha256）、基线生成器、冒烟断言
│       ├── service.mk
│       ├── smoke.sh
│       └── Dockerfile
└── docs/
    ├── design.md               # 系统设计：目标、约束、分层、选型与决策记录
    ├── roadmap.md              # 规划：已交付、下一步、不做的事、未决问题
    ├── architecture.md         # 服务约定与目录职责
    ├── adding-a-service.md     # 新增服务的检查单
    ├── ocr-model-selection.md  # 档位实测对比
    ├── ocr-ui.md               # OCR 页面：设计、交互与验收
    ├── logcluster-ui.md        # 日志聚类页面：设计、交互与验收
    └── deployment.md           # 发布链路、tag 命名与暴露口径
```

想先看全貌从 `docs/design.md` 开始；想知道下一步做什么看 `docs/roadmap.md`。

## 快速开始

```bash
make build  SERVICE=ocr          # 构建该服务的产物
make test   SERVICE=ocr          # 单元 + 契约 + HTTP 测试（会真实加载模型推理）
make run    SERVICE=ocr          # 在 0.0.0.0:8080 启动
make image  SERVICE=ocr          # 构建 docker 镜像 modelman-ocr:local
make smoke  SERVICE=ocr          # 构建镜像、起容器、打一次真实识别

make test   SERVICE=logcluster   # Python 服务：先建 venv，再跑 pytest
make smoke  SERVICE=logcluster   # 起容器、等就绪、打一次真实聚类

make test   SERVICE=forecast     # 同上；首次会按 revision + sha256 取回 1.32 GB 权重
make smoke  SERVICE=forecast     # 起容器、等就绪、打一次真实预测
```

根 `Makefile` 是服务无关的分派器：具体命令写在 `services/<name>/service.mk`，
CI 按目录发现服务。切换或新增服务用 `make <目标> SERVICE=<name>`，
不需要改根 `Makefile` 与 `.github/workflows/ci.yml`。

对运行中的服务做一次手工冒烟：

```bash
# OCR
curl -s http://127.0.0.1:8080/healthz
curl -s -F image=@services/ocr/tests/fixtures/case_05.png \
     http://127.0.0.1:8080/ocr

# 日志聚类（有状态：/cluster 会写入模板树，/match 只读）
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"lines":["user alice logged in","user bob logged in"]}' \
     http://127.0.0.1:8080/cluster

# 时序预测（分位只能是模型自带的那九档：0.1…0.9）
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"series":[{"values":[100,104,99,110,121,118,130]}],"horizon":3,"quantiles":[0.1,0.5,0.9]}' \
     http://127.0.0.1:8080/v1/forecast
```

## OCR 服务

PP-OCR v5/v6，经 MNN 在 CPU 上推理。它取代了原先把编译好的二进制挂载进通用
GPU 镜像的手工部署方式。

### 接口

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET | `/` | 否 | 自带 Web 界面：传图看字，手机可直接拍照（页面在根路径，开域名即用） |
| GET | `/app.css`、`/app.js` | 否 | 界面的样式与脚本（随镜像交付，编译期嵌入） |
| GET | `/health`, `/healthz` | 否 | 存活状态，含已加载模型与运行时长 |
| GET | `/livez` | 否 | 进程存活 |
| GET | `/readyz` | 否 | 默认档位已常驻，可以接流量 |
| GET | `/version` | 否 | 版本、提交、构建时间、生效配置 |
| GET | `/models` | 否 | 可用档位、加载状态、加载失败原因 |
| GET | `/metrics` | 是 | Prometheus 文本格式 |
| POST | `/ocr` | 是 | 识别单张图片 |
| POST | `/ocr/batch` | 是 | 一次请求识别多张图片 |

`/ocr` 接受 `multipart/form-data`，图片放在 `image` 字段，可选 `model` 与
`backend` 查询参数，返回结构与前置实现保持一致：

```json
{
  "success": true,
  "results": [
    { "text": "烈战☆大表弟", "confidence": 0.934,
      "bbox": { "left": 0, "top": 0, "width": 150, "height": 33 } }
  ],
  "time_ms": 28.4,
  "model": "v6small",
  "backend": "cpu",
  "error": null
}
```

默认不启用鉴权。设置 `AUTH_TOKEN` 后，`/ocr`、`/ocr/batch`、`/metrics` 需要带
`X-Auth-Token: <token>` 或 `Authorization: Bearer <token>`；
健康检查类端点刻意保持开放，以便容器在拿到凭据之前就能被探活。

根路径 `/` 是服务自带的页面（点选 / 拖入 / 粘贴 / 手机拍照 → 文字），
设计与部署前置见 [`docs/ocr-ui.md`](docs/ocr-ui.md)。

### 模型档位

| 档位 | 说明 |
|---|---|
| `v6small` | **默认。** 在自带样本上精度与耗时的比值最优 |
| `v6tiny` | 快约 5 倍、内存少约 60%，但会漏掉 `v6small` 能识别的约 40% 的行 |
| `v5` | 上一代，保留用于输出兼容 |

实测数据与"为什么不交付 PP-OCRv6 medium"见 `docs/ocr-model-selection.md`。

## 日志聚类服务

Drain3 在线模板挖掘。把一批日志行喂进来，得到每行所属的模板与簇 ID；模板树
累积并落盘，重启后继续用。它取代了原先手工部署在 NAS 上的同类服务，路由与
响应字段保持一致，调用方不需要改代码。

这个服务**没有模型权重**，因此和 OCR 有三处关键差别（细节见
[`services/logcluster/README.md`](services/logcluster/README.md)）：

- **"模型"是运行期累积出来的模板树**，`registry/logcluster.yaml` 登记的是聚类
  参数与状态文件格式；
- **有状态**：状态落在 `STATE_DIR`（部署时是状态卷），所以默认单副本；
  状态与参数不兼容时服务降级为 `/readyz` 503、业务端点 503，**不覆盖**旧状态；
- **镜像回滚不等于状态回滚**，回滚前要一起考虑状态格式，见 `docs/deployment.md`。

接口沿用仓库约定（`/livez` `/healthz` `/readyz` `/version` `/models` `/metrics`），
业务端点是 `POST /cluster`（学习）、`POST /match`（只读匹配）、`GET /clusters`。
根路径 `/` 也是自带页面：贴日志看模板，见
[`docs/logcluster-ui.md`](docs/logcluster-ui.md)。

鉴权现状同 OCR（`design.md` D16、D17）：默认不启用 `AUTH_TOKEN`，入口挂上即可用。**代价比 OCR 重** —— 页面上那个「聚类」按钮写的是模板树，
谁都能按，而模板污染不自愈；要收敛时启用 `AUTH_TOKEN`，页面不用改。

## 时序预测服务

TimesFM 3.0（330M 参数，PyTorch CPU）做点预测与分位预测：给一段历史值，返回未来若干步。
`point` 是中位数，`quantiles` 是 0.1…0.9 九档——**分位是模型自带的，只认这几档**，
请求别的档位（如 0.25）返回 400 并列出可用档位。

它和 OCR、日志聚类的差别（细节见 [`services/forecast/README.md`](services/forecast/README.md)）：

- **权重不进仓库**（1.32 GB，超过仓库 100 MB 的单文件上限）：构建期按不可变 `revision`
  取回，用 `registry/forecast-digests.json` 的 sha256 逐文件校验；运行期 `local_files_only`，
  不联网。构建上下文也不带权重（`.dockerignore`），否则每次构建都要先上传几个 G。
- **常驻内存是主要成本**：加载后约 1.4 GiB、加载峰值约 2.8 GB，k8s 内存上限 3Gi；
  推理线程数默认钉在 4（共享云主机的核不是自己的）。
- **超长上下文取最后一段**而不是报错，响应里用 `truncated=true` 告知；
  `MAX_CONTEXT` 默认 4096（模型上限 15360）。

服务接口沿用仓库约定，业务端点是 `POST /v1/forecast`；根路径 `/` 也是自带页面。
鉴权现状同其它服务（不启用 `AUTH_TOKEN`）；**许可证是它的额外边界**，见下一节。

## 许可

代码使用 MIT，见 `LICENSE`。`services/ocr/models/` 下的模型文件来自 PaddlePaddle /
PaddleOCR，沿用上游 Apache-2.0，出处见 `services/ocr/models/README.md`。
`services/logcluster/` 无权重文件。`services/forecast/` 的**代码**是 Apache-2.0，
但 TimesFM 3.0 的**权重**沿用上游 `timesfm-non-commercial-license-v1.0`（非商业）：
自用与评估可以，商业与生产不行，要走到商业那一步就换整档权重（TimesFM 2.5 是
Apache-2.0）。这一条也写在 `registry/forecast.yaml` 的 `license` 段里。

## 交付

本仓库不负责部署。打 tag 后由 CI 构建并推送到 GHCR，再由 `cops` 仓库固定镜像 tag
完成部署。tag 约定 `<服务名>/v<版本>`（如 `ocr/v0.1.3`、`logcluster/v0.1.1`、
`forecast/v0.1.0`），
镜像 tag 形如 `<版本>-<提交短 sha>`，永不覆盖。流程见 `docs/deployment.md`。
