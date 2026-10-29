# 注册 / 邀请码功能发布配套（pioneer-portal）

本文只覆盖 **注册（邀请码）功能** 的发布配套：随应用版本发布什么、首次安装哪个
systemd timer、如何创建邀请码、如何验证以及如何回滚。网关本体
（`deploy.sh` / `deploy.env` / 自动发布逻辑）**本版本不改**，仍按
`README.md` 与 `RELEASE_CHECKLIST.md` 的既有流程工作。

> 路径说明：生产实际布局是 `/srv/pioneer-portal`（符号链接）、
> `/srv/pioneer-portal.releases/<tag>`、`/srv/pioneer-portal.shared`、
> `/srv/pioneer-portal.deploy`。本目录其他文档中出现的 `/root/...` 是早期主机的
> 写法，**不要照抄到本机**；以服务器上的 `deploy.env` 和本文为准。

## 1. 本次发布包含什么

| 组成 | 位置 / 入口 | 说明 |
| --- | --- | --- |
| 注册页 | `/accounts/register/` | 匿名可访问；必须由中间件白名单放行 |
| 邀请码管理页 | `/users/admin/invitations/` | 管理员/staff 页面，普通用户不可访问 |
| 邀请码命令 | `manage.py create_invitation` | 运维/脚本创建邀请码；管理员 UI 也可直接发布 |
| 外发重试 outbox | `RegistrationProvision`（users 应用） | 注册时先落库，外部 Key 创建失败后由 timer 重试 |
| 重试命令 | `manage.py sync_registration_keys` | 处理 pending 行；可重复执行 |
| 重试 timer | `pioneer-portal-registration-sync.timer` | 每分钟触发一次；本目录新增，独立于 deploy gateway |

不新增 Python 依赖，不需要新增环境变量；`users` 的新迁移是**增量 schema**
（只新增两张表：`users/migrations/0005_invitationcode_registrationprovision.py`），
旧版本代码可以继续运行，因此代码回滚不需要回滚数据库。

## 2. 发布前核对（在服务器上，只读）

```bash
G=/srv/pioneer-portal.deploy/gateway/deploy.sh
$G --check                                   # 配置/标签可达，且不做任何变更
readlink -f /srv/pioneer-portal               # 当前 release
cat /srv/pioneer-portal/.release              # 当前 tag
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: 8.134.194.161' http://127.0.0.1:8002/health/
```

- 确认新功能提交已经在 `main` 上，并且待发布 tag 指向包含该提交的 commit
  （`git merge-base --is-ancestor <feature-commit> <tag>`）。**只发布 `main`
  上的 tag**，不要从功能分支或旧 release 上手工打 tag，否则后续自动发布会覆盖。
- 版本号用 `v1.11.0`（新功能，minor）。自动发布由 `main` 上的 `feat:` 提交触发；
  如需固定版本，在 tip commit 加 `Release-As: 1.11.0` trailer。
- 生产 `deploy.env` 当前是 `ALLOW_AUTO_MIGRATE=1`：网关在切换符号链接前做
  `pg_dump`（校验 gzip 且至少 5 个 `CREATE TABLE`），切换后执行 `migrate`，
  再重启 gunicorn。本次是增量迁移，保持该配置即可；发布后确认
  `/srv/pioneer-portal.deploy/state/predeploy-<tag>-*.sql.gz` 存在且可解开。
- 生产 Python 是 3.10：实现和命令不要使用 3.11+ 语法；CI 用 3.11，不能替代这步。
- 确认 `core.middleware.PortalLoginRequiredMiddleware.public_view_names` 已加入
  注册视图名，否则匿名访问 `/accounts/register/` 会被重定向到登录页。

## 3. 发布（沿用自动发布，不手工推送）

1. 功能合并到 `main`（`feat: ...`），等 `ci` / `release` workflow 成功。
2. `release` workflow 自动创建 `v1.11.0` tag 和 GitHub Release；确认 tag 的 commit
   就是 `main` 的最新提交（`gh api repos/Minhyuin/LLM-Multiplexer/commits/main`）。
3. 服务器 `pioneer-portal-deploy.timer` 每 5 分钟（±60 秒）轮询；`latest` 取最高
   `vX.Y.Z` tag，因此日期形式的 tag 不会被误部署。
4. 观察：

   ```bash
   tail -n 100 /var/log/pioneer-portal/deploy.log
   cat /srv/pioneer-portal.deploy/state/last_deployed_tag
   cat /srv/pioneer-portal/.release 2>/dev/null || echo '（bootstrap 旧 release 没有 .release 标记）'
   systemctl status pioneer-portal --no-pager
   ```

5. 健康检查通过后再做第 4 节的首次安装；健康检查失败时网关自动切回上一版
   （代码回滚，schema 不回滚），见第 6 节。

## 4. 首次安装注册重试 timer（每次主机一次）

timer 不归 deploy gateway 管，需要手工安装；**在新版本已经上线、命令存在之后**
再启用，避免每分钟执行不存在的命令。

