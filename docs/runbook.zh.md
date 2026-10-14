# 远端 codex 审查器运维手册

代码在仓库根目录的 `codex_review.py`，systemd 单元、安装脚本和配置模板在 `deploy/`，测试在 `tests/`。本手册说明设计取舍、运维需要完成的步骤，以及启用、停用、查看日志和回滚的方法。

下文把运行本服务的 Linux 主机称为「审查主机」，把 Cloudflare Tunnel 的公开主机名写为 `review.example.com`；两者都按实际环境替换。

## 1. 做什么

`CODEX_REVIEW_ACCOUNTS` 中列出的账户下的仓库有 PR 被打开、推送新提交、重新打开或转为可审查时，GitHub App 向 webhook 投递 `pull_request` 事件。审查主机上的服务收到事件后，对该 PR 的最新 head 运行一次 `codex exec review`，再以 App 身份发一条 PR review。

数据流：

```
GitHub App webhook
  -> https://review.example.com/webhook               （Cloudflare）
  -> cloudflared（codex-review-tunnel.service，审查主机只有出站连接）
  -> http://127.0.0.1:8787/webhook                    （codex-review.service）
  -> /var/lib/codex-review/state.sqlite 中的任务队列
  -> worker（并发数见 §2.6.3，默认 2）：bare 镜像 fetch -> detached 工作树 -> codex exec review -> 发 review
```

## 2. 行为约定

### 2.1 接收

- 只监听 `127.0.0.1:<CODEX_REVIEW_LISTEN_PORT>`，默认端口 8787。只接受 `POST /webhook`，其他路径返回 404，其他方法返回 405。
- 先看 `Content-Length`：缺失返回 411，超过 `CODEX_REVIEW_MAX_BODY_BYTES`（默认 4 MiB）返回 413，这两种情况都不读取请求体。
- 每个请求都用 webhook secret 校验 `X-Hub-Signature-256`，比较用 `hmac.compare_digest`。签名缺失或不符返回 401，响应体为空。
- 按 `X-GitHub-Delivery` 去重。同一投递编号第二次到达时直接返回 202，不再入队。
- 入队写入 sqlite 后立即返回 202。审查由 worker 异步执行。

### 2.2 准入过滤

只有同时满足以下条件的 PR 才会入队。不满足时跳过，并在日志里记一行原因：

| 条件 | 来源 |
|---|---|
| 事件为 `pull_request`，动作为 `opened`、`synchronize`、`reopened` 或 `ready_for_review` | 事件头与 `action` |
| 载荷中带有 `installation.account.login` 时，该账户在 `CODEX_REVIEW_ACCOUNTS` 中 | `installation` |
| 仓库属于 `CODEX_REVIEW_ACCOUNTS` 中的账户，且不在 `CODEX_REVIEW_EXCLUDE_REPOS` 中 | `repository.owner` |
| PR 处于 open；草稿只在 `CODEX_REVIEW_INCLUDE_DRAFTS=true` 时入队（默认 true） | `pull_request` |
| `head.repo.full_name` 等于 `base.repo.full_name`，即不审 fork 发来的 PR | `pull_request` |
| `author_association` 为 `OWNER`、`MEMBER` 或 `COLLABORATOR` | `pull_request` |
| 作者在 `CODEX_REVIEW_AUTHORS` 中（必填，可列多个，例如再加上自己的 bot 账号） | `pull_request.user.login` |

原因：公开仓库里任何人都能提 PR。如果不加限制，陌生人的代码会在审查主机上被 codex 读取甚至执行，同时消耗 App 所有者的额度。重新投递的事件走同一个接收入口，因此同样经过这组过滤。账户、仓库和作者名比较时不区分大小写，统一经由 `same_name`。

#### 2.2.1 陌生安装

App 要安装到多个账户，所以 manifest 里 `public: true`。这意味着任何 GitHub 用户都可以把这个 App 装到自己的账户上，GitHub 也会把他们仓库的事件投递到本服务。对此有三道校验，全部用同一个账户白名单 `CODEX_REVIEW_ACCOUNTS`：

1. 接收端：`pull_request` 事件按上表检查 `installation.account.login`（存在时）和 `repository.owner.login`；不在白名单的返回 202、不入队、记一行日志。`installation` 事件（created、deleted 等）只记一行日志，写明账户是否在白名单；服务不会自动卸载陌生安装。
2. 令牌换取：换 installation token 之前，服务先用 App JWT 调 `GET /app/installations/{id}` 取回安装所属账户，不在白名单的不换令牌，任务以 `rejected` 结束，不发任何 review。结果按安装 id 缓存。
3. 补投：§2.5 的补投扫描对每条候选投递检查 `installation_id` 所属账户，不在白名单的不请求重新投递。

### 2.3 新 head 取代旧 head

同一个 PR 收到新 head 时，该 PR 尚在排队的旧任务标为 `superseded`，正在审查的旧任务会被终止（向 codex 进程组发 SIGTERM，5 秒后发 SIGKILL）。取消请求在 codex 进程登记之前到达时也不会丢失：worker 在启动 codex 的同一把锁内先检查任务状态，任务已不是 `running` 就不启动。旧 head 的结果一律不发。原因：推送很频繁，旧 head 的审查结果已经没有用。

worker 在审查开始前和结束后各向 GitHub 查一次 PR。PR 已关闭或 head 已变时丢弃本次结果，不发 review。如果当前 head 能通过准入过滤，就把它补进队列，以免漏投时最新 head 没人审。

两次查询还会用当前配置对实时 PR 重新执行 §2.2 的准入过滤（排除仓库、草稿开关、作者白名单等）。不再满足条件的任务以 `skipped` 结束，不启动 codex，也不发 review。基础设施错误重试用尽、准备发 `failed` 之前，同样先做这项检查。

fetch、档位选择和 codex 一律使用审查开始时 PR 的实时 base 分支，而不是入队时记下的 base。审查结束时 base 又变了的，任务重新排队，不计入失败次数，下次按新 base 审查。

### 2.4 发布与幂等

