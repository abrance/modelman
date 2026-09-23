# modelman - 自托管小模型服务

给小模型准备的一条尽量无脑的交付链路：一个服务一个镜像，CI 构建并发布，
部署主机拉取，上线前有健康门禁。

当前只有一个服务：**OCR**（PP-OCR v5/v6，经 MNN 推理）。它取代了原先把编译好的
二进制挂载进通用 GPU 镜像的手工部署方式。

## 目录结构

```
modelman/
├── Cargo.toml                  # cargo workspace
├── Makefile                    # 构建 / 测试 / 镜像入口
├── registry/                   # 模型注册表：每个服务实际跑哪个模型
├── services/
│   └── ocr/                    # PP-OCR 检测 + 识别服务
│       ├── models/             # MNN 模型文件与字符字典
│       ├── src/                # 服务代码
│       ├── tests/fixtures/     # 契约测试样本与基线
│       └── Dockerfile
└── docs/
    ├── design.md               # 系统设计：目标、约束、分层、选型与决策记录
    ├── roadmap.md              # 规划：已交付、下一步、不做的事、未决问题
    ├── architecture.md         # 服务约定与目录职责
    ├── adding-a-service.md     # 新增服务的检查单
    ├── ocr-model-selection.md  # 档位实测对比
    └── deployment.md           # 镜像、镜像源与部署链路
```

想先看全貌从 `docs/design.md` 开始；想知道下一步做什么看 `docs/roadmap.md`。

## 快速开始

```bash
make build     # cargo build --release
make test      # 单元 + 契约 + HTTP 测试（会真实加载模型推理）
make run       # 在 0.0.0.0:8080 启动服务
make image     # 构建 docker 镜像 modelman-ocr:local
```

对运行中的服务做一次冒烟：

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s -F image=@services/ocr/tests/fixtures/case_05.png \
     http://127.0.0.1:8080/ocr
```

## 接口

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
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

## 模型档位

| 档位 | 说明 |
|---|---|
| `v6small` | **默认。** 在自带样本上精度与耗时的比值最优 |
| `v6tiny` | 快约 5 倍、内存少约 60%，但会漏掉 `v6small` 能识别的约 40% 的行 |
| `v5` | 上一代，保留用于输出兼容 |

实测数据与"为什么不交付 PP-OCRv6 medium"见 `docs/ocr-model-selection.md`。

## 许可

代码使用 MIT，见 `LICENSE`。`services/*/models/` 下的模型文件来自 PaddlePaddle /
PaddleOCR，沿用上游 Apache-2.0，出处见 `services/ocr/models/README.md`。

## 交付

本仓库不负责部署。打 tag 后由 CI 构建并推送到 GHCR，再由 `cops` 仓库固定镜像 tag
完成部署。流程见 `docs/deployment.md`。