```bash
cd /srv/pioneer-portal/deployment/gateway

install -m 0644 pioneer-portal-registration-sync.service /etc/systemd/system/
install -m 0644 pioneer-portal-registration-sync.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pioneer-portal-registration-sync.timer

# 立即跑一次，确认数据库/上游配置正确（看输出里的成功/失败/跳过计数）
systemctl start pioneer-portal-registration-sync.service
systemctl status pioneer-portal-registration-sync.service --no-pager
journalctl -u pioneer-portal-registration-sync.service -n 50 --no-pager

# 确认 timer 已排下一次
systemctl list-timers pioneer-portal-registration-sync.timer --no-pager
```

- 单元以 `User=pioneer` 运行（与 `pioneer-portal.service` 一致；本机不是
  `www-data`），`WorkingDirectory=/srv/pioneer-portal`，命令固定为
  `/srv/pioneer-portal/.venv/bin/python /srv/pioneer-portal/manage.py sync_registration_keys`。
- 单元文件放在 release 树里只是随版本分发；`/etc/systemd/system` 下的副本不会
  随发布自动更新。**只有 unit 内容变化时**才需要重新执行上面的 `install` +
  `daemon-reload` + `systemctl restart ...timer`。
- 不要把真实邀请码、数据库口令写进 unit 文件或仓库；环境变量统一来自
  `/srv/pioneer-portal/.env`（`EnvironmentFile`）。

## 5. 创建邀请码与验证

### 5.1 创建邀请码

```bash
# 生成高熵邀请码（示例；不要把输出贴到工单/聊天/仓库）
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'

sudo -u pioneer /srv/pioneer-portal/.venv/bin/python \
  /srv/pioneer-portal/manage.py create_invitation \
  --code '<生成的邀请码>' --days 7 --admin admin
```

- `--days` 必填，取值 **1-7**（上限 7 天，防止绕过 7 天窗口）；`--code` 可省略
  （省略时随机生成 24 位大写字母数字）；`--admin admin` 指定创建者管理员账号
  （默认 `admin`，命令会校验该账号确实有管理员权限）。
- **邀请码在有效期内可被多人使用，不是一次性**；到期自动失效。管理员页面
  `/users/admin/invitations/` 也可以直接发布邀请码，并可按行停用；停用不影响已经
  注册的账号。已有注册记录的邀请码受外键保护（`PROTECT`），只能停用，不能删除。
- **同一个 code 重复执行不会刷新到期时间**（管理员 UI 会拒绝重复 code）；需要延期或
  轮换时，生成新 code 再创建，旧 code 在管理员页面停用。命令输出会打印 code、
  `days`、`expires_at` 和创建者；不要把它重定向到会长期保留的日志。
- 邀请码是访问凭证：只通过安全渠道发给受邀人，不要写进仓库、unit、脚本、日志或
  shell 历史；命令本身也不要带 `set -x`。

### 5.2 发布后验证（全部只读或一次性测试）

```bash
# 匿名注册页应为 200（302 说明中间件白名单没生效）
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: 8.134.194.161' http://127.0.0.1:8002/accounts/register/

# 登录页仍为 200
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: 8.134.194.161' http://127.0.0.1:8002/accounts/login/
```

人工检查：

1. 管理员登录后打开 `/users/admin/invitations/`，能看到刚创建的邀请码及其状态/到期时间。
2. 用**测试邀请码**完成一次注册（中文姓名 2-50 字、拼音用户名可留空自动生成、密码需通过
   Django 密码校验）；注册成功后会自动登录并跳到“我的额度”，可看到自动创建的
   `pioneer_sk_...` API Key。
3. 确认该注册对应的 `RegistrationProvision` 已完成（首轮同步成功时 `synced_at` 立即写入；
   若首轮失败，1 分钟内 timer 重试后完成）；`journalctl -u pioneer-portal-registration-sync`
   应看到“成功”计数。
4. 用测试账号完成验证后，按团队流程停用/清理，避免占用真实名额；测试码如未用完则停用。
5. 普通用户访问 `/users/admin/invitations/` 应被拒绝/重定向。
6. 如果首轮同步失败，页面应提示“API Key 正在同步，系统将自动重试”，且下一分钟 timer
   重试后 Key 变为可用（见 6.1）。
7. 验收后撤销测试 Key：先**停用** portal 测试账号（停用会阻止 timer 对其重试，但**不会**
   自动撤销 CPA 中已 active 的 Key；不要先删除账号，否则 Key 行级联删除后无法再从面板
   取得明文），再从管理员用户编辑页取得该 Key，调 CPA 管理 API 精确删除：
   `DELETE http://127.0.0.1:8317/v0/management/api-keys?value=<URL 编码的 Key>`
   （**只支持 query 参数，不支持 body**；认证用 `Authorization: Bearer` 或
   `X-Management-Key`）。返回 200 后必须再 `GET /v0/management/api-keys` 确认已消失，
   并用该 Key 请求确认 401；不要用 `index`（并发位移会误删）。确认撤销后，再按需删除
   portal 测试账号。

## 6. 失败、重试与回滚

### 6.1 注册成功但外部密钥未就绪

