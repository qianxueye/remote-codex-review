# remote-codex-review

Self-hosted Codex PR reviewer triggered by GitHub App webhooks.

## English

**Purpose.** A GitHub App delivers `pull_request` events to a small service on your own Linux host. For each new pull request head the service runs `codex exec review` and posts one review (summary, every finding in full with its confidence, inline comments where the line is in the diff) as the App. The first review of a PR is a full round: one xhigh pass over the diff against the base branch covering both correctness and callers/edge cases, with no cap on findings. Later heads that still contain the last reviewed head get one re-review of the delta since that head, leaving out commits merged from the base branch but keeping conflict resolutions; problems the re-review finds in unchanged code are listed as register items that do not count toward the verdict, except P0/P1 and anything involving funds, stop-loss orders, credentials or a security boundary. A force-push or rebase falls back to a full round. Every parsed finding is posted and counted, with its confidence (0–100) shown next to it, or `置信度未给出` when codex gave none. Each pass must read the complete diff of every changed file: the service computes `git diff --no-renames` for the pass's own range (first round `<merge base>...HEAD`, re-review `<delta tree> HEAD`) per file in codex's own worktree (same `.gitattributes`, bytes kept, so CRLF files compare as is) and requires every line of it—or, for lines over 4000 bytes, every `fold -b -w 4000` segment—in output that the model actually saw: the tool outputs recorded in the codex session rollout (`$CODEX_HOME/sessions/**/rollout-*.jsonl`), where a truncated output counts only for the lines it still shows, intersected with the output of successful commands in the `--json` events; a session record that cannot be read or decoded is no evidence and is named in the review. The changed-file list is handed to codex as a file, never inside the prompt argument; a review that misses a file is run again on the same head with the unread files named first in the prompt, at most three reviews in all; after the last one a review that still misses a file cannot be clean—it is posted as failed with the unread files listed, or, when it has findings, as findings with a note that reading was incomplete. The body states how many reviews ran and how many files each left unread. The marker records `mode=full|delta`. Issues linked from the repository's `AGENTS.md` are fetched with their comments by the service beforehand and handed to codex as files, because the codex sandbox has no network; an issue that cannot be fetched is named in the review. One review per head, idempotent across restarts; a newer head cancels the review of the older one. A review counts as clean only when codex exited 0, printed a unified diff (a `diff --git a/` line and an `@@ ` hunk) in the output of a successful command, did not say it could not review, and ended with `VERDICT: CLEAN`; anything else is posted as failed. The service refuses to start unless codex can read a file in a scratch repository.

**How it works.** GitHub App webhook → Cloudflare Tunnel (`cloudflared`, outbound-only; the host opens no inbound port) → `codex_review.py` listening on `127.0.0.1:8787` → SQLite job queue → worker: bare mirror fetch → detached worktree → `codex exec review` → review posted via the GitHub REST API. Missed deliveries are recovered hourly through `GET /app/hook/deliveries`.

