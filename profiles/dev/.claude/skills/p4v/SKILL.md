---
name: p4v-workflow
description: P4V(Perforce) 仓库常用工作流：生成更新日志、review changelist、筛查 bug、添加 feature。当用户要求产出开发日志、审查提交、定位 bug 或开发功能时使用。
---

# P4V 工作流

## 生成更新日志（周报 / 版本日志）

1. `p4 changes -s submitted -m 50 //depot/.../@>=YYYY/MM/DD` 取范围内 CL 列表。
2. 先用 `p4 describe -s <CL>` 按描述快速分类（少拉 diff）；需要细节再 `p4 describe -d <CL>` 或 `p4 diff2`。
3. 汇总为「新功能 / 修复 / 优化 / 资产」四类，面向非代码人员可读。
4. 需要发布：`lark-cli docs +create --title "更新日志 <范围>" --markdown ... --as bot`，把链接发回群里。

## Review 一个 changelist

1. `p4 describe -d <CL>` 获取说明 + 全量 diff。
2. 逐文件审：正确性 > 并发/生命周期 > 性能 > 风格。
3. 输出：结论（通过/有条件通过/打回）→ 问题列表（严重度 / 文件:行 / 原因 / 建议）。
4. 用户 tier 为 read 时只输出意见，不改任何文件。

## 筛查 bug

1. 从用户给的日志/现象中提取关键词，Grep 定位相关代码。
2. `p4 annotate <可疑文件>` 找相关行的最近改动 CL，`p4 describe` 看改动动机。
3. `p4 filelog <file>` 看文件演变；必要时 `p4 diff2 <file>@<cl1> <file>@<cl2>` 对比版本。
4. 输出：根因假设（按可能性排序）+ 验证方法 + 建议修复；edit tier 以上可直接改工作区。

## 添加 feature

1. 先读相关模块（Glob/Grep/Read），在回复里给出实现方案要点再动手。
2. `p4 edit` / `p4 add` 打开涉及文件（edit tier 及以上）。
3. 实现并自查；如仓库构建脚本已加入白名单则构建验证，否则说明未验证。
4. 汇总改动清单（文件 + 要点）。提交仅 submit tier，changelist 描述按归因规范。
