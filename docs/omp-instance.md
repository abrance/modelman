# omp 实例：第二个工作台（omp-web）

## 这份文档的边界

`docs/agent-design.md` 描述**主工作台**（model-agent，fork pi-web）的骨架。本文描述**第二个工作台实例**：
上手体验 omp（oh-my-pi）用的独立一套。两者共享约定（cops 单元、密钥管道、域名与证书、基线对账规则），
但**镜像、基线、卷、单元互不影响**——实验田随时可删，主工作台不受影响。

## 定位

| 项 | 取值 |
|---|---|
| 是什么 | 一套独立的 omp 工作台，浏览器里用真 omp 引擎跑会话 |
| 定位 | 实验田：随时可删可重建，**不跟主工作台一起升级** |
| 域名 | `omp.xiaoyxq.top`（与主工作台同一台 cloud3、同一套 Traefik + ACME） |
| 镜像 | 我们自己的镜像，pin omp 与 omp-web 的版本 |
| 卷 | 独立 PVC 4 Gi，`PI_CODING_AGENT_DIR=/data/agent-home` |
| 不做 | 不碰主工作台的镜像与卷；不共享基线；不装主工作台的 skill |

## 形态结论：omp 是 agent，omp-web 才是浏览器面

omp（`can1357/oh-my-pi`）是 **Pi 的一个 fork**，本身是终端编码 agent（TUI／`-p` 一次性／Node SDK／
`omp --mode rpc`／ACP），**没有 HTTP 服务端**。omp 生态里也没有 pi-web 那种工作台：
`packages/collab-web` 是会话共享的浏览器客户端（需要 `/collab` 常驻 host，浏览器只管看与打断），
`packages/stats` 是用量仪表盘，`python/robomp` 是 GitHub 工单机器人。

**选 `omp-web`**（作者 `ddallabenetta`，MIT，npm 包 `omp-web`，当前 `0.4.4`）：
它是 pi-web 的 fork，但**不是另一个 agent**——它在 Next.js 服务端**进程内跑 omp 自己的 SDK**
（依赖 `@oh-my-pi/pi-*`），对着同一个 omp agent 目录，所以终端里开的会话能在浏览器继续，反之亦然。
它自带会话浏览、实时对话、模型角色、provider 配置、skill 管理、文件预览。

⚠️ 同名项目要分清：`github.com/17380936778/omp-web` 是**另一个** fork，它依赖上游
`@earendil-works/pi-*`，浏览器里跑的是 **pi**，不是 omp。选型时不要混。

## 实测证据（2026-09-28，本机 Docker）

| 验证项 | 结果 |
|---|---|
| omp 版本与完整性 | `omp/18.4.2`；`omp-linux-x64` 272 MiB，SHA256 与官方 `SHA256SUMS.txt` 一致 |
| provider 兼容 | `models.yml` 自定义 provider（`baseUrl: https://api.boboa.top/v1`、`api: openai-responses`）被加载；`omp -p "…" --model yun777/GLM-5.3-Flash` 返回真实回答 |
| omp 支持 `openai-responses` | 是（omp 仓库的 models 文档把它列为合法 api 值） |
| 数据面兼容 | omp CLI 写的 `sessions/--workspace--/<ts>_<uuid>.jsonl` 被 omp-web `0.4.4` 的 `/api/sessions` 正常列出 |
| 浏览器自建会话 | 有：`app/api/agent/new` + `lib/rpc-manager.ts` 的 `createAgentSession()`（omp SDK 进程内） |
| 反代 Host 信任 | 设 `OMP_WEB_HOSTNAME=0.0.0.0` + `OMP_WEB_ALLOWED_HOSTS=omp.xiaoyxq.top` 后，`Host: omp.xiaoyxq.top` 的 `/` 与 `/api/sessions` 均 200。**不需要额外反代层，不需要 fork** |
| 运行时依赖 | 需 **Bun ≥ 1.3.14**（omp SDK 是 TS 源码 + `bun:` 内置）；omp-web 启动器本身跑在 Node 或 Bun 上 |

## 镜像

| 组成 | 来源与 pin |
|---|---|
| omp | 官方 release 自包含二进制 `omp-linux-x64`，pin `v18.4.2`，构建期校验 SHA256 |
| omp-web | npm `omp-web@0.4.4` |
| Bun | `1.3.14` |
| 基础 | `node:24-bookworm-slim`（glibc；官方提示 musl 需另装 `libstdc++`/`libgcc`） |
| 语言能力 | TypeScript/JavaScript（`typescript-language-server` + `biome`）、Go（`gopls`）；**不装 Rust 工具链** |
| 其它 | `git`、`ripgrep`、`gh`（暂不配 token）、`tmux`（留终端入口用） |

构建期可以联网（拉 release、装 npm 包）；**运行期不做任何安装动作**，与主工作台同一口径。

## 卷与基线

| 落点 | 内容 |
|---|---|
| 出厂种子（镜像内只读） | `config.yml`（默认档与 advisor 档）、`models.yml`（provider 定义，密钥只写环境变量名） |
| 卷 `/data/agent-home` | `sessions/`、`agent.db`（SQLite 凭据）、`models.db`、`models.json`（omp-web 同步产物）、用户改动 |

对账规则**沿用** `docs/agent-design.md` 的 P1：种子缺失则复制；哈希等于上次基线则升级为基线；
本地改过则保留本地并记 `conflicts`。omp 侧运行期会写 `models.db`／`agent.db`／`models.json`，
这些不进基线，回滚镜像不回滚它们。

