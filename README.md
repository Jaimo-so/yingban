# 影伴

个人 AI 电影伙伴。本仓库只公开项目代码和使用说明；内部 PRD、阶段交接、设计方案及评测报告不进入 Git。

## 界面预览

### 此刻 · 从一部电影聊起

从“聊聊我看完的电影”或“帮我找一部电影”开始，选择今晚与电影相处的方式。

![影伴首页：聊聊我看完的电影与帮我找一部电影两个入口](docs/images/yingban-home.png)

### 这周，可以从这里开始

当日票房展示区提供电影信息、票房排名和数据来源入口，也可以直接留在想看或聊聊这部电影。

![影伴当日票房展示区：电影排名、票房数据及留在想看和聊聊这部入口](docs/images/yingban-box-office.png)

### 我的电影 · 留下观影轨迹

按“看过”“想看”“不感兴趣”整理电影，查看海报片单，并从这里添加电影或进入本月电影回顾。

![影伴我的电影页面：看过、想看、不感兴趣分类及电影海报片单](docs/images/yingban-my-movies.png)

## 核心能力

- 本机双击版无需登录或后台密码；多人或公网部署支持用户名和密码注册、登录，管理员后台仍使用独立口令。已有邀请码可在注册时绑定原账户及电影记录。
- 新用户以 1～5 部真实看过的电影建立口味基线；推荐在数据层硬排除已看电影。
- 支持聊电影、结构化推荐、片单、观后感、续聊摘要、电影对话记录、月度回顾和可撤回分享；新的成功聊天均保存在服务端 `chat_records` 表中，既有绑定电影的对话从 `conversation_records` 迁移显示。
- 三个内置 Skill 分别服务内容创作、多次观影认知和院线新片决策；草稿不会自动发布。
- 支持 TMDB、公开 Web Search、语音和图像能力；附加能力失败不影响主文字回复。
- 管理后台统一管理模型、提示词、Skill、联网、语音、图像、邀请码、用量和健康指标。

不可破坏的边界：AI 不冒充真人，不建立隐蔽人格档案；敏感现实信息不自动进入长期电影记忆；密码、邀请码、会话、管理员口令和第三方密钥不得写入文档、日志或前端；当前不做付费、广告、公共影评广场或默认公开用户内容。

## 架构入口

| 路径 | 职责 |
| --- | --- |
| `app.py` | CLI、依赖组装和 Uvicorn 启动 |
| `fastapi_app.py`、`server.py` | FastAPI 桥接、认证、用户与管理员 API |
| `agent.py` | Agent 提示词、工具循环、Skill 注入与安全短路 |
| `storage.py` | SQLite schema、迁移、事务、账户隔离与业务数据 |
| `integrations.py`、`voice.py`、`image_models.py` | 电影、搜索、语音和图像供应商 |
| `product_skills.py` | 三个内置 Skill 的定义与默认版本 |
| `local_bootstrap.py` | 可选的受保护部署凭据初始化；双击本机版不调用 |
| `web/` | 用户端与管理端静态资源及交互 |
| `frontend/` | Next.js App Router 静态导出外壳 |
| `scripts/sqlite_maintenance.py` | SQLite 检查、备份和迁移 |

## 本地启动

要求 Python 3.12、Node.js 24 LTS、npm 和 `uv`。

macOS 可在 Finder 中直接双击 `启动影伴.command`。启动器会自动定位项目、在需要时构建前端、启动本地服务，并在健康检查通过后打开默认浏览器。双击启动固定使用 `127.0.0.1:8765` 的“本机免认证模式”：用户端直接进入，管理后台也不要求口令，邀请码管理会隐藏。它不会生成或要求用户寻找任何初始密码。

首次启动或 `requirements.txt` 更新后，`uv` 会联网补齐本机缓存里缺少的 Python 依赖；已缓存的依赖不会重复下载。启动器不会覆盖已有 `.env`、邀请码或数据库；数据库恰好只有一个活跃账户时，本机模式会继续使用该账户，保留它的电影数据。终端窗口保持开启时，影伴服务持续运行；按 `Control-C` 可停止。

```bash
git clone https://github.com/Jaimo-so/yingban.git
cd yingban
npm --prefix frontend ci
npm --prefix frontend run build

YINGBAN_HOST=127.0.0.1 \
YINGBAN_LOCAL_OPEN_ACCESS=true \
uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python app.py serve
```

本地用户端为 <http://127.0.0.1:8765/>，产品介绍页为 <http://127.0.0.1:8765/landing>，管理端为 <http://127.0.0.1:8765/admin>。介绍页沿用影伴的品牌色与实际产品界面，交互包含滚动叙事和电影分类预览；图片为本项目素材，场景文字不代表真实用户评价。不要使用 `file://` 直接打开 `web/index.html`。

