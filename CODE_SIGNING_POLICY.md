# Code signing policy / 代码签名政策

## Current status / 当前状态

Note Sync Hub is preparing an application to the SignPath Foundation open-source
code-signing program. The current `v1.2.0` Release is not code-signed. This
policy describes the controls that apply to future signed Windows releases and
does not claim that SignPath approval has already been granted.

Note Sync Hub 正在准备申请 SignPath Foundation 开源代码签名计划。当前
`v1.2.0` Release 尚未进行代码签名。本文说明未来 Windows 签名版本将遵循的
控制措施，不表示项目已经获得 SignPath 批准。

Planned signing attribution / 计划使用的签名声明：

> Free code signing provided by SignPath.io, certificate by SignPath Foundation.

## Project roles / 项目角色

| Role / 角色 | Account / 账号 | Responsibility / 职责 |
| --- | --- | --- |
| Committer | [@xing-skyline](https://github.com/xing-skyline) | Maintains source, tests, and build configuration / 维护源码、测试和构建配置 |
| Reviewer | [@xing-skyline](https://github.com/xing-skyline) | Reviews changes before they enter a release build / 审核进入发布构建的改动 |
| Signing approver | [@xing-skyline](https://github.com/xing-skyline) | Manually approves each formal signing request / 人工批准每一次正式签名请求 |

External pull requests must be reviewed and accepted by the maintainer before
their code can enter a release build. A pull request workflow may produce an
unsigned test artifact, but it is not a release or an approved signing input.

外部 Pull Request 必须经维护者审核并接受，相关代码才能进入发布构建。
Pull Request 工作流可以生成未签名测试产物，但该产物不是正式 Release，也不是
已经批准的签名输入。

## Build and release provenance / 构建与发布来源

- Formal signed artifacts will be built from this public GitHub repository by a
  GitHub-hosted Windows runner using the repository's public workflow and build
  script.
- The workflow installs declared dependencies, runs the unit tests and Ruff,
  builds the PyInstaller executable, and verifies its Windows product and
  version metadata.
- The unsigned build artifact is submitted to the signing service only from the
  verifiable release workflow. Each formal signing request requires manual
  approval by the signing approver.
- The final SHA-256 checksum is calculated only after signing. A signed
  executable is never published with the checksum of its unsigned input.
- Before SignPath integration is approved, GitHub Actions produces only an
  unsigned build artifact and does not automatically publish a Release.

- 正式签名产物将由 GitHub 托管的 Windows Runner 从本公开仓库构建，构建过程
  使用仓库中公开的工作流和构建脚本。
- 工作流安装已声明的依赖，运行单元测试和 Ruff，构建 PyInstaller EXE，并验证
  Windows 产品与版本元数据。
- 只有可验证的正式发布工作流生成的未签名产物才会提交给签名服务；每次正式
  签名请求均须由 Signing approver 人工批准。
- 最终 SHA-256 仅在签名完成后计算，签名 EXE 不会沿用未签名输入的哈希。
- 在 SignPath 集成获批前，GitHub Actions 只生成未签名构建产物，不会自动发布
  Release。

## Privacy and data access / 隐私与数据访问

Note Sync Hub does not send telemetry, usage analytics, note contents,
credentials, or configuration data to the project maintainer. It accesses only
the Joplin and SiYuan API endpoints and the local Obsidian directory that the
user explicitly configures and actively chooses to use. It contains no
advertising, bundled software, or hidden data collection.

Note Sync Hub 不会向项目维护者发送遥测、使用统计、笔记内容、凭据或配置数据。
程序只访问用户明确配置并主动选择使用的 Joplin、思源 API 端点以及本地
Obsidian 目录。程序不包含广告、捆绑软件或隐藏的数据收集。

## Key and token management / 密钥与令牌管理

If the application is accepted into the SignPath Foundation program, the
certificate private key will be generated and protected by the signing
service's hardware security module (HSM). The private key will not be available
to the maintainer.

Certificate private keys and API tokens are never committed to this repository,
embedded in source code or build artifacts, or printed in ordinary workflow
logs. A future SignPath API token, if required, will be stored only as an
encrypted GitHub Actions secret and will not be shared in issues, pull requests,
documentation, or chat.

若项目获准加入 SignPath Foundation 计划，证书私钥将由签名服务的硬件安全模块
（HSM）生成和保护，维护者无法取得该私钥。

证书私钥和 API Token 不会提交到本仓库，不会嵌入源码或构建产物，也不会输出到
普通工作流日志。未来如需 SignPath API Token，只会保存为 GitHub Actions 加密
Secret，不会通过 Issue、Pull Request、文档或聊天分享。