- 每个 head 只发一条 review，event 为 `COMMENT`，`commit_id` 为该 head。
- 正文第一行是标记：`<!-- remote-codex-review head=<完整 sha> verdict=clean|findings|failed|invalid effort=medium|high|xhigh|none tests=read-only mode=full|delta -->`。`invalid` 只由 §7 的 `--invalidate` 写入，表示该条 review 已作废。`mode=full` 是首轮全面审查，`mode=delta` 是增量复审（§2.6.5）；`effort=none` 只出现在增量为空、没有调用 codex 的复审上。codex 未运行就失败时（例如本节末条的基础设施错误），标记中没有 `effort=`、`tests=` 和 `mode=`。合并列车只读取 `head=` 和 `verdict=`，并按子串检查 ` tests=read-only`，`mode=` 加在末尾不影响它的解析。codex 运行过的 review 都是只读审查，标记带 `tests=read-only`，正文另有一行说明。早期 review 的标记中可能是 `tests=tests`，解析时照常识别。
- 正文包含结论摘要和每条发现的完整内容。发现中的 `路径:行号` 去掉工作树前缀后，如果该行在 PR diff 的右侧，再另外作为行内评论。GitHub 对整条 review 返回 422 时，去掉全部行内评论重发一次；因为正文本来就包含全部发现，所以不会丢失内容。
- 发 review 前，先查 GitHub 上是否已有本 App 针对同一 head 的标记。有标记就只记账，不重复发。服务重启后重跑中断的任务时，也靠这一步避免重复。`verdict=invalid` 的标记不算，所以作废后重新入队的 head 会再审一次。
- 结论判定（§2.4.1）默认为 `failed`。最终消息优先取 `-o` 输出文件，取不到时取 `--json` 事件流中最后一条 `agent_message`。
- codex 因上游临时故障失败时先按 §2.4.2 重试，不发 review。超时（复审默认 40 分钟，首轮默认 60 分钟，见 §2.6.5）或判定为 `failed` 时，发 `verdict=failed`，正文写明「This is not a pass」，并逐条列出未通过的检查。`failed` 是终态，不会自动重试；需要重审时用 §7 的 `--enqueue`。例外是完整阅读核验（§2.4.1）：没读全的 head 在发布前先自动重审，最多审 `READ_ATTEMPTS`（3）次，之后才按上述规则发布。
- 结论为 `failed`，最后一次审查仍有没读全的文件，或正文超过 60000 字符被截断时，codex 的完整输出（含最终消息）写入 `/var/lib/codex-review/output/<任务 id>.log`（目录 0700，文件 0600）。正文中写明该文件，以 `$CODEX_REVIEW_STATE_DIR/output/<任务 id>.log` 表示，不写主机上的实际路径。服务不会自动清理这些文件。
- GitHub 或 git 出错（不是 codex 本身失败）时，任务重新排队，等待 5 分钟乘以已失败次数后再试。累计 `CODEX_REVIEW_MAX_ATTEMPTS`（默认 3）次仍失败时，发 `failed` 标记。

#### 2.4.1 结论判定

codex 以 `--json` 运行，标准输出是 JSONL 事件流，标准错误单独保存。事件格式见 §9。

- 每条发现必须带置信度（0–100）。提示要求 codex 在每条发现正文末尾写一行 `Confidence: <0-100>`；codex 0.160.1 的 `--json` 输出和最终消息都不含 review 模式的结构化 `confidence_score`，所以只能从文字中读。文字写成 `confidence_score: 0.8` 时按 0–1 刻度乘以 100；`Confidence:` 后的值超过 100、`confidence_score` 超过 1，或没有写，都按缺失处理。
- 能解析的发现全部发布到 PR 并参与判定，不设置信度门槛（业主 2026-10-14「只显示分数吧」，取代 10-13 的「置信度 50 以上才提交」）。正文和行内评论都标出「置信度 NN」，显示时取整；缺失的标「置信度未给出」。
- codex 退出码为 0、有最终消息，并且有解析出的发现时，判为 `findings`。只要有一行看起来是发现标题（以 `[P0]` 到 `[P3]` 开头，前面可以有 `-`、`*`、序号或 `**`），却不符合 `- [P<n>] <标题> — <路径>:<行>[-<行>]` 的格式，就判为 `findings`，不论其余发现解析得怎样；这行原文留在正文的摘要里。路径可以用反引号包住，以容纳空格，例如 `` `src/order handler.py:12-14` ``。解析不了的条目连同其后的缩进正文，列在正文「未能解析的发现（按发现处理）」一节。最终消息含 `[P0]` 到 `[P3]` 标记却解析不出任何一条发现，或者写了 `VERDICT: FINDINGS` 却没有任何发现时，也判为 `findings`，相关文字在正文摘要中。这些都是解析失败，因此一律从严处理。
- 完整阅读核验（业主 2026-10-14「同意补上完整阅读核验」）。应读清单来自 bare 镜像中这一遍范围的 `git diff --name-only --no-renames`（改名列为旧路径和新路径两个文件）；每个文件的参考 diff 在 codex 使用的同一个工作树里计算：首轮是 `git --literal-pathspecs -C <工作树> diff --no-renames <合并基点>...<head> -- <路径>`（合并基点是持仓库锁时 `git merge-base refs/remotes/origin/<base> <head>` 的提交号，清单、参考 diff 和提示都用它；工作树与镜像共用 `refs/remotes/origin/<base>`，别的任务抓取后这个引用会移动，固定的提交号不会），复审是 `… <增量基准树> <head> -- <路径>`，因此 `.gitattributes`（例如 `*.dat -diff`、diff 驱动）两边一致；输出按原始字节解码，不做换行转换，CRLF 文件的 `\r` 保留在两边比较。证据取两份记录的交集：（1）codex 会话记录 `$CODEX_HOME/sessions/<年>/<月>/<日>/rollout-*.jsonl` 中本次线程（`--json` 的 `thread.started.thread_id`）及其子线程（`session_meta.parent_thread_id` 等于它）的工具输出项（`custom_tool_call_output`、`function_call_output`），即模型真正看到的文本：code-mode 的输出项是若干文本段，内含每条命令的 `{"chunk_id", "exit_code", "output", …}` JSON，只取 `exit_code` 为 0 的 `output`；模型用 `text(r.output)` 输出时，文本段就是命令输出本身，不带 JSON：这样的文本段与同一会话记录文件中某个 `CommandExecution` 项的 `formatted_output` 完全相同时，按该项的 `exit_code` 判定，等于 0 才算；对不上任何一项的（例如 MCP 工具的返回）不算。codex 0.160 的 `exec review` 在审查子线程（`session_meta.source` 为 `{"subagent": "review"}`）里以 code-mode 执行命令，模型看到的是子线程的工具输出项；整个线程及其子线程都没有工具输出项时，模型看到的是各 `CommandExecution` 项的 `formatted_output`，只取 `exit_code` 为 0 的。截断分两层。工具包装层截断时，文本段以 `Warning: truncated output (original token count: N)` 开头且不等于任何 `formatted_output`，整项不算。命令自身的输出被截断时（`formatted_output`、某条命令的 `output`，或与 `formatted_output` 相同的文本段），开头是 `Warning: truncated output (original token count: N)\nTotal output lines: M\n\n`，中间某一行内嵌 `…K tokens truncated…`（或 `chars`）：去掉开头两行，含省略标记的那一行整行作废，其余行照常算，被省略的中间部分不在证据中；只有第一句、没有 `Total output lines` 行的，该条不算。命令输出正文中间出现这段文字（例如 diff 本身含有它）不算截断。JSON 认不出、形状认不出的也不算。命令中途让出时，中间块带 `session_id`、没有 `exit_code`；同一会话记录文件中 `event_msg`/`item_completed` 的 `CommandExecution` 项的 `process_id` 等于这个 `session_id`。中间块归入它之后下一个同 `process_id` 的结束项（之后没有时取之前最后一个；`process_id` 会复用）；该项 `exit_code` 为 0、各中间块都没有截断、且它们按顺序直接拼接（不加换行，块可能在一行中间切开）后是该项 `aggregated_output` 的前缀时才算。之后若有一个未截断、`exit_code` 为 0 且 `output` 正好等于剩余部分的块，就与前面拼成一整段，这个块不再单独计入。（2）`--json` 事件流中 `status=completed`、`exit_code=0` 的 `aggregated_output`。模型看到的每段输出按行切分，没有在（2）中出现的行作废，不参与匹配。会话记录读不到（没有线程 id、目录不存在、找不到该线程的文件）时一律不算证据，正文写明原因「codex 会话记录不可用，一律不算证据：…」。覆盖按文件、按位置计算：参考 diff 的每一行（`diff --git` 头、`index`、`old mode`/`new mode`、`Binary files … differ`、`deleted file mode`、hunk 头和每一行内容）都要被证据中的某一行覆盖，文件才算读全；证据中的一行最多覆盖一个文件的一行，所以 100 行相同的新增行要看到 100 次，别的文件里读到的相同行也不能顶替。匹配方式（`Coverage`）：每段输出从头开始，取与某个文件参考 diff 连续相同的最长一段，把对应位置记为已读，再从这段之后继续；紧接上一段已读位置、且连续相同至少 `COVERAGE_GRAM`（8）行或到输出末尾的一段优先（分页阅读）；同样长的候选有多个时，先取紧接已读位置的，再取未读位置最多的，再按文件和位置顺序取第一个。超过 `FOLD_WIDTH`（4000 字节）的行，按 `fold -b -w 4000` 切成的每一段都被覆盖也算（切点落在多字节字符中间时两边都按 UTF-8 宽松解码，替换字符一致）。二进制、只改权限、删除、改名的文件没有 `@@` 行，但同样有 `diff --git` 头和若干元数据行，按同一规则判定。头行都没出现的记为「未读」，头行出现但有行缺失的记为「部分」。不以字节上限代替这项核验；`READ_LIMIT`（20000 字节）只是提示里给 codex 的建议，用来避免 code-mode 约 1 万 token 的截断。提示（`READ_RULE`）要求 codex 先读改动文件清单（服务写在 `work/<任务 id>/changed-files.txt`，一行一个路径，不放进提示参数，以免超过单个 argv 的长度上限），再逐个文件 `git diff --no-renames <范围> -- <路径>`，不要调小工具的输出预算，过长的用 `| sed -n '<起>,<止>p'` 分段，每段不超过 150 行，含超长行的段再经 `fold -b -w 4000`，并在结论里写明读过的文件数和总数（服务不解析这句话，只按上面的证据核对）。缺任何一个文件时，不论有没有发现，同一 head 在同一工作树里立即再审一次，提示末尾加 `READ_RETRY_RULE`：写明这是第几次审查（上限 3），上次没读全的文件写在 `work/<任务 id>/review<n>/unread-files.txt`（一行一个），要求先读清单再逐个读完；最多审 `READ_ATTEMPTS`（3）次，次数是常量，没有配置项。只有最后一次的结果用于判定（业主 2026-10-14：没读全与容量、429、5xx 同理，先重试，用尽后才判）。最后一次仍缺文件的：没有发现就判 `failed`，正文的检查清单中列出未读全的文件（会话记录不可用时附原因）；有发现则照常发 `findings`，正文开头加一行「**阅读不完整**：…」列出这些文件。正文的范围说明一行写明「本 head 审查 N 次（上限 3）；各次未读全的文件数：a、b、c」，日志每次记一行 `review <n>/3: … 没有在合格的命令输出中读全`。codex 退出码非 0、超时或临时故障时不再重审，按原有规则处理。不设排除清单，生成文件、锁文件也要读。

