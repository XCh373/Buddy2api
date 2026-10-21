# Buddy2api 2.0

[English](README_EN.md) | [中文](README.md)

> Local consumer AI clients → one OpenAI-compatible API for Codex, OpenCode, Cherry Studio, NextChat, and similar agents. Work Buddy / CodeBuddy, Qoder, QwenWork, TraeWork, iFlytek Loomy, and SenseTime Xiaohuanxiong are on by default; pick one in the UI dropdown. Each request stays on one channel. QClaw is retired; its credits moved to WorkBuddy.

Release **2.2.5**. Default channels include WorkBuddy domestic and international, Qoder CN and international, QwenWork, TraeWork, Loomy, and Xiaohuanxiong. Channels without a stored login are omitted from the model list. Local use only, and only with accounts you personally own. Do not expose this on the public internet, and do not share credentials, API keys, or the database.

**Your own accounts only. Do not use SMS-code platforms, bulk registration, or any batch method to farm credits.** Do not expose the gateway to other people, claim rewards for them, or resell quota.

Default loopback startup opens the management page without an Admin Token. Restarting does not invalidate local access. Repeated startup opens the existing instance; a database can only be used by one process. Use `--no-browser` for background services. `start.sh` and `start-launchd.sh` do not automatically read `.admin_token`; setting `--admin-token` or `CB_GATEWAY_ADMIN_TOKEN` explicitly enables token authentication and is required for non-loopback listeners. Enter that token in management settings when token mode is enabled. Client API keys and upstream account credentials are unchanged.

## What is this?

Buddy2api listens on `http://127.0.0.1:8787/v1`. You stay signed into the official apps; this gateway imports those sessions and forwards chat. Typical clients use Chat Completions. Codex uses `/v1/responses`. A key can stay on one channel, or use automatic channel and pick the vendor with a `channel/model` id.

Five channels are on by default. A channel with no local login shows empty on Accounts; nothing is imported until you click Import. `qclaw` in `CB_GATEWAY_PROVIDERS` is ignored.

Qoder CN reads `%APPDATA%\com.qodercn.app.stable\auth.v1.dat`. Qoder international reads `%APPDATA%\com.qoder.app.stable\auth.v1.dat`. Their login files, model catalogs, and tokens are not interchangeable. Quota is shown from claim results and the official balance. Tool calls are returned to the local client.

Loomy reads a local `auth-session.json`. Its daily balance is granted upstream; one-click claim only reads that balance. Long Loomy chats are trimmed to a 160k-token budget before send. Xiaohuanxiong reads a local `auth.json`, and one-click claim triggers the daily grant. Neither channel uses the WorkBuddy CLI header set.

```powershell
python server.py
```

| Channel | Default | Where logins live |
|---|---|---|
| WorkBuddy / CodeBuddy domestic | on | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| WorkBuddy international | on | Same folder; `*.workbuddy.ai` accounts move to `workbuddy-i` |
| Qoder CN | on | `%APPDATA%\com.qodercn.app.stable\auth.v1.dat` |
| Qoder international | on | `%APPDATA%\com.qoder.app.stable\auth.v1.dat` |
| QwenWork | on | `%APPDATA%\QwenWorkCN` |
| TraeWork | on | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |
| iFlytek Loomy | on | `%PUBLIC%\Loomy\*\userData\auth-session.json` |
| SenseTime Xiaohuanxiong | on | `%USERPROFILE%\.box-agent\config\auth.json` |

Narrow with `CB_GATEWAY_PROVIDERS=workbuddy` if you only want one.

## Before you start

1. **An empty Accounts page after startup is expected.** 2.0 does not import on boot. For WorkBuddy, use **Browser login** and sign in on the official page. You can also pick a channel → Detect → Import. **Renew and claim** refreshes saved logins and claims today's points. A single account can use **Renew** in its row menu. Scheduled renewal and check-in live in Settings and stay off until you turn them on and save.
2. **One API key is one channel.** Create the key with a channel selected. A WorkBuddy key uses `auto` / `glm-5.2`; a Qoder key uses `auto` or `Qwen3.8-Flash`; a QwenWork key uses `auto` or `qwork-advanced`; a TraeWork key uses `auto` or `qwen-3.7-plus`. Mismatched model/key returns 400 or 403 — there is no cross-vendor failover. Automatic channel uses a prefixed id such as `qoder/DeepSeek-Flash` for that hop.
3. **HTTP 503 `channel_unavailable`** means that channel has no imported account.
4. **Run QwenWork with `python server.py` on Windows.** A Linux Docker container cannot decrypt its DPAPI files; the UI says so. WorkBuddy can stay on Docker.
5. If the chat client is itself in Docker, Base URL is `http://host.docker.internal:8787/v1`.

