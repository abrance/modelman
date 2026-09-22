# 新增一个服务

以新增 `forecast`（时序预测）为例，说明从零到上线的完整步骤。
顺序有依赖：先定接口与配置，再写实现，然后建基线，最后接发布。

## 一、建目录与骨架

```
services/forecast/
  Cargo.toml 或 requirements.txt
  Dockerfile
  README.md
  src/
  models/
  tests/fixtures/
```

Rust 服务需要在根 `Cargo.toml` 的 `members` 里加一项；Python 服务不加，
但要在自己的目录里带完整依赖声明与 `Dockerfile`。

## 二、实现接口约定

必须实现的端点见 `AGENTS.md` 的"服务接口约定"一节。
业务端点自行定义，但错误响应要能给出可读原因，状态码要区分：

| 情况 | 状态码 |
|---|---|
| 请求体格式错误、参数非法、模型名不存在 | 400 |
| 未携带或携带错误 token | 401 |
| 服务过载、等待超时 | 503 |
| 加载失败、推理内部错误 | 500 |

## 三、把配置收到一处

环境变量只在一个文件里读取，其余代码只接受配置结构体。
默认值必须能在开发机上直接跑通，即 `MODELS_DIR` 缺省指向本地 `models/`。

## 四、落实推理侧约束

1. 推理放阻塞线程。
2. 锁粒度到引擎，不同模型不互相排队。
3. 并发用信号量设上界，默认值不超过 2。
4. 大输入设体积与尺寸上限，解码走带限制的路径。

## 五、准备契约测试样本与基线

1. 把若干真实样本放进 `tests/fixtures/`，命名用中性的 `case_NN.<ext>`，
   具体内容与期望值写在基线文件里。
2. 写一个生成基线的可执行程序（参考 `src/bin/gen-fixtures.rs`），
   输出每样本的期望结果与整体基线。
3. 写契约测试：逐样本比对，并检查汇总延迟不劣化。
4. 跑一遍生成基线，**人工确认结果合理**，再提交。

注意样本可能含真实数据。本仓库公开，新增样本前先确认是否可以公开。

## 六、登记模型

在 `registry/<服务名>.yaml` 里登记模型来源、版本、随镜像交付的文件清单，
以及不可用的模型与原因。

## 七、验证镜像

```bash
make build SERVICE=forecast
make test  SERVICE=forecast
make image SERVICE=forecast
docker run --rm -p 8080:8080 modelman-forecast:local
```

确认容器内 `/healthz`、`/readyz`、`/version`、`/models` 都正常，
并且健康检查用的 `--healthcheck` 参数在镜像内可用（镜像里不装 curl 时这是唯一手段）。

## 八、接发布与部署

1. 复制 `.github/workflows/release-ocr.yml` 为 `release-forecast.yml`，
   把触发 tag 前缀改为 `forecast/v*`，镜像名改为 `modelman-forecast`。
2. 在 `cops` 仓库新增 `apps/model-forecast/`，包含 `.env`、`compose.yaml`、`app.conf`，
   端口从 91xx 段取下一个可用值。
3. 按 `docs/deployment.md` 的说明确认暴露方式与资源上限。
