# remote-codex-review

Self-hosted Codex PR reviewer triggered by GitHub App webhooks.

## English

**Purpose.** A GitHub App delivers `pull_request` events to a small service on your own Linux host. For each new pull request head the service runs `codex exec review` against the PR's base branch and posts one review (summary, each passing finding in full, inline comments where the line is in the diff) as the App. Issues linked from the repository's `AGENTS.md` are fetched with their comments by the service beforehand and handed to codex as files, because the codex sandbox has no network; an issue that cannot be fetched is named in the review. One review per head, idempotent across restarts; a newer head cancels the review of the older one. When nothing is published (including when all raw findings are withheld), a review counts as clean only when codex exited 0, completed the turn without errors, printed a unified diff (a `diff --git a/` line and an `@@ ` hunk) in the output of a successful command, did not say it could not review, and declared exactly one `VERDICT: CLEAN` or `VERDICT: FINDINGS` line; `BLOCKED` and missing or conflicting verdicts fail closed. The service refuses to start unless codex can read a file in a scratch repository.

**Confidence policy.** Findings and scores come from the same run's rollout `ExitedReviewMode.review_output`, matched by exec thread ID and session worktree. Missing, malformed, ambiguous or conflicting structured results fail closed. P1 findings are published regardless of confidence. All other priorities (including P2/P3) require raw `confidence_score > CODEX_REVIEW_MIN_CONFIDENCE` (default `0.80`; finite number in `[0, 1]`; comparison happens before formatting). For those priorities, exactly `0.80`, missing, nonnumeric, nonfinite and out-of-range scores are withheld. Each published finding shows its score with enough digits to preserve the raw value; missing/invalid P1 scores are labeled explicitly. Withheld findings appear publicly only as a count; their full structured list and threshold are saved in `CODEX_REVIEW_STATE_DIR/output/<job id>.log` (0600). The model's overall explanation is replaced with counts when there are raw findings, so it cannot disclose withheld details. At least one published finding means `findings`; all withheld means `clean` only after the clean-run evidence checks above pass. The existing head/verdict marker and failure/invalidation handling remain in use.

**How it works.** GitHub App webhook → Cloudflare Tunnel (`cloudflared`, outbound-only; the host opens no inbound port) → `codex_review.py` listening on `127.0.0.1:8787` → SQLite job queue → worker: bare mirror fetch → detached worktree → `codex exec review` → review posted via the GitHub REST API. Missed deliveries are recovered hourly through `GET /app/hook/deliveries`.

**Health.** A worker thread survives every error: a failed claim or review is logged and retried with backoff (5 s doubling to 300 s). Workers claim nothing while the state directory has less than `CODEX_REVIEW_MIN_FREE_MB` (default 1024) free; the transition is logged once as `error:` and shown in `systemctl status`, and a webhook that cannot be recorded is answered 503 so GitHub retries it. The main thread sends `WATCHDOG=1` to systemd while at least one worker lives; if every worker has exited it logs the fact and exits with status 4, which `Restart=on-failure` restarts. The reviewer has no notification channel beyond the journal and `systemctl status`.

