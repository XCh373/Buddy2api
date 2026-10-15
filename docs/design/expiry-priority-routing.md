# WorkBuddy 临期额度优先路由开发方案

日期：2026-10-14。状态：用户已授权实施，功能实现与验收已完成；本方案记录设计约束，当前运行与发布状态见 [验证记录](../verification/expiry-priority-routing.md)。

## 1. 目标与开发边界

目标：在账号可用、手动优先级相同的情况下，优先把新请求路由到有更早到期额度包的 WorkBuddy 账号，减少临期额度闲置。

这里的“临期”指额度包失效时间，不是 access token 到期时间。Token 可以刷新，不能作为额度消耗顺序的依据。本项目只能选择账号；账号内实际扣哪个额度包由上游决定，不能承诺指定额度包优先扣费。

本期包含：可开关的策略、缓存可信度与余额消耗处理、后台额度刷新、兼容现有粘性与失败重试、设置入口和路由预览、相关回归验证。不改变模型/通道绑定，不跨通道调度，不新增按余额负载均衡，不重构 dashboard，不做 Magpie 集成。

用户授权执行本方案且未调整以下默认值，实施按这些选择执行：

- 老用户默认保持现有策略，手动启用临期优先。
- 手动 priority 高于自动临期排序；用户可以保留备用账号或指定主账号。
- 临期窗口默认 30 天，与现有 dashboard 提醒口径一致。
- 仅 WorkBuddy 支持；其他通道继续现有行为。

## 2. 工作区与运行隔离

| 用途 | 当前状态 / 约定 |
| --- | --- |
| 日常服务目录 | 本地日常 worktree（`Buddy2api`），`main`，实现前基线 `11853e3946844cbb17ac5e784966bb6efe26a8d0` |
| 开发 worktree | 本地开发 worktree（`Buddy2api-dev`） |
| 开发分支 | `feat/expiry-priority-routing`，从上述已提交基线创建 |
| 日常端口 | `127.0.0.1:8787`，继续由原 launchd 配置启动 |
| 开发端口 | 建议 `127.0.0.1:8788`，启动前重新检查占用 |
| 开发数据 | worktree 的 `data/dev.db`，通过 `CB_GATEWAY_DB_PATH` 显式指定 |
| 开发依赖 | worktree 的 `.venv`；不得在 WorkBuddy 的运行环境中安装开发依赖 |
| 凭据 | 首先使用模拟数据；真实验证仅使用指定测试账号和独立客户端 Key |

创建 worktree 不会复制被 Git 忽略的数据库、密钥或虚拟环境，也不会启动开发服务。`.gitignore` 已覆盖 `data/`、`.venv/`、数据库与密钥。开发分支提交/推送不升级日常目录；修改日常目录、重启日常服务、合并到日常 `main` 或远端发布均为后续独立步骤。

开发环境在实施阶段单独创建。安装 `requirements-dev.txt` 后，在开发目录启动：

```sh
CB_GATEWAY_DB_PATH="$PWD/data/dev.db" \
CB_GATEWAY_AUTO_IMPORT=0 \
.venv/bin/python server.py --host 127.0.0.1 --port 8788 --no-browser
```

默认密钥会跟随开发数据库生成，不能指向日常数据库的密钥。不要修改当前 launchd 或 Magpie 的日常地址。两个数据库中的同一真实账号仍共享上游额度，可能存在刷新令牌竞争；本地数据隔离不等于上游账号隔离。

## 3. 已核对的实现锚点

锚点均针对基线 `11853e3`；实施时按函数名重新定位。