最终消息只经过一次 `read_review()`，拆成摘要、已解析发现、未能解析的发现块、登记行四部分；`assess()` 据此判定（含完整阅读核验），`conclude()` 只负责渲染正文。
- 同时满足以下全部条件时，判为 `clean`：
  1. codex 退出码为 0；
  2. 事件流中有 `turn.completed`，没有 `turn.failed`、`error` 条目和顶层 `error` 事件。例外：回合正常完成时，内容属于临时故障的顶层 `error` 事件（例如 `Reconnecting... 2/5 (unexpected status 503 …)`）只是 codex 自动重连的通知，不计；
  3. 至少有一条 `command_execution` 条目以 `status=completed`、`exit_code=0` 结束，并且它的 `aggregated_output` 含有统一 diff 的结构：一行以 `diff --git a/` 或 `diff --git "a/` 开头（后者是 git 默认 `core.quotePath=true` 时非 ASCII 路径的写法），另有至少一行以 `@@ ` 开头的 hunk 头。合并提交的组合 diff（`diff --cc`、`diff --combined`，hunk 头为 `@@@ `）不算，因为它只含合并时的冲突处理，不是 PR 相对 base 的改动。这一条只看命令的输出，不分析命令怎么写；输出被截断时，只要剩下的部分仍有这两种行就算。只跑过 `git status`、`ls`、`git -v`、`echo`、`git diff --stat` 的不算。与 base 没有任何差别的 PR，`git diff` 输出为空，也按这一条判 `failed`，原因中写明这种情况；服务目前没有单独的「无改动」处理。只改二进制文件或只改名的 PR 没有 hunk 头，同样判 `failed`；
  4. 最终消息中没有 codex 说自己没能完成审查的语句。这一条只拦截意思明确的说法：`Reviewer failed to output a response.`、`Review blocked`、`the verdict is a placeholder`、`I could not read`、`The reviewer was unable to read`、`unable to read the diff`、`the review is incomplete`、`codex-code-mode-host is missing`、「我无法读取」、「本次审查无法读取文件」、「审查未完成」、「未能读取 diff」，完整列表见 `codex_review.py` 的 `FAILURE_SIGNAL_RE`。结论描述被审代码时也会用到的词不在列表中，例如单独的 blocked、placeholder、cannot view、no such file or directory、工具缺失；工具坏到读不了 diff 的情况由第 3 条判定；
  5. 最终消息中恰好有一行 `VERDICT: CLEAN`。复审中只有登记项（§2.6.5）时，恰好一行 `VERDICT: FINDINGS` 也可以；前 4 条照样全部要求；
  6. 完整阅读核验通过：应读清单中没有「未读」或「部分」的文件，且会话记录可读。
- 其余情况一律判为 `failed`。

第 5 条的结论行来自提示词：提示要求 codex 在 `overall_explanation` 末尾写 `VERDICT: CLEAN`、`VERDICT: FINDINGS` 或 `VERDICT: BLOCKED`。`--json` 事件流不包含 review 模式的结构化字段 `overall_correctness`，而且工具失败时 codex 仍可能把该字段填成占位值，所以不用它判定。结论行写在 `overall_explanation` 内，因为 codex 只取模型输出中的 JSON 对象，对象之外的文字会被丢弃。

原因：审查主机缺少 `codex-code-mode-host` 时，codex 的每次工具调用都失败，模型仍然输出没有 `[P*]` 的说明。旧规则把这种输出判为 `clean`。现在缺任何一条证据都发 `failed`。

#### 2.4.2 上游临时故障的重试

