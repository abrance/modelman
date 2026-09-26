# OCR 服务

PP-OCR 检测 + 识别服务，通过 MNN 在 CPU 上推理。取代了此前手工挂载二进制、
复用通用 GPU 镜像的部署方式。

## 为什么不用 Transformers 系 OCR

同一批样本上的量级差别：

| 方案 | CPU 单张耗时 | 权重体积 | 常驻内存 |
|---|---|---|---|
| PP-OCRv6 small + MNN（本服务） | 5–20 ms | 15 MB | 约 74 MB（稳态，见下） |
| TrOCR 一类 Transformer OCR | 数百 ms | 数百 MB 至 1 GB | 1.5 GB 以上 |

部署目标是只有约 2 GB 空闲内存的云主机，且 OCR 是高频调用，因此选择前者。
代价是不走 HuggingFace 生态，模型以 MNN 格式随仓库交付，转换步骤见下。

## 模型来源

原始权重是 PaddleOCR 的 PP-OCR 系列推理模型，经 `paddle2onnx` + `mnnconvert`
转为 MNN 格式，随仓库提交在 `services/ocr/models/`。

当前交付三个档位与实测对比见 `docs/ocr-model-selection.md`。
`PP-OCRv6 medium` 的转换产物在 MNN 中加载失败，因此不交付。

## 接口

```
GET  /            自带 Web 界面（免鉴权，页面本身不含数据）；根路径不再返回 JSON
GET  /app.css     界面样式
GET  /app.js      界面脚本
GET  /livez       存活探针（根路径返回页面前，这个位置是它）
GET  /healthz      liveness + 已加载模型
GET  /readyz       默认档位是否常驻
GET  /version      版本 / 提交 / 生效配置
GET  /models       档位清单、加载状态、加载失败原因
GET  /metrics      Prometheus
POST /ocr          ?model=v6small&backend=cpu，multipart 字段 image
POST /ocr/batch    同一表单内多个 image 字段，逐个返回结果
```

`/ocr` 响应示例：

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

`bbox` 与前置实现一致地返回，可用于叠加高亮；`confidence` 低于
`CONF_THRESHOLD`（默认 0.3）的行会被过滤掉。

## Web 界面

打开根路径 `/` 就是一个可以传图看字的页面：点选、拖入、粘贴图片，手机可以直接拍照。
它会列出 `/models` 里的档位、把 `bbox` 叠在缩略图上、支持逐个复制与导出 `.txt`，
并在浏览器本地留一份只含文字的历史记录。设计与取舍见 `docs/ocr-ui.md`。

三份静态资源在编译期嵌进二进制（`include_str!`），所以：

- `static/` **不进镜像**，`Dockerfile` 与 `.dockerignore` 都不需要改；
- 改页面要重新发版（新镜像 tag），不是改文件就能生效；
- 页面不引任何 CDN 或第三方脚本，运行环境不需要外网。

三条静态路由**免鉴权**：手机得先把页面打开，才谈得上填 token。
设置了 `AUTH_TOKEN` 时，页面会在 `localStorage` 里存一份并在每次请求里带上；
token 无效或缺失时相应输入框会自动展开并标红。

想从公网/手机访问，步骤写在 `docs/deployment.md`。两个要点：