| 锚点 | 已有行为与设计影响 |
| --- | --- |
| `auth_manager.py:pick_account`、`_route_sort_key` | 过滤排除账号与冷却账号，先选最高 priority；同优先级先返回当前粘性账号。首次选取按 weight、加权请求数、请求数、ID 排序 |
| `auth_manager.py:pick_account_with_fallback` | 无 active 候选时刷新同通道 expired 账号；expired 指授权状态，不能等同额度包失效 |
| `database.py:get_active_accounts` | SQL 只按 priority、加权请求数、请求数、ID 排序；Python 再次排序且处理粘性，Python 是最终选择层 |
| `auth_manager.py:_safe_resource_item` | 额度包已有 `expire_ts`、`remaining_precise`、周期起止时间等；时间标准化为 Unix 秒，账号 token 时间为毫秒 |
| `auth_manager.py:fetch_account_resources`、`_resource_failure` | 默认缓存 60 秒；成功写入 `account_resource_cache`；失败可以返回 `ok=True, stale=True` 的旧成功缓存，失败状态本身目前不持久化 |
| `database.py:get_account_resource_cache` | 依据持久化更新时间计算 cache age；不能把取出缓存的时间当成成功查询时间 |
| `database.py:add_log`、`account_increment_usage` | 本地有累计 `total_credits`，但没有实际逐包扣费记录 |
| `providers/workbuddy/__init__.py`、其他 provider facade | 多个通道复用 `auth_manager.pick_account`，必须显式限制新增策略只用于 WorkBuddy |
| `proxy.py:_json_chat_with_stall_retry`、`_stream_upstream` | 每次重试传入 `tried_ids`；串流输出前可以切换账号，既有重试与计费边界需保留 |
| `server.py:admin_update_settings` | settings 表现成可用，但管理写入接口只接受 `backend_url/default_domain/timeout`，需增加校验后的策略字段 |
| `web/index.html` 的 Settings 与账号排序 | “最近到期优先”目前只影响显示顺序；保存设置目前未包含路由策略 |

仅给 SQL 的 ORDER BY 添加到期时间不会完成本功能：现有粘性返回会绕过排序，缓存过期与已消耗额度也无法靠静态 SQL 顺序解决。本期保持现有账号 SQL 的资格与基础排序，通过 Python 策略层消费批量缓存。

## 4. 策略配置契约

使用现有 settings 表中的单个对象 `workbuddy_routing`，不新增 accounts 字段或数据库表。

```json
{
  "mode": "legacy",
  "expiry_window_days": 30,
  "cache_max_age_seconds": 300,
  "refresh_interval_seconds": 120
}
```

- `mode`：只接受 `legacy` / `expiry_first`；缺省为 `legacy`。
- `expiry_window_days`：整数 1–30，默认 30。
- `cache_max_age_seconds`：整数 60–1800，默认 300。
- `refresh_interval_seconds`：整数 30–600，默认 120，必须小于 `cache_max_age_seconds`。
- 布尔值不作为整数接受；未知字段、非对象、非法枚举、无效范围返回 400，并且整次请求不得部分保存。
- 管理接口读取返回规范化后的完整对象。设置中已有旧键继续保持兼容。
- 内部读取损坏配置时使用 `legacy`，记录无敏感内容的警告；不因坏配置阻断推理服务。
- 策略读取与候选计算共用一份配置快照；保存后下一次选号使用新配置。后台任务最迟在下一轮调度感知变化。

为避免扩大通用 settings API，允许列表只增加 `workbuddy_routing`，不要开放任意 settings 写入。

## 5. 缓存与额度可信度

### 5.1 可用于临期排序的快照

只有同时满足以下条件的快照参与自动优先：

1. 最近官方查询成功，返回的是新数据，且有可校验的 `packages` 列表。
2. 成功时间与当前时间间隔不超过配置的最大年龄；未来时间戳、损坏 JSON、NaN/Infinity 或异常数字视为未知。
3. 最近一次官方查询没有明确失败。失败展示仍可使用旧缓存，但路由取消该账号的临期加成，直到新查询成功。
4. 有可校验的本地累计消耗基线；旧版本缓存没有该基线时，保留展示，但等下一次刷新才用于临期排序。

在原 resource payload 中添加内部字段：`route_usage_baseline`、`route_last_attempt_at`、`route_last_attempt_ok`。成功时更新快照；失败时只写最后尝试结果，不改成功更新时间、不清空已有展示数据，也不把失败结果转换成新成功缓存。

所有后台和手动资源刷新通过同一 per-account 异步锁处理，避免慢失败覆盖更新的成功数据。锁内开始请求前重新读取当前 `total_credits` 作为基线；同一进程内任务取消必须释放锁。缓存数据库读写失败只使临期加成降级，保留原选号逻辑；实际账号列表读取失败仍按既有错误处理，不能伪装为正常空列表。