codex 的回合没有正常完成（退出码非 0 或有 `turn.failed`），并且决定性的错误消息属于临时故障时，本次结果作废，不发 review。决定性消息取最后一个 `turn.failed` 的 `error.message`，没有 `turn.failed` 时取最后一个顶层 `error` 事件。临时故障包括：模型满载（`Selected model is at capacity`）、overloaded、429 与 rate limit、usage limit、5xx、`stream disconnected`、连接重置或中断、网络超时，完整列表见 `codex_review.py` 的 `TRANSIENT_RE`。其他失败（输出格式不对、没有读代码的证据、工具缺失、401 等）是确定性失败，立即按 §2.4.1 发 `failed`。

- 同一任务重新排队，延迟按 `CODEX_REVIEW_RETRY_FIRST_SECONDS`（默认 60）逐次翻倍，封顶 `CODEX_REVIEW_RETRY_MAX_DELAY_SECONDS`（默认 1800），即 1、2、4、8、16、30、30 分钟。每次重试记一行日志 `transient codex failure #<n>, retrying in <s> s: <错误原文>`。
- 自第一次临时故障起，已过时间加下一次延迟超过 `CODEX_REVIEW_RETRY_WINDOW_SECONDS`（默认 7200）时不再重试，发 `failed`，正文写明临时故障次数、持续时间和最后一条错误原文。默认参数下最多运行 8 次 codex。
- 重试状态存放在 `state.sqlite` 的 `retries` 表，任务以 `queued` 状态等待，服务重启后照常继续。
- 等待重试期间 PR 收到新 head，或重试前查询发现 head 已变，旧任务按 §2.3 标为 `superseded`。同一 head 再次入队时重新计算重试时长。
- 服务停机（SIGTERM）时，正在运行的 codex 进程被终止；启动 codex 的加锁检查中会查看停机标志，停机后不再启动，任务回到队列。
- 本服务自己的超时不算临时故障，直接发 `failed`：一次审查跑满超时后再重试，2 小时内最多再跑两次，代价高且多半同样超时。

### 2.5 漏投补救

服务启动时执行一次，之后每小时执行一次：用 App JWT 调用 `GET /app/hook/deliveries`，回看最近 `CODEX_REVIEW_REDELIVER_HOURS`（默认 24）小时。对满足以下全部条件的投递，调用 `POST /app/hook/deliveries/{id}/attempts` 请求重新投递：

- 事件为 `pull_request`；
- 同一投递编号（guid）的所有尝试都没有得到 2xx 响应；
- 本地从未收到并接受过这个 guid；
- 这次尝试的 id 以前没有请求过重新投递；
- 投递的 `installation_id` 所属账户在 `CODEX_REVIEW_ACCOUNTS` 中（§2.2.1）。

服务不轮询 PR 列表。

### 2.6 执行审查

- 每个仓库在 `/var/lib/codex-review/mirrors/<owner>/<repo>.git` 维护一份 bare 镜像。每次 fetch 取回 `refs/pull/<n>/head` 和 base 分支，base 写到 `refs/remotes/origin/<base>`。
- installation token 通过环境变量 `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` 设置 `http.https://github.com/.extraheader`，不出现在命令行参数、URL 或日志里。
- 每个任务在 `/var/lib/codex-review/work/<任务 id>/tree` 建一个 detached 工作树，结束后删除。每次审查的事件流和标准错误写在同一任务目录的 `review<n>/events.jsonl`、`review<n>/stderr.log`（n 为第几次，见 §2.4.1 的自动重审）。服务启动时会清掉上次残留的工作树。需要保留的输出按 §2.4 另存到 `output/`，不随工作树删除。
- codex 命令（`<effort>` 的取法见 §2.6.1，只读规则见 §2.6.2）：

  ```
  codex exec --json --sandbox read-only \
    -c approval_policy="never" \
    -c model_reasoning_effort=<effort> \
    review "<提示>" -o <工作目录>/last-message.txt
  ```

  提示由 `codex_review.py` 的 `review_prompt()` 生成：首轮要求 codex 运行 `git diff <合并基点>...HEAD`，复审要求运行 `git diff <增量基准树> HEAD`（§2.6.5）；两者都要求先列出改动文件再逐个读完 diff（§2.4.1 的完整阅读核验）、列出全部发现、不设条数上限，每条带置信度，并按 §2.4.1 写结论行。`codex exec review` 不允许 `--base` 和自定义提示同时出现，所以命令不带 `--base`，改在提示中写明对比 `origin/<base>`；`origin/<base>` 是 bare 镜像中的 `refs/remotes/origin/<base>`，工作树可以直接引用。`read-only` 沙箱不允许 codex 执行的命令写文件，也不允许访问网络；服务写在 `work/<任务 id>/` 下的改动文件清单、未读清单和治理 issue 文件对 codex 可读。`approval_policy="never"` 使 codex 不会停下来等待批准。配置键的出处见 §9。
- codex 自己的 `config.toml`（`/var/lib/codex-review/.codex/config.toml`）由 `deploy/codex-config.toml` 模板生成，只含模型和 provider，不含任何密钥。

#### 2.6.1 推理档位

首轮（`mode=full`）一律用 `xhigh`，不看改动文件，这是业主对首轮全面审查的裁定。

复审（`mode=delta`）的档位按增量中的文件决定，增量文件取自 `git diff --name-only --no-renames <增量基准树> <head>`；`--no-renames` 使改名同时列出旧路径和新路径。复审沿用 `high`、纯文档 `medium` 两档，不用 `xhigh`：同一 PR 已经过 `xhigh` 首轮，复审只读上次已审 head 之后的改动，范围小；本地合并列车的复审也用同样的两档。

- 改动文件全部匹配 `medium` 表中的某个模式，并且没有一个匹配 `high` 表时，用 `medium`；
- 其余情况一律用 `high`，包括没有任何改动文件的情况。

默认表：

| 表 | 模式 |
|---|---|
| `medium` | `docs/wiki/**`、`docs/*.md`、`**/README.md` |
| `high` | `docs/runbooks/**` |

运行手册里有可执行的命令，所以 `docs/runbooks/**` 下的文件（包括其中的 `README.md`）一律用 `high`。模式语法：`**/` 匹配零层或多层目录，`**` 匹配任意字符，`*` 和 `?` 不跨越 `/`。例如 `docs/*.md` 匹配 `docs/notes.md`，不匹配 `docs/adr/0001.md`。

匹配表在 `/etc/codex-review/effort-rules.json`（由 `CODEX_REVIEW_EFFORT_RULES` 指定；不设置该键时使用内置默认表）。格式：

```json
{
  "default": {"medium": ["docs/wiki/**", "docs/*.md", "**/README.md"],
              "high": ["docs/runbooks/**"]},
  "repos": {"<owner>/<repo>": {"medium": ["docs/**"], "high": ["docs/runbooks/**"]}}
}
```

`repos` 中某个仓库的表整体取代 `default`，不与它合并；仓库名比较不区分大小写。以 `_` 开头的键是注释，加载时忽略。这张表只用于复审。修改后重启服务生效：`systemctl restart codex-review`。选中的档位写在 review 标记的 `effort=` 中。

#### 2.6.2 只读审查

