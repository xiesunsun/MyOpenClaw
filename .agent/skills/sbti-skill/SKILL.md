---
name: sbti-skill
description: Use when the user asks to "跑一下 SBTI 测试", "做 SBTI 人格测评", "score SBTI answers", "analyze this persona with SBTI", "看看 Claude 自己是什么 SBTI", "answer the SBTI questionnaire", or otherwise needs the bundled `sbti.md` questionnaire and `sbti_skill.py` scorer in this directory.
---

# SBTI Self-Test

使用此技能完成本目录内的 SBTI 问卷阅读、答案整理、结果计算与简短解释。

## 目标

针对以下任一对象生成结构化结果：

- 当前 Claude / agent 的默认风格
- 用户指定的 persona
- 用户直接给出的答案 JSON

## 核心流程

1. 读取题目。
   - 优先读取 `sbti.md`
   - 必要时执行 `python3 sbti_skill.py dump`

2. 确定答题对象。
   - 若用户指定 persona，则按该 persona 作答
   - 若用户已提供答案，则直接进入评分
   - 若用户要求“测一下 Claude 自己”，则按当前助手风格作答，并明确结果带有主观性

3. 生成答案 JSON。
   - 必答键：`q1` 到 `q30`、`drink_gate_q1`
   - 允许值：`A/B/C/D` 或题目对应数值
   - 仅当 `drink_gate_q1 = 3` 时加入 `drink_gate_q2`

4. 运行评分。
   - `python3 sbti_skill.py score --answers-file answers.example.json`
   - 或 `python3 sbti_skill.py score --answers '{"q1":"B", ...}'`
   - 需要结构化结果时，优先加上 `--json`

5. 输出结果。
   - `finalType`
   - `top3`
   - `dimSummaries`
   - 基于脚本输出给出简短解释

## 执行规则

- 不漏题。
- 不在未评分时凭空编造最终类型。
- 需要解释时，基于 `dimSummaries` 做简洁总结，不夸大结论。
- 用户只要结果时，优先简短返回；用户要过程时，再展示答案 JSON 与计算命令。

## 常用命令

```bash
uv run python /Users/ssunxie/code/myopenclaw/.agent/skills/sbti-skill/scripts/sbti_skill.py dump
uv run python /Users/ssunxie/code/myopenclaw/.agent/skills/sbti-skill/scripts/sbti_skill.py interactive
uv run python /Users/ssunxie/code/myopenclaw/.agent/skills/sbti-skill/scripts/sbti_skill.py score --answers-file /Users/ssunxie/code/myopenclaw/.agent/skills/sbti-skill/answers.example.json
uv run python /Users/ssunxie/code/myopenclaw/.agent/skills/sbti-skill/scripts/sbti_skill.py score --answers '<json_string>' --json
```

## 关键文件

- `plan.md`：保留的项目计划文件
- `sbti.md`：完整题库
- `sbti_skill.py`：评分与导出脚本
- `answers.example.json`：答案示例
