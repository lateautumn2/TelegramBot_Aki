# Telegram AI Bot

自托管的 Telegram AI 机器人：通过 Base URL + API Key 接入任意 OpenAI 兼容服务（OpenAI、DeepSeek、OpenRouter、One-API / New-API 中转、Ollama 等），支持私聊和群聊对话、用户 / 群组白名单，管理员在 Telegram 里就能切换模型、管理 Key 和参数。

产品设计见 [docs/PRD.md](docs/PRD.md)。

## 功能

- 多轮对话，流式输出（私聊使用 Telegram 原生草稿流），Markdown 渲染，长回复自动分段
- 生成中可随时点「停止」，已生成的部分保留并计入上下文
- 白名单用户、白名单群组（群内所有成员可用）、管理员 / 超级管理员
- 群里通过 @Bot、回复 Bot 或 `/ask` 提问，每人上下文独立；可引用他人消息提问
- 多组接入配置（URL + Key），自动拉取模型列表，`/model` 一键切换
- `/settings` 调整系统提示词、temperature、上下文长度等，即时生效
- API Key 加密存储，日志自动隐藏 Key 和 Token

---

## 1. 准备 Telegram Bot

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather)，发送 `/newbot` 按提示创建，得到 **Bot Token**。
2. **要在群里用**：对 @BotFather 发送 `/setprivacy` → 选择你的 Bot → **Disable**。否则 Bot 看不到群里 @ 它的消息（或者把 Bot 设为群管理员）。改完要把 Bot 移出群再重新拉入才生效。
3. 你的 user_id：先把 `ADMIN_IDS` 留空启动一次，私聊 Bot 发送 `/id` 即可看到，填好后重启。

## 2. 配置 `.env`

复制 `.env.example` 为 `.env` 并填写：

| 配置 | 必填 | 说明 |
|---|---|---|
| `BOT_TOKEN` | ✅ | @BotFather 给的 Token |
| `ADMIN_IDS` | ✅ | 超级管理员 user_id，逗号分隔 |
| `SECRET_KEY` | ✅ | 加密 API Key 用的随机字符串（≥16 位）。**设置后不要修改**，否则已保存的 Key 无法解密 |
| `DEFAULT_BASE_URL` / `DEFAULT_API_KEY` / `DEFAULT_MODEL` | | 首次启动时自动创建一组接入配置；之后在 Bot 内用 `/provider` 管理 |
| `ALLOWED_USER_IDS` / `ALLOWED_CHAT_IDS` | | 首次启动时导入的白名单；之后用 `/allow_user`、`/allow_group` 管理 |
| `PROXY_URL` | | 代理，同时用于 Telegram 和 AI 请求，支持 `http://`、`socks5://` |
| `DATA_DIR` | | 本地运行时的数据目录，默认 `./data`（Docker 下固定为 `/data`） |
| `LOG_LEVEL` | | 默认 `INFO` |
| `TGBOT_IMAGE` | Docker | 镜像地址，例如 `ghcr.io/your-name/tgbot:latest` |

生成 `SECRET_KEY`：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## 3. 运行方式 A：本地 / Windows