## 部署（cops 单元）

| 项 | 取值 |
|---|---|
| 单元 | `apps/omp-web`，命名空间 `cops`，`DEPLOY_TARGET=cloud3` |
| 入口 | `omp.xiaoyxq.top`（IngressRoute 拆 web + websecure 两条，ACME HTTP-01） |
| 容器端口 | `30141` |
| 探针 | `/manifest.webmanifest`（静态资源不经过鉴权中间件，未登录也 200）——`/` 与 `/api/*` 在开启密码后会被中间件拦 |
| PVC | `omp-web-data` 4 Gi，`Recreate` |
| 密钥 | `OMP_WEB_PASSWORD`（与主工作台同一密码值，走 cops secrets）、`AGENT_PROVIDER_API_KEY`（777ai，复用） |

容器环境变量：

| 变量 | 值 | 为什么 |
|---|---|---|
| `PI_CODING_AGENT_DIR` | `/data/agent-home` | omp 与 omp-web 都用它定位 agent 目录（omp-web 的回退链也认它） |
| `OMP_WEB_HOSTNAME` | `0.0.0.0` | 绑定所有接口 |
| `OMP_WEB_ALLOWED_HOSTS` | `omp.xiaoyxq.top` | 否则非 loopback 的 Host 一律 403 |
| `OMP_WEB_AUTHENTICATED` | `1` | 开启密码 |
| `OMP_WEB_PASSWORD` | 引用密钥 | 只引用不写值 |
| `OMP_WEB_NO_OPEN` | `1` | 容器里不尝试开浏览器 |

## 鉴权口径

沿用主工作台的口径：入口只输密码，`OMP_WEB_PASSWORD` 由部署侧注入，仓库与镜像里只有引用。
与主工作台**共用同一个密码值**（有意选择：一台被撞影响两台，换独立密码只需改一处 secret）。

## 第一版装什么

| 项 | 取值 |
|---|---|
| 模型 provider | `yun777`（`api.boboa.top`，`api: openai-responses`）：`glm-5.3`、`GLM-5.3-Flash` |
| 默认档 | `GLM-5.3-Flash`（与主工作台一致） |
| advisor 档 | `GLM-5.3-Flash` |
| LSP | TypeScript/JavaScript、Go |
| 联网搜索 | omp 内置的免 key 源（duckduckgo/startpage 一类） |
| GitHub | 只装 `gh`，不配 token |
| 不装 | Chromium/浏览器工具、MCP、Python eval、Rust 工具链、主工作台的 skill |

## 切片顺序

1. **镜像**：新建仓库（`platform/{docker,runtime,seed}`）＋ CI 出镜像到 GHCR；冒烟：`omp --version`、
   `bun --version`、omp-web 启动后 `/manifest.webmanifest` 200、`/api/sessions` 200。
2. **单元与域名**：cops `apps/omp-web` ＋ 用户给 `omp.xiaoyxq.top` 加 A 记录（→ 186.244.201.55）；
   Traefik 路由与证书签发。
3. **验收**：浏览器登录；新建会话跑一轮（走 777ai）；LSP 与 `gh` 冒烟；`omp -p` 在容器里跑一次。
4. **后续（按需）**：终端入口（`term.omp.xiaoyxq.top`）、omp/omp-web 升级流程、基线对账、
   与主工作台共用 skill 或物料的接缝。

## 明确不做

| 不做 | 原因 |
|---|---|
| 自建 collab 中继 | 需要常驻 `/collab` host，浏览器只能看与打断，不如 omp-web 完整 |
| 自写 Web 前端 | omp-web 已进程内跑 omp SDK；自写是重复造且要长期跟 omp 版本 |
| fork omp-web | 不改上游代码就能满足需求（反代与鉴权全走环境变量）；要改再 fork |
| fork/copy 上游 `17380936778/omp-web` | 它的引擎是 pi 而非 omp，与本文目标不符 |
| 在第一版就加终端入口 | omp-web 能自建会话，先减小暴露面 |

## 待定

| 待定 | 现状与倾向 |
|---|---|
| 镜像仓库落点 | 倾向新建独立仓库（如 `abrance/omp-box`）；不放进 cops（部署仓库不构建镜像） |
| 终端入口 | 先不加；需要 CLI 行为（`omp stats`、扩展调试）时再加 `term.` 子域 |
| omp 版本跟进节奏 | 实验田：只在需要时升；升之前重跑本文的实测表 |

## 与主工作台的关系（一览）

| 维度 | 主工作台 model-agent | omp 实例 omp-web |
|---|---|---|
| 上游 | fork `agegr/pi-web` | 包 npm `omp-web`（不 fork） |
| 引擎 | pi（`@earendil-works/pi-*`） | omp（`@oh-my-pi/pi-*`） |
| 镜像 | `ghcr.io/abrance/model-agent` | 待建（`omp-box`） |
| 基线清单 | pi-web fork 仓库里的 seed 清单（6 包 + 1 skill） | omp 的 `models.yml` + `config.yml` |
| 卷 | `model-agent-data` 4 Gi | `omp-web-data` 4 Gi |
| 域名 | `ai.xiaoyxq.top` | `omp.xiaoyxq.top` |
| 密码 | `PI_WEB_PASSWORD` | `OMP_WEB_PASSWORD`（同一值） |
| provider | `777ai` | `yun777`（同一端点） |
