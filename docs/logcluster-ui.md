# 日志聚类 Web 界面设计

给日志聚类服务加一个自带页面：**贴一段日志，看它被归成哪些模板**。现状是只有 API，
要么敲 curl，要么自己写脚本，所以需要一个页面；但不想为此引入新的部署单元、新的镜像、
前端构建链或 node 工具链。

实现与后续变更都按本文件走。服务本身的接口契约见 `services/logcluster/README.md`，
系统级设计见 `design.md`。

## 约束

下面每一条都是既有决定推出来的，不是本功能新选的；它们直接决定了方案形状。

| 约束 | 来源 | 对设计的影响 |
|---|---|---|
| 服务无账号体系，暴露面由部署侧决定 | `design.md` D9 | 浏览器要用必须经 cops 的入口（线上是 k3s 的 IngressRoute），入口不在本仓库范围内 |
| `AUTH_TOKEN` 当前未启用 | `design.md` D16 | 挂上入口后页面与接口都公开。**代价比 OCR 的页面重**：`/cluster` 是**写**接口，任何人贴日志都会改变模板树 |
| 模板树是累积状态，污染不自愈 | `design.md` D13/D16 | 页面必须让人一眼看出"这个按钮会写东西"，而不是把它做成一个普通查询界面 |
| 运行环境不保证外网可达 | `design.md`（权重随镜像交付的理由） | 页面不得引用任何 CDN、外部字体、外部脚本 |
| 一个服务一个镜像、随镜像交付 | `architecture.md` | 页面随镜像交付，不新增容器、不运行时挂载 |
| `MAX_LINES=2000`、`MAX_LINE_CHARS=8192`、`MAX_BYTES=8 MiB` | `services/logcluster/src/config.py`、`services/logcluster/src/api.py` 的 `BodySizeLimitMiddleware` | 客户端先按 `/version` 报的上限算一遍，超了不发请求；服务端仍是最终闸门 |
| 根路径曾是 404 | `design.md` 服务契约表 | 页面放在根路径，顺带补上这个缺口 |

## 非目标

- 不做账号体系、多用户、权限。
- 不做前端构建（无 node / 打包器），不做依赖管理。
- **不做历史记录**。OCR 页面有历史是因为"拍过的图想再抄一次"，而这里的持久化历史
  就是**服务端的模板树本身**（模板面板展示的就是它）。再存一份前端历史只会和模板树
  不一致。
- **不做模板删除、清空、编辑**。写接口只有"追加/归并"，没有单条删除；清空是运维动作
  （清状态卷，见 `services/logcluster/README.md`），不该出现在页面按钮上。
- 不改 `/cluster`、`/match`、`/clusters` 的请求与响应契约。

## 链路

```
浏览器 ──https──► 入口（cops 侧的 IngressRoute，仓库外）
                    │
                    └──► logcluster 服务（cloud3 k3s Pod）
                             GET  /, /app.css, /app.js   （免鉴权）
                             GET  /healthz, /version     （免鉴权，页面读状态与上限）
                             GET  /clusters              （当前免鉴权）
                             POST /cluster, /match       （当前免鉴权）
```

同源调用，所以不需要动 CORS。页面一律用**绝对路径**（`/cluster`、`/clusters`），
与 OCR 页面同款约定：入口必须落在**域名根路径**（线上是
`https://logcluster.xiaoyxq.top`），挂在子路径下会打不开数据。

## 后端

### 三条路由

| 方法 | 路径 | 内容 | 鉴权 |
|---|---|---|---|
| GET | `/` | `index.html`，`text/html; charset=utf-8` | 否 |
| GET | `/app.css` | 样式，`text/css; charset=utf-8` | 否 |
| GET | `/app.js` | 脚本，`application/javascript; charset=utf-8` | 否 |

免鉴权是刻意的：页面得先能打开，才谈得上填 token（与 OCR 页面同一条理由）。
三条路由只暴露页面结构，不含任何数据。

### 实现要点

- **启动时把三份文件读进内存**（`services/logcluster/src/ui.py` 模块级 `read_text`）。Python 没有
  `include_str!` 那种编译期嵌入，用"启动时读"换同样的效果：文件缺失就是启动失败，
  而不是运行时 404。文件放 `services/logcluster/src/static/` 下，因为 `Dockerfile` 只 `COPY services/logcluster/src`。
