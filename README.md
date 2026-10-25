# Buddy2api 2.0

[English](README_EN.md) | 中文

> 把本机已经登录的消费级 AI 客户端，接成 OpenAI 兼容接口，给 Codex、OpenCode、Cherry Studio、NextChat 等用。默认打开 Work Buddy / CodeBuddy、Qoder、千问办公（QwenWork）、TraeWork、讯飞 Loomy、商汤小浣熊；管理页下拉选其中一个。一次请求只走一个通道。QClaw 已停止运营，通道已下架，积分请改用 WorkBuddy。

当前版本 **2.2.5**。默认通道含 WorkBuddy 国内/国际、Qoder 中国/国际、千问办公、TraeWork、Loomy、小浣熊。没有登录凭证的渠道不会出现在模型列表里。这个项目只供你本人持有的账号在本机使用。不要公开部署，也不要把登录凭据、API Key、数据库文件发给别人。

**仅限自己的账号。严禁使用接码平台、批量注册或任何批量方式薅羊毛。** 不要把网关公开给别人用，也不要代领或倒卖额度。

默认本机启动会自动打开管理页，无需填写管理 Token，重启后已有页面仍可直接使用。重复启动会打开已运行的同一实例；同一数据库不能同时由多个实例使用。后台服务可加 `--no-browser`。`start.sh` 和 `start-launchd.sh` 与直接运行 `python server.py` 一样，不会自动读取 `.admin_token` 文件；显式传入 `--admin-token` 或设置 `CB_GATEWAY_ADMIN_TOKEN` 才启用令牌鉴权，非本机监听必须配置。启用令牌模式后需在管理页设置中填写。客户端 API Key 和上游账号授权不受影响。

## 这是什么？

Buddy2api 在本机提供 `http://127.0.0.1:8787/v1`。你在官方客户端里登录并且还有额度，这个网关把本机登录导入进来，把请求转到对应厂商。普通客户端走 Chat Completions；Codex 走 `/v1/responses`。Key 可以绑一家通道，也可以选自动通道，用 `通道/模型` 指定这一跳的厂商。

默认通道都开。没装、没登录的通道，账号页检测为空，不会自动入库，模型列表也不会出现它们的模型名。QClaw 已下架，环境变量里的 `qclaw` 会被忽略。

Qoder 中国版读 `%APPDATA%\com.qodercn.app.stable\auth.v1.dat`，国际版读 `%APPDATA%\com.qoder.app.stable\auth.v1.dat`。两边的登录文件、模型目录和令牌不共用。额度在领取结果和官方余额里看。工具调用交回本机客户端。

讯飞 Loomy 读本机 `auth-session.json`。每日额度由上游自动发放，一键领取只核对到账余额；长对话发送前按 16 万 token 预算裁旧历史。商汤小浣熊读本机 `auth.json`，一键领取会触发每日发放。两家都用自己的请求头，不走 WorkBuddy 的 CLI 指纹。

```powershell
python server.py
```

| 通道 | 默认 | 本机登录位置 |
|---|---|---|
| WorkBuddy / CodeBuddy 国内 | 开 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| WorkBuddy 国际 | 开 | 同上，域名是 `*.workbuddy.ai` 的账号会归到 `workbuddy-i` |
| Qoder 中国 | 开 | `%APPDATA%\com.qodercn.app.stable\auth.v1.dat` |
| Qoder 国际 | 开 | `%APPDATA%\com.qoder.app.stable\auth.v1.dat` |
| 千问办公 QwenWork | 开 | `%APPDATA%\QwenWorkCN` |
| TraeWork | 开 | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |
| 讯飞 Loomy | 开 | `%PUBLIC%\Loomy\*\userData\auth-session.json` |
| 商汤小浣熊 | 开 | `%USERPROFILE%\.box-agent\config\auth.json` |

路径不对时可用 `CB_AUTH_DIR`、`CB_QODER_USER_DATA_DIR`、`CB_QODER_I_USER_DATA_DIR`、`CB_QWENWORK_AUTH_DIR`、`CB_TRAEWORK_AUTH_DIR`、`CB_LOOMY_AUTH_DIR`、`CB_XHX_AUTH_DIR` 指定。各通道的登录文件不要混在同一个目录。只要其中一家时，可设 `CB_GATEWAY_PROVIDERS=workbuddy` 收窄。

## 注意事项

按下面「安装与启动」即可。这几条是 2.0 里最容易踩空的：