WorkBuddy 5.6+ encrypts sensitive fields in `.info` login snapshots. Detection and manual import try to decrypt them with the locally installed WorkBuddy app; a snapshot is marked invalid or skipped if the local key cannot be read. The detection list groups snapshots by channel and UID, showing the newest by default while keeping older snapshots expandable.

## Install

1. [Git](https://git-scm.com/downloads), [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/) (Python 3.12), and sign into Work Buddy / CodeBuddy at least once.
2. Reopen the terminal, then:

```powershell
git --version
conda --version
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python server.py
```

3. Open http://127.0.0.1:8787 → Accounts → Detect → Import → Test → API Keys (select a channel; pick Codex if the client is Codex) → point your client at `http://127.0.0.1:8787/v1`.

Windows script: `.\start.bat`. Docker helper: `.\start-docker-win.ps1` (WorkBuddy mount; use native Python for QwenWork).

Later starts: `conda activate buddy2api` then `python server.py` in the project directory.

## FAQ

- `conda` not found: use Miniconda Prompt, or `conda init powershell` and reopen the terminal.
- `No module named ...`: activate `buddy2api`, then `python -m pip install -r requirements.txt`.
- Port 8787 in use: stop the old process or `python server.py --port 8788`.
- No accounts in the UI: import has not been run yet.
- Key create fails: the channel dropdown is required.
- 403 `key_channel_mismatch`: the model prefix does not match the key’s channel.
- 400 `unknown_model`: that model does not belong to this key’s channel.

## Update

The background version check uses jsDelivr first and caches the result briefly. A manual **Check for updates** action and the pre-apply recheck query the configured repository's GitHub `main` through the Contents API, then GitHub raw as a fallback. If both authoritative sources fail, the gateway reports that the remote version is unavailable instead of treating a CDN value as confirmation that the install is current. This fork defaults to `XCh373/Buddy2api`'s `main`; override the source with `CB_GATEWAY_UPDATE_REPO=owner/repo`. A top-bar **Update to vx.y.z** button runs `git pull --ff-only` from the current branch's configured tracking remote, reinstalls requirements, and restarts. For this fork, `main` should track `origin/main`, and `origin` should point to your fork so the check and pull targets stay aligned. Docker images, zip copies, and non-`main` checkouts keep using a manual pull. To preview the button on an already-current install, start with `CB_GATEWAY_FAKE_LATEST=9.9.9`; the click does not pull or restart.

## Upgrade from 1.4.x

The database migrates on startup. Existing keys stay on `workbuddy`. Startup no longer auto-imports; empty channel is 503; new keys must pick a channel; the official-balance column shows credits only.

## Client

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Created in the UI, bound to one channel |
| Model | WorkBuddy, Loomy, and Xiaohuanxiong use `auto`; Qoder uses `auto` or `Qwen3.8-Flash`; QwenWork uses `auto` or `qwork-advanced`; TraeWork uses `auto` or `qwen-3.7-plus`. Move old QClaw keys to WorkBuddy |

Unprefixed `auto` follows the key’s channel. Use a separate key per channel. On the Models page, “一键读取供应模型” refreshes each channel’s supplier list separately; a TraeWork-only id such as Doubao is never merged into WorkBuddy.

### Model capacity discovery

Refreshing supplier models now retains upstream input/output capacities. `/v1/models` exposes `context_window` and `max_output_tokens`; WorkBuddy's `maxInputTokens` and `maxOutputTokens` map to these fields, rather than treating its desktop `contextWindow.defaultLength` as the maximum. DSH custom providers can import these capacities through **Fetch available models**, then add and save the selected models. Existing configured models are not overwritten automatically.

Missing fields independently fall back to 262,144 context tokens and 32,768 output tokens on every channel. The per-field `capacity_source` is `catalog` or `fallback`. Fallbacks are configuration defaults, not verified upstream limits. Explicit `max_tokens` and `max_completion_tokens` requests are clamped only when the catalog contains a known output limit; converted Responses `max_output_tokens` requests use the same path. Omitted budgets stay omitted, and unknown capacities do not impose a hard cap. Reaching a configured output budget can still produce `length`; discovery does not prevent all long-task truncation.

### Reasoning effort

WorkBuddy's collected responses reject partial text without completion metadata instead of synthesizing a successful `stop`. An explicit `finish_reason` followed by EOF remains valid without `[DONE]`; a `[DONE]` event alone does not make text without a finish reason complete. A valid `stop` is returned as the model's completed answer even when tool results appear in the history. The gateway does not infer another tool call from the answer text, and the legacy `CB_GATEWAY_TOOL_STALL_RETRY` and `CB_GATEWAY_TOOL_STALL_FAIL_STREAM` variables no longer affect requests.

Agent clients can send top-level `reasoning_effort` to Chat Completions and the standard `reasoning: {"effort": "high"}` object to Responses. Compatibility forms used by OpenCode, DSH, Cherry, and Claude-style clients are also accepted: `reasoning.effort`, `reasoningEffort`, `thinking.type`, `thinking.effort`, `output_config.effort`, and `enable_thinking`. Accepted levels are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`; `off` is an alias for `none`.

| Channel | Effective capability |
|---|---|
| WorkBuddy | DeepSeek V4 Pro/Flash supports `low` / `high` / `max`; standard levels are projected onto those tiers. The default is `high` and can be disabled with `CB_GATEWAY_DEFAULT_REASONING_EFFORT=off` |
| QwenWork | The protocol exposes only an `is_reasoning` switch. `none` disables it and any other explicit tier enables it; distinct effort levels are unavailable |
| TraeWork | The current session protocol has no verified reasoning control field, so effort selection is not supported |

Chat streams preserve `reasoning_content`. Responses streams expose standard `response.reasoning_summary_*` events and accept valid reasoning-only completions.

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-your-key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

## Expiring credit routing

In Settings, use the WorkBuddy account routing controls to enable expiry-first routing. It is off by default. Manual account priority takes precedence; within that priority, accounts with earlier expiring usable credits are preferred. Accounts with the same expiry retain stickiness. Other channels keep their existing routing.

Defaults: a 30-day expiry window, resource refresh every 120 seconds, and a maximum cache age of 300 seconds. Failed or stale snapshots and conservatively exhausted credits lose their expiry advantage; routing falls back to the existing policy. The saved-policy preview explains the expected account without changing stickiness or querying resources.

The upstream owns deduction within an account. Local consumption estimates do not capture all external or in-flight usage. Background resource queries may refresh credentials; separate local databases do not isolate the same upstream account across instances. Disabling the policy stops new policy-specific background queries.

## Environment

`CB_GATEWAY_PROVIDERS` (default `workbuddy,workbuddy-i,qwenwork,qoder,qoder-i,traework,loomy,xiaohuanxiong`; `qclaw` is ignored), `CB_GATEWAY_AUTO_IMPORT` (default `0`), `CB_AUTH_DIR` / `CB_QODER_USER_DATA_DIR` / `CB_QODER_I_USER_DATA_DIR` / `CB_QWENWORK_AUTH_DIR` / `CB_TRAEWORK_AUTH_DIR` / `CB_LOOMY_AUTH_DIR` / `CB_XHX_AUTH_DIR`, `CB_GATEWAY_ADMIN_TOKEN`, `CB_GATEWAY_MASTER_KEY`.

`CB_GATEWAY_DEFAULT_REASONING_EFFORT` controls the default reasoning effort for WorkBuddy DeepSeek V4 Pro/Flash. It accepts `low`, `high`, or `max`, defaults to `high`, and can be disabled with `off`. A Responses `reasoning.effort` or Chat Completions `reasoning_effort` value overrides the default.

Keep `--host 127.0.0.1`. Do not share the database, auth folders, or key screenshots.

## License

MIT