若要做多人或公网部署，不要启用 `YINGBAN_LOCAL_OPEN_ACCESS`。用户可直接在首页注册用户名和密码；已有邀请码的用户在注册时填写原邀请码，即可继续使用原账户和电影记录。管理员后台仍使用独立口令。注册是开放的，目前没有邮箱验证和密码找回流程。可先运行可选的安全初始化；仅在迁移已有邀请码账户或需要旧接口兼容时生成邀请码：

```bash
uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python local_bootstrap.py

uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python app.py generate-invites 1
```

## 修改界面内容，不改业务代码

直接编辑 [`web/ui-content.json`](web/ui-content.json)，保存后刷新本地页面即可生效。页面每次启动都会重新读取这份 JSON；只改它不需要修改组件、重新构建 Next.js 或重启本地 Python 服务。线上需要把更新后的文件按现有发布流程部署，修改本地文件不会自动更新线上。

| 想修改什么 | JSON 中的位置 | 编辑方式 |
| --- | --- | --- |
| 首页主标题、说明 | `home.title`、`home.description`、`home.eyebrow` | 改引号内文字 |
| 首页两个入口 | `home.discussion`、`home.recommendation` | 改 `kicker`、`title`、`description` |
| 票房区标题和提示 | `home.boxOffice` | 改文案；`{date}` 会自动填实际日期 |
| 是否展示海报年份、地区 | `poster.showYear`、`poster.showRegions` | 使用 `true` / `false`，不加引号 |
| 海报替代文字 | `poster.alt` | 保留 `{title}` 自动填入实际电影名 |
| 票房卡显示哪些字段、先后顺序 | `cards.home.fields` | 调整 `rank`、`title`、`amount`、`meta` 的顺序；删除某项即可隐藏该项 |
| 推荐卡显示内容 | `cards.recommendation.fields` | 调整或删除 `genres`、`reason`、`notes` |
| 我的电影、搜索行显示内容 | `cards.history.fields`、`cards.search.fields` | 调整或删除 `title`、`meta` |
| 卡片按钮名称、排列和显示 | 相应 `actions` / `watchlistActions` / `watchedActions` / `conversationActions` | 改 `label`、调换完整对象的顺序；删除对象即可隐藏按钮，`id` 保持原值 |
| 推荐反馈选项 | `cards.recommendation.feedbackReasons` | 改 `label` 或排序，`id` 保持原值 |
| 选片按钮、笔记徽标 | `cards.onboarding`、`cards.history.noteBadge` | 改文案；无障碍标签中的 `{title}` 自动替换 |
| 搜索按钮与状态提示 | `search`、`cards.search.watchedLabel`、`cards.search.otherLabel` | 改文字；影响冷启动搜索和片单搜索的对应提示 |

例如，将 `cards.home.actions` 改为下面的内容，会把讨论按钮放前面，并更改按钮名称；它们的点击功能保持原样：

```json
[
  {"id": "discuss", "label": "和阿映聊聊"},
  {"id": "watchlist", "label": "收藏到想看"}
]
```

把 `cards.home.fields` 改成 `["title", "rank"]`，票房卡就只显示片名和排名，按钮与海报保留。上述例子都是对应字段的值，不能用它们替换整个 JSON 文件。

占位符含义：`{title}` 片名，`{date}` 榜单日期，`{rank}` 排名，`{amount}` 已格式化票房，`{sessions}` 场次，`{audience}` 人次，`{notes}` 内容提示，`{originalTitle}` 原片名，`{genres}` 类型，`{year}` 年份，`{count}` 聊天记录数。每个位置只支持文件中原有的对应占位符。所有文案按纯文字显示，不执行 HTML。

编辑后可先执行 `npm --prefix frontend run check:content`。构建时也会自动检查 JSON、字段、按钮 ID 和占位符。配置错误时页面显示具体位置；修正后点击“修正配置后重新加载”即可恢复。

数据与结构分别位于：

- `web/ui-content.json`：上述可编辑文案、显示字段和按钮配置。
- `web/movie-components.js`：五类卡片的 DOM 结构、图片降级和原生按钮交互。
- `web/app.js`：获取真实数据、当前用户状态和 API／跳转动作。
- `web/styles.css`：颜色、字号、间距和响应式布局。

真实电影标题、海报、票房、“看过／想看”状态仍来自业务 API 和数据库；该 JSON 不覆盖用户记录。修改按钮的 `label` 只改显示名称，不会改变其 `id` 对应的业务动作。新增业务功能、全新卡片结构或调整颜色字号仍需修改对应的业务／组件／样式文件；管理后台、聊天正文、模型开场白和公共分享页未接入这份用户端卡片配置。