### 5.2 可消耗额度包

从 packages 重新计算，不直接信任旧的 `next_expire_ts`、`expired` 或 `days_to_expire` 展示字段：

- `remaining_precise` 是有限正数。
- `expire_ts` 是有效的秒级时间戳，并满足 `now < expire_ts <= now + window_days * 86400`。恰好到期不再参与，恰好等于窗口结束可以参与。
- 已知 cycle / deduction 开始时间未到时，不参与；对于结束时间，保守排除已经结束的可用周期或扣费区间。未知字段不擅自推断官方语义。
- 使用现有资源标准化结果。原始 `Status` 数值的业务枚举没有本地明确契约，不自行把某个数字解释成可用或不可用。
- 无日期、长期有效、已过期、零余额、窗口外、单位不兼容的包均无临期加成，账号本身仍可作为普通候选。

一个账号的排序时间是所有符合条件的包中最早的 `expire_ts`，展示金额是该时间点的有效剩余之和。跨秒/毫秒的容错放在资源标准化入口，选择器不根据数值猜测单位。

### 5.3 处理查询后的本地消耗

不能一直使用旧的正余额将请求粘在已耗尽临期包的账号上。设：

```text
delta = current_total_credits - route_usage_baseline
effective_remaining(package) = max(0, remaining_precise(package) - delta)
```

在官方查询开始前记录基线，因此查询期间的已记录消耗也会被保守扣减。对每个包都扣除同一个 delta 是有意的保守估计：本地无法知道上游实际扣了哪个包，宁可提前取消优先，也不假设“必定先扣最早到期包”。金额归零后尝试该账号下一个仍有有效剩余的临期包；没有则回普通候选。

累计消耗为负差值、非有限值或计费单位无法确认时，快照不能用于临期加成，等待刷新。此方法不能覆盖其他客户端产生的消耗、尚未落账的并发请求或未返回计费信息的请求；这些误差依赖后台刷新和现有失败重试收敛，不声称精确逐包扣费，不在本期引入额度预占或分布式锁。

## 6. 账号选择与粘性规则

选择过程：

1. 按 provider 读取 active 账号；沿用排除 ID、冷却过滤。没有候选时保留原 fallback 入口。
2. 只保留候选中最高的手动 priority。临期账号不能越过更高的手动 priority。
3. 对 WorkBuddy 且 `mode=expiry_first` 时，一次批量读取上述候选的资源缓存，生成每个账号的临期摘要，不逐账号打开 SQLite 连接。
4. 有可信临期候选时，只保留最早 `expire_ts` 的账号组；其他账号暂不参加本次选择。没有可信临期候选时使用全部最高 priority 账号，完全回到既有策略。
5. 当前粘性账号仍在最佳组内则继续使用。只有手动 priority 或临期时间组发生变化、账号失去资格时才切换；同秒到期的账号不因余额小幅变动不断切换。
6. 最佳组没有粘性账号时，按既有 `_route_sort_key` 选择，并原子更新该 provider 的 sticky ID。

有效排序关系：`手动 priority > 可信临期时间组 > 组内粘性 > 原 weight/请求数/ID`。金额只用于判断有无可消耗额度和解释结果，不新增“余额越大越优先”的排序。

关闭策略不清空现有粘性状态，恢复原算法即可；其他通道不读取额度包缓存。本期不改变 expired 授权恢复账号的先后顺序。恢复成功后的账号先保证请求可用，再由后台获取额度；没有可信额度数据时不声称它有临期优先。

| 例子 | 预期 |
| --- | --- |
| A/B priority 都是 0；A 一天后到期，B 十天后到期，当前 sticky=B | 切换 A |
| A priority=0 且一天后到期，B priority=10 且长期有效 | 使用 B |
| A/B priority 相同且同秒到期，sticky=B | 继续 B |
| A 最早包已按本地消耗扣到 0，其次包二十天后到期；B 两天后到期 | 使用 B |
| A 的查询失败或缓存超龄，B 有可信临期包 | 使用 B |
| 所有缓存未知或所有包在窗口外 | 按原策略选择，不能返回额外的 503 |
| A 临期但处于冷却、已禁用或在 tried_ids 中 | 不选 A，沿用原候选/重试规则 |

