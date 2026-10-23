# Telegram AI Bot 产品需求文档（PRD）v0.1

> 状态：**v1 完成（M1–M4）**，M5 为可选项 ｜ 日期：2026-10-23
> 决议见文档末尾第 10 节。

---

## 1. 背景与目标

做一个自托管的 Telegram 机器人，通过 **Base URL + API Key** 接入任意 OpenAI 兼容的大模型服务（OpenAI、DeepSeek、OpenRouter、One-API/New-API 中转、Ollama 等），提供：

1. 私聊与群聊中的多轮 AI 对话
2. 基于**白名单用户 / 白名单群组**的访问控制
3. 管理员在 Telegram 内直接**切换模型**、**管理配置**，无需改配置文件或重启

### 非目标（v1 不做）
- 图片生成、语音转写/TTS、联网搜索、插件/Function Calling、MCP
- Web 管理后台、计费/额度系统、多 Bot 实例
- 非 OpenAI 协议的原生接入（Anthropic/Gemini 原生 API 可通过中转网关解决，后续再考虑）

---

## 2. 开源方案调研

| 项目 | 技术栈 | 值得借鉴 | 不足 |
|---|---|---|---|
| [yym68686/ChatGPT-Telegram-Bot (TeleChat)](https://github.com/yym68686/chatgpt-telegram-bot) | Python + python-telegram-bot | `whitelist` / `BLACK_LIST` / `ADMIN_LIST` / `GROUP_LIST` 四类名单；**群组在白名单内则群成员全员可用**；`/model` 快速切换；`/info` 菜单式配置；`RESET_TIME` 闲置自动清空上下文；论坛话题（Topic）独立上下文 | 只能配一个 Provider；配置以文件存储；GPL-3.0；若未设 ADMIN_LIST 则任何人可改配置（不安全的默认值） |
| [tbxark/ChatGPT-Telegram-Workers](https://github.com/tbxark/chatgpt-telegram-workers) | TypeScript / Cloudflare Workers + KV | **多 Provider 模型**：每个 Provider 含 名称/Base URL/Key/协议/可用模型列表，可一键「拉取模型列表」；管理员 `/setenv` 动态修改全局配置；Telegram Mini App 管理面板；Webhook Secret 防伪造 | 偏 Serverless，自托管调试较麻烦；配置为全局 JSON，粒度粗 |
| [n3d1117/chatgpt-telegram-bot](https://github.com/n3d1117/chatgpt-telegram-bot) | Python + python-telegram-bot | 流式输出、`/reset`、`/stats` 用量统计、按用户预算；长对话**自动摘要**压缩 token | 只有用户白名单，**没有群组白名单**（社区 issue #276 提过需求）；仅官方 OpenAI |
| [father-bot/chatgpt_telegram_bot](https://github.com/father-bot/chatgpt_telegram_bot) | Python + MongoDB | `openai_api_base` 自定义地址；`allowed_telegram_usernames` 白名单（空 = 全开放）；多「聊天模式」预设 | 依赖 MongoDB，较重 |
| [Vertdasy/Telegram-Ai-Bot](https://github.com/Vertdasy/Telegram-Ai-Bot) | Python + aiogram 3 + SQLite | `AI_BASE_URL` 兼容任意 OpenAI 协议；管理面板（用户列表、封禁、广播、切模型、**备用模型 fallback**、限流） | 功能面偏 C 端运营 |
| [gaoyifan/chatgpt-telegram-bot](https://github.com/gaoyifan/chatgpt-telegram-bot) | Python | 管理员在群里执行 `/add_whitelist` 即把该群加入白名单，操作非常顺手 | 功能较少 |

### 结论 —— 本项目取各家所长
- **权限模型**取 TeleChat：用户白名单 + 群组白名单 + 管理员，群白名单内成员免单独授权；但**默认安全**（未配置管理员则无人可改配置）。
- **Provider 模型**取 tbxark：多 Provider，每个独立 URL + Key，支持拉取 `/v1/models`。
- **交互**取 gaoyifan：在群里直接 `/allow_group` 授权当前群。
- **存储**取 Vertdasy：SQLite，零运维；配置运行时可改、重启不丢。
- **流式**：使用 Telegram Bot API 9.5 起对所有 Bot 开放的 `sendMessageDraft` 原生草稿流（私聊），群聊回退到节流的 `editMessageText`。

---

## 3. 角色与权限

| 角色 | 来源 | 能力 |
|---|---|---|
| **超级管理员 (Owner)** | 环境变量 `ADMIN_IDS`，不可在 Bot 内删除 | 全部功能，含增删管理员 |
| **管理员 (Admin)** | Owner 在 Bot 内添加（存 DB） | 切换模型、管理 Provider、管理白名单、修改参数 |
| **白名单用户** | 管理员添加 | 私聊对话；在任意已授权群中对话 |
| **白名单群组成员** | 群在白名单内即可 | 在该群中对话（私聊仍需个人白名单） |
| **其他人** | — | 无法使用 |

### 鉴权规则

```
收到消息 →
  发送者是 Admin/Owner？                → 放行
  私聊： 发送者在用户白名单？            → 放行，否则拒绝
  群聊： 群在群组白名单？                → 放行（群内所有成员）
         否则 发送者在用户白名单？      → 取决于配置项 allow_whitelisted_user_in_any_group（默认 false）
         否则                           → 静默忽略
```

- **私聊被拒**：回复一条提示，并附上对方的 `user_id`，方便转发给管理员加白。（同一用户 N 分钟内只提示一次，防刷）
- **未授权群**：静默忽略；可选配置「被拉入未授权群时自动退群」（默认关闭）。
- **黑名单**：v1 不单独做，移出白名单即可（白名单模式下二者等价）。
- **开放模式**：配置项 `access_mode = whitelist | open`，默认 `whitelist`。

---

## 4. 功能需求

### 4.1 对话

| 编号 | 需求 | 优先级 |
|---|---|---|
| C1 | 私聊中直接发文字即对话，支持多轮上下文 | P0 |
| C2 | 群聊触发方式：**@Bot**、**回复 Bot 的消息**、`/ask 问题`；其余消息不响应 | P0 |
| C3 | 流式输出：私聊用 `sendMessageDraft`，群聊用 `editMessageText`（≥1.2s 节流）；任一失败自动降级为一次性发送 | P0 |
| C4 | Markdown → Telegram HTML 渲染（代码块、加粗、链接等），解析失败回退纯文本 | P0 |
| C5 | 超过 4096 字符自动分段发送，不截断代码块 | P0 |
| C6 | `/new`（别名 `/reset`）清空当前会话上下文 | P0 |
| C7 | 上下文长度控制：保留最近 N 轮（默认 20 条消息），并按 token 预算从旧到新裁剪 | P0 |
| C8 | 会话闲置超时自动开启新会话（默认 60 分钟，可配，0 = 不过期） | P1 |
| C9 | 生成中可点「⏹ 停止」按钮中断；同一会话同一时间只处理一个请求，新消息排队或提示稍候 | P1 |
| C10 | 错误友好提示：超时、401 Key 无效、429 限流、模型不存在等，给出可读原因（管理员可见原始错误） | P0 |
| C11 | 图片输入（Vision）：发图 + 文字提问，当前模型支持时转发 | P2 |
| C12 | 论坛群（Topic）每个话题独立上下文 | P2 |

**会话隔离粒度（默认值，见待确认问题 Q2）**
- 私聊：每个用户一个会话
- 群聊：每个「群 × 用户」一个会话（同群不同人互不干扰）

### 4.2 模型与 Provider 管理（管理员）

一个 **Provider** = `名称 + Base URL + API Key + 可用模型列表`。可配置多个，例如「官方 OpenAI」「DeepSeek」「中转站」。

| 编号 | 需求 | 优先级 |
|---|---|---|
| M1 | 添加 Provider：依次输入名称 → Base URL → API Key（输入 Key 的消息发送后**立即被 Bot 删除**） | P0 |
| M2 | 测试连通性：调用 `GET {base_url}/models`，失败则用一个极短的 chat 请求兜底 | P0 |
| M3 | 拉取模型列表：从 `/models` 获取并让管理员勾选启用；也可手动添加模型名（部分中转站不提供 `/models`） | P0 |
| M4 | 切换默认模型：`/model` 弹出按钮列表（按 Provider 分组），点击即切换，全局生效 | P0 |
| M5 | 编辑 / 删除 Provider；删除正在使用的 Provider 时要求先切换 | P0 |
| M6 | Base URL 容错：自动兼容 `https://x.com`、`https://x.com/v1`、`https://x.com/v1/chat/completions` 三种写法 | P0 |
| M7 | 首次启动可通过环境变量 `DEFAULT_BASE_URL` / `DEFAULT_API_KEY` / `DEFAULT_MODEL` 自动创建一个 Provider，免去初始化步骤 | P0 |
| M8 | 为特定群/用户单独指定模型（覆盖全局默认） | P2 |
| M9 | 备用模型：主模型失败时自动切换到 fallback 模型重试一次 | P2 |

### 4.3 访问控制管理（管理员）

| 编号 | 需求 | 优先级 |
|---|---|---|
| A1 | `/allow_user <user_id>`、`/deny_user <user_id>`；也可**回复某人的消息**执行 `/allow_user` 直接加白 | P0 |
| A2 | 在群中执行 `/allow_group` 授权当前群；`/deny_group` 取消；私聊中也可 `/allow_group <chat_id>` | P0 |
| A3 | `/whitelist` 分页查看白名单用户与群组（显示名称 + ID），带按钮可直接移除 | P0 |
| A4 | Owner 专属：`/add_admin`、`/remove_admin` | P1 |

### 4.4 参数配置（管理员）

通过 `/settings` 打开按钮菜单修改，所有配置存 DB，**即时生效、重启不丢**。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `system_prompt` | "You are a helpful assistant." | 全局系统提示词 |
| `temperature` | 0.7 | |
| `max_tokens` | 不限制 | 单次回复上限 |
| `history_limit` | 20 | 保留的历史消息条数 |
| `session_timeout_min` | 60 | 闲置多久自动新会话 |
| `stream` | 开 | 流式输出开关（部分中转站流式不稳定时可关） |
| `group_trigger` | mention+reply+command | 群聊触发方式 |
| `access_mode` | whitelist | whitelist / open |
| `request_timeout_s` | 120 | 上游 API 超时 |

### 4.5 统计与运维

| 编号 | 需求 | 优先级 |
|---|---|---|
| S1 | `/status`（管理员）：当前模型、Provider、版本、运行时长、今日请求数 | P1 |
| S2 | 记录每次请求的用户、群、模型、token 用量（若上游返回 usage），`/stats` 查看按用户/模型汇总 | P2 |
| S3 | 结构化日志输出到 stdout + 文件，日志中 API Key 一律脱敏 | P0 |

---

## 5. 命令清单

| 命令 | 权限 | 场景 | 说明 |
|---|---|---|---|
| `/start` | 所有人 | 私聊 | 欢迎语；未授权者显示自己的 user_id |
| `/help` | 已授权 | 私聊/群 | 使用说明 |
| `/new` `/reset` | 已授权 | 私聊/群 | 清空当前会话 |
| `/ask <问题>` | 已授权 | 群 | 群内提问 |
| `/id` | 所有人 | 私聊/群 | 显示当前 user_id / chat_id |
| `/model` | 管理员 | 私聊/群 | 查看并切换模型 |
| `/provider` | 管理员 | **仅私聊** | 管理 Provider（涉及 Key，禁止在群内操作） |
| `/settings` | 管理员 | **仅私聊** | 参数配置菜单 |
| `/allow_user` `/deny_user` | 管理员 | 私聊/群 | 用户白名单 |
| `/allow_group` `/deny_group` | 管理员 | 私聊/群 | 群组白名单 |
| `/whitelist` | 管理员 | 私聊 | 查看白名单 |
| `/status` | 管理员 | 私聊 | 运行状态 |
| `/add_admin` `/remove_admin` | Owner | 私聊 | 管理员管理 |

启动时调用 `setMyCommands`，按 scope 分别为普通用户与管理员注册命令菜单（管理员看到更多命令）。

---

## 6. 关键交互示例

**切换模型**
```
管理员: /model
Bot:    当前模型：deepseek-chat（DeepSeek）
        [DeepSeek]  ✅deepseek-chat   deepseek-reasoner
        [中转站]    gpt-4o   claude-sonnet-4   gemini-2.5-pro
管理员: 点击「gpt-4o」
Bot:    ✅ 已切换到 gpt-4o（中转站）
```

**添加 Provider**
```
管理员: /provider → [➕ 添加]
Bot:    请输入名称            → 管理员: 中转站
Bot:    请输入 Base URL       → 管理员: https://api.example.com/v1
Bot:    请输入 API Key        → 管理员: sk-xxxx   （Bot 立即删除该消息）
Bot:    ✅ 连接成功，发现 87 个模型，请选择要启用的：[分页勾选按钮] [完成]
```

**未授权用户**
```
陌生人: 你好
Bot:    你还没有使用权限。请把你的 ID 发给管理员：123456789
```

---

## 7. 技术方案

| 维度 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.10+ | 生态成熟，调研项目大多为 Python |
| TG 框架 | **aiogram 3** | 全异步、FSM 适合多步配置流程、Router 结构清晰、更新跟进快 |
| AI SDK | 官方 `openai` SDK（`AsyncOpenAI(base_url, api_key)`） | 天然兼容所有 OpenAI 协议服务，流式/错误类型齐全 |
| 存储 | SQLite + SQLAlchemy 2（async）/ aiosqlite | 零运维，单文件便于备份 |
| 敏感信息 | API Key 用 Fernet 加密入库，密钥来自环境变量 `SECRET_KEY` | DB 文件泄露不等于 Key 泄露 |
| 运行模式 | 默认 Long Polling；可选 Webhook（带 `secret_token` 校验） | Polling 无需公网域名，Webhook 便于云部署 |
| 部署 | Dockerfile + docker-compose，数据目录挂载卷 | |
| 代理 | 支持 `HTTPS_PROXY`，分别用于 Telegram 与 AI 请求 | 国内网络环境需要 |

### 7.1 目录结构（草案）
```
tgbot/
├── bot/
│   ├── main.py            # 入口：加载配置、初始化 DB、启动 polling/webhook
│   ├── config.py          # 环境变量（pydantic-settings）
│   ├── db/                # models.py / repo.py
│   ├── middlewares/       # auth.py（鉴权）、throttle.py
│   ├── handlers/          # chat.py / admin_model.py / admin_provider.py / admin_acl.py / settings.py
│   ├── services/          # llm.py（调用+流式）、session.py（上下文）、render.py（MD→HTML、分段）
│   └── keyboards/
├── tests/
├── data/                  # SQLite 文件（挂载卷）
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

### 7.2 数据模型
```
providers     (id, name, base_url, api_key_enc, enabled, created_at)
models        (id, provider_id, name, enabled)
settings      (key, value_json)                     -- 全局配置 KV，含 current_model_id
admins        (user_id, added_by, created_at)       -- Owner 来自环境变量，不入此表
allowed_users (user_id, display_name, added_by, created_at)
allowed_chats (chat_id, title, added_by, created_at)
sessions      (id, scope_key, model_id, last_active_at)   -- scope_key 如 "p:123" / "g:-100xx:123"
messages      (id, session_id, role, content, tokens, created_at)
usage_log     (id, user_id, chat_id, model, prompt_tokens, completion_tokens, created_at)  -- P2
```

### 7.3 环境变量
```
BOT_TOKEN=          # 必填
ADMIN_IDS=          # 必填，逗号分隔，Owner 列表
SECRET_KEY=         # 必填，用于加密 API Key（首次可用脚本生成）
DEFAULT_BASE_URL=   # 可选，首次启动自动创建 Provider
DEFAULT_API_KEY=
DEFAULT_MODEL=
ALLOWED_USER_IDS=   # 可选，首次启动导入白名单
ALLOWED_CHAT_IDS=
PROXY_URL=          # 可选
WEBHOOK_URL=        # 可选，不填则 polling
DATA_DIR=./data
LOG_LEVEL=INFO
```
环境变量仅作**首次初始化**用；之后以 DB 中的配置为准，管理员在 Bot 内修改。

### 7.4 部署前置（需在 @BotFather 操作）
1. 创建 Bot 获取 Token
2. 群聊要让 Bot 能看到 @提及：`/setprivacy` → Disable，或把 Bot 设为群管理员（隐私模式开启时 Bot 只能收到命令和回复它的消息）
3. 改隐私设置后需将 Bot 移出群再重新拉入才生效

---

## 8. 安全要点
- 鉴权中间件在所有 handler 之前执行，**默认拒绝**。
- 涉及 Key 的操作只允许在私聊进行；展示时脱敏为 `sk-ab****yz`。
- 管理员发送的 Key 消息由 Bot 主动删除，避免留在聊天记录。
- Webhook 模式校验 `X-Telegram-Bot-Api-Secret-Token`。
- 日志与错误回显中过滤 Key、Token。
- 回调按钮（callback_data）同样做权限校验，防止非管理员伪造点击。

---

## 9. 里程碑

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **M1 基础对话** | 项目骨架、配置、DB、单 Provider（环境变量）、私聊多轮对话、流式、MD 渲染、分段、`/new` | 私聊可流畅多轮对话，长回复/代码块显示正常 |
| **M2 权限** | 鉴权中间件、用户/群白名单、群聊触发、加白命令、`/id` | 白名单外用户/群无法使用；群里 @ 和回复可触发 |
| **M3 管理配置** | 多 Provider 管理、拉取模型、`/model` 切换、`/settings`、Key 加密 | 管理员全程在 Telegram 内完成配置，重启后配置保留 |
| **M4 打磨与部署** | 停止按钮、闲置超时、错误提示、`/status`、Docker、README | `docker compose up -d` 一键运行 |
| M5（可选） | Vision、Topic 隔离、单独指定模型、fallback、用量统计 | 按需 |

---

## 10. 决议（2026-10-23 已确认：全部采纳建议）

> 补充：本地开发环境为 Python 3.10，代码兼容 3.10+，Docker 镜像使用 3.12。
> M1 阶段尚无白名单，为防止 Key 被盗刷，临时仅允许 `ADMIN_IDS` 中的用户使用；M2 已接入完整权限。
> M2 实现补充：A4（`/add_admin`、`/remove_admin`）提前在 M2 完成；管理员把 Bot 拉进未授权群时，Bot 发一条提示引导执行 `/allow_group`（不自动授权）；群聊中 @Bot 并回复他人消息（或回复 Bot 较早的消息）时，被回复内容作为引用发给 AI；普通群升级为超级群（chat_id 变化）时自动迁移授权。
> M3 实现补充：模型勾选列表支持关键词搜索；`/model <模型名>` 可直接切换；`/settings` 额外开放「上下文 token 预算」与 M2 新增的三个访问相关开关；群聊触发方式可分别开关 @Bot / 回复 / `/ask`；正在使用的模型不能取消勾选，它所属的接入配置不能删除；新增 `/cancel` 取消多步输入。
> M4 实现补充：部署方式改为——推送 `vX.Y.Z` tag 触发 GitHub Actions，先跑测试，再构建 amd64/arm64 镜像推送到 GHCR，服务器用 `docker-compose.yml` 拉取运行；依赖锁定在 `requirements.lock`。私聊停止使用 Telegram 原生草稿停止按钮（`can_stop` + `stopped_message_generation` 更新），群聊为「⏹ 停止」按钮，仅提问者和管理员可点；停止后已生成内容计入上下文。`/status` 的请求统计基于新增的 `request_log` 表，按服务器时区计算「今日」。可选项（Webhook、Windows 启动脚本、关闭时收尾）未做。

| # | 问题 | 决议 |
|---|---|---|
| Q1 | 技术栈用 **Python + aiogram 3** 可以吗？还是更倾向 Node.js/TypeScript 或 Go？ | Python |
| Q2 | 群聊上下文：**每人独立**（建议）还是**全群共享**一个上下文？ | 每人独立，后续可加配置切换 |
| Q3 | 普通用户能否自己用 `/model` 切换模型（在管理员启用的列表里选），还是只有管理员能切、全局生效？ | v1 仅管理员切换、全局生效 |
| Q4 | 是否需要支持多个 Provider？还是只要一组 URL + Key 就够？ | 支持多个，成本不高但很实用 |
| Q5 | 管理员配置方式：Telegram 内**命令 + 按钮菜单**（建议）还是需要 **Web 管理后台**？ | 命令 + 按钮，v1 不做 Web |
| Q6 | 部署环境：自己的 VPS / 国内服务器（需代理）/ 云平台？是否有公网域名用 Webhook？ | Docker + Polling |
| Q7 | v1 是否需要图片识别、用量统计等 P2 功能？ | 不需要，M5 再做 |
