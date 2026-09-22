# a-stock-screen

个人同业选股脚本，用当前 TuShare 基础信息、同日 PB 与连续三年 `roe_waa` 安排研究顺序。它不是买入建议、收益模型或 Framework A 的延续。

正式范围见 `a-stock-tracker` 的[路线图](https://github.com/on195594/a-stock-tracker/blob/master/docs/plans/2026-09-22-personal-stock-selection-roadmap.md)和[实施 Spec](https://github.com/on195594/a-stock-tracker/blob/master/docs/specs/2026-09-22-peer-screen-spec.md)。当前已实现同业发现、候选审查和显式指定旧快照的变化跟踪；本人已接受“国投电力继续研究，甘肃能源、湖北能源暂不研究”作为第二次交付判断，该判断不是交易结论。

## 使用

```bash
# 显式联网刷新并生成报告
../a-stock-tracker/.venv/bin/python screen.py \
  --tracker-root ../a-stock-tracker --anchor 600900.SH --refresh

# 刷新并与明确指定的旧快照比较
../a-stock-tracker/.venv/bin/python screen.py \
  --tracker-root ../a-stock-tracker --anchor 600900.SH --refresh \
  --compare output/<旧运行目录>/snapshot.json

# 完全离线复看；也可同时与明确指定的旧快照比较
../a-stock-tracker/.venv/bin/python screen.py \
  --input output/<当前运行目录>/snapshot.json \
  --compare output/<旧运行目录>/snapshot.json
```

只有 `--refresh` 允许联网。旧 SQLite 仅以 `mode=ro` 读取；输出写入被 Git 忽略的 `output/`。

## 验证

```bash
../a-stock-tracker/.venv/bin/python -m pytest test_screen.py -q
../a-stock-tracker/.venv/bin/ruff check screen.py test_screen.py
../a-stock-tracker/.venv/bin/ruff format --check screen.py test_screen.py
../a-stock-tracker/.venv/bin/mypy --ignore-missing-imports screen.py
```
