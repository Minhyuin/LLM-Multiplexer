# 用量同步修复发布配套（pioneer-portal v1.12.3）

本文只覆盖 **用量账本采集修复** 的发布配套：为什么改、随应用版本发布什么、首次安装
哪个 systemd timer、如何验证捕获率、如何回滚。网关本体（`deploy.sh` / `deploy.env` /
自动发布逻辑）本版本不改，仍按 `README.md` 与 `RELEASE_CHECKLIST.md` 的既有流程工作。

> 路径说明：生产布局是 `/srv/pioneer-portal`（符号链接）、
> `/srv/pioneer-portal.releases/<tag>`、`/srv/pioneer-portal.shared`、
> `/srv/pioneer-portal.deploy`；内核在 `/opt/cliproxyapi`。文档里出现的 `/root/...`
> 是早期主机写法，不要照抄。

## 1. 本次修复了什么

现场结论：v1.12.2 的账本只采到 **约 1.5% 的请求**。2026-10-29 14:50 → 11-03 15:43 期间
代理侧 `main.log` 记账 **7607 次** `POST /v1/...`，门户 `usage_usagelog` 只有 **117 行**。

三个叠加原因：

1. **只有页面加载才抽队列。** `usage/views.py` 在“请求事件”页加载时调用同步，管理员
   不看页面就没有任何采集。入库时刻与访问时刻完全吻合（11-01 20 时 3 条、11-02 17 时
   11 条、11-03 15 时 8 条）。
2. **代理队列是消费式 + 有时限。** `/v8/management/observability/usage/queue` 取走即删；
   超出 `redis-usage-queue-retention-seconds` 的事件被 `pruneLocked` 直接丢弃。该值原为
   `60`，即事件最多存活一分钟。
3. **主路径每次空跑。** 门户先请求 `/v0/management/usage`，该路由在该版本**不存在**（404），
   每次同步都白跑一趟才回落队列。

对应改动：

| 位置 | 改动 |
| --- | --- |
| `/opt/cliproxyapi/config.yaml` | `observability.usage.redis-usage-queue-retention-seconds` `60` → `3600`（内核硬上限） |
| `usage/services.py` | 删除 `fetch_cliproxy_usage_records()` / `normalize_cliproxy_usage_payload()`；`sync_cliproxy_usage_records()` 改为 keeper 存在则走 keeper，否则直接抽队列（`count=10000`） |
| `usage/services.py` | 新增 `USAGE_QUEUE_PATHS`：首选 `/v8/management/observability/usage/queue`，仅在 404 时回退 `/v0/management/usage-queue` |
| `usage/services.py` | 空队列不再写 `UsageImportJob`（否则 30 秒一次 = 2880 行/天噪声） |
| `deployment/gateway/pioneer-portal-usage-sync.{service,timer}` | 新增：每 30 秒执行一次 `manage.py sync_cliproxy_usage` |

不新增 Python 依赖、不新增环境变量、**无数据库迁移**，代码回滚不需要回滚 schema。

## 2. 发布前核对（本机 + 服务器，只读）

本机：

```bash
python manage.py test usage            # 期望全绿
python - <<'PY'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lab_portal.settings")
django.setup()
from usage.services import USAGE_QUEUE_PATHS; print(USAGE_QUEUE_PATHS)
PY
```

服务器：

```bash
curl -s -H "Authorization: Bearer $CLIPROXY_MANAGEMENT_KEY" \
  'http://127.0.0.1:8317/v8/management/config/observability/usage/redis-usage-queue-retention-seconds'
# 期望 {"…":3600}；若还是 60，先改配置并重启 cliproxyapi

curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CLIPROXY_MANAGEMENT_KEY" \
  'http://127.0.0.1:8317/v8/management/observability/usage/queue?count=1'
# 期望 200（0 条也是 200）
```

> 不要用错的 key 反复试：管理接口有 IP 封禁（`IP banned due to too many failed attempts`），
> 本机一旦被封，门户所有管理调用会 403 约 30 分钟，只能靠重启内核解除。

