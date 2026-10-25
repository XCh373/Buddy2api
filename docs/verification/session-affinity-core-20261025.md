# 独立会话账号亲和核心验收（2026-10-25）

范围：Buddy2api独立核心；不包含前置代理兼容、生产启用、部署或厂商计费验证。起点为main的`d291cbdb7817b9cd0ed6e26a58da986fcf60eef2`，与10月24日相同，origin/main刷新后也相同。主题分支`codex/session-affinity-core`。

本轮先审查源码diff与文件归属，再执行测试。历史626/645项数字不作为本轮结果。六份无关事件材料、历史本机回奏及客户端/代理探针保留在原工作树，不进入核心提交；旧代理补丁分支未操作。发布副本仅由明确列出的暂存文件及已有版本文件生成。

## 本轮实际验证

```sh
python -m compileall -q .
python tests/offline_runner.py -q tests/test_session_identity.py tests/test_session_affinity.py tests/test_session_affinity_integration.py tests/test_session_affinity_service_lifecycle.py
python tests/offline_runner.py -q -k 'not test_repeated_start_and_restart'
git diff --check
```

初次本地核心门禁为61项通过，后续CI发现并修复流关闭缺陷；该初次数字不作为最终验收。完整隔离回归已在仅含待提交内容的独立副本执行；CI保留必需检查名`test suite`，使用相同离线入口，不跳过批准流程。

| 验收目标 | 直接测试证据 |
|---|---|
| 直连Chat/Responses × stream/non-stream；同会话连续、恢复及不同会话隔离 | `test_direct_actual_chain_preserves_binding_and_no_leaks`；`test_isolation_and_scope` |
| 内部身份跨协议转换、上下文裁剪、预算重试、token恢复 | `test_router_private_identity_survives_bridge`；`test_budget_retry_and_responses_trim_keep_identity`；`test_expired_token_fallback_and_stop` |
| 策略B及priority/冷却/池/临期/weight/tried_ids边界 | `test_highest_priority_cooldown_and_no_return_to_recovered_account`；`test_weight_usage_and_invalid_snapshot_never_steal_binding`；`test_priority_migration_local_exclusion_and_late_result`；`test_provider_pool_and_shared_key_isolation` |
| 关闭/无身份/不可信来源/匿名/不适用池降级，不touch状态 | `test_disabled_and_missing_identity_differential_reference`；`test_missing_identity_and_disabled_endpoint_quota_error_equivalence`；`test_legacy_and_unverified_fields`；`test_anonymous_ignores_identity_and_keeps_old_request_behavior` |
| 预检查、模型目录及预览不修改绑定 | `test_noninference_never_touches_binding` |
| 并发首次绑定、迁移、迟到成功/失败、取消、容量与存储故障 | `test_atomic_first_bind_and_restart`；`test_concurrent_asgi_first_requests`；`test_real_concurrent_migration_late_result_cannot_restore_old_binding`；`test_cancellation_during_lease_acquisition_does_not_leak_pin`；状态/存储故障测试 |
| 上游/响应/错误/日志/调试不泄漏身份，secret隐藏 | `test_direct_actual_chain_preserves_binding_and_no_leaks`；`test_invalid_identity_before_precheck_or_quota`；`test_debug_redaction_including_native_cache_hints`；`test_secret_hidden_and_minimal_browser_cors` |
| 完成/上游错误结束、已开始流关闭，不依赖GC释放pin | `test_completed_response_releases_pin_without_generator_gc`；`test_started_stream_close_propagates_to_pin_without_gc` |
| 真实HTTP服务启动→绑定→停止→重启→恢复原账号与generation | `test_isolated_service_start_bind_stop_restart_and_lifecycle`，实际server.main/lifespan，临时SQLite与7个合成账号 |
| 账号/Key删除、namespace变化、持久TTL边界 | 同一生命周期测试；`test_secret_change_and_caller_scope` |

初次本地完整隔离回归628项通过、1项排除；但PR首次Python 3.12 CI为627通过、1失败、1排除，因此未合并。

根因已用保留迭代器强引用的测试在本地稳定复现：Responses解析器遇到DONE/错误事件提前退出，没有显式关闭下层Chat流；活动绑定引用依赖异步生成器垃圾回收时点。最小修复在Responses、上下文预算包装、模型回写包装中传递aclose，lease释放使用嵌套finally；没有改变选号、重试上限或输出内容。新增跨协议正常/错误结束及已开始流显式关闭回归，断言不借助GC释放pin。修复后核心门禁 **67 passed / 0 failed / 0 skipped**（56.57秒）；完整隔离回归 **634 passed / 0 failed / 0 skipped / 1 deselected**（104.75秒），compileall与diff检查通过。最终以这两组结果及更新后PR CI为准。

旧645项包含本地客户端/代理探针，本轮独立核心提交不包含这些探针，不能直接用总数比较覆盖强弱。

## 生命周期与隔离说明

生命周期用6次实际服务启动及4次重复/冲突启动探针，所有子进程有超时和finally清理。首次和重启后都检查旧Cookie仍可访问本机管理、同端口重复启动返回已有实例、同库不同端口被拒绝；再检查账号/Key删除、namespace变化和TTL。

旧`tests/test_local_access.py::test_repeated_start_and_restart`仍排除：其原始入口无出口审计，不能用于本机严格离线验收。本轮隔离测试已覆盖它的两轮启动、旧Cookie、重复实例及数据库锁目标，并增加会话绑定恢复；两者启动包装不同，不能声称执行过旧测试本身。调试器事件循环、远程监听鉴权等原有独立测试仍在完整回归中。

离线入口使用临时数据库、合成主密钥与空凭据目录，Python审计hook默认拒绝未登记connect/DNS/bind和真实凭据/生产数据库访问。服务子进程在导入server前安装同一审计hook，macOS再加系统隔离；其他系统不依赖macOS工具。只登记临时本地启动/健康检查端口，WorkBuddy HTTP精确限制为合成目标并由MockTransport响应，token/目录等路径使用Mock。临时会话和日志放在本轮TemporaryDirectory中，结束自动回收。

## 产品边界与回滚

默认关闭，仅标准单进程及WorkBuddy两个独立账号池。绑定不是消息串行、exactly-once或缓存命中承诺；容量/TTL回收、来源域变化可能形成新分配。多实例与生产负载/端到端性能不在本轮承诺内，性能预算未裁决。

关闭`session_affinity.enabled`恢复后续请求旧选号，不需删除绑定表或改账号/额度数据。未修改生产配置/数据库、未启用亲和、未部署、未重启生产服务、未发计费请求。合并代码不代表启用或发布版本。
