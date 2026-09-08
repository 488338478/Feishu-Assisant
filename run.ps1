$ErrorActionPreference = "Stop"

# 脚本位于 assistant 包目录，因此项目根目录是其父目录。
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

python -m assistant.main