需要 Python 3.10+。

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[dev]"
```

```bash
.venv/Scripts/python -m bot
```

（Linux / macOS 把 `.venv/Scripts/python` 换成 `.venv/bin/python`。）看到 `Starting @你的bot名` 即启动成功，按 Ctrl+C 停止。必须在项目目录下运行，程序从当前目录读取 `.env`。

## 4. 运行方式 B：Docker（GitHub Actions 构建，GHCR 托管）

### 4.1 发布镜像

1. 把项目推送到 GitHub（`.env`、`data/` 已在 `.gitignore` 中，不会被提交）。
2. 打一个**版本 tag** 并推送，就会触发 [.github/workflows/release.yml](.github/workflows/release.yml)：

   ```bash
   git tag v1.0.0
   ```

   ```bash
   git push origin v1.0.0
   ```

3. 流程：先跑全部测试，测试通过后构建 `linux/amd64` + `linux/arm64` 镜像，推送到 `ghcr.io/<你的用户名>/<仓库名>`，打上 `1.0.0`、`1.0`、`latest` 三个标签。
   - tag 必须是 `vX.Y.Z` 格式；`v1.1.0-rc1` 这样的预发布版本不会更新 `latest`。
   - 在仓库的 **Actions** 页面查看进度；镜像出现在仓库主页右侧的 **Packages**。
4. GHCR 包默认**私有**。二选一：
   - 在包页面 **Package settings → Change visibility** 设为 Public，服务器可以直接拉取；
   - 保持私有，在服务器上用一个有 `read:packages` 权限的 Personal Access Token 登录：

     ```bash
     docker login ghcr.io -u 你的GitHub用户名
     ```

### 4.2 在服务器上部署

准备一个目录，放入仓库里的 [docker-compose.yml](docker-compose.yml) 和你的 `.env`（记得设置 `TGBOT_IMAGE`，镜像名必须是**小写**），然后：

```bash
docker compose pull
```

```bash
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
```

- 数据库在该目录下的 `./data/bot.db`，容器重建不会丢失。
- 容器以非 root 用户运行，启动时会自动修正 `./data` 的权限。
- 容器异常退出会自动重启；日志单个文件最大 10 MB，保留 3 个。
- 代理在宿主机上时，`PROXY_URL` 写 `http://host.docker.internal:端口`，不要写 `127.0.0.1`。

### 4.3 升级

发布新 tag 后，在服务器上：

```bash
docker compose pull && docker compose up -d
```

想固定版本，把 `TGBOT_IMAGE` 写成具体版本号，例如 `ghcr.io/your-name/tgbot:1.0.0`。数据库会在启动时自动补齐新版本需要的表，无需手动迁移。

---

## 5. 使用

### 权限

| 角色 | 来源 | 能做什么 |
|---|---|---|
| 超级管理员 | `.env` 的 `ADMIN_IDS` | 全部功能，包括增删管理员 |
| 管理员 | 超级管理员用 `/add_admin` 添加 | 切换模型、管理接入配置和参数、管理白名单；在任何私聊、任何群里都能对话 |
| 白名单用户 | `/allow_user` | 私聊对话 |
| 白名单群组 | `/allow_group` | 群里**所有成员**都能对话 |

不在白名单的人私聊 Bot 会收到自己的 ID（方便转给管理员）；未授权的群里 Bot 完全不响应。

### 在群里使用

1. 按第 1 节关闭隐私模式，把 Bot 拉进群，管理员在群里发送 `/allow_group`。
2. 群成员通过以下方式提问（可在 `/settings` → 群聊触发方式里分别开关）：
   - `@你的Bot 问题`
   - 回复 Bot 的消息继续追问
   - `/ask 问题`
3. 每个人的上下文互相独立。@Bot 时如果同时回复了别人的消息，被回复的内容会作为引用一起发给 AI，例如回复一条英文并 `@Bot 翻译一下`。
4. 回复生成中，消息下方有「⏹ 停止」按钮，只有提问的人和管理员能点。

### 管理模型与配置

以下命令仅管理员可用；涉及 Key 和参数的只能在私聊里操作。

- **`/model`**：列出所有已启用的模型，点一下即切换，对所有人生效。也可以直接 `/model deepseek-chat`。
- **`/provider`**：管理接入配置（一组 名称 + Base URL + API Key）。
  - **添加**：按提示依次发送名称、URL、Key，你发出的 Key 消息会被立刻删除。随后自动测试连接、拉取模型列表，点选要启用的模型（支持搜索）。
  - 服务商不提供模型列表时，点「手动添加」输入模型名，Bot 会发一个极短的请求验证。
  - 详情页可以改名称 / URL / Key、测试连接、管理模型、删除。正在使用的模型不能取消勾选，它所属的接入配置也不能删除，需先用 `/model` 切走。
