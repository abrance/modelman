# modelman - 自托管小模型服务

给小模型准备的一条尽量无脑的交付链路：一个服务一个镜像，CI 构建并发布，
部署主机拉取，上线前有健康门禁。

当前有两个服务，技术栈不同、交付约定相同：

| 服务 | 任务 | 技术栈 | 权重 | 运行形态 |
|---|---|---|---|---|
| [ocr](services/ocr/README.md) | PP-OCR 文字识别 | Rust + MNN | `.mnn` 随镜像交付 | 无状态，`127.0.0.1:9101` |
| [logcluster](services/logcluster/README.md) | Drain3 日志模板聚类 | Python + FastAPI | 无神经网络权重，参数即"模型" | **有状态**，模板树落在命名卷，`127.0.0.1:9103` |

两者共用同一套交付约定：一个服务一个镜像、契约测试门禁、同一组探活与状态端点、
`make <目标> SERVICE=<name>` 分派。差异只在容器内部。设计依据见 `docs/design.md`。

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
│       ├── src/
│       ├── tests/fixtures/     # 合成样本 + 契约基线
│       ├── tools/              # 契约基线生成器
│       ├── service.mk
│       ├── smoke.sh
│       └── Dockerfile
└── docs/
    ├── design.md               # 系统设计：目标、约束、分层、选型与决策记录
    ├── roadmap.md              # 规划：已交付、下一步、不做的事、未决问题
    ├── architecture.md         # 服务约定与目录职责
    ├── adding-a-service.md     # 新增服务的检查单
    ├── ocr-model-selection.md  # 档位实测对比
    ├── ocr-ui.md               # OCR 网页界面：设计、交互与部署前置
    └── deployment.md           # 镜像、镜像源与部署链路
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
- **有状态**：状态落在 `STATE_DIR`（部署时是命名卷），所以默认单副本；
  状态与参数不兼容时服务降级为 `/readyz` 503、业务端点 503，**不覆盖**旧状态；
- **镜像回滚不等于状态回滚**，回滚前要一起考虑状态格式，见 `docs/deployment.md`。

接口沿用仓库约定（`/livez` `/healthz` `/readyz` `/version` `/models` `/metrics`），
业务端点是 `POST /cluster`（学习）、`POST /match`（只读匹配）、`GET /clusters`。
鉴权现状同 OCR：默认不启用，只绑回环，对外暴露前必须先加 `AUTH_TOKEN`。

## 许可

代码使用 MIT，见 `LICENSE`。`services/ocr/models/` 下的模型文件来自 PaddlePaddle /
PaddleOCR，沿用上游 Apache-2.0，出处见 `services/ocr/models/README.md`。
`services/logcluster/` 无权重文件。

## 交付

本仓库不负责部署。打 tag 后由 CI 构建并推送到 GHCR，再由 `cops` 仓库固定镜像 tag
完成部署。tag 约定 `<服务名>/v<版本>`（如 `ocr/v0.1.1`、`logcluster/v0.1.0`），
镜像 tag 形如 `<版本>-<提交短 sha>`，永不覆盖。流程见 `docs/deployment.md`。