## 3. 发布（沿用自动发布，不手工推送）

1. 提交带 `Release-As: 1.12.3` trailer，push 到 `main`，等 `ci` / `release` workflow 成功。
2. `release` workflow 自动打 `v1.12.3` tag；确认 tag 的 commit 是 `main` 最新提交。
3. 服务器 `pioneer-portal-deploy.timer` 每 5 分钟轮询并切换符号链接。
4. 观察：

   ```bash
   tail -n 100 /var/log/pioneer-portal/deploy.log
   cat /srv/pioneer-portal.deploy/state/last_deployed_tag
   systemctl status pioneer-portal --no-pager
   ```

## 4. 首次安装用量同步 timer（每次主机一次）

timer 不归 deploy gateway 管（`deploy.sh` 只自动装 fingerprint worker 单元），需要手工安装，
且**必须在新版本已上线、`sync_cliproxy_usage` 命令存在之后**再启用。

```bash
cd /srv/pioneer-portal/deployment/gateway

install -m 0644 pioneer-portal-usage-sync.service /etc/systemd/system/
install -m 0644 pioneer-portal-usage-sync.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pioneer-portal-usage-sync.timer

# 立即跑一次，确认管理接口/数据库都通
systemctl start pioneer-portal-usage-sync.service
systemctl status pioneer-portal-usage-sync.service --no-pager
journalctl -u pioneer-portal-usage-sync.service -n 50 --no-pager

# 确认下一次触发时间在 30 秒内
systemctl list-timers pioneer-portal-usage-sync.timer --no-pager
```

- 单元以 `User=pioneer` 运行（与 `pioneer-portal.service` 一致），
  `EnvironmentFile=/srv/pioneer-portal/.env`，`TimeoutStartSec=60`。
- `Type=oneshot`：上一次没跑完不会叠加；漏掉的 tick 由下一次补上（队列保留 1 小时）。
- 单元文件放在 release 树里只随版本分发；`/etc/systemd/system` 下的副本不会自动更新，
  改动单元需要重新 `install` + `daemon-reload`。

## 5. 验证捕获率（发布后 30 分钟起）

```bash
# 门户侧：最近入库行数
sudo -u pioneer /srv/pioneer-portal/.venv/bin/python /srv/pioneer-portal/manage.py shell -c \
  "from usage.models import UsageLog; from django.utils import timezone; from datetime import timedelta;
print(UsageLog.objects.filter(request_time__gte=timezone.now()-timedelta(hours=1)).count())"

# 代理侧：同一小时的请求数
grep -c 'POST "/v1/' /opt/cliproxyapi/logs/main.log   # 或用下面的口径
grep 'gin_logger' /opt/cliproxyapi/logs/main.log | grep 'POST "/v1/' | wc -l

# 同步任务是否在跑
systemctl list-timers pioneer-portal-usage-sync.timer --no-pager
journalctl -u pioneer-portal-usage-sync.service --since '-10min' --no-pager | tail -30
```

通过标准：**同一小时内入库行数 ≈ `POST /v1/...` 次数**（允许因上游失败/非门户 key 少量差异）。
修复前该比例约为 1.5%，修复后应接近 100%。

## 6. 回滚

- 代码回滚：网关会自动切回上一版 release（`deploy.sh` 健康检查失败时）；本版本无 schema
  变更，回滚无需动数据库。
- timer 回滚：`systemctl disable --now pioneer-portal-usage-sync.timer` 即可停采，
  不影响门户本身；采集停止后账本回到“只有页面加载才同步”的状态。
- 内核回滚：`cp /opt/cliproxyapi/config.yaml.bak-20261103-162046 /opt/cliproxyapi/config.yaml
  && systemctl restart cliproxyapi`（重启会清空内存队列，未抽取的事件丢失）。
- retention 调小不会破坏数据，只会让窗口变短；调小后必须保持 timer 启用，否则重新漏采。