- **HTTPS 是必须的**：HTTP 下 `navigator.clipboard` 不可用，「一键复制」会失效。
- **鉴权目前不开**（`docs/design.md` D15）：入口挂上就是公开的，任何能访问的人
  都能调 `/ocr`。影响面被 `MAX_CONCURRENCY`、`QUEUE_TIMEOUT_SECS` 与 compose 限额卡住，
  表现为对方拿到 503 而不是主机过载。以后要收敛只需在 `cops` 开 `AUTH_TOKEN`，
  页面会自动保存并带上，**不用改代码**。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MODELS_DIR` | `/app/models` | 模型目录 |
| `LISTEN_ADDR` | `0.0.0.0:8080` | 监听地址 |
| `DEFAULT_MODEL` | `v6small` | 未指定 `model` 参数时使用的档位 |
| `PRELOAD_MODELS` | 同 `DEFAULT_MODEL` | 启动即加载的档位，逗号分隔；设为空字符串则不预热 |
| `CONF_THRESHOLD` | `0.3` | 低于该置信度的行被丢弃 |
| `MAX_CONCURRENCY` | `2` | 同时进行的推理数上限 |
| `MAX_SIDE` | `0` | 长边上限像素，`0` 表示不缩放 |
| `MAX_IMAGE_BYTES` | `20971520` | 单个上传体积上限 |
| `QUEUE_TIMEOUT_SECS` | `30` | 等待推理槽位的上限，超时返回 503 |
| `AUTH_TOKEN` | 未设置 | 设置后 `/ocr`、`/ocr/batch`、`/metrics` 需要带 token |
| `OMP_NUM_THREADS` | 未设置 | 建议与 `MAX_CONCURRENCY` 一致，避免线程超额 |
| `RUST_LOG` | `ocr_service=info` | 日志级别 |

`MAX_SIDE` 默认关闭是刻意的：缩图会抹掉小字号文本，而这些模型恰好擅长识别小字。
只有在大尺寸截图占主导且可接受精度变化时才开启。

## 资源占用与上限

**上传的图片不落盘。** multipart 字段直接读进内存（`api.rs` 的 `collect_uploads` 里
`field.bytes()`），解码、缩放、推理也都在内存里完成，进程运行期间不写任何临时文件。
所以这个服务写不满磁盘，它消耗的是内存，而内存的每一层都有闸门。

**内存实测**（本机 docker，读 `/proc/1/status`，镜像与线上同源）：单档预热（`v6small`）
启动后约 24 MB；打过 20 次真实识别后稳态约 74 MB——推理分配的工作内存不归还，所以
**稳态即峰值**；两档预热各推理 20 次约 95 MB。引用内存数字统一用这个口径
（"预热后 + 若干次请求后的稳态 RSS"），不要拿刚启动的瞬时值。

| 闸门 | 数值 | 位置 |
|---|---|---|
| 单张图体积 | `MAX_IMAGE_BYTES`，默认 20 MiB | 先看 `content-length` 头，读完再判一次 |
| 单请求 body | `MAX_IMAGE_BYTES × 4`，默认 80 MiB | `DefaultBodyLimit`，防畸形上传 |
| 解码结果 | 宽/高 ≤ 20000、单张位图 ≤ 512 MiB | `decode_image` 里的 `image::Limits` |
| 在飞请求数 | `MAX_CONCURRENCY`、`QUEUE_TIMEOUT_SECS` | 等不到槽位的请求到超时返回 503，不无限堆积 |

峰值 ≈ 模型常驻（两档 76 MB）+ 在飞数量 × 单张峰值，容器的 `memory` 上限是 1500 MB。

`MAX_SIDE=0`（默认，不缩放）下要留意：`MAX_IMAGE_BYTES` 卡的是**压缩字节数**，不是解码
后的位图，一张高压缩比的大 PNG 能解出几百 MB 的位图（512 MiB 那道闸拦住）。真被这种图
刷，表现是容器 OOM 重启，不是磁盘涨。要压掉这个峰值就设 `MAX_SIDE=1920`，代价见上一节。

宿主机上真正会长的是下面这些，都不归这个服务管，但排查磁盘时先看它们
（上限与机制以 `cops` 仓库的实际部署为准）：

| 增长点 | 上限 | 机制 |
|---|---|---|
| 容器/集群日志（每个请求 2 行 DEBUG 访问日志） | 有轮转上限 | 部署侧声明（compose `max-size` / k8s 日志轮转） |
| 镜像 | 有保留窗口 | 部署脚本按窗口回收旧版本 |
| 入口对请求体的磁盘缓冲 | 不在本仓库 | traefik 默认流式转发、不缓冲；换成会缓冲的入口后，大图会先落主机 `/tmp` |

自查（在云主机上）：

```bash
docker system df
du -sh /var/lib/docker/containers/*/ | sort -h | tail -5
df -h /
```

## 有意的行为差异

相对此前的手工部署，本服务在三处做了改变，都属于修正而非妥协：

1. **未使用 Vulkan。** 实测同一档位 CPU 5–20 ms、Vulkan 30–75 ms，
   且 Vulkan 需要 libvulkan 运行时与一个脆弱的静态初始化符号链接步骤。
   因此默认构建不含 Vulkan，`backend=gpu` 会返回明确的 400 而不是静默变慢。
2. **不再用一把全局锁串行化所有请求。** 锁粒度降到单个引擎，不同档位不再互相排队，
   健康检查也不会被在途推理挡住。
3. **不再交付加载不了的档位。** `PP-OCRv6 medium` 在 MNN 中加载失败，
   从目录与清单中移除，而不是留一个必然 500 的选项。

## 本地开发

```bash
make build
make test                     # 会真实加载模型并跑 20 个样本
make fixtures                 # 仅在有意改动基线时执行
make run PORT=8080
```

`make test` 里的契约测试是模型变更的唯一门禁，必跑。

页面的验收清单可以跑一遍（可选，需要另装 playwright）：

```bash
python3 -m pip install playwright && python3 -m playwright install chromium
make run SERVICE=ocr &                                    # 或另起几个不同配置的实例
python3 services/ocr/tools/ui_acceptance.py --base http://127.0.0.1:8080
```

`--scenario auth` / `--scenario too-large` 分别验鉴权与超限路径，见脚本头部注释。
