# 独立会话账号亲和

默认关闭，仅支持标准单进程 Buddy2api 的 `workbuddy`、`workbuddy-i` 独立账号池。它尽可能复用同一会话的账号，不需要前置代理，不管理聊天历史，也不保证缓存命中、积分节省、消息顺序或 exactly-once。

## 独立输入契约

客户端向 Chat Completions 或 Responses 入口正常使用 API Key 鉴权，并可选发送：

```http
X-Buddy2api-Session-Id: conversation-001
X-Buddy2api-Session-Namespace: editor
```

会话创建时保存一个逻辑 ID，每轮、重试、恢复都复用；独立子代理用自己的 ID。不要每请求生成新 ID，也不要给所有会话配置一个共享常量。

ID 为1–256个 ASCII 字母、数字或 `._~-`，区分大小写。namespace 可选，最长64个相同字符集字符，省略时采用该 Key 配置的值，未配置为 `default`。显式 namespace 必须在该 Key 的允许列表中。

特性开启且请求适用时，显式头为空、重复（同值也不允许）、含空白/逗号/非法字符、超长、孤立或未授权 namespace，返回400 `invalid_session_identity`，早于预检查、额度预留及上游调用。非法显式头不会回退到其他字段。

身份优先级：显式契约 > 为该已鉴权 Key 明确配置的 native 契约 > 无亲和降级。显式值与 native/缓存值不同不算冲突，因为它们不是天然别名。请求体内同名字段或 `_session_affinity` 无法注入内部身份。

绑定域包含 API Key 行ID、namespace、契约来源/版本、协议scope、Provider账号池和会话ID。显式契约在Chat/Responses间共享scope；native各自隔离。不同Key同ID互不影响。同Key的不同客户端宜使用不同namespace，但namespace不是防伪边界；需要互不信任的调用方隔离时应分配不同Key。

不使用请求ID、trace ID、`previous_response_id`、API Key、IP、缓存键或消息正文哈希推导会话。无法确认身份就按原调度执行，不查找一个“看起来相似”的旧绑定。

## 可选 native 契约

管理员必须为具体Key选择契约，不根据User-Agent或客户端名称自动启用：

| 契约 | 仅适用于Responses的输入 | 缺失/冲突行为 |
|---|---|---|
| `pi-ai-responses-session-v1` | 一个合法的 `session_id` 头 | 缺失、重复、非法则降级 |
| `codex-responses-session-v1` | 一个合法的 `session-id` 头；若有 `thread-id`，必须一个且同值 | 缺失、重复、非法或镜像不同则降级；不单靠thread-id回退 |

同一Key只能选择一个native契约；混用不同native来源时应分Key或采用显式契约。

这些契约曾由受控客户端生命周期验证：DSH 0.2.0-rc.2 Responses+short、Pi 1.2.0 Responses默认short，以及Codex CLI 0.162.1的新建/恢复/分叉。它们不是对所有版本、Desktop、扩展子代理或缓存模式的保证。DSH缓存none/普通Chat及Pi普通Chat可能不发送稳定身份，必须使用独立显式契约或降级；不能用缓存键替代。

原生行为未知的客户端可直接实现本页显式头契约。经过代理时，代理须保留相应字段；被过滤即降级，HTTP200不代表亲和命中。代理兼容属于独立工作项，不构成核心运行依赖。

## 配置

通过现有管理设置接口保存完整的 `session_affinity` 对象。下面仅是配置示例，`123`为示例API Key行ID，保存前须选择自己的调用方与namespace；文档不会自动启用功能：

```json
{
  "session_affinity": {
    "enabled": true,
    "idle_ttl_seconds": 86400,
    "capacity": 10000,
    "per_caller_capacity": 1000,
    "touch_interval_seconds": 60,
    "busy_timeout_ms": 25,
    "profiles": {
      "123": {
        "namespace": "editor",
        "namespaces": ["editor", "terminal"],
        "native": "pi-ai-responses-session-v1"
      }
    }
  }
}
```

配置缺失时默认 `enabled=false`，其余默认值同上，profiles为空。显式头不要求native profile。配置拒绝未知字段、不合法范围或未知契约；idle TTL为60秒至30天，capacity/per-caller为1至100000，touch粒度1至3600秒且小于TTL，SQLite busy timeout为1至100毫秒，per-caller不大于总容量。

这些是状态保留与资源参数，不是上游缓存TTL或积分临期阈值。25ms不是端到端请求耗时上界；磁盘、线程池和系统调度仍有开销，性能预算必须单独验收。

## 选号策略 B：保护已有会话

1. 先检查同池active账号及既有硬排除/冷却，人工priority优先。
2. 绑定仍可用且处于当前最高人工priority组时保留；weight、累计用量、其他会话的通道粘性、较早临期包或快照失效不会单独抢走绑定。
3. 首次绑定或绑定确实不可保留时，复用现有临期分组、通道粘性及排序；亲和选号不更新原通道粘性。
4. 仅被本次 `tried_ids` 排除、全局仍合格的绑定只临时绕行，不永久迁移。
5. token恢复后，亲和请求重新经过候选及绑定校验；不改原有stop/expire/rotate、尝试上限或已输出后不重放的边界。

功能关闭、匿名、无可靠身份、非适用Provider均使用旧调度，不创建、迁移或touch绑定。预检查、目录刷新和只读预览不携带会话身份，不改变绑定。没有新增余额硬排除、紧急临期抢占或Provider熔断。

## 状态与故障边界

SQLite小表仅保存HMAC绑定键、调用方/账号行ID、Provider、generation及时间戳，不保存原始会话ID、API Key或prompt。内部随机secret通过现有凭据加密层保存，管理响应隐藏它，日志/调试转储不输出内部身份。

首次绑定与迁移在短事务中先提交再调用上游。单进程内首次选择原子化，迁移使用generation/CAS；迟到成功/失败不写绑定，不能把新绑定改回旧账号。已在途请求允许完成，亲和不控制同会话消息顺序。

新建/迁移持久失败在下一次POST前终止。初次流式分配在响应头前完成，失败可返回503；流开始后只能按现有SSE错误风格结束，不再提交下一账号。现有客户端额度预留语义保留，存储失败不等于自动返还已预留次数。

热命中按touch粒度更新；持久touch失败可继续使用仍有效的内存分配。没有逐SSE事件读写。容量不足且候选淘汰项都在途时，可降级原选号，不承诺该次亲和；调试原因不包含身份值。

TTL按最近分发访问计时，活动请求由lease保护；结束/异常/取消释放，不用逐事件touch维持流。重启恢复最后持久化的有效绑定，可能损失一个touch粒度的活跃时间；不恢复上游缓存和进程内冷却。

删除账号或Key级联删除绑定并使本进程缓存失效。更换namespace/来源域形成新键，旧记录待TTL/LRU回收。secret更换需重启重建索引，不支持在线轮换。多worker/多实例/外部共享数据库不在支持范围；SQLite唯一键不能替代分布式调度协调。

## 验收与回滚

[核心验收记录](../verification/session-affinity-core-20261025.md)给出本轮命令、生命周期与安全/差分映射。核心ASGI与Mock上游测试不安装、导入或启动任何前置代理。隔离生命周期测试实际运行server.main，覆盖绑定恢复、重复启动、数据库锁、旧Cookie及删除/TTL/域变化。

关闭 `session_affinity.enabled` 即恢复后续请求的旧选号路径。保留新增表即可，不必删除账号、额度或历史数据；已提交的在途请求允许完成。生产启用、部署、重启另行决定。
