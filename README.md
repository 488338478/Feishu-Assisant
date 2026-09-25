# 飞书团队助手

通过飞书文字消息调用 Claude Code CLI，结合 lark-cli 操作飞书资源；可按用户和群切换为文档、毕设或 P4 研发助手。

当前定位：已有消息、工具执行、授权和调度骨架的内部试用版本。能力是否可用还取决于飞书权限、CLI 认证和工作区配置。

## 文档入口

| 文档 | 读者与用途 |
|---|---|
| [服务器部署入口](SERVER_DEPLOY.md) | 服务器 AI 从 Git clone 开始的入口提示词与首轮命令 |
| [操作手册](docs/USER_GUIDE.md) | 普通用户的指令示例、管理员启动与配置、常见问题 |
| [功能现状与开发方向](docs/STATUS_AND_ROADMAP.md) | 2026-09-25 按代码和服务器验收结果核对的功能清单、限制和路线讨论 |
| [架构说明](ARCHITECTURE.md) | 模块职责、消息链路和原始设计；部分内容已过时，见现状文档 |
| [架构图](ARCHITECTURE_DIAGRAM.md) | 架构概览；旧图未完整覆盖按需授权和群聊 @ 门控 |
| [服务器部署指南](deploy/README.md) | Linux 部署模板；结合操作手册中的更新说明使用 |
| [按需授权设计](docs/superpowers/specs/2026-09-09-lark-on-demand-auth-design.md) | 授权流程设计意图，实际限制见现状文档 |
| [原始重构计划](PLAN.md) | 历史设计记录，不代表当前待办列表 |

## 开始使用

机器人启动后，私聊直接发送文字；群聊使用飞书真正的 @ 功能选择机器人，然后发送「我的权限」或「查看配置」。群内普通消息不会触发回复，但会在应用具备“接收群聊中所有消息”权限时进入当前群的本地检索库；AI 仅在判断信息不足时按需读取，禁止跨群检索。文字消息保持原文，非文字消息保存事件中的原始 content。

当前主要能力：

- profile 按 `user_profiles` → `chat_profiles` → `default_profile` 解析，read/edit/submit tier 再独立收窄工具权限。
- 支持在飞书发送「切换模型 DeepSeek 4.1」「切换模型 DeepSeek 4.0 Pro」；模糊切换意图会返回明确指令。
- 同群共享群聊记录和业务记忆，Claude resume 链按 profile 隔离；失效 session 会自动清除并重试一次。
- 会话接近估算上下文上限时会先询问；用户回复「确认压缩」后执行 Claude Code `/compact` 并继续原请求，回复「取消压缩」则保留原会话。
- dev 模式通过 P4 CLI 和工作区文件访问代码，不操作 P4V 图形界面。服务器部署脚本会验证 agent 写权限、dev Write/Edit 配置和 systemd 写路径。

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

2026-09-25 核对：156 个测试通过，覆盖消息门控、群聊原文检索与跨群拒绝、profile/tier 路由、会话恢复、上下文压缩确认、模型切换、授权、评论和部署配置。Alibaba Cloud Linux 3 上的 systemd、P4 工作区写权限及服务启动已通过部署脚本验收；测试仍不能替代每个飞书业务权限和多人身份场景的在线验收。