**Security boundary.**
- Admission filter: the installation account and the repository owner must be in `CODEX_REVIEW_ACCOUNTS`, the PR must come from the same repository (no forks), and the author login must be listed in `CODEX_REVIEW_AUTHORS`. A listed login ending in `[bot]` is accepted only from a GitHub App account (`user.type` `Bot`); any other listed login only from a user account (`User`). `author_association` is logged but does not gate: the webhook reports `NONE` for App bots and `CONTRIBUTOR` for private-organization members. Everything else is acknowledged with 202 and dropped with a log line. All name comparisons are case-insensitive.
- Public App, private service: the App is `public: true` so it can be installed on more than one account, which also lets strangers install it. The service therefore checks the account allowlist three times: on every webhook, before exchanging an installation token (`GET /app/installations/{id}`), and before requesting a redelivery. A stranger's installation never gets a token and never triggers codex.
- Sandbox: every review is read-only: codex runs with `--sandbox read-only` and `approval_policy="never"` and is told not to run tests or start services; commands it executes can neither write files nor reach the network. Tests are left to local review and CI. The systemd unit adds `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, `MemoryMax=3G`, `CPUQuota=300%` and hides any self-hosted GitHub Actions runner directory.
- Credentials: the App private key, webhook secret and tunnel token are root-installed files with mode 0600; the installer never writes them. Installation tokens are passed to git through the environment, never argv or URLs.

**Install.** `deploy/install.sh` (Ubuntu 22.04 x86_64, run as root) installs packages, users, the pinned full `codex` package (with its `codex-code-mode-host`, `bwrap` and `rg` helpers) under `/opt/codex`, the pinned `cloudflared` binary and the systemd units without enabling them. The full procedure—creating the App from `deploy/app-manifest.json`, placing credentials, building the tunnel, enabling, logs, rollback—is in [`docs/runbook.zh.md`](docs/runbook.zh.md) (Chinese).

**Tests.** `python3 -m unittest discover -s tests -v` (Python ≥ 3.10, standard library only; GitHub and codex are replaced by fakes). `shellcheck -S warning deploy/install.sh`.

## 中文

**用途。** 一个 GitHub App 把 `pull_request` 事件投递到自己的 Linux 主机上的小服务。每个新的 PR head 到达后，服务对 PR 的 base 分支运行一次 `codex exec review`，并以 App 身份发一条 review：结论摘要、每条发布发现的全文、能对应到 diff 行的发现另加行内评论。被审仓库 `AGENTS.md` 中链接的 issue 及其评论由服务预先读取，以文件形式交给 codex，因为 codex 的沙箱不能联网；读取失败的 issue 会在 review 中写明。每个 head 只发一条，重启后不重复；新 head 到达会终止旧 head 的审查。没有发布项时（包括原始发现全部扣留），只有 codex 退出码为 0、回合正常完成且无错误、某条成功命令的输出中有统一 diff（`diff --git a/` 行和 `@@ ` hunk 头）、没有说明自己未能完成审查，并声明唯一的 `VERDICT: CLEAN` 或 `VERDICT: FINDINGS` 行时才判为无发现；BLOCKED、缺失或冲突的结论行均发 failed。服务启动时先让 codex 读取临时仓库中的一个文件，读不到就拒绝启动。

**置信度约束。** 发现及分数取自本次 rollout 的 `ExitedReviewMode.review_output`，按 exec thread ID 和 session 工作树核对；结构化输出缺失、格式错误、归属不唯一或与最终发现冲突时发 `failed`。P1 不受置信度门槛限制，一律发布；其他优先级（含 P2/P3）只发布原始 `confidence_score > CODEX_REVIEW_MIN_CONFIDENCE` 的发现，默认 `0.80`，配置须为 `[0, 1]` 内有限数，严格大于且先比较后显示。这些优先级中，恰好 `0.80`、缺失、非数值、非有限或越界分数均扣留；每条发布项保留足以还原原始分数的显示精度，P1 分数缺失或无效时明确标注。扣留项公开只写数量，完整结构化列表和阈值保存在 `CODEX_REVIEW_STATE_DIR/output/<任务 id>.log`（0600）。存在原始发现时，用数量说明替代模型总评，避免泄露扣留内容。至少发布一条为 `findings`；全部扣留时须通过上述全部 clean 证据检查才为 `clean`；head/verdict 标记以及失败、作废处理沿用原约定。

**工作方式。** GitHub App webhook → Cloudflare Tunnel（`cloudflared`，只有出站连接，主机不开放入站端口）→ 监听 `127.0.0.1:8787` 的 `codex_review.py` → SQLite 任务队列 → worker：bare 镜像 fetch → detached 工作树 → `codex exec review` → 通过 GitHub REST API 发 review。每小时通过 `GET /app/hook/deliveries` 补救漏投。

**健康状态。** 工作线程不因任何错误退出：取任务或审查失败都会记入日志并退避重试（5 秒起，翻倍到 300 秒为止）。状态目录可用空间低于 `CODEX_REVIEW_MIN_FREE_MB`（默认 1024）时不取任务；状态变化只记一行 `error:` 日志，并显示在 `systemctl status` 中；无法写入队列的 webhook 返回 503，由 GitHub 重投。只要还有工作线程存活，主线程就向 systemd 发送 `WATCHDOG=1`；全部线程退出时记录日志并以退出码 4 退出，由 `Restart=on-failure` 重启。除日志和 `systemctl status` 外，本服务没有其他通知渠道。

**安全边界。**
- 准入过滤：安装所属账户和仓库所属账户必须在 `CODEX_REVIEW_ACCOUNTS` 中，PR 必须来自同一仓库（不审 fork），作者登录名必须列在 `CODEX_REVIEW_AUTHORS` 中。列表中以 `[bot]` 结尾的登录名只接受 GitHub App 账户（`user.type` 为 `Bot`），其他登录名只接受用户账户（`User`）。`author_association` 只记入日志，不作为准入条件：webhook 对 App bot 报 `NONE`，对私有组织成员报 `CONTRIBUTOR`。其余一律返回 202 后丢弃并记一行日志。所有名称比较不区分大小写。
- 公开 App、私有服务：App 设为 `public: true` 才能安装到多个账户，这也意味着陌生人能把它装到自己账户上。因此服务在三处核对账户白名单：每次 webhook、换 installation token 之前（`GET /app/installations/{id}`）、请求补投之前。陌生安装拿不到令牌，也不会触发 codex。
- 沙箱：所有审查都是只读审查：codex 以 `--sandbox read-only`、`approval_policy="never"` 运行，并被要求不运行测试、不启动服务；它执行的命令不能写文件，也不能访问网络。测试由本地审查和 CI 负责。systemd 单元另加 `ProtectSystem=strict`、`ProtectHome`、`PrivateTmp`、`NoNewPrivileges`、`MemoryMax=3G`、`CPUQuota=300%`，并隐藏主机上的 self-hosted GitHub Actions runner 目录。
- 凭据：App 私钥、webhook secret、隧道令牌都是 root 安装的 0600 文件，安装脚本不写入它们。installation token 通过环境变量传给 git，不出现在命令行参数或 URL 中。

**安装。** `deploy/install.sh`（Ubuntu 22.04 x86_64，以 root 运行）安装软件包、系统用户、固定版本的 codex 整包（含 `codex-code-mode-host`、`bwrap`、`rg` 等配套程序，装在 `/opt/codex`）和 `cloudflared`，以及 systemd 单元，但不启用。完整步骤（用 `deploy/app-manifest.json` 创建 App、放置凭据、建隧道、启用、日志、回滚）见 [`docs/runbook.zh.md`](docs/runbook.zh.md)。

**测试。** `python3 -m unittest discover -s tests -v`（Python ≥ 3.10，只用标准库，GitHub 和 codex 都用替身）；`shellcheck -S warning deploy/install.sh`。

## Layout

| Path | Content |
|---|---|
| `codex_review.py` | webhook receiver, SQLite queue, review workers, redelivery recovery |
| `tests/` | offline `unittest` suite; older exec recordings use explicitly synthetic scored companions; `tests/fixtures/real-review-rollout.jsonl` preserves a sanitized review rollout and its rendered message |
| `deploy/install.sh` | idempotent installer |
| `deploy/*.service` | systemd units for the service and the tunnel |
| `deploy/codex-review.env.example` | every configuration key with its default; no secrets |
| `deploy/effort-rules.json` | default reasoning-effort tables, overridable per repository |
| `deploy/codex-config.toml` | codex model/provider configuration template |
| `deploy/app-manifest.json` | GitHub App manifest (placeholders for owner and hostname) |
| `deploy/cloudflared-ingress.json` | tunnel ingress rule for the Cloudflare API |
| `docs/runbook.zh.md` | operations runbook (Chinese) |
