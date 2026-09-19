# 服务器部署入口

这是服务器 AI 在 **Git clone 完成后首先读取** 的入口。支持环境：Alibaba Cloud Linux 3、systemd；Git 由管理员预先安装。

## 从零开始时给服务器 AI 的信息

服务器在 clone 之前看不到本文件，因此首次指令直接包含仓库地址。私有仓库应使用服务器部署密钥或凭证助手，不要把 token 写进 URL 或聊天：

```text
Git 已安装。请在 Alibaba Cloud Linux 3 上部署飞书助手。
仓库地址：https://github.com/488338478/Feishu-Assisant.git
分支：master

先将仓库 clone 到 /srv/agent/assistant，再完整阅读仓库根目录的 SERVER_DEPLOY.md 和 deploy/AI_DEPLOY_ALINUX3.md，然后严格按“首次部署”执行。任何命令非零退出时停止，不要删除既有目录，不要输出密钥。完成后按文档的 AI 交付格式汇报。
```

服务器 AI 应先执行以下等价步骤：

```bash
export REPO_URL='https://github.com/488338478/Feishu-Assisant.git'
export DEPLOY_REF='master'
test -n "$REPO_URL"
test -n "$DEPLOY_REF"
command -v git
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/agent
test ! -e /srv/agent/assistant
git clone --branch "$DEPLOY_REF" --single-branch "$REPO_URL" /srv/agent/assistant
cd /srv/agent/assistant
git rev-parse HEAD
cat SERVER_DEPLOY.md
cat deploy/AI_DEPLOY_ALINUX3.md
```

读完两个文件后执行：

```bash
sudo bash deploy/install-alinux3.sh
sudoedit /srv/agent/assistant.env
sudo bash deploy/install-alinux3.sh --start
```

`assistant.env` 需要人工或受控密钥系统填写，服务器 AI 不应要求把密钥发到聊天中。飞书控制台权限和上线验收继续按 [完整部署手册](deploy/AI_DEPLOY_ALINUX3.md) 执行。

## 已存在代码目录

如果 `/srv/agent/assistant` 已经存在，不要再次 clone、删除目录或覆盖文件。进入目录，确认 `git status --short` 为空，然后阅读 [完整部署手册](deploy/AI_DEPLOY_ALINUX3.md) 的“升级”章节。工作区不干净时停止并报告文件列表。
