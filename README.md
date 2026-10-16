# 我的书影音

## 在页面导入 ZIP

访问网站时先使用 `owner` 和网站密码登录，再点击右上角「数据管理」。首页、收藏数据和导入接口均受密码保护。

整站共用同一次登录，浏览器保留登录信息期间，切换页面、预览和确认导入无需重复输入密码。

上传先通过管理员身份验证，再由后端按私有配置核验导出归属。允许导入的账号不写入前端、接口响应或仓库；不符合权限时只显示「已拒绝」。

- **ZIP 导入**：选择一个 NeoDB NDJSON 导出 ZIP，点击「预览导入」。未选文件或与上次网页成功导入的 ZIP 的 SHA-256 相同，数据不变。新 ZIP 显示新增、修改、移除及总数，点击「确认更新本站」才替换收藏。
- **取消或失败**：不改变现有收藏。预览后若收藏数据已变化，需要取消并重新导入，避免覆盖新记录。

ZIP 上限 64 MiB，所需文件解压后合计不超过 96 MiB。浏览器只读取并上传 `catalog.ndjson` 和 `journal.ndjson`；不解压或上传含私钥的 `actor.ndjson` 和附件。服务器再次验证导出账号和收藏记录归属。原始 ZIP 不保存到服务器。

确认更新前自动备份当前数据到状态目录的 `backups/`；本地为 `../.mydbdb-local/backups/`，部署后为 `/var/lib/mydbdb/backups/`。网页导入哈希保存在同目录 `web-import.json`，与下文命令行的哈希记录独立。预览在内存中保留 30 分钟；重启服务后需重新预览，已保存的收藏不受影响。

导入包含全部收藏及短评，包括 NeoDB 上设为非公开的收藏。网站需要密码才能查看；确认导入前请核对预览。

网页 ZIP 导入不需要 NeoDB 令牌，也不依赖服务器连接 NeoDB。ZIP 组件使用随代码部署的 [fflate 0.8.3](https://github.com/101arrowz/fflate)，许可证见 `vendor/fflate.LICENSE`，无需第三方 CDN。

## 从 NeoDB 导出包更新收藏

将 NeoDB 导出的 ZIP 放进 `data/` 文件夹（不用解压），然后在项目目录运行：

```powershell
python fetch_posters.py
```

需要 Python 3.10 或更高版本，无须安装依赖。每次运行只检查 `data/` 顶层的 ZIP，不按文件名或修改时间判断新旧：

- **0 个 ZIP**：保持当前数据不变。
- **1 个 ZIP，SHA-256 未变化**：跳过解析，不改写 JSON，也不改变其修改时间。
- **1 个 ZIP，首次导入或 SHA-256 变化**：解析成功后更新 JSON，并记录哈希。即使文件名相同，内容变化也会更新；仅改名不会更新。
- **多个 ZIP**：报错并保留当前数据，请只留下要导入的一个 ZIP。

哈希记录保存在 `data/.import-state.json`，分别记录各输出路径最后成功导入的 ZIP。如果输出文件被删除，即使 ZIP 未变也会重新生成。解析失败不更新数据或哈希。

生成的 `media_data.json` 仍在项目根目录，由 `index.html` 的统计、收藏和时间轴共同读取。脚本每次运行检查一次，不在后台持续监听；放入新 ZIP 后再次运行，并刷新页面即可。

默认复用现有 JSON 中的海报链接，新增条目使用导出包的 `cover_image_url`。解析过程不联网、不需要 API 密钥，也不下载图片；浏览器显示封面仍需网络。完全重新使用 NeoDB 封面：

```powershell
python fetch_posters.py data/export.zip --no-poster-cache
```

显式指定 ZIP 路径会直接解析，不受哈希检查限制，也不修改自动检查的哈希记录。另存结果而不覆盖页面数据：

```powershell
python fetch_posters.py data/export.zip -o preview.json --poster-cache media_data.json
```

## 本地打开页面

在项目目录启动本地服务，而不是双击 HTML 或使用 `python -m http.server`：

```powershell
python server.py
```

浏览器访问 <http://127.0.0.1:8000>，查看「统计」「收藏」或「时间轴」。

统计页从同一份 JSON 汇总全部记录的分类、状态，以及按标记日期分组的每日、月度和年度数量。年份选项随数据生成，首次加载默认显示最新记录所在年份。没有日期的记录计入总数，但不计入日期图表。

首次本地导入前，由管理员使用可信导出包配置导入权限：

```powershell
python server.py --set-import-owner data/export.zip
```

配置写入网站目录之外的 `../.mydbdb-local/import-owner.json`，不是从网页上传内容自动绑定。配置缺失时拒绝导入。本地服务仅允许本机访问；公网部署由 Nginx 验证网站密码，页面、数据和所有导入接口（包括分片及确认）都在验证之后开放。

端口被占用时可运行 `python server.py --port 8001`，并打开对应端口。服务只监听本机，不使用旧 ngrok 地址；只提供页面、脚本和展示数据，不提供 ZIP、账号配置或目录列表。

## 数据对应关系

- `catalog.ndjson` 提供标题、分类、发行年份、封面和 UUID。
- `journal.ndjson` 的 `ShelfMember` 决定当前收藏和状态；按完整条目 URL 与目录关联，不按标题合并。`ShelfLog` 是历史变化，不能再次计入收藏；没有当前收藏的目录条目不导入。
- `Rating.value` 是个人 10 分制评分；不使用目录中的站点评分。未评分输出 `null`。
- `Comment.content` 保留短评原文和换行，未评论输出空字符串。
- `timestamp` 取 `ShelfMember.content.published` 的日期，保留导出的日历日期，不转换时区。不使用导入或编辑时间 `updated`，避免历史记录集中到导入日。
- `year` 始终是字符串，依次取 `year`、`pub_year`、`release_year`、`release_date` 或 `opening_date` 中的年份；缺失时为空字符串。
- 同一 UUID 优先复用旧海报。旧 JSON 没有 UUID 时，只在标题、分类、年份均匹配且两侧唯一时复用。缓存仅提供海报，状态、评分、评论和日期均以 ZIP 为准。

输出包含现有页面所需的八个字段，并增加 `uuid` 用于下次精确匹配海报。解析完成后才替换输出文件；格式错误、缺失目录条目或不支持的分类/状态会报错并保留原文件。

## 备份与发布

原始 ZIP 含账号私钥，不要上传或放到公开静态站点。脚本只读取目录和日志，不读取 `actor.ndjson`，不解压附件；`.gitignore` 已排除原始备份和 NDJSON 文件。

生成的 JSON 包含导出包中的所有当前收藏，包括非公开收藏及其个人短评。`media_data.json` 与 ZIP 均不随代码提交；本地文件保持原位。旧 Git 历史中已经发布的 JSON 不会因为取消跟踪而消失。

## 自动部署

推送代码到 `main` 后，GitHub Actions 先测试，再部署到独立的 HTTPS 端口 **18443**。首次需要在服务器安装专用服务、设置网站访问密码并上传收藏数据，见 [部署指南](deploy/README.md)。

服务器端浏览收藏、统计、时间轴及导入 ZIP 均需要密码。已移除新增条目和 NeoDB 同步界面及接口；无需保存 NeoDB 令牌。后续代码部署不会覆盖收藏和私有导入配置。

## 测试

```powershell
python -m unittest discover -s tests -v
node --test tests/dashboard.test.cjs tests/add.test.cjs
```
