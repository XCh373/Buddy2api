# GitHub 发布流程

本仓库采用单人维护、CI 强制验收的流程。日常开发不直接推送 `main`。

1. 从最新 `main` 创建主题分支，在分支上完成修改。
2. 本地运行 `python -m compileall -q .` 和 `python tests/offline_runner.py -q -k 'not test_repeated_start_and_restart'`。隔离器默认拒绝网络与生产凭据访问；旧启动测试由 `test_isolated_service_start_bind_stop_restart_and_lifecycle` 在临时数据库、登记端口和 Mock 上游下覆盖重启、重复启动、数据库锁与旧浏览器 Cookie 行为。未纳入发布的本地客户端/代理探针应在独立目录运行，不属于核心验收。
3. 检查提交清单，排除凭据、数据库、账号材料、备份和运行输出。
4. 推送主题分支并创建 Pull Request。
5. 等待必需的 `test suite` 检查变绿，再合并到 `main`。

不要求第二个人批准；CI 通过是不能跳过的批准条件。

发布时，在 GitHub Actions 页面运行 `Create release`，选择 `main` 并填写新的 `vMAJOR.MINOR.PATCH`。工作流会复核 CI、创建不可变 tag 和 GitHub Release。不要手工创建、移动或覆盖公开 tag。