- **不加依赖**：不用 `StaticFiles` 挂目录——那会暴露整个目录、多一层中间件，
  三个路由各返回一个 `Response` 就够。
- **响应头**：`Cache-Control: no-cache`（换镜像后浏览器拿到旧页面比多几个字节麻烦得多）；
  CSP 与 OCR 页面逐字一致，`default-src 'none'` 起手只放开同源。
- 顺带把 `design.md` 契约表里"logcluster 根路径还是 404"这条缺口补掉。

### 页面从哪读状态与上限

启动时读两个**免鉴权**端点，不硬编码任何服务端参数：

| 端点 | 取什么 | 用途 |
|---|---|---|
| `GET /version` | `profile_id`、`sim_th`、`depth`、`mask_rules`、`state_loaded`、`max_lines`、`max_line_chars`、`max_bytes`、`auth_required` | 状态条显示生效参数；客户端按真实上限做输入校验；`auth_required` 决定是否提示填 token |
| `GET /healthz` | `cluster_count`、`total_size`、`uptime_secs` | 状态条上的"模板数 / 覆盖行数"，每 10 秒刷新；它免鉴权，所以启用 token 后状态条仍然可用 |
| `GET /clusters` | 模板表 | 模板面板（这一条以后会需要 token） |

参数改了（比如调 `SIM_TH`）页面自动跟着变，不会出现"页面里的默认值和生效值不一致"。

## 页面

无构建：一个 `index.html` + 一个 `app.css` + 一个 `app.js`。与 OCR 页面共用的是
**形状**（CSS 变量、卡片、状态条、token 折叠区），不是代码：输入是文本不是图片，
结果是一行一模板，没有画布与位置框，JS 基本重写。

### 布局（手机优先）

单页，从上到下三段；宽度 ≥ 720px 时把「输入」与「结果」并排两列，手机上单列。

| 区域 | 内容 |
|---|---|
| **状态条** | 模板数 / 覆盖行数 / 运行时长；生效参数（`sim_th`、`depth`、掩码规则数、档位 id）；状态不可用时标红并说明 |
| **输入** | 文本域（每行一条日志，等宽字体）；拖放区与「选择文件」（多个纯文本文件按行合并进来）；实时显示行数与字节数并对照上限；「聚类」（写）与「只匹配」（只读）两个按钮；token 折叠区 |
| **结果** | 汇总：耗时、涉及模板数、**本次新建/改写了几个模板**；逐行列表：日志原文、模板、变更徽标、提取出的参数 |
| **模板面板** | 模板表（id、命中次数、模板文本），按命中次数降序，可刷新、可复制单条模板 |

### 三个动作与它们的代价

| 按钮 | 调用 | 会不会改服务端状态 | 页面怎么表达 |
|---|---|---|---|
| 聚类 | `POST /cluster` | **会**：新建模板、改写既有模板 | 按钮上写明"会写入模板树"，与只读动作视觉区分 |
| 只匹配 | `POST /match` | 不会 | 结果区标注"只匹配、未写入" |
| 刷新模板 | `GET /clusters` | 不会 | 面板右上角图标按钮 |

把"写"和"读"分开是刻意的：这个服务唯一有风险的操作用户必须自己按，且按之前知道代价
（`design.md` D16）。

### 输入的处理与限制

- 按行拆分；连同文件拖入的行一起顺序拼接。
- 去掉空行与行尾 `\r`，并提示"忽略了 N 个空行"——空行会各变成一条模板，白白污染模板树。
- 实时显示 `行数 / max_lines`、`字节数 / max_bytes`，任一超限时**禁用两个按钮**并说明
  超了哪一条；单行超 `max_line_chars` 时点名是第几行（与服务端 400 的措辞对齐）。
- 输入为空时不发请求。

### 结果怎么读

- `change_type` 是这次操作的重点：`cluster_created`（新建了模板）与
  `cluster_template_changed`（既有模板被改写）的行高亮并打徽标，`none` 表示只是命中。
  汇总行直接给"本次新建 X 个、改写 Y 个"——**模板树被改动的地方只有这里看得出来**。
