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
  -> worker（并发上限 2）：bare 镜像 fetch -> detached 工作树 -> codex exec review -> 发 review
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

同一个 PR 收到新 head 时，该 PR 尚在排队的旧任务标为 `superseded`，正在审查的旧任务会被终止（向 codex 进程组发 SIGTERM，5 秒后发 SIGKILL）。旧 head 的结果一律不发。原因：推送很频繁，旧 head 的审查结果已经没有用。

worker 在审查开始前和结束后各向 GitHub 查一次 PR。PR 已关闭或 head 已变时丢弃本次结果，不发 review。如果当前 head 能通过准入过滤，就把它补进队列，以免漏投时最新 head 没人审。

### 2.4 发布与幂等

- 每个 head 只发一条 review，event 为 `COMMENT`，`commit_id` 为该 head。
- 正文第一行是标记：`<!-- remote-codex-review head=<完整 sha> verdict=clean|findings|failed effort=medium|high -->`。codex 未运行就失败时（例如本节末条的基础设施错误），标记中没有 `effort=`。
- 正文包含结论摘要和每条发现的完整内容。发现中的 `路径:行号` 去掉工作树前缀后，如果该行在 PR diff 的右侧，再另外作为行内评论。GitHub 对整条 review 返回 422 时，去掉全部行内评论重发一次；因为正文本来就包含全部发现，所以不会丢失内容。
- 发 review 前，先查 GitHub 上是否已有本 App 针对同一 head 的标记。有标记就只记账，不重复发。服务重启后重跑中断的任务时，也靠这一步避免重复。
- 结论判定：codex 最终消息中含 `[P0]` 到 `[P3]` 即为 `findings`，否则为 `clean`。最终消息优先取 `-o` 输出文件，取不到时取输出中最后一个单独的 `codex` 行之后的文本。
- codex 非零退出、超时（默认 40 分钟）或没有最终消息时，发 `verdict=failed`，正文写明「This is not a pass」。`failed` 是终态，不会自动重试；需要重审时用 §7 的 `--requeue`。
- 结论为 `failed`，或正文超过 60000 字符被截断时，codex 的完整输出（含最终消息）写入 `/var/lib/codex-review/output/<任务 id>.log`（目录 0700，文件 0600）。正文中写明该文件，以 `$CODEX_REVIEW_STATE_DIR/output/<任务 id>.log` 表示，不写主机上的实际路径。服务不会自动清理这些文件。
- GitHub 或 git 出错（不是 codex 本身失败）时，任务重新排队，等待 5 分钟乘以已失败次数后再试。累计 `CODEX_REVIEW_MAX_ATTEMPTS`（默认 3）次仍失败时，发 `failed` 标记。

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
- 每个任务在 `/var/lib/codex-review/work/<任务 id>/tree` 建一个 detached 工作树，结束后删除。服务启动时会清掉上次残留的工作树。需要保留的输出按 §2.4 另存到 `output/`，不随工作树删除。
- codex 命令（`<effort>` 的取法见 §2.6.1）：

  ```
  codex exec --sandbox workspace-write \
    -c sandbox_workspace_write.network_access=false \
    -c approval_policy="never" \
    -c model_reasoning_effort=<effort> \
    review --base origin/<base> -o <工作目录>/last-message.txt
  ```

  `workspace-write` 只允许写工作树和临时目录。`network_access=false` 关闭 codex 所执行命令的网络访问。`approval_policy="never"` 使 codex 不会停下来等待批准。配置键的出处见 §9。
- codex 自己的 `config.toml`（`/var/lib/codex-review/.codex/config.toml`）由 `deploy/codex-config.toml` 模板生成，只含模型和 provider，不含任何密钥。

#### 2.6.1 推理档位

档位按 PR 的改动文件决定。改动文件取自 `git diff --name-only --no-renames origin/<base>...<head>`；`--no-renames` 使改名同时列出旧路径和新路径。

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

`repos` 中某个仓库的表整体取代 `default`，不与它合并；仓库名比较不区分大小写。修改后重启服务生效：`systemctl restart codex-review`。选中的档位写在 review 标记的 `effort=` 中。

### 2.7 为什么用常驻服务

webhook 需要一个一直在监听的进程。队列、并发上限和「新 head 终止旧审查」也需要同一个进程掌握正在运行的 codex 进程组。因此用一个常驻的 `codex-review.service`，不用 timer。

### 2.8 systemd 加固与 codex 沙箱

codex 0.160.1 在 Linux 上用 bubblewrap（`bwrap`）隔离它执行的命令：优先用 `PATH` 中的 `bwrap`，找不到时用随包附带的版本（出处见 §9）。bubblewrap 需要创建用户命名空间，并挂载一个新的 `/proc`。所以 `codex-review.service`：

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
5. 安装 `/opt/codex-review/codex_review.py`，写入 codex 的 `config.toml`；`/etc/codex-review/codex-review.env` 和 `effort-rules.json` 不存在时才从模板复制；
6. 下载 codex-cli 0.160.1 和 cloudflared 2026.10.0，校验 sha256 后安装到 `/usr/local/bin`；已是该版本时跳过下载；
7. 安装两个 systemd 单元并执行 `daemon-reload`，**不启用**；
8. 列出仍缺少的凭据。