选择器不能在持有 `_route_lock` 时联网或刷新 token。策略摘要先计算，短临界区只处理 sticky；摘要使用本次候选快照，不宣称消除所有并发与管理员修改竞态。

## 7. 后台刷新与解释能力

在 FastAPI lifespan 中管理一个刷新任务。`legacy` 或 WorkBuddy 通道被禁用时，不发起策略专用官方查询。启用后调度一次预热，随后按配置间隔刷新 active WorkBuddy 账号；最大并发 2，单账号资源查询使用独立的 25 秒超时上限，不随聊天请求的长超时无限等待。单账号错误不终止整轮任务。

每轮任务不重叠、不启动额外线程事件循环；停止服务时取消并等待任务退出。大量账号使完整轮次超过刷新间隔时，结束后再安排下一轮，不堆积任务；超龄账号自然降级。首个推理请求不等待预热：缓存还没有准备好时使用原策略。

页面手动“额度”刷新与后台刷新共享同一成功/失败元数据；领取成功后触发该账号的后台资源刷新，不阻塞正常请求。页面刷新可借助刚完成的同账号刷新结果，避免后台与 dashboard 对同一账号重复联网；明确的强制刷新按串行次序完成。刷新前再次检查账号是否还 active，避免对已禁用账号持续刷新。

前端 Settings 增加“WorkBuddy 账号路由”区域，保留原页面结构：

- 模式：现有策略 / 临期额度优先。
- 临期窗口与缓存最大年龄可设置；刷新间隔显示在同一区域。
- 文案明确“同优先级内优先，账号内实际扣包顺序由上游决定”。
- 设置保存沿用现有按钮，错误明确展示，不改变原有账号列表显示排序。

新增只读 `GET /admin/routing/preview?channel=workbuddy`，遵守已有 admin 鉴权。与真实选号共享摘要和选择计算，但不写 sticky、不刷新额度或凭据。其他 channel 返回 400。返回：生效配置、当前 sticky、预期选中账号 ID、每个候选的 priority、资格原因、快照成功时间/年龄、是否可信、最早临期时间和保守金额。仅返回解释所需安全字段，不能返回账号 token、客户端 Key 或凭据 extra。

资格原因使用明确枚举：`expiry_candidate`、`no_snapshot`、`legacy_snapshot`、`refresh_failed`、`cache_expired`、`invalid_snapshot`、`no_expiring_credit`、`local_usage_exhausted`、`lower_manual_priority`、`cooldown`、`inactive`。请求级 exclude 不由预览模拟；真实重试验证中单独检查。沿用已有请求日志的 account_id 证明真正选中的账号，不新增每次请求的数据库日志表。

## 8. 实施顺序与文件边界

1. **策略与纯选择计算**：新增 `account_routing.py`，实现规范化配置、可信额度摘要、最佳组计算与无副作用预览；新增 `tests/test_expiry_routing.py`。冻结时间，用明确 A/B 账号测试产品规则。
2. **数据与选号接入**：`database.py` 增加批量缓存读取和刷新尝试元数据更新；`auth_manager.py` 接入策略与原 sticky、刷新成功基线/per-account 锁；保留 public picker 签名和其他 provider 行为。完成缓存故障降级、消耗扣减和并发读取测试。
3. **自动刷新与配置 API**：`server.py` lifespan、settings 完整校验、只读预览接口；在独立刷新模块 `routing_refresh.py` 管理任务，避免纯策略模块依赖异步网络。`auth_manager.py` 的资源查询作为唯一官方入口，不在 worker 中复制 HTTP/刷新凭据实现。
4. **管理界面**：只修改 `web/index.html` 既有 Settings 区域，增加策略设置和安全预览入口；旧显示排序继续保持纯展示用途。`control_plane.py:claim_one` 只安排成功领取后的资源刷新，不扩展领取范围。
5. **统一验收**：完成相关回归、独立服务验证和差异审查，记录真实扣费证据与仍未验证的边界。此时才具备合并或本机升级的依据。

