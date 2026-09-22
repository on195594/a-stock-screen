# a-stock-screen 开发约束

修改前阅读 `README.md`，并以相邻 `a-stock-tracker` 中的正式路线图与 Spec 为准。

- 本工具只做个人同业发现、候选审查，以及明确按需启动的快照变化跟踪。
- 不恢复 Framework A，不增加交易、回测、模型、通知、定时任务或新数据库。
- `a-stock-tracker` 的数据库只能用 SQLite `mode=ro` 读取；不得修改其代码、配置、实验数据或输出。
- 只有 `--refresh` 可以联网；离线重放不得读取现场数据或覆盖旧输出。
- 凭据、`output/` 和本地缓存不得提交。
- 变化跟踪只能由用户显式提供 `--compare` 旧快照；不得自动猜测 latest、定时运行或通知。

验证：

```bash
../a-stock-tracker/.venv/bin/python -m pytest test_screen.py -q
../a-stock-tracker/.venv/bin/ruff check screen.py test_screen.py
../a-stock-tracker/.venv/bin/ruff format --check screen.py test_screen.py
../a-stock-tracker/.venv/bin/mypy --ignore-missing-imports screen.py
```