- **`/settings`**：系统提示词、temperature、回复上限、历史条数、上下文预算、闲置超时、请求超时、流式开关、群聊触发方式、访问模式（白名单 / 对所有人开放）等，每项都能一键恢复默认。
- **`/status`**：版本、运行时长、当前模型、今日请求数（成功 / 停止 / 失败，按服务器时区计算）、白名单和管理员数量、数据库大小、最近一次失败原因。
- **`/cancel`**：输入到一半想放弃时使用。

### 命令一览

| 命令 | 谁可以用 | 说明 |
|---|---|---|
| `/new`（`/reset`） | 已授权 | 开启新对话 |
| `/ask <问题>` | 已授权 | 群里提问 |
| `/id` | 所有人 | 查看 user_id / chat_id |
| `/help` | 已授权 | 帮助（按角色显示） |
| `/model [模型名]` | 管理员 | 切换模型 |
| `/provider` | 管理员（仅私聊） | 管理接入配置与启用的模型 |
| `/settings` | 管理员（仅私聊） | 参数配置 |
| `/status` | 管理员（仅私聊） | 运行状态 |
| `/cancel` | 管理员 | 取消正在进行的输入 |
| `/allow_user <id>` | 管理员 | 添加白名单用户；也可以**回复某人的消息**发送 |
| `/deny_user <id>` | 管理员 | 移除白名单用户，同样支持回复 |
| `/allow_group [chat_id]` | 管理员 | 在群里发送则授权当前群；私聊时带上群 ID |
| `/deny_group [chat_id]` | 管理员 | 取消授权群组 |
| `/whitelist` | 管理员（仅私聊） | 查看白名单，点按钮直接移除 |
| `/add_admin <id>` | 超级管理员 | 添加管理员，同样支持回复 |
| `/remove_admin <id>` | 超级管理员 | 移除管理员；不带参数则列出所有管理员 |

---

## 6. 备份与迁移

所有数据都在 `DATA_DIR/bot.db` 一个文件里（接入配置、白名单、参数、对话记录）。

- **备份**：停止 Bot 后复制 `bot.db`，**同时保存好 `.env` 里的 `SECRET_KEY`**。没有原来的 `SECRET_KEY`，数据库里的 API Key 无法解密。
- **迁移到新服务器**：把 `bot.db` 放到新目录的 `data/` 下，`.env` 使用同一个 `SECRET_KEY`，启动即可。

## 7. 常见问题

**启动报 `Telegram rejected BOT_TOKEN`**
Token 填错了，或者该 Bot 已被 @BotFather 重置过 Token。

**连不上 Telegram / AI 服务（timeout、connection 错误）**
检查网络和 `PROXY_URL`。Docker 部署时代理地址不能写 `127.0.0.1`，见 4.2。

**群里 @ Bot 没反应**
1. 是否已关闭隐私模式，并在修改后把 Bot 移出群重新拉入；
2. 群是否已 `/allow_group`（群里发 `/id` 可以看到 chat_id）；
3. `/settings` 里群聊触发方式是否关掉了 @Bot。
未授权的群里 Bot 不会有任何回复，这是预期行为。

**接入配置显示「无法解密」**
`SECRET_KEY` 和保存 Key 时用的不一样。改回原来的值；如果找不回，只能在 `/provider` 里重新填写各个 Key。

**换了 `DEFAULT_*` 不生效**
`DEFAULT_*` 只在首次启动时使用，之后请用 `/provider` 修改。

**GitHub Actions 推送镜像失败（403 / denied）**
工作流已自行声明了 `packages: write` 权限，常见原因有两个：
1. 同名的镜像包之前被手动创建过：在包页面 **Package settings → Manage Actions access** 里给这个仓库加上 Write 权限；
2. 仓库属于某个组织，组织策略限制了 Actions 的权限：需要组织管理员放开。

---

## 8. 开发

```bash
.venv/Scripts/python -m pytest
```

- 运行时依赖锁定在 `requirements.lock`，Docker 镜像和 CI 都用它安装，保证和测试时的版本一致。
- 升级依赖时：修改 `pyproject.toml`，在一个干净的虚拟环境里 `pip install .`，再用 `pip freeze --exclude tgbot > requirements.lock` 重新生成，跑一遍测试后提交。