1. **启动后账号页是空的，这是正常的。** 默认不再自动入库。到「账号」页：选通道 → 重新检测 → 一键导入。已启用的通道都能选。没有登录文件的通道不会出现在模型列表里。
2. **一把 API Key 只打一个通道。** 创建时必须选通道。WorkBuddy 的 Key 发 `auto` / `glm-5.2`；Qoder 的 Key 发 `auto` 或 `Qwen3.8-Flash`；QwenWork 的 Key 发 `auto` 或 `qwork-advanced`；TraeWork 的 Key 发 `auto` 或 `qwen-3.7-plus`。通道和模型对不上会 400 或 403，不会帮你转到另一家。自动通道用 `qoder/DeepSeek-Flash` 这类带前缀的名字指定这一跳。
3. **某个通道返回 503 `channel_unavailable`：** 这个通道还没导入可用账号。
4. **QwenWork 请在 Windows 上直接跑 `python server.py`。** Linux Docker 读不了它用 DPAPI 加密的本机文件；管理页会写明这一点。WorkBuddy 可以继续用 Docker。
5. 本项目和聊天客户端最好在同一台电脑。客户端如果跑在 Docker 里，Base URL 填 `http://host.docker.internal:8787/v1`，不要填容器自己的 `127.0.0.1`。

WorkBuddy 5.6+ 会把 `.info` 登录快照中的敏感字段加密。检测和手动添加会尝试通过本机已安装的 WorkBuddy 解密；如果无法读取本机密钥，该快照会显示为无效或无法导入。检测列表会按通道和 UID 合并同一账号的历史快照，默认显示最新一条，旧快照仍可展开查看。

## 安装与启动

还没装环境时按这几步走。已经有虚拟环境的，装完 `requirements.txt` 后执行 `python server.py` 即可。

### 1. 安装工具