注册视图会先尝试同步创建外部 Key；失败时用户仍注册成功并自动登录，页面提示
“API Key 正在同步，系统将自动重试”，同时 `RegistrationProvision` 留下 pending 行
（`synced_at IS NULL`）。timer 每分钟调用 `sync_registration_keys` 重试；失败行保持
pending 并累计 `attempts`/`last_attempt_at`，成功行写 `synced_at` 并把 Key 置为 active。
已停用账号、已吊销的 Key 会被跳过（不会因为重试被重新授权）。单轮最长
`TimeoutStartSec=180` 秒；若上游卡住，下一分钟的 tick 会因上一轮仍在运行而跳过，恢复后
继续。排障顺序：

```bash
systemctl status pioneer-portal-registration-sync.service --no-pager
journalctl -u pioneer-portal-registration-sync.service -n 100 --no-pager
# 立即重试一轮（不要直接反复手跑 CLI，避免和 timer 并发）
systemctl start pioneer-portal-registration-sync.service
```

确认上游已恢复但下一分钟后仍不消化，再让后端负责人检查模型状态；**不要手工 UPDATE/DELETE
`RegistrationProvision`**，也不要重复创建邀请码来“绕过”失败的 outbox 行。若待处理行
超过单轮上限（命令默认 `--limit 200`），后续每分钟会继续消化；不要并发手跑多个实例。

### 6.2 代码回滚

```bash
# 1) 先停注册重试，避免旧 release 没有该命令时每分钟报错
systemctl disable --now pioneer-portal-registration-sync.timer

# 2) 再按既有方式回滚代码（示例，目标为上一 release）
ln -sfn /srv/pioneer-portal.releases/<previous-tag> /srv/pioneer-portal.new
mv -Tf /srv/pioneer-portal.new /srv/pioneer-portal
systemctl restart pioneer-portal
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: 8.134.194.161' http://127.0.0.1:8002/health/
```

- 增量 schema 保留即可，旧代码会忽略新表；**不要**为了代码回滚去恢复数据库。
- 只有迁移本身造成故障时，才按 `state/predeploy-<tag>-*.sql.gz` 恢复；恢复会覆盖
  发布后写入的数据，属于破坏性操作，必须先停门户并确认影响。
- 回滚不会撤销已发放的 CPA Key；如需撤销某账号访问，按 6.3 的顺序处理。
- 回滚后重新部署新版本时，记得重新启用 timer。

### 6.3 邀请码泄露 / 滥用

- 立即在 `/users/admin/invitations/` 停用泄露的码，换新码；检查已使用记录。当前 UI 只提供
  停用，不提供删除；已产生注册记录的邀请码在数据库层受 `PROTECT` 保护，不要手工删。
- 应用当前**没有**注册速率限制或验证码；邀请码是唯一门槛。若出现批量注册，先停用
  相关码；需要网络层限流时另行走 nginx 变更（不属于本次发布）。
- 紧急情况下可在 nginx 对 `/accounts/register/` 返回 403（独立运维变更，需改配置并
  reload，不在本 release 内自动完成）。
- **已有 portal 账号的停用/删除不会自动撤销 CPA Key（本版本不会自动撤销）**：停用/删除只
  影响门户登录和注册重试，CPA 的 API Key 列表独立保留。要真正切断某成员访问：先停用
  portal 账号（阻止 timer 继续补推/重试；已 active 的 Key 仍然有效），从管理员用户编辑页
  取得该 Key，按 5.2 第 7 条的 `value` 精确 DELETE，`GET` 确认消失并用该 Key 请求确认
  401，最后按需删除 portal 账号。不要先删除 portal 账号，否则 Key 行级联删除、无法再从
  面板取得明文。

## 7. 已知风险（发布时向负责人确认）

1. **HTTP 明文**：生产入口仍是 `http://8.134.194.161`，注册/登录口令和会话不加密；
   在配置 HTTPS 之前，邀请码只发给可信对象，不要公开传播。
2. **无速率限制/验证码**：公开注册页可能被撞码或批量注册；监控注册量与失败率。
3. **迁移自动执行**：生产 `ALLOW_AUTO_MIGRATE=1` 与 `RELEASE_CHECKLIST.md` 的
   “显式迁移”原则不同；本次为增量迁移且网关会先备份，接受该差异。若负责人要求
   严格手工迁移，需先改生产 `deploy.env` 并停 timer，属于额外运维窗口。
4. **timer 独立于 gateway**：发布/回滚不会自动停启它；回滚前必须手工停，见 6.2。
5. **unit 副本不随发布更新**：unit 内容变更后需按第 4 节重装。
6. **注册请求内同步调用外部 Key 创建**：上游慢会拖慢注册响应；失败会落 outbox 并提示
   自动重试，不阻塞用户注册成功。

## 8. 与既有文档的关系

- 发布门禁、备份/恢复、健康检查、符号链接切换：见 `RELEASE_CHECKLIST.md` 与 `README.md`。
- 本文只补充注册功能特有的：`/accounts/register/`、邀请码命令、outbox 重试 timer、
  验证与回滚注意事项。