这份 JSON 会公开提供给浏览器，只放界面内容，不放 API 密钥或私人数据。

## 验证

```bash
cd yingban
uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python -m unittest discover -s tests -p 'test_*.py'

npm --prefix frontend run build
npm --prefix frontend run test:ui
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run verify:parity
```

## 配置边界

- 双击启动不需要 `.env`，并通过独立环境变量临时开启本机免认证。服务同时校验绑定地址、客户端地址、`Host` 和 `Origin` 都是回环地址；不要把这种模式放到反向代理、隧道、端口转发或公网部署后面。
- 手动或生产部署可从 `.env.example` 配置。不要提交真实密钥、邀请码、会话令牌或数据库文件。
- SQLite、模型、搜索、语音和图像能力均通过环境变量配置；未配置的附加能力应安全降级，不影响主文字回复。
- 聊天中的“看过／想看”判断使用独立轻量模型：后台“大模型配置”可以关闭“启用观影状态自动判断”并查看当前生效模型；Anthropic 默认 Claude Haiku 4.5，阶跃星辰默认 Step 3.5 Flash；自定义 OpenAI 兼容接口需填写“观影状态判断小模型”，或设置 `MOVIE_STATE_MODEL_ID`。关闭或判断无效时不会自动改电影状态，手动操作仍可使用。
- 生产部署需要独立评估数据库持久化、备份、锁、并发、监控和密钥管理；仓库中的默认配置不代表高可用生产方案。

## GitHub 自动部署

推送到 `main` 后，`.github/workflows/deploy.yml` 会运行完整产品测试、构建 Next.js、校验静态导出，再通过 SSH 发布。Pull Request 只运行验证。也可在 GitHub Actions 手动运行 `Test and deploy`，仅 `main` 可以部署。

当本地产品位于更大的工作区中时，在产品目录执行：

```bash
# 预览将同步的公开文件
python3 scripts/publish_github.py

# 提交并推送这些文件，触发自动部署
python3 scripts/publish_github.py --push -m "说明这次修改"
```

此命令需要已登录的 GitHub CLI（`gh auth login`）和 Git 作者配置。它创建临时仓库副本，只同步公开文件清单；不会把父工作区的提交历史、内部阶段文档、PRD、评测产物、真实环境文件或数据库推送到 GitHub。若 GitHub 在操作期间出现新提交，普通 push 会失败，应重新检查差异再运行。直接使用独立 GitHub 克隆目录开发时，也可以照常 `git commit`、`git push`。

仓库 Actions Secrets：`YINGBAN_DEPLOY_KEY`（专用部署私钥）、`YINGBAN_KNOWN_HOSTS`（预先核验的服务器主机公钥）。Actions Variables：`YINGBAN_HOST`、`YINGBAN_USER`、`YINGBAN_PORT`、`YINGBAN_HEALTH_URL`。真实值不写入源码。

服务器使用既有 `yingban.service`、`/etc/yingban/yingban.env` 和 `/var/lib/yingban/yingban.db`。`scripts/deploy_receive.py` 需由管理员安装为 root 所有、0755 的 `/usr/local/sbin/yingban-deploy`；专用公钥在 `authorized_keys` 中配置 `restrict,command="/usr/local/sbin/yingban-deploy"`。该密钥只接受 `deploy <commit SHA>` 和标准输入中的发布包，不提供交互 shell、端口转发或任意文件上传；应用代码仍以 `yingban` 用户运行。拥有部署权限等同于可以修改应用代码并访问应用可访问的数据，因此部署密钥只授予可信仓库维护者。

每次发布先校验文件清单、Python 语法和依赖一致性，再运行既有数据库备份命令、创建新 release、原子切换 `current`、重启并核对健康及模型模式。失败时切回上一代码版本；不会自动恢复旧数据库。涉及数据库迁移的修改必须自行确保旧版兼容，否则代码回退不足以撤销迁移。`requirements.txt` 改动会在切换前明确失败，需要先安排服务器依赖升级与回退方案。服务器接收脚本不会随普通应用发布包自动更新，修改该脚本须由管理员安装。

发布日志和结果位于仓库 Actions 页面；成功版本目录中的 `DEPLOYED_COMMIT` 记录实际部署的 Git 提交。保存本地文件本身不会触发发布，执行推送后才触发。

## 开源许可证

本项目采用 [MIT License](LICENSE) 开源。你可以使用、复制、修改和分发本项目代码，但必须保留版权与许可证声明；软件按“现状”提供，不附带任何担保。
