# ${sessionId} 会话身份转发 —— 隔离测试包（支线）

本目录是「magpie `${sessionId}` → Buddy2api 会话身份」修复的**隔离测试支线**。
与生产 `tests/` 分离，便于独立复核与合并。

## 结论摘要
- **可以翻案**：magpie 0.1.2701 起，provider `headers` 里用 `X-Buddy2api-Session-Id: ${sessionId}`
  已能把 agent 原生会话 id 端到端送到 Buddy2api 并建立稳定、可区分的亲和身份。
  旧「magpie 无法传输身份」只对 0.1.1549/1587 成立。
- **风险 1**：无原生 session 时 `${sessionId}` 填**常量** fallback（跨连接恒定）→ 无 id 流量
  塌缩为同一身份（实测 3 请求 distinct=1、binding=1）。启用前必须保证每条路径有真实 id。
- **风险 2**：provider `headers` 会**覆盖/剥离**客户端自带的 `X-Buddy2api-Session-Id`（防伪造）。

详细结论与边界见 `../../verification/sessionid-placeholder/conclusion.md`（本地，不推送）。

## 内容
- `test_sessionid_forwarding.py`：**自包含**复现（仅依赖 stdlib + httpx + 本机 magpie），
  直接起隔离 magpie + 原始 tap，验证三条：同会话同 id / 不同会话不同 id / 无 id 常量塌缩。
- 生产全量用例仍在 `tests/test_session_affinity_magpie_path.py`（本支线已扩展
  `isolated_magpie(headers=)` + 5 个 `${sessionId}` 用例；旧 `buddy2apiIdentity` 用例标 `xfail`）。

## 如何运行
```bash
# 自包含复现（推荐先跑，证明机制）
BUDDY_AFFINITY_MAGPIE_BINARY=/Applications/magpie.app/Contents/MacOS/magpie \
  python docs/testing/sessionid-placeholder/test_sessionid_forwarding.py

# 生产全量用例（需 pytest + requirements 环境；mac 上 nested sandbox-exec 需放行）
BUDDY_AFFINITY_MAGPIE_BINARY=/Applications/magpie.app/Contents/MacOS/magpie \
BUDDY_AFFINITY_EVIDENCE_DIR=/tmp/affinity-evidence \
  python tests/offline_runner.py -q tests/test_session_affinity_magpie_path.py
```

## 已验证结果
- 自包含复现：3/3 PASS。
- 全量用例（2026-11-02）：`12 passed, 1 skipped (Pi CLI 缺失), 3 xfailed (旧补丁已删)`。