所有 PR 都做只读审查，不按改动路径分档：codex 不运行测试，测试由本地审查和 CI 负责。提示另加 `READ_ONLY_RULE`：要求 codex 不运行 pytest、vitest 或其他测试套件，不启动数据库或其他服务，结论中写明「本次为只读审查，未运行测试」。沙箱为 `--sandbox read-only`。

#### 2.6.3 并发

`CODEX_REVIEW_CONCURRENCY` 是同时运行的 codex 审查数。按「单个审查的内存峰值 × N ≤ 可用内存的 70%」取 N，上限 6，并且 N × 峰值要低于单元的 `MemoryMax=3G`。峰值的测法：用 `systemd-run` 以服务账号和同样的加固参数运行一次审查，每 0.5 秒读取该临时单元 cgroup 的 `memory.current`，取最大值；可用内存取 `free -m` 的 available 列。只读审查不运行测试，峰值主要是 codex 进程本身。

#### 2.6.4 治理 issue 预取

被审仓库根目录的 `AGENTS.md` 可能要求先读某些 GitHub issue，而 codex 的只读沙箱不能访问网络。因此服务在启动 codex 之前代为读取：

- 从工作树的 `AGENTS.md` 中找出所有 `github.com/<owner>/<repo>/issues/<n>` 链接（仓库名可以以点开头，例如 `.github`；`.` 和 `..` 不算），去重后最多取 20 个；没有 `AGENTS.md` 或其中没有链接时跳过这一步。
- 用本仓库的 installation token 调 `GET /repos/<owner>/<repo>/issues/<n>`，再用 `GET /repos/<owner>/<repo>/issues/<n>/comments` 取全部评论，把标题、正文和评论写到 `work/<任务 id>/governance/<owner>/<repo>/<n>.md`（权限 0444）。目录按 owner 和 repo 分层，不同仓库的同号 issue 不会写到同一个文件。该目录在工作树之外，随任务目录一起删除。请求地址只由链接中匹配到的 owner、repo 和编号拼成，令牌只发往 `api.github.com`。
- 提示中列出每个 issue 对应的文件路径，并要求 codex 读取这些文件，不要从网络获取。
- 某个 issue 读取失败（403、404、网络错误等）时，审查照常进行：提示中把它列为不可用，发布的 review 正文（包括超时、判定失败和临时故障重试用尽后的 failed 正文）写明 `Governance issue <owner>/<repo>#<n> linked from AGENTS.md could not be prefetched (<错误>)`，日志另记一行。结论仍按 §2.4.1 判定：codex 只说某个 issue 没读到不算审查失败，说读不到 diff 或代码才算。
- App 没有 `issues: read` 权限时，每次读取都会得到 403。补授权限的步骤见 §4.1。

#### 2.6.5 首轮与复审

同一个 PR 的第一次审查做全面首轮，之后的 head 只审修复增量。

- 上一个已审 head：`state.sqlite` 的 `jobs` 表中，同一仓库、同一 PR、不同于当前 head、状态为 `clean` 或 `findings` 且有 review id 的任务，按完成时间取最新的一条（最多看 5 条）。服务再用 `GET /repos/<owner>/<repo>/pulls/<n>/reviews/<review id>` 读回该条 review：标记的 head 必须一致，verdict 必须仍是 `clean` 或 `findings`；被 `--invalidate` 作废或已删除（404）的跳过，看下一条。`failed`、`superseded`、`skipped` 的任务不算已审。同一 head 作废后重新入队时，它自己不会被当成上一个已审 head。
- 每次审查开始时，把实际使用的 base 分支（从实时 PR 读取）写入该任务的 `jobs.base_ref`。
- 首轮（`mode=full`）：找不到上一个已审 head、PR 的 base 分支与上一个已审 head 审查时的 base 不同（PR 改了目标分支）、上一个已审 head 已不在 bare 镜像中、它不是新 head 的祖先（例如 force-push 或 rebase），或计算增量基准失败时，做首轮。首轮只跑一遍（业主 2026-10-14「全面审计就不需要两遍了！」，取代 10-13 的两遍独立审查合并），档位 `xhigh`，超时为 `CODEX_REVIEW_FIRST_TIMEOUT_SECONDS`（默认 3600 秒）。提示要求同时审改动本身的正确性和调用方与边界输入，并读完全部改动文件的 diff。判为 `failed` 或超时，发 `failed`。正文写明选择首轮的原因。
- 复审（`mode=delta`）：只跑一遍，超时为 `CODEX_REVIEW_TIMEOUT_SECONDS`。增量基准树按以下步骤计算：
  1. 在 `refs/remotes/origin/<base>` 的第一父链上，取新 head 已包含的最新提交 M（新 head 一个都不包含时做首轮）；
  2. 在 `work/<任务 id>/delta-base` 建一个停在上一个已审 head 的临时工作树，执行 `git merge --no-commit --no-ff M`。`git add --all` 之前先用 `git ls-files --unmerged` 列出未合并的条目：只有两边都存在、都是普通文件、并且工作树文件中有 `<<<<<<<` 和 `>>>>>>>` 冲突标记的文本冲突才保留在基准树中；modify/delete、符号链接、二进制、改名等冲突在工作树中可能只剩一边的版本，增量无法显示其解决结果，这时做首轮。然后用 `git write-tree` 得到基准树，再删除临时工作树。
  3. 交给 codex 的是 `git diff <基准树> HEAD`：从 base 合入的改动已在基准树中，不出现在增量里；解冲突的改动仍在增量里（删除冲突标记的 hunk 即解冲突结果）。这里不用 `git merge-tree --write-tree`，因为 Ubuntu 22.04 自带的 git 2.34 还没有这个选项（2.38 起才有）。
- 增量为空（新 head 相对上一个已审 head 只有从 base 合入的改动）时，只看上一条有效 review 的 verdict：是 `clean` 且正文没有被截断，就不调用 codex，直接发 `verdict=clean effort=none tests=read-only mode=delta`；是 `findings`（包括只有 P3 的情况），或正文被截断、无法确认是 clean，就做首轮。不从上一条正文里解析发现、也不按关键词分类，因为解析和分类都会漏掉边界情况；这种情况少见，回到首轮的成本可以接受。发 clean 之前与普通审查一样重新读取实时 PR：PR 已关闭、head 已变、不再满足准入条件（例如变成被排除的草稿）时不发布；base 已变时重新排队。
- 复审提示附上上一条 review 的正文（最多 20000 字符），要求报告：增量引入的问题；增量中对上次发现修得不完整或不对的地方。增量之外、本次未改动代码中的问题写进 `增量外问题（登记，不阻塞）` 一节。服务把这一节从最终消息中拆出（只认独立成行的节标题，可以带 `**` 或 `#`；发现正文中间出现同样的文字不算）：
  - P0/P1、涉及资金、止损、凭据、安全边界，或没有 `[P0]` 到 `[P3]` 标记、无法确认优先级的，列入正文「增量外但阻塞的问题」一节，结论计为 `findings`；登记项一节中不以列表符号开头、但以优先级标记开头的行，也单独算一条；
  - 其余条目列入正文「登记项」一节，不计入 verdict；这些条目需要登记为 issue 并在 PR 上回复，不阻塞合并。置信度不设门槛，只随条目显示。
