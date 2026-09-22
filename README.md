# a-stock-screen

个人同业选股脚本，用当前 TuShare 基础信息、同日 PB 与连续三年 `roe_waa` 安排研究顺序。它不是买入建议、收益模型或 Framework A 的延续。

正式范围见 `a-stock-tracker` 的[路线图](https://github.com/on195594/a-stock-tracker/blob/master/docs/plans/2026-09-22-personal-stock-selection-roadmap.md)和[实施 Spec](https://github.com/on195594/a-stock-tracker/blob/master/docs/specs/2026-09-22-peer-screen-spec.md)。当前已实现同业发现和候选审查；变化跟踪尚未启动。

## 使用

```bash
# 显式联网刷新并生成报告
../a-stock-tracker/.venv/bin/python screen.py \
  --tracker-root ../a-stock-tracker --anchor 600900.SH --refresh

# 完全离线复看已有快照
../a-stock-tracker/.venv/bin/python screen.py \
  --input output/<运行目录>/snapshot.json
```

只有 `--refresh` 允许联网。旧 SQLite 仅以 `mode=ro` 读取；输出写入被 Git 忽略的 `output/`。

## 验证

```bash
../a-stock-tracker/.venv/bin/python -m pytest test_screen.py -q
../a-stock-tracker/.venv/bin/ruff check screen.py test_screen.py
../a-stock-tracker/.venv/bin/ruff format --check screen.py test_screen.py
../a-stock-tracker/.venv/bin/mypy --ignore-missing-imports screen.py
```