**Security boundary.**
- Admission filter: the installation account and the repository owner must be in `CODEX_REVIEW_ACCOUNTS`, the PR must come from the same repository (no forks), the author must be `OWNER`/`MEMBER`/`COLLABORATOR` and listed in `CODEX_REVIEW_AUTHORS`. Everything else is acknowledged with 202 and dropped with a log line. All name comparisons are case-insensitive.
- Public App, private service: the App is `public: true` so it can be installed on more than one account, which also lets strangers install it. The service therefore checks the account allowlist three times: on every webhook, before exchanging an installation token (`GET /app/installations/{id}`), and before requesting a redelivery. A stranger's installation never gets a token and never triggers codex.
- Sandbox: every review is read-only: codex runs with `--sandbox read-only` and `approval_policy="never"` and is told not to run tests or start services; commands it executes can neither write files nor reach the network. Tests are left to local review and CI. The systemd unit adds `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, `MemoryMax=3G`, `CPUQuota=300%` and hides any self-hosted GitHub Actions runner directory.
- Credentials: the App private key, webhook secret and tunnel token are root-installed files with mode 0600; the installer never writes them. Installation tokens are passed to git through the environment, never argv or URLs.

**Install.** `deploy/install.sh` (Ubuntu 22.04 x86_64, run as root) installs packages, users, the pinned full `codex` package (with its `codex-code-mode-host`, `bwrap` and `rg` helpers) under `/opt/codex`, the pinned `cloudflared` binary and the systemd units without enabling them. The full procedure—creating the App from `deploy/app-manifest.json`, placing credentials, building the tunnel, enabling, logs, rollback—is in [`docs/runbook.zh.md`](docs/runbook.zh.md) (Chinese).

**Tests.** `python3 -m unittest discover -s tests -v` (Python ≥ 3.10, standard library only; GitHub and codex are replaced by fakes). `shellcheck -S warning deploy/install.sh`.

## 中文

**用途。** 一个 GitHub App 把 `pull_request` 事件投递到自己的 Linux 主机上的小服务。每个新的 PR head 到达后，服务运行 `codex exec review`，并以 App 身份发一条 review：结论摘要、每条发现的完整内容和置信度、能对应到 diff 行的发现另加行内评论。同一 PR 的第一次审查是全面首轮：对 PR 相对 base 分支的 diff 做一遍 xhigh 审查，正确性与调用方、边界都要审，不限发现条数。之后的 head 如果仍包含上一个已审 head，只复审这之后的增量，不含从 base 合入的提交，但含解冲突的改动；复审在未改动代码中发现的问题列为登记项，不计入结论，P0/P1 以及涉及资金、止损、凭据、安全边界的问题除外。force-push 或 rebase 后回到首轮。能解析的发现全部发布并计入结论，每条标出置信度（0–100），没有给出的标「置信度未给出」。每一遍都必须读完全部改动文件的 diff：服务在 codex 使用的同一个工作树里（同一套 `.gitattributes`，保留原始字节，CRLF 文件原样比较）按这一遍的范围（首轮 `<合并基点>...HEAD`，复审 `<增量基准树> HEAD`）逐个文件算出 `git diff --no-renames` 的文本，要求其中每一行（超过 4000 字节的行按 `fold -b -w 4000` 的每一段）都出现在模型真正看到的输出里：codex 会话记录（`$CODEX_HOME/sessions/**/rollout-*.jsonl`）中模型实际看到的命令输出（被截断的输出只算没有被省略的行），再与 `--json` 事件里退出码为 0 的命令输出取交集；会话记录读不到或认不出格式时一律不算证据，并在 review 正文写明。改动文件清单以文件形式交给 codex，不放进提示参数；缺任何一个文件，同一 head 自动重审，提示里先列出上次没读全的文件，最多审 3 次；最后一次仍缺文件的不能判 clean：没有发现就发 failed 并列出未读全的文件，有发现则照常发布并在正文标明阅读不完整。正文写明审查了几次、每次没读全的文件数。标记中记录 `mode=full|delta`。被审仓库 `AGENTS.md` 中链接的 issue 及其评论由服务预先读取，以文件形式交给 codex，因为 codex 的沙箱不能联网；读取失败的 issue 会在 review 中写明。每个 head 只发一条，重启后不重复；新 head 到达会终止旧 head 的审查。只有 codex 退出码为 0、某条成功命令的输出中有统一 diff（`diff --git a/` 行和 `@@ ` hunk 头）、没有说明自己未能完成审查，并以 `VERDICT: CLEAN` 结尾时才判为无发现，其余情况一律发 failed。服务启动时先让 codex 读取临时仓库中的一个文件，读不到就拒绝启动。

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
| `deploy/effort-rules.json` | default reasoning-effort tables, overridable per repository |
| `deploy/codex-config.toml` | codex model/provider configuration template |
| `deploy/app-manifest.json` | GitHub App manifest (placeholders for owner and hostname) |
| `deploy/cloudflared-ingress.json` | tunnel ingress rule for the Cloudflare API |
| `docs/runbook.zh.md` | operations runbook (Chinese) |
