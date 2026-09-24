**note-sync-hub：Mac 启动**

双击本工程的 `启动Mac.command`。首次换电脑时先运行 `./setup-macos.sh`，它只管理本工程的 `.venv`。

保留 Windows 原入口。Mac 使用自己的依赖环境，不复用 Windows `.venv`。
