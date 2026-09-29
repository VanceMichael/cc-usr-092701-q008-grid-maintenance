# 输电检修窗口管控

集中检修期的作业管控领域服务：接收线路、区段、设备、人员资质、风险票、工序依赖、
检测结果与缺陷整改，按安全隔离范围分配停电窗口，锁定现场确认，并在全部作业闭环后
才允许调度员恢复送电。纯 Python 3.11+ 标准库实现，无第三方依赖。

## 目录结构

```
src/grid_maintenance/
  context.py    领域上下文资料读取与校验（原有）
  model.py      实体与状态枚举（人员/资质、线路/区段/铁塔/设备、风险票、
                窗口、工序、检测证据、冲突、缺陷、故障、放行依据）
  errors.py     违反管控规则时抛出的具体异常
  ledger.py     只追加的命令台账与幂等回执（重复回执回放、重放比对）
  service.py    ControlService：全部业务规则与状态机的唯一入口
  tracing.py    线路总览追溯：线路→区段→铁塔→设备→试验/缺陷→放行依据
  demo.py       天林Ⅰ线一个完整停电窗口的端到端演示
tests/
  test_context.py  上下文资料测试（原有）
  test_service.py  作业管控规则测试（21 个用例）
```

## 规则与实现对应

| 业务要求 | 实现位置 |
| --- | --- |
| 按安全隔离范围（区段）分配窗口，同区段时间不得重叠 | `allocate_window` |
| 窗口停电前可调整；停电/现场确认后锁定不可改 | `reschedule_window` |
| 仅调度员可批准停、送电；现场确认核对风险票并锁定隔离边界 | `approve_deenergization` / `confirm_isolation` |
| 承包队只能更新本队作业，开工需有效资质与前序闭环 | `start_operation` |
| 检测结果只追加：新增缺陷暂停后续工序，探伤等结论永不消失 | `record_evidence` / `_raise_defect` |
| 人工检查与无人机结论冲突：双证据保留并升级复核，第三方判定 | `_detect_conflict` / `resolve_conflict` |
| 缺陷整改后由他人验收，不得验收自己整改的缺陷；驳回可重改 | `rectify_defect` / `accept_defect` |
| 中途故障暂停窗口全部作业，排除后恢复，结论不撤销 | `raise_incident` / `resolve_incident` |
| 任一作业/缺陷/冲突/故障/风险票未闭环不得送电 | `approve_restoration` / `_assert_all_closed` |
| 重复回执回放、同键不同内容判重放、禁止重复送电 | `ledger.Ledger` + `_run` 幂等包裹 |
| 从线路追到每基铁塔、每项试验、整改责任人与放行依据 | `tracing.build_overview` / `render_overview` |

送电通过后生成不可变的 `ReleaseBasis` 快照（作业数、缺陷数、证据数、故障数、
风险票、批准人、批准时间、触发命令号），作为最终放行依据留存。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译

```bash
python3 -m compileall -q src tests
```

## 命令行

```bash
# 领域资料摘要
python3 -m src.grid_maintenance.context fixtures/context.json

# 端到端管控演示（窗口分配→停送电锁定→五道工序→冲突升级→缺陷闭环→故障暂停→放行追溯）
python3 -m src.grid_maintenance.demo
```
