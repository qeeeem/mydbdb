# 部署到 8.148.157.132

入口：`https://8.148.157.132:18443`（必须使用 HTTPS）。整个网站需要密码：用户名 `owner`，密码在首次安装时设置。首页、脚本、收藏数据和所有导入接口都在验证之后开放。

整个站点使用同一认证范围，浏览器保留登录信息后会自动复用，页面和导入操作不会分别要求输入密码。

防火墙只需放行 **TCP 18443**。Python 后端 `18081` 只监听 `127.0.0.1`，不要对公网开放。安装前检查两个端口，发现占用就退出，不停止占用进程。

## 将已安装的公开站点改为密码访问

在服务器管理员终端运行已上传的脚本：

```bash
sudo bash /home/ledger-deploy/mydbdb-require-login.sh
```

脚本来自本仓库 `deploy/require-login.sh`，仅删除 mydbdb 配置中的匿名浏览例外，复用现有密码。变更前备份到 `/etc/mydbdb/nginx.before-private.conf`，配置校验通过后平滑重载 Nginx，并检查页面和数据均返回 401；其他站点配置不变。不要重跑初始化安装。

部署账号只有重启 mydbdb 的 sudo 权限，不能执行这项 Nginx 迁移。先由管理员完成，再运行新版本 Actions；工作流会要求整站密码保护生效。

## 首次安装

在服务器管理员终端执行：

```bash
curl --fail --location https://raw.githubusercontent.com/qeeeem/mydbdb/main/deploy/setup.sh -o /tmp/mydbdb-setup.sh
sudo bash /tmp/mydbdb-setup.sh 18443 18081
```

安装需要现有 Nginx、Python 3.10+、OpenSSL、curl、systemd、sudo、flock，并复用服务器已有的 IP 证书 `/etc/nginx/ssl/8.148.157.132/`。脚本不安装系统包、不变更证书续期、不覆盖已有文件。运行时设置网站访问密码，不要使用 NeoDB 密码。

当前服务器的系统 Python 是 3.6。Alibaba Cloud Linux 3 可并行安装 Python 3.11，并作为第三个参数指定，**不替换系统 Python，不修改已有应用环境**：

```bash
sudo dnf install -y python3.11
sudo bash /tmp/mydbdb-setup.sh 18443 18081 /usr/bin/python3.11
```

如果服务器无法连接 `raw.githubusercontent.com`，在本机通过已授权的 SSH 上传脚本，不需要在服务器重试下载或关闭 TLS 校验：

```powershell
scp -i <部署私钥路径> -o StrictHostKeyChecking=yes deploy/setup.sh ledger-deploy@8.148.157.132:/home/ledger-deploy/mydbdb-setup.sh
```

然后在服务器管理员终端运行 `sudo bash /home/ledger-deploy/mydbdb-setup.sh 18443 18081 /usr/bin/python3.11`。服务启动、发布检查都使用指定解释器。

脚本新增 `/etc/nginx/conf.d/mydbdb.conf`，检查配置后平滑重载 Nginx；不改现有 `80`、`443`、`8088` 站点配置。后续 Actions 不再修改或重载 Nginx。

将本项目部署公钥添加到 `ledger-deploy` 的 `authorized_keys`，保留已有公钥，并加 `restrict` 前缀。该用户新增的 sudo 权限只有：

```text
/usr/bin/systemctl restart mydbdb.service
```

在 **mydbdb** 仓库设置 Actions secrets；ledger 仓库的同名 Secret 不会自动共享：

| Secret | 内容 |
| --- | --- |
| `LEDGER_SSH_KEY` | 获准登录 `ledger-deploy` 的部署私钥 |
| `LEDGER_KNOWN_HOSTS` | 已核实的 `8.148.157.132` SSH 主机公钥记录 |

不要把私钥或 NeoDB 令牌粘贴到代码、Issues 或 Actions 日志。工作流使用严格 SSH 主机校验，不自动信任扫描到的密钥。

首次部署前，通过 SSH/SFTP 将本地 `media_data.json` 上传到 `/var/lib/mydbdb/media_data.json`。原始 ZIP 和账号私钥都不要上传。允许导入的账号另存为服务用户私有的 `/var/lib/mydbdb/import-owner.json`，不写入 Git。可先在本机运行 `python server.py --set-import-owner data/export.zip`，再用已验证 SSH 将生成的 `../.mydbdb-local/import-owner.json` 上传到该路径，权限设为 `600`。网页不能设置或更换允许导入的账号。

