<div align="center">

# SIM at Home

[English](README.md) · **中文**

**把只用来收验证码的实体 SIM 留在家里路由器的 4G 棒上，让短信转发到手机。**

Python 标准库 + curl · OpenWrt · SQLite · [MIT](LICENSE)

</div>

## 使用范围

**仅供个人自用：一张你本人实名的卡，插在你自己家里，只转发给你自己。不代收他人短信。**

中国移动的号码可以先试「无忧行」号码托管收验证码，不行再用这套。已有调研记录的条件包括：号码正常无欠费、先开国际或港澳台漫游、实体 SIM 离线后开启；部分记录要求在境外开通。它不能发短信，实体卡重新联网会取消托管，也依赖 App 在线。实际能否开通，以你的号码和 App 当前要求为准。

## 为什么做这个

只用来收验证码的卡，不必一直跟着手机走。家里的路由器可以代你读取短信、发送提醒，并保留本地历史。

推送可能漏掉，所以每条短信都**先写入本地 SQLite，再发送通知**。数据库是权威副本，通知只是提醒你查看。

## 有哪些功能

- SQLite 短信库和独立通知队列，每条短信、每个通知目标分别重试。
- 首次运行只建立基线：保存已有短信，不把旧验证码重新推送。
- 支持 ntfy（推荐自托管）或 Bark 通知。
- 可从 root-only 凭据文件自动读取 SIM PIN 解锁，并用持久保护锁限制尝试。
- 可选设备清理：达到 90 条时，核对数据库、只读快照和通知状态后，删除最旧 30 条。
- 可选只读收件箱网页和 API，默认只监听 `127.0.0.1`。
- 正常服务日志不打印短信正文；只有显式传入 `--show-messages` 才显示。

## 测过的硬件

| 组件 | 实测环境 |
| --- | --- |
| 路由器 | FriendlyElec NanoPi R3S |
| 系统 | iStoreOS，基于 OpenWrt |
| 4G 棒 | 中兴 ZTE MF833U1，USB ID `19d2:1706` |
| 网卡驱动 | `cdc_ether` |
| 设备管理地址 | `192.168.0.1`（出厂默认） |

只测过单根 MF833U1。其他路由器、4G 棒及固件组合没有验证过。

## 工作方式

```text
你的 SIM → MF833U1 → Python 轮询 → SQLite 短信库 + 通知队列
                                      ├─→ ntfy / Bark → 你的手机
                                      └─→ 只读 SQLite 快照
                                                 └─→ 可选收件箱网页
```

设备暂时不可达时，已有通知队列仍会重试。`accepted_upstream` 只表示通知服务接受了请求，不证明手机已经显示。通知采用至少一次投递：如果上游接受后进程退出，来不及记下成功状态，恢复后可能重复提醒。

## 安装

以下步骤在路由器上以 root 操作，从本仓库目录执行。安装后的服务名和配置目录沿用 `sim-card-forwarding`。

### 1. 安装依赖

只使用 Python 标准库和 curl，不需要 pip 包。OpenWrt 把标准库拆成了多个软件包：

```sh
opkg update
opkg install python3-light python3-urllib python3-openssl python3-codecs python3-sqlite3 curl
```

路由器固件还需要支持设备使用的 `cdc_ether` 驱动。

### 2. 建立专用设备接口

先确认 4G 棒的实际网卡名；`eth2` 是实测示例。辅助脚本检查 `cdc_ether` 驱动、备份 `/etc/config/network`，然后创建 `mf833u1_1`，但不启动接口：

```sh
sh openwrt/restore-interface.sh eth2
```

启动前核对以下选项。接口已经存在时也要核对，因为脚本会保留已有配置：

```text
network.mf833u1_1.proto=dhcp
network.mf833u1_1.device=eth2
network.mf833u1_1.defaultroute=0
network.mf833u1_1.peerdns=0
network.mf833u1_1.delegate=0
```

**专用接口必须保持 `defaultroute=0`、`peerdns=0`、`delegate=0`。** 否则 4G 棒可能抢走默认路由或 DNS。该接口应与家庭 LAN 隔离，不要桥接进 LAN，也不要对外开放设备管理页。

```sh
ifup mf833u1_1
```

确认正常上网的默认路由没有改变。

### 3. 安装程序并配置

```sh
sh openwrt/install.sh
cp /etc/sim-card-forwarding/instances.d/modem-1.conf.example \
   /etc/sim-card-forwarding/instances.d/modem-1.conf
chmod 600 /etc/sim-card-forwarding/instances.d/modem-1.conf
```

安装器不会自动启用或启动两个服务。在路由器上编辑 `modem-1.conf`：

- 保持 `id=modem-1`，自定 `label`。
- 按自己的设备填写 `interface`、`expected_mac` 和 `expected_usb_path`。全零 MAC 是占位值，启用 PIN 自动解锁前必须替换；实测型号的 `expected_usb_id` 为 `19d2:1706`。
- `cellular_data=off` 会维持手动拨号、断开蜂窝数据，适合只收短信的卡；`keep` 则不干预数据连接。
- 按下一节选择通知。示例默认是 `mode=none`、`content=summary`。
- 保留 `first_run=baseline`，首次只存已有短信、不推送；`retention_days=0` 表示不自动删除本地历史。

### 4. 选择通知方式

**ntfy** 推荐使用自己的 HTTPS 服务器、私有主题和发布 Token；手机使用同一主题的只读权限订阅。服务器 URL 不得带账号密码、子路径、查询或 fragment。

