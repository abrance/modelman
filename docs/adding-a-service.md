# 新增一个服务

以新增 `forecast`（时序预测）为例，说明从零到上线的完整步骤。
顺序有依赖：先定接口与配置，再写实现，然后建基线，最后接发布。

## 一、建目录与骨架

```
services/forecast/
  service.mk            构建入口，根 Makefile 与 CI 据此分派
  smoke.sh              容器冒烟检查，CI 与发布流程共用
  Cargo.toml 或 requirements.txt
  Dockerfile
  README.md
  src/
  models/
  tests/fixtures/
```

Rust 服务需要在根 `Cargo.toml` 的 `members` 里加一项；Python 服务不加，
但要在自己的目录里带完整依赖声明与 `Dockerfile`。

### 声明构建入口

根 `Makefile` 只定义通用目标，具体命令由 `services/<name>/service.mk` 声明，
所以新增服务不需要改根 `Makefile`：

| 变量 | 对应目标 | 说明 |
|---|---|---|
| `SERVICE_BUILD` | `make build` | 产出 release 产物 |
| `SERVICE_TEST` | `make test` | 单元 + 契约 + HTTP 测试 |
| `SERVICE_RUN` | `make run` | 前台启动 |
| `SERVICE_SMOKE` | `make smoke` | 调用本服务的 `smoke.sh` |
| `SERVICE_FIXTURES` | `make fixtures` | 重新生成契约基线 |
| `SERVICE_CLEAN` | `make clean` | 清理构建产物 |

还有一个可选的：

| 变量 | 对应目标 | 说明 |
|---|---|---|
| `SERVICE_IMAGE_ARGS` | `make image` | 追加给 `docker build` 的参数；Python 服务用它把提交与构建时间作为 build-arg 注入镜像 |

少定义哪个，跑对应目标时就报哪个，不会静默成功。

`make test` / `run` / `image` / `smoke` / `fixtures` 都依赖 `make build`，
所以 `SERVICE_BUILD` 要能把“测试与运行的前置条件”准备好（Rust 是编译产物，
Python 是装好钉死版本的 venv），否则第一次在干净机器上跑 `make test` 会直接报错。

Python 服务的参考实现是 `services/logcluster`：它的 `service.mk` 用
venv + pytest 实现同样的六个变量，根 `Makefile` 没有为它改任何一行。

`make image` 与 `make image-run` 是通用的：前者按 `services/<name>/Dockerfile`
构建 `modelman-<name>:<TAG>`，后者把 `PORT` 映射到容器 8080。
`smoke.sh` 从环境变量读 `IMAGE` 与 `PORT`，行为要求：等 `/readyz`、打真实请求、
容器提前退出或超时时把日志打出来并以非零退出。

CI 遍历 `services/*/service.mk` 得到服务矩阵，**新增服务不需要改
`.github/workflows/ci.yml`**。

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
2. 写一个生成基线的可执行程序（参考 `services/ocr/src/bin/gen-fixtures.rs`），
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
make smoke SERVICE=forecast
```

`make smoke` 会构建镜像、起容器、等就绪、打一次真实请求并报告健康状态，
行为由新服务的 `smoke.sh` 定义，CI 与发布流程跑的是同一个脚本。
需要手工进容器看时用 `make image` + `make image-run`。

确认容器内 `/healthz`、`/readyz`、`/version`、`/models` 都正常，
并且健康检查用的 `--healthcheck` 参数在镜像内可用（镜像里不装 curl 时这是唯一手段）。

## 八、接发布与部署

1. 复制 `.github/workflows/release-ocr.yml` 为 `release-forecast.yml`，
   把触发 tag 前缀改为 `forecast/v*`，镜像名改为 `modelman-forecast`。
   CI（`ci.yml`）不用动，它按目录发现服务。发布 workflow 保持一服务一份，
   因为 tag 前缀与镜像名本来就不同。
2. 在 `cops` 仓库新增 `apps/model-forecast/`，包含 `.env`、`compose.yaml`、`app.conf`，
   端口从 91xx 段取下一个可用值。
3. 按 `docs/deployment.md` 的说明确认暴露方式与资源上限。
4. 跑一遍 `make docs-check`，确认接口契约表（`AGENTS.md` 与 `docs/design.md`）已同步新端点。
