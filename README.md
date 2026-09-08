# 影伴

邀请制个人 AI 电影伙伴。本仓库只公开项目代码和使用说明；内部 PRD、阶段交接、设计方案及评测报告不进入 Git。

## 核心能力

- 邀请码稳定绑定账户；管理员可生成、备注、停用、换发和逐条恢复新格式邀请码。
- 新用户以 1～5 部真实看过的电影建立口味基线；推荐在数据层硬排除已看电影。
- 支持聊电影、结构化推荐、片单、观后感、续聊摘要、电影对话记录、月度回顾和可撤回分享；已绑定电影的成功对话原文保存在服务端 `conversation_records` 表中。
- 三个内置 Skill 分别服务内容创作、多次观影认知和院线新片决策；草稿不会自动发布。
- 支持 TMDB、公开 Web Search、语音和图像能力；附加能力失败不影响主文字回复。
- 管理后台统一管理模型、提示词、Skill、联网、语音、图像、邀请码、用量和健康指标。

不可破坏的边界：AI 不冒充真人，不建立隐蔽人格档案；敏感现实信息不自动进入长期电影记忆；邀请码、会话、管理员口令和第三方密钥不得写入文档、日志或前端；当前不做付费、广告、公共影评广场或默认公开用户内容。

## 架构入口

| 路径 | 职责 |
| --- | --- |
| `app.py` | CLI、依赖组装和 Uvicorn 启动 |
| `fastapi_app.py`、`server.py` | FastAPI 桥接、认证、用户与管理员 API |
| `agent.py` | Agent 提示词、工具循环、Skill 注入与安全短路 |
| `storage.py` | SQLite schema、迁移、事务、账户隔离与业务数据 |
| `integrations.py`、`voice.py`、`image_models.py` | 电影、搜索、语音和图像供应商 |
| `product_skills.py` | 三个内置 Skill 的定义与默认版本 |
| `local_bootstrap.py` | 本机首次启动的安全凭据与首个邀请码初始化 |
| `web/` | 用户端与管理端静态资源及交互 |
| `frontend/` | Next.js App Router 静态导出外壳 |
| `scripts/sqlite_maintenance.py` | SQLite 检查、备份和迁移 |

## 本地启动

要求 Python 3.12、Node.js 24 LTS、npm 和 `uv`。

macOS 可在 Finder 中直接双击 `启动影伴.command`。启动器会自动定位项目、在需要时构建前端、启动本地服务，并在健康检查通过后打开默认浏览器。首次启动会自动创建权限为 `0600` 的本机 `.env`、生成随机安全密钥、管理员口令和首个用户邀请码；邀请码会显示在启动窗口并复制到剪贴板，页面打开后按 `Command-V` 粘贴即可，不需要事先知道或自行设置密码。管理员口令只在首次创建时显示，同时保存在本机 `.env` 的 `YINGBAN_ADMIN_TOKEN` 中。

首次启动或 `requirements.txt` 更新后，`uv` 会联网补齐本机缓存里缺少的 Python 依赖；已缓存的依赖不会重复下载。初始化不会覆盖已有 `.env`、邀请码或数据库；如果检测到已有数据库但安全凭据缺失，会停止并提示手动处理，避免破坏已有登录。终端窗口保持开启时，影伴服务持续运行；按 `Control-C` 可停止。

```bash
git clone https://github.com/Jaimo-so/yingban.git
cd yingban
npm --prefix frontend ci
npm --prefix frontend run build

uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python local_bootstrap.py

uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python app.py serve
```

本地用户端为 <http://127.0.0.1:8765/>，管理端为 <http://127.0.0.1:8765/admin>。不要使用 `file://` 直接打开 `web/index.html`。

生成一个本地邀请码：

```bash
uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python app.py generate-invites 1
```

## 验证

```bash
cd yingban
uv run --isolated --python 3.12 \
  --with-requirements requirements.txt \
  python -m unittest discover -s tests -p 'test_*.py'

npm --prefix frontend run build
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run verify:parity
```

## 配置边界

- 双击启动会安全创建本机 `.env`；手动或生产部署可从 `.env.example` 配置。不要提交真实密钥、邀请码、会话令牌或数据库文件。
- SQLite、模型、搜索、语音和图像能力均通过环境变量配置；未配置的附加能力应安全降级，不影响主文字回复。
- 生产部署需要独立评估数据库持久化、备份、锁、并发、监控和密钥管理；仓库中的默认配置不代表高可用生产方案。

## 开源许可证

本项目采用 [MIT License](LICENSE) 开源。你可以使用、复制、修改和分发本项目代码，但必须保留版权与许可证声明；软件按“现状”提供，不附带任何担保。
