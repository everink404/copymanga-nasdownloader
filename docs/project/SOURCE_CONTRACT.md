# Source API 1.0 与通用访问契约

SC-20261018 r2，对应 [S1](S1.md)。本文件描述拟实施协议，代码基线仍为 v0.1.7；以下接口当前尚不存在。

## 1. 身份与描述

source_id 为不可变小写 ASCII 标识（例如 hipmh），满足 ^[a-z][a-z0-9_]{0,63}$，不包含域名；换域名不换 ID。comic_id、group_id、chapter_id 是插件提供的非空不透明字符串，每项 UTF-8 最多 512 字节，超限报协议错误而非截断；不自动大小写转换或 Unicode 归一化。插件负责按其 identity_scheme 生成稳定值。标题、序号和 URL 只用于展示或请求，不是身份。

ComicIdentity=(source_id,comic_id)，SubscriptionIdentity=(source_id,comic_id,group_id)，ChapterIdentity=(source_id,comic_id,group_id,chapter_id)。没有实际分组时插件使用稳定 group_id=default，不能每次重造。
核心 key 为规范 JSON 字符串数组（UTF-8、无空白、原字符串、固定四元组顺序）的完整 SHA-256；hash 用于索引和路径，不是图片内容证明。数据库仍保留完整原身份并作唯一性核对，不允许用截断 hash 认领记录。

Descriptor 必须包含 source_id、display_name、plugin_version、source_api_major=1、identity_scheme、capabilities、settings_schema、endpoints、rate_limits（api_requests_per_minute、image_requests_per_minute 默认值）。插件升级只要改变 ID 方案或清单证明语义就声明新 identity_scheme，旧记录进入需要兼容处理状态，不能当新章节全量下载。
版本不匹配/加载失败/手动禁用分别显示 incompatible/unavailable/disabled；记录保留，运行返回 SOURCE_UNAVAILABLE，不返回空目录。

## 2. 最小模型

| 模型 | 必须字段 | 可选字段/约束 |
| --- | --- | --- |
| ComicSummary | comic_id、title | cover_request、authors、status；封面失败不阻断文字展示 |
| ComicDetail | ComicIdentity、title | description、authors、tags、status、web_url；缺失用 null，不伪造 |
| Group | group_id、name | 顺序仅展示；无分组仍返回 default |
| Chapter | ChapterIdentity、title、ordinal | source_revision、published_at；ordinal 是同一次目录快照内顺序，不是持久身份 |
| PageResult[T] | items、next_cursor、end_reached | total、snapshot_token；有后续必须给 cursor，结束必须显式标记 |
| CatalogSnapshot | identity、ordered_chapters、fingerprint、consistency、observed_at | total_hint、source_snapshot_token；只有完成验证后才产生可制定范围的快照 |
| ImageManifest | ChapterIdentity、entries、complete、manifest_version、identity_scheme、proof_kind | expected_count、source_revision、access_generation；complete=false 不得下载或计无更新 |
| ImageEntry | ordinal、page_key、request_plan | revision、expected_sha256、expected_bytes、transform_version=none；序号从 1 连续且 page_key 在清单内唯一 |

capabilities 至少标识 browse、search、filters、groups、anonymous、account_required 和 manifest_refresh。UI 不展示插件未声明或已标记受限的功能。search=false 与“搜索返回 0 个匹配项”不同；search 失效返回错误，不能拿热门作品冒充命中结果。
筛选为插件提供的字段 schema（enum/text/bool，稳定 field_id），核心校验请求类型与值；未知字段返回 400，不默默忽略。

## 3. 插件生命周期与操作

同步 Python 协议，单 worker；不为本阶段引入异步框架。以下是概念签名，S2 才新增相应 SDK/实现。

```text
SourcePlugin.descriptor() -> Descriptor
SourcePlugin.open_session(config_snapshot, Transport, SecretProvider) -> SourceSession
SourceSession.browse(kind, cursor, filters, OperationBudget) -> PageResult[ComicSummary]
SourceSession.search(query, cursor, filters, OperationBudget) -> PageResult[ComicSummary]
SourceSession.get_comic(comic_id, OperationBudget) -> ComicDetail
SourceSession.list_groups(comic_id, OperationBudget) -> list[Group]
SourceSession.list_chapters(comic_id, group_id, cursor, OperationBudget) -> PageResult[Chapter]
SourceSession.get_manifest(ChapterIdentity, refresh, OperationBudget) -> ImageManifest
SourceSession.prepare_image_request(ImageEntry, OperationBudget) -> RequestPlan
SourceSession.close() -> None
```