1. [Git](https://git-scm.com/downloads)，Windows 保持默认选项
2. [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/)，推荐 Python 3.12
3. 先打开并登录你要用的官方客户端（至少 Work Buddy / CodeBuddy）

装完后**重新打开** PowerShell、Windows Terminal 或 Anaconda Prompt：

```powershell
git --version
conda --version
```

找不到 `conda` 时，用开始菜单里的 **Anaconda Prompt / Miniconda Prompt**。也可以在那里执行 `conda init powershell`，关掉窗口再开。

### 2. 克隆项目

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
Get-ChildItem README.md, requirements.txt, server.py
```

后面的命令都要在这个目录里执行。

### 3. 用 Conda 启动（推荐）

```powershell
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python server.py
```

看到监听信息后，浏览器打开：

```text
http://127.0.0.1:8787
```

停止服务：回到终端按 `Ctrl+C`。下次开机后：

```powershell
cd <你的项目路径>\Buddy2api
conda activate buddy2api
python server.py
```

提示符前面应出现 `(buddy2api)`，再执行 `python -m pip`，避免装到系统 Python。

### 其他启动方式

- **脚本：** Windows 安装 Python 时勾选 Add Python to PATH，在项目目录执行 `.\start.bat`。Linux / macOS：`chmod +x start.sh && ./start.sh`。脚本优先用名为 `buddy2api` 的 Conda 环境，没有 Conda 才建 `.venv`；本机默认管理方式与直接运行 `python server.py` 一致。
- **Docker：** `powershell -ExecutionPolicy Bypass -File .\start-docker-win.ps1`。容器支持 README 所列通道；本机没有 WorkBuddy 登录目录时脚本仍会启动。QClaw、QwenWork 和 Loomy 的本机登录文件请用上面的 `python server.py` 导入；Docker 容器无法解密 QwenWork 的 Windows DPAPI 文件。TraeWork 登录文件不是 DPAPI，本机导入后 Docker 也能使用数据库里的 token。

### 第一次打开网页之后

本机管理页自动授权，不用粘贴 Token，也不依赖管理 Cookie。

1. 打开「账号」。WorkBuddy 可以点「浏览器登录」，在官方页面登录后自动入库，国内和国际站都能选。也可以点「一键导入全部通道」读取各启用通道的默认登录目录；自定义路径仅用于当前通道。未登录的通道会跳过。已入库的号用「一键续期并领取」续期并领取今日积分；只想领取时用「一键领取今日积分」。单个账号可在行菜单点「续期」。定时续期和定时领取在设置页，默认关闭，要手动打开并保存。
2. 点该账号的「测试」，能返回一句话就说明这条通道通了。
3. 打开「API Keys」，选择通道后创建。要一把 Key 打多家时选自动通道，模型写成 `通道/模型`。Codex 接口用 `/v1/responses`。创建后可以再显示、复制完整 Key。
4. 在客户端里填：
   - Base URL：`http://127.0.0.1:8787/v1`
   - API Key：刚复制的 Key
   - 模型：WorkBuddy、Loomy、小浣熊用 `auto`；Qoder 用 `auto` 或 `Qwen3.8-Flash`；千问办公用 `auto` 或 `qwork-advanced`；TraeWork 用 `auto` 或 `qwen-3.7-plus`。上游加了新模型时，到「模型配置」点「一键读取供应模型」；各通道目录分开保存，选错通道仍会 400/403。Qoder 的额度在领取结果和官方余额里看。Loomy 的每日额度由上游自动发放，一键领取会读到账余额。小浣熊的一键领取会触发每日发放。

管理页打不开或要远程访问时：

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-请换成足够长的随机值"
python server.py
```

### 更新

管理页后台自动检查版本时优先走 jsDelivr，并使用短期缓存。手动点击「检查更新」及一键更新前的复核，会直接核对配置仓库 `main` 的 GitHub Contents API；API 不可用时再尝试 GitHub raw。两种权威来源都不可用时显示「暂时查不到远端版本」，不会用 CDN 旧缓存确认已是最新。本 fork 默认检查 `XCh373/Buddy2api` 的 `main`；可通过环境变量 `CB_GATEWAY_UPDATE_REPO=owner/repo` 覆盖检查源。发现新版本后，顶栏出现「更新到 vx.y.z」，点一下会对当前分支配置的跟踪远端执行 `git pull --ff-only`、安装依赖并重启；本 fork 的 `main` 应跟踪 `origin/main`，且 `origin` 应指向你的 fork。检查源和实际拉取远端需要保持一致。Docker、不是 git 克隆、或不在 `main` 上时，按钮会说明原因，仍可用下面的手动步骤。本机已是最新、只想看按钮时：`$env:CB_GATEWAY_FAKE_LATEST="9.9.9"` 再启动，点下去不会真的拉取或重启。

先 `Ctrl+C` 停掉正在跑的服务：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r requirements.txt
python server.py
```

## 常见问题

- WorkBuddy 聚合响应（包括默认停转重试所用的工具续聊路径）在缺少完成标记时返回上游错误，不再把部分正文默认为正常 `stop`。明确的 `finish_reason` 后直接 EOF 仍被接受；仅收到 `[DONE]`、但正文没有结束原因时不会当作正常完成。此校验不能判定模型主动 `stop` 是否过早，也不保证解决所有长会话停转。

- `git` 或 `conda` 不是内部命令：关掉终端重开；Conda 用户改用 Miniconda Prompt。
- `No module named ...`：先 `conda activate buddy2api`，再 `python -m pip install -r requirements.txt`。
- 下载依赖很慢：确认能访问 PyPI，不要混用好几个 Python。
- 端口 8787 被占用：关掉旧的 Buddy2api，或 `python server.py --port 8788`。
- 网页里一个账号都没有：还没导入。选对通道再检测；登录目录不对就设 `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR`。
- 创建 Key 失败：没选通道。
- 客户端 503 `channel_unavailable`：这个 Key 绑定的通道还没有可用账号。
- 客户端 403 `key_channel_mismatch`：模型带了别的通道前缀，和当前 Key 不一致。
- 客户端 400 `unknown_model`：模型不属于这把 Key 的通道。换 Key，或改成该通道认识的 id。

## 从 1.4.x 升级

启动时会自动改数据库。旧 Key 视为绑在 `workbuddy` 上，原来的 `auto` / `glm-5.2` 还能用。

和 1.4 不同的地方：启动不再自动导入账号；空仓是 503 而不是普通 `server_error`；新建 Key 必须选通道；官方余额只显示积分，不把各厂数字加在一起。

## 客户端接入

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 管理页创建，已绑定通道 |
| 模型 | WorkBuddy：`auto` / `glm-5.2`。Qoder：`auto` 或 `Qwen3.8-Flash`。QwenWork：`auto` 或 `qwork-advanced`。TraeWork：`auto` 或 `qwen-3.7-plus`。Loomy、小浣熊：`auto` |
| Stream | 建议开 |

接口：`/v1/chat/completions`、`/v1/responses`、`/v1/models`。绑死一家的 Key，没加前缀的 `auto` 走那一家。自动通道的 Key，带前缀的模型打到对应通道；`auto` 走第一个已有账号的通道。额度用尽不会换到另一家。

### 模型容量与自动发现

「一键读取供应模型」会保存上游提供的输入/输出容量。`/v1/models` 返回 `context_window` 和 `max_output_tokens`；WorkBuddy 的 `maxInputTokens` / `maxOutputTokens` 会映射为这两个字段，不把官方客户端的 `contextWindow.defaultLength` 当成模型最大容量。DSH 自定义提供方使用「获取可用模型」重新添加并保存模型时，可自动读取容量；已有模型配置不会自动覆盖。

各通道缺失的容量字段分别默认显示 262,144 上下文 / 32,768 输出，`capacity_source` 标记每个字段来自 `catalog` 还是 `fallback`。默认值是配置兜底，不是上游保证；同名模型在不同通道的限制也可能不同。仅当目录中有已知输出限制时，网关会把显式的 `max_tokens` / `max_completion_tokens` 裁剪到该限制（Responses 的 `max_output_tokens` 经转换后同样适用）。未指定输出预算的请求不注入预算，未知容量不用于强制裁剪。达到输出上限仍可能以 `length` 结束，容量发现不能保证长任务永不截断。

### 思考强度

智能体可以在 Chat Completions 中发送顶层 `reasoning_effort`，在 Responses 中发送标准的 `reasoning: {"effort": "high"}`。网关也兼容 OpenCode、DSH、Cherry 和 Claude 风格的 `reasoning.effort`、`reasoningEffort`、`thinking.type`、`thinking.effort`、`output_config.effort`、`enable_thinking` 等写法。可用档位为 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`、`ultra`；`off` 等同于 `none`。

```json
{
  "model": "deepseek-v4-pro",
  "messages": [{"role": "user", "content": "分析这个问题"}],
  "reasoning_effort": "high"
}
```

| 通道 | 实际能力 |
|---|---|
| WorkBuddy | DeepSeek V4 Pro/Flash 支持 `low` / `high` / `max`，标准档位会投影到这三档；未指定时默认 `high`，可用 `CB_GATEWAY_DEFAULT_REASONING_EFFORT=off` 关闭默认 |
| QwenWork | 协议只有 `is_reasoning` 开关；`none` 关闭，其它显式档位开启，无法区分多档强度 |
| TraeWork | 当前会话协议没有可验证的思考控制字段，因此暂不支持调档 |

Chat 流会保留 `reasoning_content`。Responses 流会转换成标准的 `response.reasoning_summary_*` 事件，仅有推理、没有最终正文的有效响应也会正常完成。

OpenCode 示例（WorkBuddy Key）：

```json
{
  "provider": {
    "workbuddy": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-cb-你的key"
      },
      "models": {
        "auto": { "name": "WorkBuddy Auto" },
        "glm-5.2": { "name": "GLM-5.2" }
      }
    }
  }
}
```

```powershell
opencode run -m workbuddy/auto "你好"
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-你的key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'
```

QwenWork、TraeWork、Loomy、小浣熊各用自己那把 Key，不要混用。原来绑 QClaw 的 Key 要改到 WorkBuddy。

## 启动参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址，本机用保持这个值 |
| `--port` | `8787` | 端口 |
| `--admin-token` | 不设置 | 本机默认自动授权；远程监听必须配置管理凭证 |
| `--no-admin-auth` | 关 | 显式启用本机自动授权，仍校验请求来源；只允许回环监听 |
| `--no-browser` | 关 | 不自动打开浏览器，适合后台服务 |

## 临期额度优先路由

另有可选的[独立会话账号亲和](docs/design/session-affinity.md)，默认关闭，仅适用 WorkBuddy 国内/国际账号池。启用后，同一可靠会话优先保留仍可用且属于最高人工优先级组的绑定，再为新会话或失效绑定使用临期和原通道规则。客户端可通过独立显式头接入，无需任何前置代理；亲和不保证缓存命中或消息串行。

管理页“设置 → WorkBuddy 账号路由”可以启用“临期额度优先”，默认仍使用现有策略。同一手动优先级内，优先使用更早到期的可用额度包所在账号；手动优先级始终优先。相同到期时间保留账号粘性，其他通道不受影响。

默认临期窗口为 30 天，后台每 120 秒刷新额度，缓存超过 300 秒、查询失败或额度已被本地消耗抵扣时取消临期加成。没有可信临期额度时回到现有路由。关闭策略后不再发起策略专用后台查询。后台额度查询可能刷新账号凭据，因此同一个上游账号在多个实例中运行时仍可能相互影响。

设置中的“预览已保存的路由策略”可以查看预计账号、临期时间、保守剩余额度与降级原因，不会切换账号或刷新额度。实际账号内扣哪个额度包由上游决定；本地消耗抵扣是保守估计，无法精确覆盖其他客户端用量或尚未落账的并发请求。这里的临期指额度包，不是 Token 有效期。

详细设计见 [开发方案](docs/design/expiry-priority-routing.md)。

## 环境变量

| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_PROVIDERS` | 启用哪些通道，逗号分隔。默认 `workbuddy,workbuddy-i,qwenwork,qoder,qoder-i,traework,loomy,xiaohuanxiong`。`qclaw` 已下架，写了也会忽略。只想留一家时再改 |
| `CB_GATEWAY_AUTO_IMPORT` | 设 `1` 则启动时自动导入。默认 `0` |
| `CB_GATEWAY_CHECKIN_GAP_MS` | 一键领取和一键续期并领取的账号间隔，默认 `800` |
| `CB_GATEWAY_KEEPALIVE` | 设 `1` 强制打开定时续期，设 `0` 强制关闭。不设时跟随设置页，默认关闭 |
| `CB_GATEWAY_KEEPALIVE_HOUR` | 定时续期的本地小时，默认 `22`。只在该小时前 10 分钟执行 |
| `CB_GATEWAY_AUTO_CHECKIN` | 设 `1` 强制打开定时领取，设 `0` 强制关闭。不设时跟随设置页，默认关闭 |
| `CB_GATEWAY_AUTO_CHECKIN_HOUR` | 定时领取的本地小时，默认 `9` |
| `CB_GATEWAY_DEFAULT_REASONING_EFFORT` | WorkBuddy DeepSeek V4 Pro/Flash 的默认思考强度，支持 `low` / `high` / `max`，默认 `high`；设为 `off` 可关闭默认值。Responses 的 `reasoning.effort` 或 Chat Completions 的 `reasoning_effort` 会覆盖它 |
| `CB_AUTH_DIR` | WorkBuddy 登录目录 |
| `CB_QODER_USER_DATA_DIR` | Qoder 中国版用户数据目录，下面应有 `auth.v1.dat` |
| `CB_QODER_I_USER_DATA_DIR` | Qoder 国际版用户数据目录，下面应有 `auth.v1.dat` |
| `CB_QWENWORK_AUTH_DIR` | QwenWork 登录目录 |
| `CB_TRAEWORK_AUTH_DIR` | TraeWork `storage.json` 所在目录 |
| `CB_LOOMY_AUTH_DIR` | Loomy 数据根目录，下面应有 `*/userData/auth-session.json` |
| `CB_LOOMY_CONTEXT_BUDGET` | Loomy 发送前上下文预算，默认 `160000`。`0` 关闭发送前裁剪 |
| `CB_GATEWAY_MAX_BODY_BYTES` | 请求体上限，默认 `33554432`（32 MiB） |
| `CB_XHX_AUTH_DIR` | 小浣熊 `auth.json` 所在目录或文件 |
| `CB_HOST_AUTH_DIR` | Docker 脚本用的本机 WorkBuddy 目录 |
| `CB_GATEWAY_ADMIN_TOKEN` | 固定管理 Token |
| `CB_GATEWAY_DB_PATH` | 数据库路径 |
| `CB_GATEWAY_MASTER_KEY` | 跨系统搬数据库时的加密主密钥 |
| `CB_GATEWAY_LOG_RETENTION_DAYS` | 日志保留天数，默认 `90` |
| `CB_GATEWAY_USER_AGENT` | 只影响 WorkBuddy 出站头，默认 `CLI/2.109.2 CodeBuddy/2.109.2` |

## 数据和安全

- 账号 Token 写入前会加密。Windows 用系统 DPAPI。
- 不要把 `*.db`、登录目录、日志、带 Key 的截图发出去。
- 不要把服务绑到公网。保持 `127.0.0.1`。

## License

MIT