- `parameters` 展示为小标签，说明模板里哪些片段是变量。
- 匹配模式显示每行命中/未命中与命中的模板 id。
- 错误（400 超限、401 无 token、413 体积超限、503 状态不可用）原样显示服务端 `error` 字段，
  不自己编文案。

### 安全

- **模板文本与日志行都是不可信输入**（来自任何人贴的日志），一律用 `textContent`
  渲染，禁止 `innerHTML`。这一条比 OCR 页面更关键：那里渲染的是识别结果，这里渲染的是
  **会被再次回显给别人看的模板**。
- 不引用任何外部资源；CSP 禁内联脚本，顺带保证上面这条不被绕开。
- token（若启用）存 `localStorage` 的 `logcluster.token`，与 OCR 的 key 分开。

## 测试

与 OCR 页面同款三层，机制见 `docs/ocr-ui.md`：

| 层 | 覆盖 |
|---|---|
| `services/logcluster/tests/test_http.py` | 三条路由 200、content-type 与 `utf-8`、`no-cache`、CSP 三项；页面引用 `/app.js` 与 `/app.css`；`app.js` 调 `/cluster`、`/match`、`/clusters` 且带 `X-Auth-Token`，且**不含** `innerHTML`；设置 `AUTH_TOKEN` 时页面仍免鉴权，而同场景下 `/cluster` 返回 401 |
| `smoke.sh`（容器内） | 三条 `curl` 断言：`/` 含 `<title`、`/app.css` 含 CSS 变量、`/app.js` 含 `X-Auth-Token`，并检查响应头 |
| `services/logcluster/tools/ui_acceptance.py`（真浏览器，可选） | 贴三行 → 聚类出模板；模板面板出现条目；只匹配标注未写入；超 `MAX_LINES` 时按钮被禁用；无 console 报错；手机宽度无横向溢出 |

## 风险

| 风险 | 影响 | 处置 |
|---|---|---|
| 页面一挂，写接口就公开了 | 任何人贴日志都能改变你的模板树，而模板污染不自愈，只能清空重学 | 有意接受（`design.md` D16）。页面上"聚类"按钮明确标注会写入；要收敛时启用 `AUTH_TOKEN`，页面已支持填 token，不用改代码 |
| 模板文本回显导致 XSS | 贴进来的日志里带脚本，被渲染成 HTML 就在别人的浏览器里执行 | 一律 `textContent`；严格 CSP；测试里断言 js 不含 `innerHTML` |
| 大输入把服务打满 | 占用共享主机的 CPU / 内存 | 客户端按 `/version` 的上限先拦一道；服务端 `MAX_LINES`/`MAX_LINE_CHARS`/`MAX_BYTES` 是真正的闸门（400/413），并发上界仍由 `MAX_CONCURRENCY` 等卡住 |
| 页面显示的参数与生效值不一致 | 用户按错的阈值理解结果 | 参数全部从 `/version` 读，不硬编码 |
| 入口不在仓库里 | 无法用自动化保证挂载方式正确 | 只在 `deployment.md` 给清单（HTTPS、根路径），不试图代管 |

## 文档与部署

| 文件 | 改动 |
|---|---|
| `services/logcluster/README.md` | 接口表加三条页面路由；新增「Web 界面」一节（地址 `/`、写入代价、token 怎么给、上限怎么看） |
| `README.md` | 接口表加根路径返回页面与两条静态资源 |
| `docs/deployment.md` | 「把页面对公网或手机开放」一节从"OCR 页面"扩成两个页面共用，日志聚类单列其写入代价 |
| `docs/roadmap.md` | 「五、日志聚类也做一个页面」从待定改成已实现 |
| `docs/design.md` | 记 D17：页面同样不带鉴权开放，且把"打开域名的人能写模板树"从纸面变成看得见 |

## 实施顺序

1. 本文件（设计）与 `design.md` D17，先评审再动代码。
2. 后端三条路由 + `services/logcluster/src/static/` 三份文件。
3. `services/logcluster/tests/test_http.py` 补齐，`smoke.sh` 加断言。
4. `services/logcluster/tools/ui_acceptance.py` 与真浏览器过一遍。
5. 文档与接口表同步。
6. 发版：`services/logcluster/src/build_info.py` 的 `VERSION` 涨到 `0.1.1`，打 `logcluster/v0.1.1`；
   `cops` 那边挂入口（HTTPS + 根路径）。