```ini
[notifications]
mode=ntfy
content=full
ntfy_server=https://ntfy.example.com
ntfy_topic=personal-sms
ntfy_token=REPLACE_WITH_YOUR_PUBLISHER_TOKEN
```

**Bark** 填写自己的设备 Key 和对应服务器：

```ini
[notifications]
mode=bark
content=full
bark_server=https://api.day.app
bark_key=REPLACE_WITH_YOUR_BARK_KEY
```

以上内容用于修改已有的 `[notifications]` 段，不要重复追加同名段。`summary` 只提醒有新短信；`full` 包含号码和正文，通知服务器及手机都能看到这些内容。凭据只放在 root 所有、权限 `0600` 的配置文件里，不写进仓库或命令参数。

### 5. 按需设置 PIN 自动解锁

SIM 有 PIN 保护时，用隐藏输入交互保存：

```sh
sh openwrt/set-pin-credential.sh modem-1
```

PIN 写到 `/etc/sim-card-forwarding/credentials.d/modem-1.pin`，文件权限 `0600`，父目录为 root-only `0700`。服务默认带 `--auto-unlock`，提交 PIN 前会核对网卡、MAC、USB 路径和 USB ID。

PIN 被拒绝后不再自动尝试。请求结果不确定时也会阻止重试，除非设备仍报告完整的 3 次机会，且配置身份一致。失败后应人工检查，不要反复清锁尝试猜测的 PIN。

### 6. 检查并启动

先做只读检查，不保存短信、不推送、不解锁 SIM：

```sh
python3 /usr/libexec/sim-card-forwarding/mf833u1_forwarder.py --once --dry-run
```

再启动服务：

```sh
/etc/init.d/sim-card-forwarding enable
/etc/init.d/sim-card-forwarding start
logread -e sim-card-forwarding
```

服务每 10 秒轮询并导出只读快照。权威库位于持久目录 `/overlay/sim-card-forwarding/state-data/inbox.sqlite3`，权限 `0600`。第一次成功运行建立基线后，再发一条新的测试短信，在手机上核对实际到达。

## 给设备短信腾空间

实测 MF833U1 最多存 100 条，满了就收不到新短信。**示例配置默认关闭自动清理**（`device_cleanup_threshold=0`）。需要启用实测策略时，修改已有 `[runtime]` 段并重启转发服务：

```ini
[runtime]
device_sms_capacity=100
device_cleanup_threshold=90
device_cleanup_batch=30
```

达到 90 条时，程序精确选择最旧 30 条，检查 SQLite 完整性、权威库和只读快照均有完整正文、这些短信的所有已有通知任务均为 `accepted_upstream`。删除前再次核对设备 ID 与内容，删除后验证其他短信仍在。副本缺失、通知待重试、设备记录变化或没有快照时，均拒绝清理。

清理只删除设备上的副本；本地 SQLite 历史由另行设置的保留策略决定。即使不开收件箱网页，清理也需要只读快照；随附转发服务脚本已配置导出。

## 可选：只读收件箱

`inbox_server.py` 提供 `GET /`、`GET /api/v1/messages`、`GET /api/v1/status`，网页按游标加载历史。随附收件箱服务需要手动配置后才能启用。

1. 用路由器的账号管理工具创建非 root 的 `sim-inbox` 用户和组；实测环境使用了 `shadow-useradd`，安装器不会代建账号。
2. 将 `/overlay/sim-card-forwarding/inbox-view/` 设为 `root:sim-inbox`、权限 `2750`；导出的 `inbox.sqlite3` 为 `root:sim-inbox`、权限 `0640`。`/etc/sim-card-inbox/` 为 `root:sim-inbox`、权限 `0750`。
3. 创建 `/etc/sim-card-inbox/users.allow`，属主属组为 `root:sim-inbox`、权限 `0640`，每行仅填写你自己的精确 Tailscale 登录名。`owner@example.com` 只是占位示例。
4. 等转发器导出新快照，再核对后续原子替换后文件权限和属组仍正确。
5. 按已安装版本的语法，把**私有 Tailscale Serve** HTTPS 映射到 `127.0.0.1:8787`。后端依赖 Serve 注入身份头，再与 allowlist 精确比对。
6. 启用并启动 `/etc/init.d/sim-card-inbox`，验证只有你的允许身份能访问，监听地址只有 loopback。

后端保持 `127.0.0.1`，不要直接暴露给 LAN 或公网，否则客户端可伪造身份头。收件箱不使用公开 Funnel 映射。网页服务只读导出副本，不应有权读取 PIN、通知凭据或权威库。

## 已知限制

- **不能发短信。** 硬件可以发送，当前软件没有实现。
- 只验证过上面这套单根 MF833U1 环境。
- 上游接受不等于手机已显示，重试期间也可能重复提醒。
- PIN 自动解锁需要正确的设备身份和已确认的 PIN，错误或不确定的尝试会触发保护。
- 没有 MMS 取件客户端，二进制彩信通知可能保留为十六进制文本。

## 开发

```sh
python3 -m unittest discover -s tests
```

## 致谢与许可

实现参考了 [czwstc/ZTE-MF833U1-SMSForwarder](https://github.com/czwstc/ZTE-MF833U1-SMSForwarder)。

[MIT](LICENSE)，Copyright (c) 2026 ZerbLion。上游版权声明和完整 MIT 许可见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
