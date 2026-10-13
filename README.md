# remote-codex-review

Self-hosted Codex PR reviewer triggered by GitHub App webhooks.

## English

**Purpose.** A GitHub App delivers `pull_request` events to a small service on your own Linux host. For each new pull request head the service runs `codex exec review` against the PR's base branch and posts one review (summary, every finding in full, inline comments where the line is in the diff) as the App. One review per head, idempotent across restarts; a newer head cancels the review of the older one. A review counts as clean only when codex exited 0, demonstrably ran commands against the repository, reported no tool failure and ended with `VERDICT: CLEAN`; anything else is posted as failed. The service refuses to start unless codex can read a file in a scratch repository.

**How it works.** GitHub App webhook → Cloudflare Tunnel (`cloudflared`, outbound-only; the host opens no inbound port) → `codex_review.py` listening on `127.0.0.1:8787` → SQLite job queue → worker: bare mirror fetch → detached worktree → `codex exec review` → review posted via the GitHub REST API. Missed deliveries are recovered hourly through `GET /app/hook/deliveries`.

**Security boundary.**
- Admission filter: the installation account and the repository owner must be in `CODEX_REVIEW_ACCOUNTS`, the PR must come from the same repository (no forks), the author must be `OWNER`/`MEMBER`/`COLLABORATOR` and listed in `CODEX_REVIEW_AUTHORS`. Everything else is acknowledged with 202 and dropped with a log line. All name comparisons are case-insensitive.
- Public App, private service: the App is `public: true` so it can be installed on more than one account, which also lets strangers install it. The service therefore checks the account allowlist three times: on every webhook, before exchanging an installation token (`GET /app/installations/{id}`), and before requesting a redelivery. A stranger's installation never gets a token and never triggers codex.
- Sandbox: every review is read-only: codex runs with `--sandbox read-only` and `approval_policy="never"` and is told not to run tests or start services; commands it executes can neither write files nor reach the network. Tests are left to local review and CI. The systemd unit adds `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, `MemoryMax=3G`, `CPUQuota=300%` and hides any self-hosted GitHub Actions runner directory.
- Credentials: the App private key, webhook secret and tunnel token are root-installed files with mode 0600; the installer never writes them. Installation tokens are passed to git through the environment, never argv or URLs.

**Install.** `deploy/install.sh` (Ubuntu 22.04 x86_64, run as root) installs packages, users, the pinned full `codex` package (with its `codex-code-mode-host`, `bwrap` and `rg` helpers) under `/opt/codex`, the pinned `cloudflared` binary and the systemd units without enabling them. The full procedure—creating the App from `deploy/app-manifest.json`, placing credentials, building the tunnel, enabling, logs, rollback—is in [`docs/runbook.zh.md`](docs/runbook.zh.md) (Chinese).

**Tests.** `python3 -m unittest discover -s tests -v` (Python ≥ 3.10, standard library only; GitHub and codex are replaced by fakes). `shellcheck -S warning deploy/install.sh`.

## 中文

**用途。** 一个 GitHub App 把 `pull_request` 事件投递到自己的 Linux 主机上的小服务。每个新的 PR head 到达后，服务对 PR 的 base 分支运行一次 `codex exec review`，并以 App 身份发一条 review：结论摘要、每条发现的完整内容、能对应到 diff 行的发现另加行内评论。每个 head 只发一条，重启后不重复；新 head 到达会终止旧 head 的审查。只有 codex 退出码为 0、确实执行过读取代码的命令、没有报告工具失败并以 `VERDICT: CLEAN` 结尾时才判为无发现，其余情况一律发 failed。服务启动时先让 codex 读取临时仓库中的一个文件，读不到就拒绝启动。

**工作方式。** GitHub App webhook → Cloudflare Tunnel（`cloudflared`，只有出站连接，主机不开放入站端口）→ 监听 `127.0.0.1:8787` 的 `codex_review.py` → SQLite 任务队列 → worker：bare 镜像 fetch → detached 工作树 → `codex exec review` → 通过 GitHub REST API 发 review。每小时通过 `GET /app/hook/deliveries` 补救漏投。

**安全边界。**
- 准入过滤：安装所属账户和仓库所属账户必须在 `CODEX_REVIEW_ACCOUNTS` 中，PR 必须来自同一仓库（不审 fork），作者必须是 `OWNER`/`MEMBER`/`COLLABORATOR` 且列在 `CODEX_REVIEW_AUTHORS` 中。其余一律返回 202 后丢弃并记一行日志。所有名称比较不区分大小写。
- 公开 App、私有服务：App 设为 `public: true` 才能安装到多个账户，这也意味着陌生人能把它装到自己账户上。因此服务在三处核对账户白名单：每次 webhook、换 installation token 之前（`GET /app/installations/{id}`）、请求补投之前。陌生安装拿不到令牌，也不会触发 codex。
- 沙箱：所有审查都是只读审查：codex 以 `--sandbox read-only`、`approval_policy="never"` 运行，并被要求不运行测试、不启动服务；它执行的命令不能写文件，也不能访问网络。测试由本地审查和 CI 负责。systemd 单元另加 `ProtectSystem=strict`、`ProtectHome`、`PrivateTmp`、`NoNewPrivileges`、`MemoryMax=3G`、`CPUQuota=300%`，并隐藏主机上的 self-hosted GitHub Actions runner 目录。
- 凭据：App 私钥、webhook secret、隧道令牌都是 root 安装的 0600 文件，安装脚本不写入它们。installation token 通过环境变量传给 git，不出现在命令行参数或 URL 中。

**安装。** `deploy/install.sh`（Ubuntu 22.04 x86_64，以 root 运行）安装软件包、系统用户、固定版本的 codex 整包（含 `codex-code-mode-host`、`bwrap`、`rg` 等配套程序，装在 `/opt/codex`）和 `cloudflared`，以及 systemd 单元，但不启用。完整步骤（用 `deploy/app-manifest.json` 创建 App、放置凭据、建隧道、启用、日志、回滚）见 [`docs/runbook.zh.md`](docs/runbook.zh.md)。

**测试。** `python3 -m unittest discover -s tests -v`（Python ≥ 3.10，只用标准库，GitHub 和 codex 都用替身）；`shellcheck -S warning deploy/install.sh`。

## Layout

| Path | Content |
|---|---|
| `codex_review.py` | webhook receiver, SQLite queue, review workers, redelivery recovery |
| `tests/` | offline `unittest` suite; `tests/fixtures/` holds recorded `codex exec --json` output |
| `deploy/install.sh` | idempotent installer |
| `deploy/*.service` | systemd units for the service and the tunnel |
| `deploy/codex-review.env.example` | every configuration key with its default; no secrets |
| `deploy/effort-rules.json` | default reasoning-effort and test-tier tables, overridable per repository |
| `deploy/codex-config.toml` | codex model/provider configuration template |
| `deploy/app-manifest.json` | GitHub App manifest (placeholders for owner and hostname) |
| `deploy/cloudflared-ingress.json` | tunnel ingress rule for the Cloudflare API |
| `docs/runbook.zh.md` | operations runbook (Chinese) |