然后在 [Deploy mydbdb](https://github.com/qeeeem/mydbdb/actions/workflows/deploy.yml) 选择 **Run workflow → main**。安装只建空数据库，数据上传和首次部署完成后才能看到收藏。

## 代码与数据分开保存

| 路径 | 用途 |
| --- | --- |
| `/opt/mydbdb/releases/<版本>/` | 每次发布的代码，保留旧版本 |
| `/opt/mydbdb/current` | 当前版本符号链接 |
| `/var/lib/mydbdb/media_data.json` | 持久化收藏 |
| `/var/lib/mydbdb/import-owner.json` | 导入归属配置，仅服务用户可读，不对外返回 |
| `/var/lib/mydbdb/web-import.json` | 上次网页成功导入的 ZIP 哈希 |
| `/var/lib/mydbdb/backups/` | 每次网页确认更新前的收藏备份 |
| `/var/lib/mydbdb-deploy/` | 上传包、发布锁、版本记录 |
| `/etc/mydbdb/` | 端口配置、管理员密码散列 |
| `/etc/systemd/system/mydbdb.service` | 独立服务，随机器启动 |

Actions 只上传页面、前端脚本、Python 源码、随附 ZIP 组件和版本标识。部署前运行测试；发布后校验后端版本、数据可读性、HTTPS 证书、页面、数据和全部 API 的未登录 401 响应。失败时自动切回旧代码；首次发布没有旧版本时保留错误供排查。收藏与私有配置不会随发布回退。

更新收藏优先使用页面「数据管理」：选择 ZIP，核对预览后确认。先由 Nginx 核验管理员密码，再由后端私有配置核验导出归属；失败只返回拒绝，不说明账号。导入自动备份并原子替换，不需要暂停服务。ZIP 在浏览器剔除账号私钥和附件，仅上传两份收藏文件；通过分片保持每次请求低于 64 KiB，无需调整现有 Nginx 上传限制。ZIP 组件也由受管理员验证保护的 `/api/import/library.js` 提供，无需改 Nginx 或开放新端口。

所有 `/api/` 路径均需要管理员身份验证，包括状态、ZIP 组件、上传分片、预览和确认；不能通过绕过页面直接上传。后端只监听回环地址，不向公网开放。网页 ZIP 导入无需访问 NeoDB。备份不自动删除，管理员按自己的保留策略管理。

## 改端口

首次安装可将命令参数改成其他空闲端口，例如 `sudo bash /tmp/mydbdb-setup.sh 19443 19081`。同时把仓库 Actions variable `MYDBDB_PORT` 改为 `19443`，并放行该端口。

已安装后不要重跑初始化脚本。管理员须同时修改 `/etc/mydbdb/deploy.conf`、`mydbdb.service` 的 `--port`/`--public-origin`、`mydbdb.conf` 的 `listen`/上游地址；检查新端口后执行 `nginx -t`、`systemctl daemon-reload`、重启 mydbdb、平滑重载 Nginx，再修改 Actions variable。工作流发现端口与服务器配置不一致会停止，不擅自迁移。

## 查看状态与手动回退

```bash
systemctl status mydbdb.service
journalctl -u mydbdb.service -n 50 --no-pager
curl --fail http://127.0.0.1:18081/_health
```

手动回退时，在管理员终端将第一行改为实际旧版本号：

```bash
release=旧版本号
test -f "/opt/mydbdb/releases/$release/server.py" || exit 1
exec 9>/var/lib/mydbdb-deploy/deploy.lock
flock -w 120 9
ln -sfn "/opt/mydbdb/releases/$release" /opt/mydbdb/current.next
mv -Tf /opt/mydbdb/current.next /opt/mydbdb/current
systemctl restart mydbdb.service
curl --fail http://127.0.0.1:18081/_health
```

参考：[GitHub Actions Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)、[Nginx HTTP Basic Authentication](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)、[阿里云并行安装 Python 3.11](https://www.alibabacloud.com/help/en/alinux/support/install-a-newer-python-3-on-alibaba-cloud-linux-3)。
