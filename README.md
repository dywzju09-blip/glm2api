# GLM Studio

多 GLM 账号聚合网关 —— 把多个**智谱清言网页账号**和**智谱开放平台 API Key** 收进一个账号池，
统一调度轮询，对外只暴露一个 OpenAI 兼容接口和自己发放的 API Key。
管理界面参考了 [grok2api](https://github.com/chenyme/grok2api) 的交互风格（主页账号卡片 + 5h/7d 滚动窗口用量条 + 前端直接增删改）。

## 功能

- **双通道账号池**
  - `web`：chatglm.cn 网页账号（refresh_token），走网页逆向协议，支持 glm-5.2 / glm-4.7 / glm-4.6 等全套模型及 `-think` / `-search` 变体，自动刷新 token 并写回、device_id 自动轮换防风控
  - `guest`：游客模式（免登录，额度最低，适合试跑）
  - `official`：智谱开放平台 API Key 直连（[open.bigmodel.cn](https://open.bigmodel.cn)），OpenAI 兼容透传
- **调度**：同优先级轮询 / 优先级顺序，请求失败自动切换下一账号；认证失败冷却、连续失败自动禁用；每账号可设 5h / 7d Token 限额与每日请求限额
- **用量看板**：主页每个账号一张卡片，展示 5h / 7d 滚动窗口用量进度条、今日请求 / Token、7 天成功率、冷却倒计时
- **对外 Key 管理**：前端一键创建 / 禁用 / 删除，支持 RPM、每日请求限额、模型白名单
- **兼容接口**
  - `POST /v1/chat/completions`（流式 + 非流式，支持 tools）
  - `POST /v1/messages`（Anthropic 兼容，Claude Code 可直连）
  - `POST /v1/images/generations`（网页通道绘图）
  - `GET /v1/models`
- **请求日志**：每条请求记录 Key、模型、命中账号、Token、耗时、错误，保留 8 天
- **零构建前端**：管理面板纯静态文件，无 CDN 依赖，国内 VPS 直接可用
- **持久化**：SQLite（WAL），单文件，重启不丢配置

## 快速开始（macOS / Linux 本地）

```bash
cd glm-studio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

ADMIN_KEY=你的管理密钥 .venv/bin/python server.py
# 默认监听 http://0.0.0.0:8000，管理面板 http://127.0.0.1:8000/admin
```

不设置 `ADMIN_KEY` 时，首次启动会自动生成随机密钥并打印到日志（同时存入数据库）。

## VPS 部署（Docker，推荐）

```bash
# 上传整个 glm-studio 目录到 VPS 后：
cd glm-studio
# 1. 改 docker-compose.yml 里的 ADMIN_KEY 为强随机值
# 2. 启动
docker compose up -d --build

# 查看日志
docker compose logs -f
```

公网访问：
- 管理面板：`http://<你的VPS IP>:8000/admin`
- API 端点：`http://<你的VPS IP>:8000/v1/chat/completions`

防火墙记得放行 8000 端口（或按需改 compose 端口映射）。生产环境建议前面挂 Nginx/Caddy 加 HTTPS。

## 使用流程

1. 打开 `/admin`，用 `ADMIN_KEY` 登录
2. **账号池 → 添加账号**：
   - **网页账号**：浏览器登录 [chatglm.cn](https://chatglm.cn) → F12 → Application → Cookies → 复制 `__Secure-next-auth.session-token` 的值，粘贴为 refresh_token
   - **官方 Key**：在 [open.bigmodel.cn](https://open.bigmodel.cn) 控制台创建 API Key 粘贴
   - 先点「测试」确认可用
3. **API Keys → 创建 Key**：得到 `sk-glm-...`，发给任何 OpenAI 兼容客户端
4. 客户端配置：
   - Base URL：`http://<host>:8000/v1`
   - API Key：`sk-glm-...`

> 注意：创建第一个 Key 之前 `/v1` 接口处于「开放模式」（无需鉴权），创建后立即强制鉴权。公网部署请第一时间把账号和 Key 配好。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_KEY` | 自动生成 | 管理面板登录密钥 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `DATA_DIR` | `data` | SQLite 数据目录 |

其余调度参数（策略、并发、自动禁用阈值、官方模型清单等）在管理面板「系统设置」里改，实时生效。

## 项目结构

```
glm-studio/
├── server.py               # 入口
├── glmstudio/
│   ├── app.py              # FastAPI 组装
│   ├── db.py               # SQLite 存储层
│   ├── pool.py             # 账号池 + 调度器（轮询/冷却/自动禁用/模型路由）
│   ├── public_api.py       # /v1/* 对外接口
│   ├── admin_api.py        # /admin/api/* 管理接口
│   └── glmcompat/          # GLM 网页协议层（vendor 自 glm2api-manage，AGPL-3.0）
├── static/                 # 管理面板（纯静态，无构建步骤）
├── Dockerfile
└── docker-compose.yml
```

## 致谢与许可

- GLM 网页协议实现（token 刷新、签名、SSE 翻译、工具调用转换）vendor 自
  [t479842598/glm2api-manage](https://github.com/t479842598/glm2api-manage)（AGPL-3.0，见 `glmstudio/glmcompat/LICENSE.glm2api-manage`），
  因此本项目整体按 **AGPL-3.0** 发布。
- 管理界面的交互与视觉风格参考 [chenyme/grok2api](https://github.com/chenyme/grok2api)（MIT），前端代码为独立实现。

## 风险提示

网页通道（`web` / `guest`）基于 chatglm.cn 私有网页接口，属于逆向协议：
官方改版可能随时失效；高频使用存在账号被风控的可能。生产关键业务建议以 `official`
官方 API Key 为主、网页账号为补充容量。