- 本地合并列车的 `local-review.sh` 用同一套策略。两边各自以自己最近一次结论为增量起点。

### 2.7 为什么用常驻服务

webhook 需要一个一直在监听的进程。队列、并发上限和「新 head 终止旧审查」也需要同一个进程掌握正在运行的 codex 进程组。因此用一个常驻的 `codex-review.service`，不用 timer。

### 2.8 systemd 加固与 codex 沙箱

codex 0.160.1 在 Linux 上用 bubblewrap（`bwrap`）隔离它执行的命令：优先用 `PATH` 中的 `bwrap`，找不到时用包内的 `codex-resources/bwrap`（出处见 §9）。bubblewrap 需要创建用户命名空间，并挂载一个新的 `/proc`。所以 `codex-review.service`：

- 保留 `NoNewPrivileges`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/codex-review`、`ProtectHome`、`PrivateTmp`、`RestrictSUIDSGID`、`UMask=0077`，以及 `MemoryMax=3G`、`CPUQuota=300%`、`Nice=10`；
- 不设 `RestrictNamespaces`、`PrivateUsers`，因为它们会阻止 bwrap 创建命名空间；
- 不设 `ProtectKernelTunables`、`ProtectKernelLogs`、`ProtectProc`、`ProcSubset`。这些选项会在 `/proc` 上叠加挂载，内核随后会拒绝在用户命名空间中挂载新的 `/proc`；
- 不设 `SystemCallFilter`、`MemoryDenyWriteExecute`，避免拦截 bwrap 和 codex 需要的系统调用。

Ubuntu 22.04 默认允许非特权用户命名空间（`/proc/sys/kernel/unprivileged_userns_clone` 为 1）。Ubuntu 24.04 起 AppArmor 默认限制非特权用户命名空间，届时需要重新验证。

`codex-review-tunnel.service`（cloudflared）不运行 bwrap，因此启用了全套加固，包括 `RestrictNamespaces`、`ProtectKernel*`、`PrivateDevices`、空的 `CapabilityBoundingSet`。

### 2.9 与 GitHub Actions runner 账户隔离

审查主机上同时跑着 self-hosted runner（默认目录 `/opt/actions-runner`）时，服务账号 `codex-review` 不得加入 runner 用户所在的任何组，也不能读写 runner 目录：

- 安装脚本核验：`codex-review` 与 `/opt/actions-runner` 及其下一级目录属主用户的组没有交集；`codex-review` 对 `/opt/actions-runner` 下两层内任何路径都没有写权限；`/opt/actions-runner` 下没有属于 `codex-review` 的文件。任何一项不满足，脚本即失败退出。目录不存在时跳过这一步。
- 读权限：`/opt/actions-runner` 默认是 0755，普通账户可以读。`codex-review.service` 用 `InaccessiblePaths=-/opt/actions-runner` 在服务的挂载命名空间中隐藏该目录。bwrap 继承这个挂载视图，所以 codex 执行的命令也看不到它。安装脚本用 `systemctl show -p InaccessiblePaths codex-review.service` 核验该项已生效。
- 限制：在服务之外以 `codex-review` 身份运行的命令（例如 §4.3 的 `codex login`）不受 `InaccessiblePaths` 约束。不要以该账户在服务之外运行审查。

## 3. 安装

在审查主机上以 root 运行：

```
git clone https://github.com/<owner>/remote-codex-review.git /root/remote-codex-review
bash /root/remote-codex-review/deploy/install.sh
```

脚本是幂等的，可以重复运行。它会：

1. 安装缺少的 `git`、`bubblewrap`、`ca-certificates`、`curl`、`openssl`、`python3`、`sqlite3`；
2. 创建系统用户 `codex-review` 和 `codex-review-tunnel`；
3. 核验与 runner 账户的隔离（§2.9）；
4. 创建 `/var/lib/codex-review/{mirrors,work,.codex}`（0700，属主 `codex-review`）、`/etc/codex-review`（0750，root:codex-review）、`/etc/codex-review-tunnel`（0750，root:codex-review-tunnel）；
5. 安装 `/opt/codex-review/codex_review.py`，写入 codex 的 `config.toml`；`/etc/codex-review/codex-review.env` 和 `effort-rules.json` 不存在时才从模板复制；已有的 `effort-rules.json`，以及 `codex-review.env` 中 `CODEX_REVIEW_EFFORT_RULES` 指向的另一个规则文件，如果还有已废弃的 `tests` 表，删去这些表，`medium`、`high` 和 `repos` 原样保留，否则服务读取规则时报 `ConfigError` 并以退出码 2 退出；
6. 下载 codex-cli 0.160.1 的整包 `codex-package-x86_64-unknown-linux-musl.tar.gz`，校验 sha256 后解到 `/opt/codex`，属主改为 root，目录和可执行文件设为 0755、其余文件 0644，与运行脚本时的 umask 无关，再把 `/usr/local/bin/codex` 设为指向 `/opt/codex/bin/codex` 的符号链接；下载 cloudflared 2026.10.0，校验 sha256 后安装到 `/usr/local/bin`。已是该版本且文件齐全时跳过下载。安装后自检：`/opt/codex` 下的 `bin/codex`、`bin/codex-code-mode-host`、`codex-resources/bwrap`、`codex-resources/zsh/bin/zsh`、`codex-path/rg` 都存在且可执行，`codex-package.json` 的版本正确，`/usr/local/bin/codex` 解析到 `/opt/codex/bin/codex`；任一项不满足即失败退出。

   只装 `codex` 一个二进制时，codex 找不到 `codex-code-mode-host`，所有工具调用都会失败。codex 按可执行文件解析后的真实路径识别包结构（出处见 §9），所以经符号链接调用也能找到包内的配套程序；
7. 安装两个 systemd 单元并执行 `daemon-reload`，**不启用**；
8. 列出仍缺少的凭据。

脚本不创建 App、不创建隧道、不写入任何令牌或密钥。

## 4. App 所有者需要完成的步骤

### 4.1 用 manifest 创建 GitHub App

manifest 是 `deploy/app-manifest.json`：

- 权限：`contents: read`、`issues: read`、`metadata: read`、`pull_requests: write`。`issues: read` 用于 §2.6.4 的治理 issue 预取；
- 订阅事件：`pull_request`；
- `url`、`hook_attributes.url` 和 `redirect_url` 使用占位值 `<owner>` 和 `review.example.com`，创建前改成实际值；
- 已经创建的 App 不会因为 manifest 改动而自动获得新权限。缺少 `issues: read` 时，由 App 所有者在 GitHub 上操作：打开 `https://github.com/settings/apps/<slug>/permissions`，在 Repository permissions 中把 Issues 设为 Read-only 并保存；然后在每个白名单账户的安装页（`https://github.com/settings/installations/<安装 id>`，组织账户在组织设置中）接受新的权限请求。接受之前，治理 issue 预取一律按「未能预取」处理；
- `public: true`：App 要安装到白名单中的每个账户，设为「仅本账户」的 App 无法安装到另一个账户。其他人即使安装了这个 App，他们的事件也会被 §2.2.1 的账户校验挡掉。

