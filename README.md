# 输电检修窗口管控

本项目保存输电检修窗口管控所需的领域上下文和校验契约，便于服务端功能围绕真实业务参与方展开。当前版本只提供资料读取、结构校验和命令行摘要，数据均为演示用虚构内容。

## 参与方

检修负责人、基建队伍、运维调度员、验收人员

## 事实资料

- 四川500千伏天林Ⅰ、Ⅱ线改造工程完成投运
- 工程包含新建铁塔、线路、避雷器、故障诊断装置和绝缘子更换
- 施工采用检修、X光探伤、即时消缺以及人工精检和无人机巡检

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译

```bash
python3 -m compileall -q src tests
```

## 命令行检查

```bash
python3 -m src.grid_maintenance.context fixtures/context.json
```
