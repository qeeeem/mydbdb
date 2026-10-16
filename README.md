# 我的书影音

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
python fetch_posters.py data/neodb_AriVo_20261015093734_ndjson.zip --no-poster-cache
```

显式指定 ZIP 路径会直接解析，不受哈希检查限制，也不修改自动检查的哈希记录。另存结果而不覆盖页面数据：

```powershell
python fetch_posters.py data/neodb_AriVo_20261015093734_ndjson.zip -o preview.json --poster-cache media_data.json
```

## 本地打开页面

在项目目录启动本地服务，而不是双击 HTML 或使用 `python -m http.server`：

```powershell
python server.py
```

浏览器访问 <http://127.0.0.1:8000>，查看「统计」「收藏」或「时间轴」。

统计页从同一份 JSON 汇总全部记录的分类、状态，以及按标记日期分组的每日、月度和年度数量。年份选项随数据生成，首次加载默认显示最新记录所在年份。没有日期的记录计入总数，但不计入日期图表。

端口被占用时可运行 `python server.py --port 8001`，并打开对应端口。服务只监听本机，不使用旧 ngrok 地址；只提供页面、脚本和展示数据，不提供 ZIP、账号配置或目录列表。

## 用现有「＋ 添加条目」同步 NeoDB

1. 点击右上角「＋」，可按电影、剧集、书籍、游戏、音乐、演出或播客搜索标题。
2. 首次使用时，在同一弹窗点击「获取 NeoDB 访问令牌」，登录你的 `neodb.social` 账号，从开发者控制台取得具备 `read`、`write` 权限的令牌。粘贴到弹窗的密码输入框，点击「连接 NeoDB」，核对显示的账号。
3. 选择搜索结果，填写状态、评分、短评和可见范围，点击「添加并同步 NeoDB」。新条目默认仅自己可见，不额外发布到联邦时间线。

令牌只保存在项目目录之外的 `../.mydbdb-local/neodb.json`（即 `temp/.mydbdb-local/neodb.json`），不写入 HTML、展示 JSON、日志或 Git。这样旧的项目静态服务器也不会提供令牌文件。不要把令牌发到聊天里。授权过期时，弹窗会要求重新连接。

已有收藏会先读取并填入原状态、评分、短评和可见范围，按钮显示「更新并同步 NeoDB」。保存时保留 NeoDB 原标签和标记日期，本站按 UUID 更新，不重复添加。

服务先提交 NeoDB，再读取确认结果，最后更新 `media_data.json`；统计、收藏和时间轴随后一起刷新。失败会保留表单并显示具体原因。如果提示「已同步 NeoDB，但本地保存失败」，修复后再次点击确认；服务会先核对远端，内容相同就只补齐本地文件，不重复提交。

搜索无需授权；写入个人收藏必须先连接账号。导入新的 ZIP 仍按导出快照替换本地数据，因此添加条目后请使用新导出的 ZIP，避免旧快照覆盖本站新增记录；NeoDB 上已同步的记录不受本地导入影响。

接口依据：[NeoDB 授权文档](https://neodb.net/api/)、[目录搜索实现](https://github.com/neodb-social/neodb/blob/a945c3fa09b6a69e20daceb345d2de36700987b0/neodb/catalog/apis.py)、[收藏读写实现](https://github.com/neodb-social/neodb/blob/a945c3fa09b6a69e20daceb345d2de36700987b0/neodb/journal/apis/shelf.py)。

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

推送代码到 `main` 后，GitHub Actions 先测试，再部署到独立的 HTTPS 端口 **18443**。首次需要在服务器安装专用服务、设置添加／同步操作的管理员密码并上传收藏数据，见 [部署指南](deploy/README.md)。

服务器端浏览收藏、统计和时间轴无需密码，收藏与短评公开可见。仍使用现有「添加条目」窗口，只有添加／同步时需要管理员验证，再连接 NeoDB；添加后同步远端并更新服务器数据。后续代码部署不会覆盖收藏或 NeoDB 授权。本地 `python server.py` 的用法不变。

## 测试

```powershell
python -m unittest discover -s tests -v
node --test tests/dashboard.test.cjs tests/add.test.cjs
```