创建链接的用法：在本机新建一个 HTML 文件，用浏览器打开后点击按钮。表单把 manifest 提交到 `https://github.com/settings/apps/new`，App 会建在当前登录账户名下：

```html
<form action="https://github.com/settings/apps/new?state=codex-review" method="post">
  <input type="hidden" name="manifest" id="manifest">
  <button type="submit">Create remote codex review App</button>
</form>
<script>
  document.getElementById('manifest').value = JSON.stringify(
    /* 在这里粘贴 app-manifest.json 的全部内容 */
  );
</script>
```

在 GitHub 页面确认创建后，浏览器会跳转到 `redirect_url`。这时隧道可能还没建好，页面会报错或显示 404，这不影响操作：从地址栏复制 `code=` 后面的值，一小时内在本机执行：

```
umask 077
mkdir -p ~/codex-review-app && cd ~/codex-review-app
gh api -X POST /app-manifests/<code>/conversions > conversion.json
jq -r .pem conversion.json > app-private-key.pem
jq -r .webhook_secret conversion.json > webhook-secret
jq '{id, slug, html_url}' conversion.json
rm conversion.json
```

`conversion.json` 还含有 client secret，提取完后立即删除。记下输出中的 `id`（App ID）和 `slug`。

如果 `webhook_secret` 为空，或者改为在网页上手动创建 App，可以自己生成 secret：执行 `openssl rand -hex 32 > webhook-secret`，再填到 App 设置页 Webhook 一栏的 Secret 中。私钥可以在 App 设置页用「Generate a private key」生成。

### 4.2 安装到白名单账户，私钥放到审查主机

1. 打开 `https://github.com/apps/<slug>/installations/new`，依次选择 `CODEX_REVIEW_ACCOUNTS` 中的每个账户，仓库范围选「All repositories」。
2. 把私钥和 webhook secret 拷到审查主机，以服务账号为属主、权限 0600 放好，再删除本机和中转位置的副本：

   ```
   scp -i <ssh-key> ~/codex-review-app/app-private-key.pem ~/codex-review-app/webhook-secret root@<review-host>:/root/
   ssh -i <ssh-key> root@<review-host> '
     install -o codex-review -g codex-review -m 0600 /root/app-private-key.pem /etc/codex-review/app-private-key.pem &&
     install -o codex-review -g codex-review -m 0600 /root/webhook-secret /etc/codex-review/webhook-secret &&
     shred -u /root/app-private-key.pem /root/webhook-secret'
   rm -P ~/codex-review-app/app-private-key.pem ~/codex-review-app/webhook-secret
   ```

3. 在审查主机编辑 `/etc/codex-review/codex-review.env`，填写 `CODEX_REVIEW_APP_ID=<id>`，并把 `CODEX_REVIEW_ACCOUNTS` 和 `CODEX_REVIEW_AUTHORS` 改成实际的账户和作者。其余键已有默认值，含义见同目录的 `codex-review.env.example`。

服务读取私钥和 webhook secret 时会检查：必须是普通文件，属主是服务账号，权限 0600。不满足时服务拒绝启动。

### 4.3 登录 codex

在审查主机上执行，按提示在浏览器里完成设备码登录：

```
sudo -u codex-review env HOME=/var/lib/codex-review CODEX_HOME=/var/lib/codex-review/.codex \
  /usr/local/bin/codex login --device-auth
```

登录凭据写在 `/var/lib/codex-review/.codex/` 下，不从别的机器复制 `auth.json`。

### 4.4 建 Cloudflare Tunnel

审查主机不开放入站端口，webhook 经 Cloudflare Tunnel 进入。

1. 在 Cloudflare Zero Trust 控制台（Networks → Tunnels）新建一条 Cloudflared 类型的隧道。控制台给出的安装命令中，`--token` 之后的长字符串就是隧道令牌。
2. 在审查主机上把令牌写入文件。令牌只经过标准输入，不出现在命令行参数里：

   ```
   install -o codex-review-tunnel -g codex-review-tunnel -m 0600 /dev/null /etc/codex-review-tunnel/token
   cat > /etc/codex-review-tunnel/token     # 粘贴令牌，回车，再按 Ctrl-D
   ```

3. 在该隧道的 Public Hostname 中只添加一条规则：主机名 `review.example.com`（按实际修改），Path 填 `^/webhook$`，Service 填 `HTTP` + `127.0.0.1:8787`。不要添加其他规则；控制台管理的隧道对未匹配的请求返回 404。
   也可以用 Cloudflare API 写入同样的规则：`PUT /accounts/<account_id>/cfd_tunnel/<tunnel_id>/configurations`，请求体为 `deploy/cloudflared-ingress.json`（先改主机名）。
4. App 设置页的 Webhook URL 必须与实际主机名一致。

## 5. 启用

1. 检查沙箱能否在服务的加固条件下启动。Linux 版 `codex sandbox` 在 codex 的 Linux 沙箱中运行给定命令。下面的命令应打印 `sandbox ok`；如果失败，先不要启用服务：

   ```
   systemd-run --wait --pipe --quiet --uid=codex-review --gid=codex-review \
     -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes \
     -p ReadWritePaths=/var/lib/codex-review -p InaccessiblePaths=-/opt/actions-runner \
     -p WorkingDirectory=/var/lib/codex-review \
     -E HOME=/var/lib/codex-review -E CODEX_HOME=/var/lib/codex-review/.codex \
     /usr/local/bin/codex sandbox -c sandbox_mode='"read-only"' -- /bin/echo sandbox ok
   ```

2. 启动隧道和服务，并设为开机自启。服务每次启动时先做自检：在 `/var/lib/codex-review/work/` 下建一个临时 git 仓库，写入随机标记，让 codex（`--sandbox read-only`、`--ephemeral`、推理档位 low）读取该文件并回显标记。codex 执行的命令输出和最终消息都含该标记才算通过，否则服务记录原因并以退出码 3 退出。单元设了 `RestartPreventExitStatus=3`，自检失败后 systemd 不会反复重启，避免每 10 秒调用一次模型：

   ```
   systemctl enable --now codex-review-tunnel.service codex-review.service
   ```

3. 验证：
   - `journalctl -u codex-review -n 20` 中应有一行 `startup:`、一行 `startup self-check passed`，以及 `listening on 127.0.0.1:8787/webhook`。出现 `startup self-check failed` 时，按其中的原因修复后再 `systemctl restart codex-review`；
   - `curl -s -o /dev/null -w '%{http_code}\n' https://review.example.com/other` 应返回 404；对 `/webhook` 用 GET 请求应返回 405，说明请求到达了服务；
   - 在 App 设置页的 Advanced → Recent Deliveries 中，新投递的响应码应为 202；
   - 推送一个测试 PR，几分钟内应出现一条带标记的 review。

## 6. 停用

```
systemctl disable --now codex-review.service codex-review-tunnel.service
```

停用后 GitHub 的投递会失败。重新启用时，服务启动后会请求重新投递最近 24 小时内的失败投递。如果需要 GitHub 停止投递，在 App 设置页取消勾选 Webhook 的 Active。