实现只使用注入 Transport，不自行创建绕过代理/预算的 HTTP 客户端。不能直接写任务/完成记录、分配本地路径、打包、删文件或偷偷重试。返回 None、空对象或异常吞掉都属于 PROTOCOL_ERROR，而不是成功。
源的目录和清单解码在插件内完成。首个嬉皮适配按阅读页公开配置、线路及清单编码处理；不执行站点脚本。图片拼图/变换引擎不在首版；遇到必须变换的源先报 UNSUPPORTED_TRANSFORM。

## 4. 完整目录与范围输入

核心逐页收集；cursor 不得循环；同一快照内 chapter_id 不得重复（跨分组可重复）。total 存在时必须与最终项数一致；任何页失败、重复、身份冲突或顺序不可判定返回 CATALOG_INCOMPLETE，不能使用已取得的前几页制定范围/计无更新。
上游提供可靠 snapshot_token 时要求各页一致；否则完整扫描两次，比较每个身份、顺序及 revision。不同则最多再扫一次，以最后两次一致为准，否则 CATALOG_CHANGED。本机稳定扫描不是上游事务快照保证，consistency 明确为 double_scan；不能宣称绝对原子。
目录 fingerprint 为有序 [chapter_key,ordinal,source_revision] 数组的规范 JSON（UTF-8、无空白，缺 revision 用 null）的 SHA-256。它绑定当次内容与顺序，不包含请求 URL。
插件只把自己的分页参数映射为不透明 cursor，前端不拼上游 offset。发现页可以逐页展示；添加订阅、更新检查与范围修改必须使用完整 CatalogSnapshot。
缺少稳定章节 ID 时返回 IDENTITY_UNSUPPORTED，不退回按名称下载；同名章节有不同 ID 则允许共存。

## 5. 清单与安全补下载证明

完整清单要求 entries 非空、ordinal=1..N、唯一 page_key，若源声明 expected_count 则必须等于 N。插件须先完成去广告/诱饵的站点规则并核对数量，不能由核心猜测删掉一张图。
manifest_version 是源清单描述指纹：SHA256(CANON([[page_key,revision或null,expected_sha256或null,transform_version],...]))，保持页顺序，transform_version 缺省为字符串 none，revision 必须是字符串或 null。短效 URL、认证 Token 与线路 host 不直接成为稳定内容身份；实际字节版本另按数据契约计算。
CANON 固定使用 Python 3.10 json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False) 的 UTF-8 输出；字符串不做 Unicode 归一化，拒绝非法代理字符，参与指纹的序号为整数；禁止不定序对象参加指纹。身份、目录、清单、content_version 使用同一编码约定，样例文件附 Unicode/引号/null 的规范字节及预期 digest。

proof_kind：
- content_hash：上游内容哈希可核对每页。
- immutable_resource：插件契约保证 page_key+revision 对应不可变内容，且线路变化仍可证明同一页；这是源语义证明，不伪称核心取得了上游哈希。
- none：源不能证明跨请求内容稳定。只允许在同一进程、同一已取得清单的下载尝试内复用本次已校验页；重启、重新获取或刷新清单后隔离旧页面 generation，重新下载，不靠序号或 URL 外形猜测。即使旧页已全部收齐，只要尚未达到下面的“持久 prepared 完整 ZIP”边界，也不能在重启后用旧页继续打包。

清单刷新后，仅在身份、证明类型、整章内容版本及顺序一致时复用已保存页。数量/顺序/内容版本改变或无法证明一致，旧 generation 保留，分配新 generation；不混包。每章一次自动清单刷新，后续失败交用户重试，避免无限积累临时代。
优先级：页面复用规则优先于通用恢复表。content_hash/immutable_resource 的旧页面只有在重新取得匹配身份/顺序/证明的清单且本地收据核验成功后才可跨重启复用。none 的例外仅限持久 prepared 意图及其预期哈希已存在、且完整 ZIP 或已发布产物能通过全包验证的本地对账；这不允许复用部分页面或构造新的混合包。完整 ZIP 没有持久 prepared 意图的，重启后一律隔离，不从其自行声称的清单重建完成意图。
同一稳定清单内，已保存页还须验证本地 sha256、尺寸、格式和清单绑定；非空文件不算完成。所有证明类型都禁止仅凭未知 ZIP 的文件名或内部自述认领完成。
嬉皮的 proof_kind 不能仅凭图片路径看起来固定就设为 immutable_resource；S3 验证前默认 none。

## 6. RequestPlan 与会话