脚本不创建 App、不创建隧道、不写入任何令牌或密钥。

## 4. App 所有者需要完成的步骤

### 4.1 用 manifest 创建 GitHub App

manifest 是 `deploy/app-manifest.json`：

- 权限：`contents: read`、`metadata: read`、`pull_requests: write`；
- 订阅事件：`pull_request`；
- `url`、`hook_attributes.url` 和 `redirect_url` 使用占位值 `<owner>` 和 `review.example.com`，创建前改成实际值；
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
     /usr/local/bin/codex sandbox -c sandbox_mode='"workspace-write"' \
       -c sandbox_workspace_write.network_access=false -- /bin/echo sandbox ok
   ```

2. 启动隧道和服务，并设为开机自启：

   ```
   systemctl enable --now codex-review-tunnel.service codex-review.service
   ```

3. 验证：
   - `journalctl -u codex-review -n 20` 中应有 `listening on 127.0.0.1:8787/webhook`，以及一行 `startup:`；
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
- 任务状态：`queued`、`running`、`clean`、`findings`、`failed`、`superseded`、`rejected`（安装所属账户不在白名单，见 §2.2.1）。
- 重新审查某个 head（例如 `failed` 之后）：

  ```
  sudo -u codex-review /usr/bin/python3 /opt/codex-review/codex_review.py \
    --config /etc/codex-review/codex-review.env --requeue 'owner/repo#<pr>@<完整 sha>'
  ```

  这只把任务改回排队状态。如果 GitHub 上已有该 head 的标记，worker 不会重复发 review；需要重新发时，先在 PR 页面删除那条 review。

## 8. 回滚

1. `systemctl disable --now codex-review.service codex-review-tunnel.service`
2. `rm /etc/systemd/system/codex-review.service /etc/systemd/system/codex-review-tunnel.service && systemctl daemon-reload`
3. 在 App 设置页停用 Webhook，或者在各账户的 Installed GitHub Apps 中挂起或卸载该 App。
4. 可选：删除 `/opt/codex-review`、`/usr/local/bin/codex`、`/usr/local/bin/cloudflared`。`/var/lib/codex-review` 中有 codex 登录凭据和镜像，`/etc/codex-review*` 中有私钥、webhook secret 和隧道令牌；确认不再需要后，用 `shred` 删除凭据文件，再删除目录和两个系统用户。
5. 在 Cloudflare 控制台删除隧道，在 GitHub 上删除 App 或吊销它的私钥。

## 9. 出处

- codex-cli 0.160.1：<https://github.com/openai/codex/releases/tag/rust-v0.160.1>，文件 `codex-x86_64-unknown-linux-musl.tar.gz`，sha256 `9226581be592d18f7e7f740a352fdb63aa61e45e39f7eb9b09d3888c84bba33f`。摘要取自 GitHub Releases API 中该文件的 `digest` 字段（`gh api repos/openai/codex/releases/tags/rust-v0.160.1`）。
- cloudflared 2026.10.0：<https://github.com/cloudflare/cloudflared/releases/tag/2026.10.0>，文件 `cloudflared-linux-amd64`，sha256 `d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db`，取法同上。
- codex 配置键：openai/codex 标签 `rust-v0.160.1` 中的 `sdk/typescript/src/exec.ts` 把 `sandboxMode` 映射为 `--sandbox`，把 `networkAccessEnabled` 映射为 `--config sandbox_workspace_write.network_access=<bool>`，把 `approvalPolicy` 映射为 `--config approval_policy="<mode>"`；`sdk/typescript/src/threadOptions.ts` 列出取值 `workspace-write` 和 `never`。`codex exec --help`（0.160.1）列出 `-s, --sandbox`，`codex exec review --help` 列出 `--base` 和 `-o, --output-last-message`。
- codex 的 bwrap 查找顺序：同一标签的 `codex-rs/core/README.md` 的 Linux 部分。
- Linux 上的 `codex sandbox`：同一标签的 `codex-rs/cli/src/main.rs` 在 Linux 上把 `sandbox` 子命令映射到 `LandlockCommand`，`codex-rs/cli/src/lib.rs` 中该结构以尾随参数接收要运行的命令。
- cloudflared 的 `--token-file`：标签 `2026.10.0` 中的 `cmd/cloudflared/tunnel/subcommands.go`；读取后用 `strings.TrimSpace` 去掉首尾空白，所以令牌文件末尾的换行不影响使用。
- GitHub webhook 投递对象的 `installation_id` 字段和 `GET /app/installations/{installation_id}`：GitHub REST API 文档 "Webhooks" 与 "GitHub Apps installations"。