## 7. 查看日志与状态

```
journalctl -u codex-review -f
journalctl -u codex-review-tunnel -f
sudo -u codex-review /usr/bin/python3 /opt/codex-review/codex_review.py --config /etc/codex-review/codex-review.env --dry-run
sudo -u codex-review sqlite3 /var/lib/codex-review/state.sqlite \
  "SELECT repo, pr, substr(head_sha,1,12), status, attempts, review_id FROM jobs ORDER BY id DESC LIMIT 20"
```

- `--dry-run` 列出排队中和运行中的任务，以及补投扫描将要请求重新投递的投递，不改动任何状态。它需要私钥和 webhook secret 都已就位。
- 任务状态：`queued`、`running`、`clean`、`findings`、`failed`、`superseded`、`rejected`（安装所属账户不在白名单，见 §2.2.1）、`skipped`（入队后不再满足准入条件，见 §2.3）。
- 重新审查某个 head（例如 `failed` 之后）：

  ```
  sudo -u codex-review /usr/bin/python3 /opt/codex-review/codex_review.py \
    --config /etc/codex-review/codex-review.env --requeue 'owner/repo#<pr>@<完整 sha>'
  ```

  这只把已记录的任务改回排队状态。如果 GitHub 上已有该 head 的标记，worker 不会重复发 review；需要重新发时，先用下面的 `--invalidate` 作废那条 review。

- 把任意 head 加入队列，包括本地从未记录或已经审完的 head：

  ```
  sudo -u codex-review /usr/bin/python3 /opt/codex-review/codex_review.py \
    --config /etc/codex-review/codex-review.env --enqueue 'owner/repo#<pr>@<完整 sha>'
  ```

  命令先用 App 身份取该仓库的安装和 PR，安装账户不在白名单、PR 不满足 §2.2 准入条件时不入队。入队使用 PR 当前的 base。给出的 sha 不是 PR 当前 head 时，命令改为把当前 head 入队并提示一行。服务进程最多 30 秒后取到新任务。

- 作废本 App 发出的某条 review：

  ```
  sudo -u codex-review /usr/bin/python3 /opt/codex-review/codex_review.py \
    --config /etc/codex-review/codex-review.env --invalidate 'owner/repo#<pr>:<review id>' --note '<作废说明>'
  ```

  命令用 `PUT /repos/{owner}/{repo}/pulls/{pr}/reviews/{review id}` 把正文改为原标记加作废说明，标记中的 verdict 改为 `invalid`。作者不是本 App 或正文没有标记时拒绝修改。作废后用 `--enqueue` 重新审查同一 head。

## 8. 回滚

1. `systemctl disable --now codex-review.service codex-review-tunnel.service`
2. `rm /etc/systemd/system/codex-review.service /etc/systemd/system/codex-review-tunnel.service && systemctl daemon-reload`
3. 在 App 设置页停用 Webhook，或者在各账户的 Installed GitHub Apps 中挂起或卸载该 App。
4. 可选：删除 `/opt/codex-review`、`/opt/codex`、`/usr/local/bin/codex`（符号链接）、`/usr/local/bin/cloudflared`。`/var/lib/codex-review` 中有 codex 登录凭据和镜像，`/etc/codex-review*` 中有私钥、webhook secret 和隧道令牌；确认不再需要后，用 `shred` 删除凭据文件，再删除目录和两个系统用户。
5. 在 Cloudflare 控制台删除隧道，在 GitHub 上删除 App 或吊销它的私钥。

## 9. 出处

- codex-cli 0.160.1：<https://github.com/openai/codex/releases/tag/rust-v0.160.1>，文件 `codex-package-x86_64-unknown-linux-musl.tar.gz`，sha256 `340801565906a7028f6baaa9ab6853addaef221f0016a1417a7c1ffdd96c21f0`。摘要取自 GitHub Releases API 中该文件的 `digest` 字段（`gh api repos/openai/codex/releases/tags/rust-v0.160.1`）。包内 `bin/codex` 与单独发布的 `codex-x86_64-unknown-linux-musl.tar.gz` 中的二进制相同（sha256 `f34a4d2301892ae96c90097786bfe5dc269f187b6f69faf42a7b357b8c081e35`）。
- 包结构：同一标签的 `scripts/codex_package/README.md`（`bin/codex`、`bin/codex-code-mode-host`、`codex-resources/bwrap`、`codex-resources/zsh/bin/zsh`、`codex-path/rg`、`codex-package.json`）。
- 配套程序查找：同一标签的 `codex-rs/install-context/src/lib.rs`。`CodexPackageLayout::from_exe` 先把可执行文件路径规范化（解析符号链接），所在目录名为 `bin` 且上一级有 `codex-package.json` 时按包结构处理；`code_mode_host_program()` 依次查找 `codex-resources/codex-code-mode-host`、包的 `bin/codex-code-mode-host`，不是包结构时查找可执行文件同目录的 `codex-code-mode-host`。`rg_command()` 优先用包内 `codex-path/rg`。
- `--json` 事件：同一标签的 `codex-rs/exec/src/exec_events.rs` 定义 `item.completed` 中的条目类型 `agent_message`、`command_execution`（含 `exit_code`、`status`）、`error` 等，没有 review 结构化结果的条目。review 结果的渲染：`codex-rs/core/src/tasks/review.rs` 的 `parse_review_output_event` 只取模型输出中的 JSON 对象，`codex-rs/protocol/src/review_format.rs` 的 `render_review_output_text` 只输出 `overall_explanation` 和发现，不输出 `overall_correctness`；没有任何内容时输出 `Reviewer failed to output a response.`。
- cloudflared 2026.10.0：<https://github.com/cloudflare/cloudflared/releases/tag/2026.10.0>，文件 `cloudflared-linux-amd64`，sha256 `d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db`，取法同上。
- codex 配置键：openai/codex 标签 `rust-v0.160.1` 中的 `sdk/typescript/src/exec.ts` 把 `sandboxMode` 映射为 `--sandbox`，把 `approvalPolicy` 映射为 `--config approval_policy="<mode>"`；`sdk/typescript/src/threadOptions.ts` 列出取值 `read-only` 和 `never`。`codex exec --help`（0.160.1）列出 `-s, --sandbox`，`codex exec review --help` 列出 `--base` 和 `-o, --output-last-message`。
- codex 的 bwrap 查找顺序：同一标签的 `codex-rs/core/README.md` 的 Linux 部分。
- Linux 上的 `codex sandbox`：同一标签的 `codex-rs/cli/src/main.rs` 在 Linux 上把 `sandbox` 子命令映射到 `LandlockCommand`，`codex-rs/cli/src/lib.rs` 中该结构以尾随参数接收要运行的命令。
- cloudflared 的 `--token-file`：标签 `2026.10.0` 中的 `cmd/cloudflared/tunnel/subcommands.go`；读取后用 `strings.TrimSpace` 去掉首尾空白，所以令牌文件末尾的换行不影响使用。
- GitHub webhook 投递对象的 `installation_id` 字段和 `GET /app/installations/{installation_id}`：GitHub REST API 文档 "Webhooks" 与 "GitHub Apps installations"。