RequestPlan 包含 method、https URL、request_role(api/page/image/cover)、headers 非敏感项、auth_scope_ref、body（仅需要的 API 操作）、重定向策略和 endpoint_ref；不得含本地文件路径。登录、短效地址和 Cookie 只在会话内存中使用，不进入日志、任务 DTO 或示例。
插件可通过 SecretProvider 读取其 source/account 作用域的值；核心统一落盘与脱敏。可信插件同进程并非沙箱，SDK 约束和测试不能被描述成能隔离恶意代码。
会话键=(source_id,account_ref,config_generation)，同键复用，close 在批次结束/配置失效执行。浏览可匿名，实际下载按源要求认证；配置/账号/代理/域名改变后新任务用新 generation，当前章使用冻结配置，结束后关闭旧会话。修改源的身份方案则阻止新任务，不能冻结后盲跑。

账号 API、网页 Cookie、图片 CDN 是独立凭据作用域。精确 host+端口+用途约束，拒绝通配任意域名。CDN 请求只附其需要的 Referer/非敏感头；账号 Authorization 不转发到 CDN。
每次跳转重新判断作用域，不跨 host 自动转发凭据，不允许 HTTPS 降级。最多 5 次跳转，网络调用计入同一预算。插件返回的新 CDN host 需属于声明允许的 host 或受验证站点配置产生的当次访问计划；不能复制 API 凭据来“修复”图片 403。
配置私密值写入 /data/core-v1 的受限私有存储或已有环境 SecretProvider，公共 API 只返回 configured=true/false，不回显密码、Token、Cookie 或带认证的代理 URL。错误诊断仅记录源/用途/host/状态/request_id。

## 7. 限流、错误与重试

同一 source/account 的 API 进程级共享限流，图片角色独立桶，但相同 host 的总限制也须遵守插件声明。拟默认 API 12 次/分钟、图片 60 次/分钟，均可配置，0 关闭对应本地桶；这是保守应用默认，不是声称源站公布的额度。保留旧 CMNAS_CM_RATE_LIMIT_PER_MINUTE 只影响旧 CopyManga 设置，不套用为嬉皮认证配置。
Transport 是唯一网络重试责任方。每次元数据/分页/图片逻辑操作最多 6 个实际 HTTP 尝试（包括跳转/刷新引起的同一资源请求）；每章自动登录刷新至多 1 次、清单刷新至多 1 次，共用预算不能嵌套重置。预算耗尽明确失败；手动重试创建新 attempt，不清理旧证据。
瞬态网络/5xx 使用 3、10 秒退避并受总预算限制。429 优先 Retry-After，缺失/无效为 60 秒；长等待持久化 retry_at 并释放 worker，可执行其他已到期任务，不提前重试。账号/源冷却适用于同作用域其他任务，防止换任务绕开 429。
普通 HTTP 超时 20 秒，图片 45 秒；实际等待、累计尝试和下一重试时刻显示。HTTP 超时不是整个任务截止时间；停当前章语义不承诺立即中断正在进行的请求或绕开服务端等待。

| ErrorCode | 含义 | 自动处理 |
| --- | --- | --- |
| NETWORK / UPSTREAM_5XX | 暂时连接失败 | 有限退避 |
| RATE_LIMIT | 429/明确频控 | Retry-After/60 秒，记 retry_at |
| AUTH_EXPIRED | 插件有证据说明登录失效 | 一次刷新，仍失败则停止 |
| ACCESS_EXPIRED | 插件有证据说明图片地址失效 | 一次清单刷新；403/404 不自动等同过期 |
| SITE_RESTRICTED | CopyManga 210 等限制 | 不当作无更新，不自动换域名无限重试 |
| NOT_FOUND / PROTOCOL_ERROR | 缺失资源/结构变化 | 停本章，保留材料 |
| CATALOG_INCOMPLETE / CATALOG_CHANGED | 目录无法作为范围依据 | 不生成下载计划或推进记录 |
| IDENTITY_UNSUPPORTED / RANGE_REVIEW_REQUIRED | 身份/范围不可靠 | blocked，用户可理解的原因 |
| INVALID_IMAGE / MANIFEST_CHANGED | 页面不对/清单已变化 | 隔离或有限重试，不混包 |
| STORAGE_ERROR / ARTIFACT_CONFLICT | 磁盘失败/产物归属冲突 | 不覆盖、不报完成 |
| SOURCE_UNAVAILABLE | 源未启用、不可兼容或加载失败 | blocked，数据保留 |

错误对象包含 code、可读 message、retryable、retry_at、request_id 和安全 context；重试由核心判定，插件提供依据。异常转换不能只剩“请求失败”，也不能暴露原始敏感 URL。

