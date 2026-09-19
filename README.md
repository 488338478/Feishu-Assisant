# 飞书团队助手

通过飞书文字消息调用本机 Claude Code CLI，结合 lark-cli 操作飞书资源；可配置为毕设文件助手或 P4 研发助手。

当前定位：已有消息、工具执行、授权和调度骨架的内部试用版本。能力是否可用还取决于飞书权限、CLI 认证和工作区配置。

## 文档入口

| 文档 | 读者与用途 |
|---|---|
| [服务器部署入口](SERVER_DEPLOY.md) | 服务器 AI 从 Git clone 开始的入口提示词与首轮命令 |
| [操作手册](docs/USER_GUIDE.md) | 普通用户的指令示例、管理员启动与配置、常见问题 |
| [功能现状与开发方向](docs/STATUS_AND_ROADMAP.md) | 2026-09-10 按代码核对的功能清单、实际配置、限制和路线讨论 |
| [架构说明](ARCHITECTURE.md) | 模块职责、消息链路和原始设计；部分内容已过时，见现状文档 |
| [架构图](ARCHITECTURE_DIAGRAM.md) | 架构概览；旧图未完整覆盖按需授权和群聊 @ 门控 |
| [服务器部署指南](deploy/README.md) | Linux 部署模板；结合操作手册中的更新说明使用 |
| [按需授权设计](docs/superpowers/specs/2026-09-09-lark-on-demand-auth-design.md) | 授权流程设计意图，实际限制见现状文档 |
| [原始重构计划](PLAN.md) | 历史设计记录，不代表当前待办列表 |

## 开始使用

机器人启动后，私聊直接发送文字；群聊使用飞书真正的 @ 功能选择机器人，然后发送「我的权限」或「查看配置」。群内普通消息不会触发回复，但会在应用具备“接收群聊中所有消息”权限时进入当前群的本地检索库；AI 仅在判断信息不足时按需读取，禁止跨群检索。文字消息保持原文，非文字消息保存事件中的原始 content。

Windows 可运行 `start-assistant.bat`；终端启动需在本包父目录执行：

```powershell
python -m assistant.main
```

首次使用请先按[操作手册](docs/USER_GUIDE.md)配置凭证和 CLI。启动脚本不会自动安装依赖或读取 `.env`。

## 本地验证

在本目录执行：

```powershell
python -m unittest discover -s tests -v
```

2026-09-10 核对：34 个测试通过，主要覆盖消息门控、身份判断、授权流程和配置同步。它们使用 mock，不等于飞书、Claude、定时任务或 P4 端到端验收通过。