依赖顺序：步骤 1 → 2 → 3 → 4 → 5。策略/API 契约确定后，前端可以使用固定模拟响应与后台刷新测试并行，但不在契约变化期间同时重写 picker。

## 9. 验证矩阵与完成条件

| 验证类别 | 必须覆盖 |
| --- | --- |
| 基础优先级 | 一天/十天/长期；手动 priority 覆盖；同秒粘性；临期组变化时解除旧 sticky |
| 包边界 | 秒级时间；恰好 now 与窗口结束；未来开始、周期已结束；多个包；无日期；零/负/非有限余额；不可解析字段 |
| 缓存可信度 | 无缓存、旧格式、成功新缓存、超龄、时间在未来、成功后失败但仍有旧 ok 缓存、失败后恢复、损坏 JSON |
| 消耗与并发 | 基线前/后请求；负差值；最早包耗尽后退下一包；多个同期限包保守扣减；未落账请求的边界；批量读取避免 N+1；刷新并发限流与不重叠 |
| 兼容 | legacy 结果与原算法一致；QClaw/QwenWork/TraeWork 完全保留；inactive/exclude/cooldown；expired token fallback；settings 老键兼容 |
| 请求链路 | Chat Completions 非串流、串流、Responses；优先账号失败后排除；串流输出后不因本策略额外重放；日志 account_id 与实际选中一致 |
| 生命周期 | 关闭策略没有新增联网；打开后预热；未预热时快速降级；一个查询失败不杀 worker；重复手动刷新与后台刷新；关闭/退出取消；已禁用账号不刷新 |
| API/UI | 非法配置整次不落库；未授权预览拒绝；预览不改变 sticky；无凭据泄漏；保存后读取一致；桌面/窄屏设置可用；列表显示排序不改变路由模式 |

实施阶段在开发虚拟环境执行：

```sh
.venv/bin/python -m pytest tests/test_expiry_routing.py tests/test_provider_schema.py tests/test_control_plane.py tests/test_workbuddy_local_features.py
.venv/bin/python -m pytest
git diff --check
```

新增 lifecycle/API 测试需要明确进入 lifespan；现有部分 ASGITransport 用例不自动启动 lifespan，不得把 worker 未启动的测试当作生命周期证明。所有单测使用独立临时数据库与模拟 HTTP，不能自动读取真实账号目录或访问官方服务。

独立服务验收在 8788 上用模拟官方资源/聊天响应证明账号选择与日志；真实测试账号仅用于补充验证可用性和额度刷新。日常 8787 地址、PID、启动目录与主工作区状态在前后核对。不得用真实推理请求自动做回归压力测试。

完成条件：默认关闭的兼容性、启用后的选号案例、缓存失败降级、worker 退出、接口鉴权和请求链路均有直接证据；全量测试通过；改动范围可解释；真实扣费的单次证据与跨账号、外部客户端和并发边界均明确记录。具体通过项与未完成项以验证记录为准，不把设计要求视为测试结果。

## 10. 发布与回退边界

本功能实现与验收位于 `feat/expiry-priority-routing`。本地提交及合并到 `main` 不会自动推送到远端，也不会重启或升级日常服务；远端发布和日常部署分别处理。

未来本机上线前：备份日常数据库及凭据密钥；选择已验收的准确提交；安装到日常专用 Python 环境；在明确的维护窗口升级并验证。运行环境迁移是单独任务，不混进路由功能实现。

功能回退优先把 `mode` 改为 `legacy`，下一次选号恢复原策略，后台在途查询取消或完成后停止调度，关闭后不再发起新请求。本期不改表结构，只添加 settings 与缓存 payload 字段；旧代码可以忽略新增字段，但代码回退仍需验证缓存读写兼容。任何未来引入的数据库迁移都需重新评估，不能沿用“只退代码即可”的假设。

一次小额真实验证观察到系统选择早到期账号，且该账号最早包余额的减少与响应 usage 相符。此证据仅适用于验证记录中的一对账号和一次请求；不同资源类型的完整资格语义、外部客户端用量和并发场景仍需分别评估，不能据此声称精确预占所有上游额度。