## 8. 图片正文与 CBZ 输入

首版支持可验证的 JPEG/PNG/WebP/GIF；格式从实际内容识别，保存正确扩展名，不一律命名 .jpg。采用 Pillow 或等价完整解码校验（依赖在 S2 明确锁定与测试），不能仅靠 Content-Type。HTML、JSON 错误页、截断数据、零字节必须失败；既有缓存页执行同样验证。
默认单页传输 25 MiB、解码 100M 像素上限，超出明确报限制，不截断后伪装成功；阈值是核心资源限制，可以通过受验证配置调整。
只根据有序 manifest 的已验证 PageReceipt 打包；.part、旧代、杂文件、子目录和未列出的页不能进入 CBZ。包内页名由核心按 ordinal 生成；只允许本章图片、ComicInfo.xml 和核心 download-manifest.json 元数据。ZIP CRC、实际页数、顺序和内容 sha256 均需核验。

## 9. 通用 Web API 与兼容

统一前缀 /api/v1。所有 key 是核心身份索引返回的不透明完整 key；Web 层查存储解析，不把任意上游 URL 当路径 ID。

| 操作 | 请求 | 返回与约束 |
| --- | --- | --- |
| 源 | GET /sources；GET/PATCH /sources/{source_id}/settings | 描述、能力、配置状态；PATCH 不回显私密值 |
| 浏览/搜索 | GET /sources/{id}/comics?cursor=&q=&filters= | PageResult；未知筛选报错；搜索失效不能伪造结果 |
| 详情/分组 | GET /comics/{comic_key}；GET /comics/{comic_key}/groups | 标准 DTO |
| 目录展示 | GET /groups/{group_key}/chapters?cursor= | 可分页展示，不代表可立即用部分结果建订阅 |
| 范围预览 | POST /subscriptions/preview | identity、mode、anchor；完整扫描后返回 plan_token、范围摘要及首章，不自动下载 |
| 添加/改范围 | POST /subscriptions；PUT /subscriptions/{id}/range | 提交有效 plan_token，过期/源或目录配置变化返回 409 重新预览 |
| 暂停/取消 | PATCH /subscriptions/{id}；DELETE /subscriptions/{id} | 暂停取消待执行任务；当前章继续；取消不删文件；活动订阅删除须先停止 |
| 下载 | POST /subscriptions/{id}/run；POST /run | 共用去重，缺插件/停用/未导入返回明确 blocked/409 |
| 任务 | GET /tasks；POST /tasks/{id}/stop；POST /tasks/{id}/retry | 实际持久状态、阶段、范围和进度；普通失败/中断/停止可重试，recovery_required 的核对/修复按数据契约事务转交，不能直接绕过意图 |
| 导入 | GET /migration/preview；POST /migration/apply | 文件指纹、数量与风险摘要；写入需要确认的预览指纹，详见数据契约 |

plan_token 只标识核心保存的完整目录快照与范围/配置版本，默认有效 10 分钟；提交前重新确认目录快照未失效，无法核对或已变化则 409。不能靠客户端声称 total 或 completion。
旧 /api/config 的读取可以返回只读旧数据视图并带 deprecated 标记；不得回显原始私密配置。旧整份配置写入、源专用订阅写入或旧运行请求不得成为新核心旁路；返回结构化 409 LEGACY_WRITE_DISABLED 与新入口说明。旧 UI 与新服务混用时明确要求刷新页面。
现有前端在 S3 同步切换通用 API，S2 测试镜像可以只暴露测试页/API，不能把旧前端显示正常作为通用 UI 验收。

recovery_required 的任务按钮显示“核对并修复”，调用同一个 retry API。该 API 先仅做本地对账；已恢复完成则返回 reconciled=true，不请求网络；确认需要下载且意图已处理才返回唯一 successor_job_id。未能核对则返回 409 LOCAL_RECONCILIATION_REQUIRED/ARTIFACT_CONFLICT 并保留占位。重复请求返回同一后继任务，不先释放锁再各建一个任务。精确转换见 DR 第 4 节。

预算示例：页 A 首次 GET 返回 429，http_used=1/6、清单刷新=0/1，保存 retry_at 后释放 worker；同作用域任务受冷却，其他可运行源执行。恢复 A 时继续同一计数，第二次 GET 为 2/6，不重置预算。若会话/原清单仍在同一进程可沿用；会话失效需新清单时消耗刷新额度，none 旧代隔离。进程重启则先中断，由用户重试建立新尝试；旧预算留历史，不伪装为原尝试继续。
