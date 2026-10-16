# 部署到 8.148.157.132

入口：`https://8.148.157.132:18443`。网站用户名为 `owner`，密码在首次安装时设置。整个网站需要登录，包括收藏 JSON 和添加接口。

防火墙只需放行 **TCP 18443**。Python 后端 `18081` 只监听 `127.0.0.1`，不要对公网开放。安装前检查两个端口，发现占用就退出，不停止占用进程。

## 首次安装

在服务器管理员终端执行：

```bash
curl --fail --location https://raw.githubusercontent.com/qeeeem/mydbdb/main/deploy/setup.sh -o /tmp/mydbdb-setup.sh
sudo bash /tmp/mydbdb-setup.sh 18443 18081
```

安装需要现有 Nginx、Python 3.10+、OpenSSL、curl、systemd、sudo、flock，并复用服务器已有的 IP 证书 `/etc/nginx/ssl/8.148.157.132/`。脚本不安装系统包、不变更证书续期、不覆盖已有文件。运行时设置网站密码，不要使用 NeoDB 密码。

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

首次部署前，通过 SSH/SFTP 将本地 `media_data.json` 上传到 `/var/lib/mydbdb/media_data.json`。原始 ZIP 和 NeoDB 令牌都不用上传；首次打开网站后，在现有添加窗口连接账号。

然后在 [Deploy mydbdb](https://github.com/qeeeem/mydbdb/actions/workflows/deploy.yml) 选择 **Run workflow → main**。安装只建空数据库，数据上传和首次部署完成后才能看到收藏。

## 代码与数据分开保存

| 路径 | 用途 |
| --- | --- |
| `/opt/mydbdb/releases/<版本>/` | 每次发布的代码，保留旧版本 |
| `/opt/mydbdb/current` | 当前版本符号链接 |
| `/var/lib/mydbdb/media_data.json` | 持久化收藏 |
| `/var/lib/mydbdb/neodb.json` | NeoDB 授权，仅服务用户可读 |
| `/var/lib/mydbdb-deploy/` | 上传包、发布锁、版本记录 |
| `/etc/mydbdb/` | 端口配置、网站密码散列 |
| `/etc/systemd/system/mydbdb.service` | 独立服务，随机器启动 |

Actions 只上传首页、前端脚本、两份 Python 源码和版本标识。部署前运行测试；发布后校验后端版本、数据可读性、HTTPS 证书和未登录访问的 401 响应。失败时自动切回旧代码；首次发布没有旧版本时保留错误供排查。收藏与授权不会随发布回退。

需要用新 ZIP 替换服务器收藏时，先在本地解析并核对，再暂停 **mydbdb** 服务、备份服务器 JSON、传入新 JSON 并重启；不要直接覆盖运行中的数据，避免丢失刚添加的记录。普通代码推送无需这个步骤。

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

参考：[GitHub Actions Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)、[Nginx HTTP Basic Authentication](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)。
