# copymanga-nasdownloader

![social-media](./assets/social-media.png)

<p align="center">
  <!--<a href="https://pypi.org/project/copymanga-downloader/" target="_blank"><img alt="PyPI - Version" src="https://img.shields.io/pypi/v/copymanga-downloader?style=for-the-badge&logo=PyPI"></a>-->
  <a href="https://github.com/misaka10843/copymanga-nasdownloader/graphs/contributors" target="_blank"><img alt="GitHub contributors" src="https://img.shields.io/github/contributors/misaka10843/copymanga-nasdownloader?style=for-the-badge&logo=github"></a>
  <a href="https://github.com/misaka10843/copymanga-nasdownloader/stargazers" target="_blank"><img alt="GitHub Repo stars" src="https://img.shields.io/github/stars/misaka10843/copymanga-nasdownloader?style=for-the-badge&label=%E2%AD%90STAR"></a>
</p>

## 前言💭

此仓库原先是[copymanga-downloader](https://github.com/misaka10843/copymanga-downloader)的重构版本，但是随着功能的更新已经逐渐与原先的仓库开发方向分道扬镳

之后会着重与多漫画源+自动化下载，当前WebUI已可以正常使用，并可以设定cron定时任务等功能

**非常非常非常不建议您每次运行时间间隔小于5天！！！！**

您需要注意的一点，为了尽可能不影响到相关站点，此程序并不会也永不会支持多线程处理，请多多支持对应站点，小站真的不容易

当前通知功能是按照[message-pusher](https://github.com/songquanpeng/message-pusher)的规范进行适配，可能并不会适配其他主流平台，请注意

## 当前支持站点

- [x] [copymanga](https://copymanga.com)
- [x] [泰拉记事社](https://terra-historicus.hypergryph.com)
- [x] [antbyw](https://www.antbyw.com)
- [x] [ガンガンONLINE](https://www.ganganonline.com/) (建议仅作为最新生肉漫画的免费下载)

## 如何使用

### 漫画源分页、搜索与筛选更新

「漫画源」默认打开全部漫画，支持题材、地区、连载状态与排序；点击“应用筛选”后从第一页开始。全部漫画、排行榜与搜索均支持上一页/下一页，每页 20 条不是总数量上限。搜索可选名称、作者和别名范围；分类筛选用于全部漫画，不与名称搜索混用。从详情返回会保留当前列表及位置。

搜索使用当前官网公开接口 `/api/kb/web/searchcl/comics`；旧 `/api/v3/search/comic` 在验证时对已知漫画返回空结果。排行榜转发 `limit`、`offset` 并使用上游总数。全部漫画与筛选读取官网 `/comics` 的公开列表数据，不自动抓取所有页。所有请求继续共享限流，并使用现有 HTTP 代理。

系统设置新增“CopyManga 官网地址”，默认 `https://www.copy4000.com`，也可通过 `CMNAS_CM_WEB_URL` 配置；API 地址仍用于搜索、排行、分类选项和图片内容接口。旧设置缺少新字段时使用默认官网地址，原 Docker 卷和订阅格式无需改变。自行编写 Compose 时如需覆盖官网地址，将 `CMNAS_CM_WEB_URL` 传入容器环境变量；仅修改宿主 `.env` 不会自动注入未声明的环境变量。

简介、封面、分组和章节目录现在直接从配置的 `CMNAS_CM_WEB_URL` 官网匿名获取，不再要求 APP 详情 API 或登录成功。程序复用同一个网页会话，动态读取目录参数并解析完整目录；WebUI 分页只是展示切片，添加订阅和定时检查使用同一份完整目录。成功目录缓存 30 秒以减少重复请求；订阅检查强制刷新，修改官网地址或代理后不会复用旧缓存。只浏览目录不会请求阅读页或下载图片。

官网返回空目录时会在新会话中重试一次；仍为空、缺失章节或无效数据时保留简介，并显示目录获取失败，不添加订阅、不推进完成记录。旧订阅仍兼容，但目录中找不到最后完成章节或出现同名章节时会报告检查失败，避免跳过章节。章节类型（话/卷/番外）与分组分别保留，不凭类型伪造分组。**目录恢复不代表图片下载限制解除**：章节图片 API 返回 210 时，下载器尝试官网公开章节页提供的图片列表；只有图片数量完整且地址有效才继续，官网也没有数据时保留失败状态。账号登录只在实际下载阶段发生，并读取当前代理和 API 配置；登录失败不会开始下载或推进记录。

从 v0.1.6 起，CopyManga 的图片下载也使用当前配置的代理和官网章节来源地址，不向图片 CDN 发送账号 Token。官网回退的临时图片目录以 `.__website` 结尾，避免与 API 的部分图片混用；失败时保留临时图片，最终 CBZ 名称不变。详细验证范围和限制见 [章节下载恢复说明](docs/copymanga-download-recovery.md)。

WebUI 的「漫画源」页可以浏览 CopyManga 日/周/月/总榜或搜索漫画，查看封面、简介、作者、分组与章节目录，并直接加入「我的订阅」。添加时可选择从头下载、只追踪以后的更新，或点击目录中的一章作为下载起点（包含该章）。保存订阅不会立即下载；可使用现有「立即运行」按钮或定时任务。无需先导入账号收藏，也不提供在线阅读。浏览请求同样受 `CMNAS_CM_RATE_LIMIT_PER_MINUTE` 限流。详细范围见 [更新规划](docs/COPYMANGA_BROWSE_PLAN.md)。

若「漫画源」显示红色错误，请按提示区分原因：`210` 是 CopyManga 站点限制当前请求，`429` 是请求过于频繁，连接失败则应检查 NAS 的网络、代理和 API 地址。页面上的「检查 API 设置」可跳转到「系统设置」，在 CopyManga API 地址中选择常见域名或填写自定义地址，然后点击「保存并生效」再重试。域名可切换并不保证绕过站点的 210 限制；简介和目录失败时应检查官网地址与代理；图片内容 API 能否使用仍取决于站点响应。更新镜像后保留原有 `/data`、`/downloads`、`/cbz` 映射即可，无需重新添加订阅。

正式版镜像位于 Docker Hub：`montequilla/copymanga-nasdownloader:latest`。每次正式发布同时提供明确版本号（如 `:v0.1.3`）、同一版本线的更新标签（如 `:v0.1`）和对应提交的完整 SHA 标签。`latest` 与版本号标签指向最新正式版；开发分支只更新 `:preview`，不会覆盖正式版。GHCR 的 `ghcr.io/everink404/copymanga-nasdownloader` 使用同样的正式版标签，开发分支使用 `:browse-preview`。镜像更新后需重建容器才能运行新版；保留 `/data`、`/downloads`、`/cbz` 卷映射和 8000 端口，已有订阅无需迁移。旧修复版仍可通过其 SHA 标签 `ghcr.io/everink404/copymanga-nasdownloader:sha-9b9530762acc6c1c99116a8abce25d7115ea07e8` 回退。

发布新正式版时，在 `feature/copymanga-source-browser` 的已验证提交上创建并推送形如 `v0.1.3` 的 Git 标签。两个发布工作流随后构建版本号、版本线、`latest` 和 SHA 标签；预发布标签（如 `v0.2.0-rc.1`）不会更新 `latest`。普通分支提交只产生预览和 SHA 标签。WebUI 左侧菜单会显示镜像构建时写入的版本号。Docker Hub 凭据来自 GitHub Actions secrets `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`；缺少凭据时 Docker Hub 发布会跳过。令牌不要写入仓库或聊天内容。

飞牛 NAS 的 Docker UI 不一定检测同名标签背后的新镜像。需要在镜像页重新拉取并重建原容器；如果使用公开镜像的 Compose 配置，则先 `docker compose pull` 再重建。仓库附带的 `docker-compose.yml` 仍用于本地构建。只重启容器不会更新代码。想固定在某一版本可填 `:v0.1.3`；想跟随同一版本线可填 `:v0.1`；想跟随每次正式发布可填 `:latest`。标签本身不会让 Docker 自动拉取或弹出更新提醒。

从 v0.1.4 起, Docker Hub 镜像采用单平台 Docker V2 清单, 用于验证 NAS 更新检测的兼容性。该调整尚需在飞牛实际确认, 不保证自动提示。排查证据和验证步骤见 [Docker 更新检测说明](docs/docker-update-detection.md)。

### 使用webUI/docker

如果您需要使用WebUI或者docker，请确保创建了`.env`文件

如果您想直接从代码库中运行WebUI，请确保运行了`frontend`中的前端文件以及运行`python3 server.py`

#### docker compose部署

```yml
version: '3.8'

services:
  cmnas:
    image: ghcr.io/misaka10843/copymanga-nasdownloader:latest
    # 如果是国内网络请注释上方的代码，然后将下方代码的#删除掉
    #image: ghcr.nju.edu.cn/misaka10843/copymanga-nasdownloader:latest
    container_name: cmnas
    restart: unless-stopped
    ports:
      - "8000:8000"  # Web 访问端口
    volumes:
      # 映射数据目录 (配置文件、日志)
      - ./data:/data
      # 映射下载目录 (临时下载文件)
      - ./downloads:/downloads
      # 映射 CBZ 输出目录 (最终打包文件)
      - ./cbz:/cbz
    environment:
      # --- 基础配置 ---
      - CMNAS_LOG_LEVEL=INFO

      # --- Copymanga 账号配置 ---
      # 如果你的 config.py 优先读取环境变量，填在这里
      - CMNAS_CM_USERNAME=your_username
      - CMNAS_CM_PASSWORD=your_password

      # --- 代理配置 ---
      # 注意：如果使用本机代理，在Docker中可能需要写 http://host.docker.internal:7890
      - CMNAS_CM_PROXY=

      # --- API URL ---
      - CMNAS_API_URL=https://api.mangacopy.com
      - CMNAS_CM_RATE_LIMIT_PER_MINUTE=${CMNAS_CM_RATE_LIMIT_PER_MINUTE:-12}

      # --- 行为配置 ---
      # 是否使用copymanga原名 (True/False)
      - CMNAS_USE_CM_CNAME=False
```

将上方的内容保存到`docker-compose.yml`中，然后运行`docker compose up -d`即可部署完成

#### 部署本 fork 的修复版本

本仓库的修复需要从对应分支构建镜像；上方的上游预构建镜像不包含本 fork 的改动。
克隆本仓库并切换到修复分支后，使用仓库自带的 `docker-compose.yml`：

```bash
git clone -b fix/copymanga-rate-limit-recovery https://github.com/everink404/copymanga-nasdownloader.git
cd copymanga-nasdownloader
cp .env.sample .env
# 修改 docker-compose.yml 中的账号、代理等配置
docker compose up -d --build
```

更新已有部署时，在切换分支并保留原有配置后执行 `docker compose up -d --build`。
保持原有 `data`、`downloads`、`cbz` 的卷映射，WebUI 仍使用 8000 端口。
限流可通过 `.env` 中的 `CMNAS_CM_RATE_LIMIT_PER_MINUTE` 配置；自定义 Compose 文件需将此变量传入容器的 `environment`。

---

在部署完成之后您可以打开url然后访问WebUI

在WebUI中允许直接配置所有当前插件的配置文件以及支持定时运行更新任务，以及直接编辑`updater.json`
![img.png](.github/assets/img.png)
![img_1.png](.github/assets/img_1.png)
![img_2.png](.github/assets/img_2.png)

### 普通命令行使用

本仓库是为了nas系统下载而进行优化，所以下载器本身并没有任何的交互/配置界面

下载器的配置是通过环境变量进行设置，更新列表是通过json进行设置

在运行下载器之前请确保bash的目录在程序目录下

#### 配置下载器

请将本仓库中的`.env.sample`更名为`.env`放在cmd的运行目录下

其中各个变量的内容如下

```dotenv
CMNAS_TOKEN= # token填写，必须登录，否则会无法请求其章节详细
CMNAS_DOWNLOAD_PATH= # 下载路径(暂存路径，将在cbz打包完成后删除)(字符串)
CMNAS_CBZ_PATH= # CBZ存放路径(字符串)
CMNAS_DATA_PATH= # 配置文件相关存放路径(字符串)
CMNAS_USE_CM_CNAME= # 是否使用copymanga的章节名，如果不使用将按照kavita的格式进行命名(True/False)
CMNAS_API_URL= # API服务器地址(字符串)
CMNAS_LOG_LEVEL= # 日志等级(DEBUG,INFO,WARNING,ERROR)
CMNAS_CM_USERNAME= # copymanga 账户名称
CMNAS_CM_PASSWORD= # copymanga 密码
CMNAS_CM_PROXY= # copymanga 使用的代理
CMNAS_CM_RATE_LIMIT_PER_MINUTE=12 # CopyManga API 全局限流（次/分钟），0 关闭
```

CopyManga 的登录、章节列表、章节详情及其重试共享同一进程的限流器，默认每次请求至少间隔 5 秒（12 次/分钟），不限制图片 CDN 或其他站点。设置为 `0` 可关闭；负数或无效值使用默认值 12。修改环境变量后重启进程或容器。多个容器的限额分别计算。

HTTP 210 会记录错误并使当前请求失败，进程继续运行。HTTP 429 按 `Retry-After`（秒数或 HTTP 日期）等待；缺失或无效时等待 60 秒，沿用现有最多 3 次请求尝试，耗尽后安全返回失败。关闭常规限流不会关闭 429 等待。

任一章节图片下载失败时，不打包 CBZ、不删除临时目录、不更新完成记录，并停止当前漫画后续章节，避免顺序记录跳过失败章节；其他漫画仍继续处理。下次运行会补下载缺失图片并复用已有非空图片。新图片先写入 `.part` 再原子替换，防止写入中断后被误认为已完成。请保留 `downloads` 卷以便恢复；旧版本遗留的非空损坏图片需要手动删除后重试。

#### 配置更新列表

当前只能自行进行配置(之后会做一个web界面进行配置管理)

请在`CMNAS_DATA_PATH`的目录下创建`updater.json`

##### copymanga

其中内部的结构如下：

```json
{
  "copymanga": [
    {
      "name": "漫画名称(指定保存的系列名/文件夹名)",
      "ep_pattern": "重命名的话数的正则提取，默认可以为空",
      "vol_pattern": "重命名的卷数的正则提取，默认可以为空",
      "path_word": "copymanga的path_word",
      "group_word": "copymanga的group_word，默认为default",
      "latest_chapter": "最后下载的章节，可以用来限制下载范围(为空则直接下载所有的内容)",
      "last_download_date": "最后下载章节的完成日期"
    },
    {
      "name": "漫画名称(指定保存的系列名/文件夹名)",
      "ep_pattern": "重命名的话数的正则提取，默认可以为空",
      "vol_pattern": "重命名的卷数的正则提取，默认可以为空",
      "path_word": "copymanga的path_word",
      "group_word": "copymanga的group_word，默认为default",
      "latest_chapter": "最后下载的章节，可以用来限制下载范围(为空则直接下载所有的内容)",
      "last_download_date": "最后下载章节的完成日期"
    }
  ]
}
```

示例如下

````json
{
  "copymanga": [
    {
      "name": "白圣女与黑牧师",
      "ep_pattern": "连载版(\\d+\\.?\\d*)",
      "vol_pattern": "",
      "path_word": "baishengnvyuheimushi",
      "group_word": "default",
      "latest_chapter": "连载版02",
      "last_download_date": "2025-04-05T20:58:29.386183"
    },
    {
      "name": "静音酱今天也睡不着觉",
      "ep_pattern": "",
      "vol_pattern": "",
      "path_word": "jinyingjiangjintianyeshuibuzhaojiao",
      "group_word": "default",
      "latest_chapter": "",
      "last_download_date": ""
    }
  ]
}
````

##### 泰拉记事社

对于漫画ID获取可以直接前往官网点击任何一个漫画例如 `https://terra-historicus.hypergryph.com/comic/6253` 中的 `6253`
就是漫画ID

配置结构如下

```json
{
  "terra_historicus": [
    {
      "name": "漫画名称",
      "comic_id": 漫画id(数字),
      "latest_chapter": "最后下载的章节，可以用来限制下载范围(为空则直接下载所有的内容)",
      "last_download_date": "最后下载章节的完成日期"
    }
  ]
}
```

示例如下

```json
{
  "terra_historicus": [
    {
      "name": "暮岁闲谈：这是平凡人生",
      "comic_id": 4579,
      "last_download_date": "2025-07-30T15:17:12.854869",
      "latest_chapter": "这是平凡人生"
    }
  ]
}
```

##### antbyw

对于漫画ID获取可以直接前往官网点击任何一个漫画例如
`https://www.antbyw.com/plugin.php?id=jameson_manhua&a=read&kuid=196880` 中的 `196880`（kuid的值）
就是漫画ID

配置结构如下

```json
{
  "antbyw": [
    {
      "comic_id": "漫画ID",
      "name": "漫画名称",
      "group_word": "单话/单行本/番外篇",
      "last_download_date": "",
      "latest_chapter": ""
    }
  ]
}
```

示例如下

```json
{
  "antbyw": [
    {
      "comic_id": "196880",
      "name": "放学后的迷宫冒险者们",
      "group_word": "单话",
      "last_download_date": "",
      "latest_chapter": ""
    }
  ]
}
```

如果需要下载两个及以上的站点可以通过下方结构配置

```json
{
  "copymanga": [
    {
      "name": "白圣女与黑牧师",
      "ep_pattern": "连载版(\\d+\\.?\\d*)",
      "vol_pattern": "",
      "path_word": "baishengnvyuheimushi",
      "group_word": "default",
      "latest_chapter": "连载版02",
      "last_download_date": "2025-04-05T20:58:29.386183"
    }
  ],
  "terra_historicus": [
    {
      "name": "暮岁闲谈：这是平凡人生",
      "comic_id": 4579,
      "last_download_date": "2025-07-30T15:17:12.854869",
      "latest_chapter": "这是平凡人生"
    }
  ]
}
```

在配置完成之后直接运行程序即可

## 如何开发其他站点

`copymanga-nasdownloader` 采用模块化设计，增加新站点支持需要遵循以下步骤。

(如果不会可以看当前已有的站点)

### 核心流程

1. **定义 Updater**: 在 `updater/` 目录下创建站点更新器，负责获取章节列表和对比本地记录。
2. **定义 Plugin**: 在 `plugins/` 目录下创建站点插件，负责解析具体章节的图片 URL 并下载。
3. **注册站点**: 在 `updater/updater.py` 和 `dispatcher.py` 中注册新站点的映射关系。

### 开发步骤

#### 第一步：创建更新器 (Updater)

在 `updater/your_site.py` 中继承 `BaseUpdater`：

* `get_chapters()`: 调用 API 或爬取网页，返回包含章节名称和 ID 的列表。
* `find_subsequent_uuids()`: 根据 `updater.json` 中的 `latest_chapter` 过滤出需要下载的新章节 ID。
* `create_download_task()`: 构造传递给插件的任务字典。

#### 第二步：创建下载插件 (Plugin)

在 `plugins/your_site/main.py` 中实现：

* `download_batch(tasks)`: 插件的入口点。
* `download_chapter()`: 处理单章逻辑。
* 调用 `downloader.downloader` 下载图片。
* 下载完成后调用 `downloader.postprocess` 进行 CBZ 打包。

#### 第三步：配置与注册

1. **环境变量**: 如需特殊配置（如 Cookie/Token），在 `utils/config.py` 和 `.env.sample` 中添加。
2. **映射注册**:

* `updater/updater.py` -> `SITE_MAPPING`
* `dispatcher.py` -> `SITE_MODULES`
