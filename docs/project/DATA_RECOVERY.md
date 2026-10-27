# 数据、范围、文件提交与旧版兼容

DR-20261018 r2，对应 [S1](S1.md)。这是实施契约，不是已运行的迁移或恢复工具。

## 1. 元数据与单 writer

新数据布局（保持 /data 等挂载根不变）：

```text
/data/core-v1/state.sqlite3        规范元数据，schema_version=1
/data/core-v1/private/            必需的私密配置，禁止公共 API 导出
/data/core-v1/migration.json      版本、旧文件指纹、备份及导入结果
/data/legacy-snapshots/<id>/      原始配置/记录字节与私有备份清单
/downloads/.core-v1/<attempt>/   本章尝试、清单与已验证页面，不混入旧目录
/cbz/<source>/<comic-folder>/<group-folder>/<chapter-file>.cbz
```

单进程使用 OS 排他 writer 锁及 SQLite 写事务；第二个 Web/CLI writer 启动失败并说明原因。锁文件存在不等于锁被占用，进程退出释放系统锁；不通过删锁文件抢锁。旧版不识别新锁，因此迁移前停止旧容器是独立前提，不能由新锁冒充证明。
数据库写使用单协调服务、BEGIN IMMEDIATE、foreign_keys=ON、journal_mode=DELETE、synchronous=FULL；任务状态与完成事实必须在同一事务提交。正常进度可合并写入，但发布意图/产物提交不能延后批量保存。磁盘错误/数据库损坏返回 STORAGE_ERROR 并停写，不回退成空数据库。
SQLite 只承诺本机可靠文件系统卷。NAS 文件系统的锁、同步和无覆盖发布能力在 S2/S4 分别验证；不承诺任意网络盘、断电硬件缓存下的绝对持久性。恢复逻辑须能处理上次未干净退出。

## 2. 逻辑表与约束

这是 schema 设计，不要求此刻创建数据库。

| 实体 | 主要字段与约束 |
| --- | --- |
| meta | schema_version、source_api_major、instance_id、created_at；未知更高版本拒绝写入 |
| sources | source_id 主键、plugin_version、identity_scheme、enabled、compatibility；缺插件保留 |
| comics / groups / chapters | 完整身份、完整 key、名称；身份唯一；章节必须归属指定分组 |
| catalog_snapshots / catalog_entries | snapshot_id、身份列表、顺序、revision、consistency、时间；快照不可原位改写 |
| subscriptions | subscription_id、三元身份唯一、mode、anchor_key、baseline_snapshot、policy_revision、paused、legacy_state |
| range_exclusions | subscription_id + chapter_key + policy_revision，reason=baseline_skipped/before_anchor；不是下载完成事实 |
| jobs | job_id、subscription_id、配置/插件/范围版本、冻结目录计划、status、phase、retry_at、stop_after_chapter；活动任务三元身份唯一 |
| attempts / pages | attempt_id、chapter_key、generation、stage、清单指纹；页面 ordinal/page_key/sha256/格式/路径/验证时间；只属本次尝试 |
| artifacts | artifact_id、chapter_key、manifest_version、content_version、目标相对路径唯一、预期 CBZ sha256、阶段、校验结果和字节数 |
| chapter_downloads | chapter_key 主键、当前 content_version、artifact_id、fact_state、committed_at；只有已核验产物能标 complete。历史内容版本留在 artifacts/事件中，不重复计章节数 |
| legacy_imports / legacy_records | 导入快照+原文件摘要+JSON Pointer 唯一、原身份/原始字段或私有备份引用、provenance、disabled/unverified；不提升证据等级 |

完成事实不是递增计数器。显示“已下载”由 fact_state=complete 且产物未标缺失/损坏的规范记录计算；重复提交同一 artifact 不加计数。旧统计另显示“旧版记录/未核实”，不能合并为已校验总数。
manifest_version 是源清单描述，不一定证明实际字节。content_version=SHA256(CANON([[page_key,实际页面sha256,实际MIME,transform_version],...]))，CANON 见源契约，transform 缺省为 none；文件版本后缀和当前完成事实使用它。源无内容 revision 时不能承诺自动发现同 ID 的远端内容替换；源有明确 revision 变化且本章原本在下载范围内时重新入队核验，旧 artifact 留存而不是多算一章。future 的 baseline_skipped 不因 revision 变化变成新章，不绕过范围排除。
原始未知字段如保存在数据库也只属于私有导入区，公共 DTO 使用白名单脱敏；不能把带凭据的 legacy 原始 JSON 当 API 返回值。

## 3. 目录、范围与更新规则

范围预览只能使用经过源契约验证的完整 CatalogSnapshot。提交绑定 snapshot_id、config_generation、identity_scheme、policy_revision；过期或不一致须重新预览，不按客户端总数制定任务。

| 模式 | 保存内容 | 后续待下载判断 |
| --- | --- | --- |
| all | 范围策略与快照，不写假完成 | 完整目录中的全部未完成、缺失/损坏待修复章节 |
| future | 当前全部 chapter_key 作为 baseline_skipped | 以后首次发现的新 ID（包括补插章节），扣除真实完成事实；初始基准不计完成 |
| from | 所选 anchor_key（含该章）及当时顺序快照 | 基准中从锚点开始的未完成；新章按当前一致目录确认仍在范围，再入队 |

from 的锚点消失、身份方案变化或重排让原范围归属不可靠时，RANGE_REVIEW_REQUIRED，不根据同名章节重新定位。future 的上游身份批量重建或插件 identity_scheme 改变也不能当大量新更新直接入队。
from 的确定规则：保存基准中锚点前后的 ID 集合；已知章节跨越锚点两侧则阻塞，侧内重排可接受。新 ID 按当前一致目录相对锚点的位置决定是否纳入。前次非空目录突然变空，或新旧非空目录完全无 ID 交集，至少标 CATALOG_CHANGED/疑似身份重建，不能自动批量开下；需重新预览确认。
all/future 的正常插章不因“比最后完成章节更早”而被吞掉；from 插在锚点之前的章节不自动加入。源目录顺序不可确认或存在冲突时先阻塞，不以 ordinal 当持久 ID。
任务计划冻结当次范围内有序 chapter_key；当前目录变动不直接重写正在执行的计划。404/章节删除使当前章失败，停止本部后续章节；下一次完整检查再处理，不越过缺口。
范围编辑保留已有完成事实，不静默重下已校验章节；修复损坏/缺失产物属于重试，不是“更改范围后全部强制覆盖”。活动任务须先停止退出再编辑范围。
暂停订阅取消排队任务，当前章按既定语义完成；取消订阅保留文件、产物归属与恢复记录，保留为 tombstone/取消状态而不是级联删掉账本。

## 4. 任务状态与重启

Job.status：waiting、running、retry_wait、completed、failed、interrupted、stopped、blocked、recovery_required。waiting/running/retry_wait/blocked/recovery_required 保留活动身份占位；failed/interrupted/stopped/completed 结束占位。启动本地对账未完成的身份不能接受新任务来绕过恢复；尚未开始章节的 blocked 可停止取消，涉及产物发布意图则先安全对账再取消。
completed 必须另带 outcome=downloaded/no_updates；无新章仅在完整目录、范围判断成功且确实无待处理项时成立。某章失败时已完成章保留，但整次任务不能 completed。
phase 是实际操作（检查目录、登录、获取图片列表、下载图片、校验页面、打包、提交完成记录、清理、等待重试），不能用 phase 字符串判断完成事实。
网络/目录阶段总量未知用 null；图片全部收齐只是下一阶段，不显示整章完成。任务完成章数来自本次已提交事实，不来自“循环跑到了第几章”。
普通重启不自动重下：waiting/running 任务标 interrupted，待本地恢复核对后再由用户重试；已持久化 retry_wait 也先核对并显示待恢复，不在启动时突发请求。启动允许本地无网络对账，但不认领身份未知的旧文件。
点击“完成当前章后停止”持久化 stop_after_chapter，当前章成功或失败退出后不开始下一章；目录检查等尚未进入章节的阶段可在安全边界直接停止。长 Retry-After 明示下一时刻，不能承诺立即结束当前章。取消 waiting 无需开始请求。
重试新建 job/attempt 并引用旧证据，共用活动任务去重；不能重置旧完成事实或无限自动创建新尝试绕过预算。

### recovery_required 的公开操作闭环

POST /api/v1/tasks/{id}/retry 对该状态执行“先本地核对，必要时修复”，不是一律允许联网：
1. 获取相同订阅的协调锁和写事务，先处理全部相关 prepared/published 意图。可验证的完整产物按第 7 节提交完成；只有缺失/损坏且发布意图已核对完成或安全归类，才设置 local_reconciled=true、reason=artifact_repair_required。意图仍不明、数据库/存储不可写或存在未知冲突，409 明示原因，保持占位且不下图。
2. 若本地已经恢复该任务全部计划，返回原任务 completed/reconciled=true；尚有普通未执行章则置 interrupted，提示普通重试，不在本地核对中自动联网。
3. 对确认需要修复的任务，一个事务内把旧占位任务终结为 stopped/outcome=handed_off_to_repair，登记唯一 successor_job_id，并创建 repair=true 的 waiting 后继任务/新 attempt，继承订阅身份和修复事实引用。活动唯一约束在事务内部转交，外部看不到空窗。无 prepared 未核对意图才允许这一步。
4. 重复点击旧任务只返回已登记后继 ID；修复失败且尚无发布意图则新任务 failed，用户对这个新 ID 再重试。修复中出现待对账意图则回 recovery_required，重复执行以上本地优先规则。成功仅事务切换当前有效 artifact，不额外加章数。
5. stop 对 local_reconciled=true 且没有未结意图的恢复任务可设 stopped/repair_not_requested，释放占位，不删损坏产物或旧事实；有未结意图先本地核对，未能核对则 409，不靠取消丢弃意图。一般 run 不得绕过已有恢复占位；占位已结束后，修复计划仍须沿同一发布/路径规则。

订阅取消/源禁用不能删除恢复事实。需要修复但源禁用时保留原因并 blocked，不通过另一源自动代下；恢复来源后再由用户重试。已完成任务的原历史保留，发现产物损坏时以恢复任务关联它，不将历史完成报告改写为“从未下载”。

## 5. 输出身份与路径

路径由核心分配，展示名称只作为可读前缀：
`<source_id>/<comic-label>--<comic-key-prefix>/<group-label>--<group-key-prefix>/<chapter-label>--<chapter-key-prefix>--<content-version-prefix>--<artifact_id>.cbz`。
label 做文件系统安全处理和 UTF-8 字节截断；清除路径分隔、设备名、控制字符及尾空格/点；标识前缀不足以认领身份。路径与完整 key 存库并唯一约束；检测碰撞（包括大小写、清理后撞名和 hash 前缀冲突）则延长前缀直到唯一或报错，不覆盖。
名称变更不自动移动既有目录或改变已分配路径。新写入仅限分配给本完整身份的路径；不得根据旧目录名认领旧 CBZ。group_id 固定纳入身份和文件布局。
artifact_id 为核心分配并持久化的 UUID hex，每个物理产物唯一；同一发布意图重试沿用该 artifact_id/路径，不重复分配。同内容修复的 chapter_key/content_version 保持相同，但新 artifact_id/路径不同，旧坏件保留。若分配路径已被未知文件占用，禁止覆盖，作冲突处理；不能靠更改标题认领。
下载页校验后若库中已有同章同 content_version 的已知有效产物，先核对其既存 hash、内部身份/页清单，匹配可直接复用并对账，不重新打包。ZIP 时间戳或 ComicInfo 展示变动不能让正常重复执行产生多份副本。没有可核验已知产物时才准备新的 artifact_id。当前事实只指一个有效 artifact，历史 artifacts 留存。
所有路径必须解析后留在用户配置根中；拒绝遍历、指向根外的链接和插件自定义本地路径。清理只限已确认 ownership 的本 attempt 文件；不递归清理作品目录、旧源目录或整个挂载。
临时 ZIP 在目标 /cbz 文件系统内创建，不能假设 /downloads 与 /cbz 同一设备。旧文件原位保留；新命名空间不覆盖旧版按名称生成的文件。

## 6. 页面、CBZ 与完成记录提交协议

全程串行，清单及内容校验见 [源契约](SOURCE_CONTRACT.md)。每一步成功须持久化其必要证据；失败保留旧产物和可恢复材料。

1. 建立 attempt/generation，记录完整身份、清单有序指纹及证明类型；私密请求值只存内存。
2. 每张图写同目录 .part，验证内容和尺寸，计算 sha256 后原子改为本次页面文件，写 PageReceipt。收据未写成功的文件不可直接算完成；恢复可按本代清单重新校验并补登记。
3. 计算核心 content_version，只使用本 manifest 对应的已验证页构建 target.cbz.part。包内包含图片、ComicInfo.xml 和 download-manifest.json（完整身份、页序及页哈希、核心/插件/清单版本、content_version；不含 URL 或凭据）。
4. 关闭并同步临时 CBZ，重开验证 ZIP/CRC、页数、页序、每页哈希；计算整个 CBZ 的 sha256。失败不发布、不清理页面。
5. 在 SQLite 事务持久化 artifact=prepared 的发布意图：身份、内容版本、目标、临时文件及预期哈希。意图未提交则禁止发布。
6. 同文件系统执行 atomic_noreplace：拟采用临时文件到最终路径的排他硬链接发布，再移除临时别名。若目标已存在，必须核对其 hash、内部清单和完整身份；相同则可恢复对账，不同则 ARTIFACT_CONFLICT，不能 os.replace 覆盖。输出卷不支持此能力时返回明确存储能力错误，S2 不可静默用非原子覆盖替代。
7. 发布后核对最终文件，同一个数据库事务写 artifact=committed、chapter_downloads=complete、job 章进度和事件。事务未成功时不对用户报告成功。
8. 完成记录落盘后进入 cleanup_pending，仅清理此 attempt 的页面/临时 ZIP；最后标 cleanup_done。清理失败作为警告保留重试，不撤销已有可核验的完成事实。

SQLite 与文件不是跨介质原子事务；采用以上持久化意图和幂等对账恢复，不把单次 rename 描述成解决全部一致性。目录/文件同步和 hardlink 支持必须在部署卷验证，物理断电保证受文件系统/硬件限制。

## 7. 故障恢复矩阵

| 中断点 | 本地恢复动作 | 禁止动作 |
| --- | --- | --- |
| 页面只写 .part | 不计成功；保留/隔离，重新获取时按清单规则处理 | 因文件非空跳过 |
| 页面已校验，收据没落盘 | 同进程原尝试可按已捕获清单重验；重启后 none 隔离，content_hash/immutable_resource 先重新取得匹配证明，再核对已知本代材料 | 用另一个 generation 的同序号文件填空，或 none 重启后补收据续用 |
| CBZ 构建/验证失败 | 同进程原清单可重验后重包；重启后 none 即使全部页齐也隔离，stable/hash 按匹配证明规则重验后可重包 | 以“页面全部存在”放宽 none 的重启规则，或删除材料/推进完成 |
| ZIP 完成但发布意图未写 | 同进程可重验后写意图；重启后所有类型都隔离该未受意图保护的 ZIP。none 页面不能重包，其他类型仅可按新匹配证明重新打包 | 仅凭 ZIP 内自述重建持久意图/完成事实 |
| prepared 已提交，最终文件没有 | 核验临时 ZIP 后尝试无覆盖发布；均无则 interrupted 待重试 | 标 complete |
| 最终 CBZ 有，记录尚未提交 | 按意图的完整身份、hash、内部页清单核对，再一次性提交账本和进度 | 重新联网作为核对本地产物的必要条件；凭文件名猜测或覆盖 |
| 完成事务已提交，响应/清理未完成 | 校验最终产物，完成事实只计一次；继续本地清理 | 重复加章数或删掉最终产物 |
| 数据库声称完成但文件丢失/损坏 | fact_state=missing/corrupt，任务 recovery_required，reason=artifact_repair_required；原历史保留，用户可修复下载 | 继续显示已验证完成或静默覆盖冲突文件 |
| 有未知 final，无发布意图 | 标未认领/冲突，保留原件；新尝试用隔离路径 | 从名称反推身份并标完成 |
| 数据库损坏/更高 schema | 停写并提供修复/快照恢复提示 | 自动初始化空库覆盖 |

恢复对账无网络完成，只处理已知身份和发布意图。缺页重下需要新的正常任务。修复产物使用新 artifact 路径/代，不覆盖损坏原件；事务把当前有效完成事实指向已校验的新产物，旧证据保留。一般运行不每两秒全盘 hash，读取已有 verified 状态；启动/重试/修复按需核验，UI 说明校验时间，不声称永远实时检查。

矩阵优先级：已持久 prepared 的完整包可只凭存储的预期 hash、完整身份和页清单本地恢复，proof=none 也适用；这不允许取出包中部分页与新请求混合。none 的重启断点明确为：只有一页→隔离；全页但无 ZIP→隔离；完整 ZIP 无意图→隔离；prepared 意图+可验证完整 ZIP→无网络发布/提交。只要产物不完整或无法验证，仍不能报完成。

## 8. 旧版导入：预览、备份、应用

首版是一次性、显式导入，不是静默在线转换。旧版存在且新 core 尚未激活时，服务进入 legacy_detected，只提供旧数据只读视图、迁移预览和设置提示，不启动自动下载。
适用旧文件：updater.json、download_tasks.json、system_config.json、web_config.json，存在的逐个保存字节与 sha256；不存在记 missing，不伪造空文件。未知字段、未知源和其他文件原位保留。日志/图片/CBZ 不全量复制，旧目录不移动；备份清单记录相关根路径。已有环境变量不向报告转储凭据，回退沿用原容器配置。

1. 确认旧容器/CLI writer 已停止。此项需要部署操作事实及用户确认，不以新进程锁替代。
2. preview 严格读取全部待导入文件；JSON 错误、错误类型或不可读须报错，不能“读失败当 {}”。显示来源、记录数量、停用/未知数量、旧下载真实性缺口、不会移动文件，以及 proposed_import_fingerprint。
3. apply 请求提交预览指纹及 legacy_writer_stopped_ack；加排他锁，再核对原文件指纹，变化则 409 重做预览。后续再次核对，防止导入期间仍有旧 writer。
4. 在 /data/legacy-snapshots/<id> 创建私有完整字节备份与校验清单；校验失败停止。快照含凭据，不加入 Git、日志或公开下载接口。
5. 在同一 /data 文件系统的 staging 目录构建新 DB、私密配置引用和 migration.json。保留每个原记录的 file_hash + JSON Pointer、原始字段/备份引用；错误或重复身份进入停用待处理，不丢弃也不启动下载。
6. 全部校验通过后提交/关闭/同步 DB，将 staging 无覆盖切换为 core-v1；激活标记只在完整新目录内。目标已存在先判断已导入或异常，不能覆盖。
7. 写入/展示导入报告，五个旧源和未知源全部 disabled/legacy_unverified。新嬉皮订阅须从新源重新添加，不把旧漫画名称自动匹配过去。

同一源快照的重复 apply 返回 already_imported，不新增记录、不计完成。新 core 已有业务数据后拒绝再次初始导入或覆盖；合并导入不属首版。core-v1 残缺、不兼容或导入中断时校验 staging/标记并给可恢复状态，不回到“没有旧数据”的新安装流程。
已有 core 激活后，旧文件再次变化应提示 legacy_external_change 并阻止兼容导入旁路；新核心不追随重写旧 JSON。正常新配置在新命名空间保存，旧原件保留。

## 9. 旧数据语义映射

| 旧数据 | 新版状态 | 不允许的推断 |
| --- | --- | --- |
| name/path_word/group_word | 保留原身份和名称，source=copymanga 的 legacy 记录 | 用相同名称认领嬉皮作品或新文件 |
| latest_chapter | legacy_range_hint，原模式不明时标需要确认 | 此章以前全已下载；游标不存在时自动从末尾开始 |
| downloaded_chapters 名称列表 | legacy_reported_names，保留来源和日期；未核实 | 强制绑定同名新 ID 并计完整完成 |
| downloaded_count | 旧版统计单独展示 | 与新规范完成事实相加 |
| 旧完成/运行任务 | 只读历史，旧未结束标 legacy_interrupted | 为新任务重放或作为源可用证据 |
| 其余四个旧源/未知源、未知字段 | 私有导入保留，UI 显示停用与原因 | 过滤掉或承诺全部已适配 |
| API/账号/代理/推送设置 | 旧源命名空间保留，私密值不回显；路径/调度按验证结果导入核心设置 | 把 CopyManga Token、域名或代理认证发给嬉皮 |
| 旧 CBZ/临时图片 | 原路径保留，未归属新核心的记录显示未知 | 扫文件名就自动标已完成 |

目录已取得、旧名称恰好唯一等都不等于历史图片完整；没有用户确认与内容证据时，不把 legacy_unverified 提升为规范完成。

## 10. 升级、备份与回退

S2/S3/S4 依次交测试镜像/候选镜像/正式镜像，绑定具体提交、版本及 digest。保留旧 v0.1.7 镜像，不覆盖固定标签；正式验证后才推进 latest。
S4 先用代表性旧卷副本预演：启动迁移预览→核对备份→显式导入→检验旧记录和文件→新增嬉皮订阅→真实任务/恢复→停止新版→用旧快照和旧镜像验证回退。未通过副本演练不声称原卷升级安全。
原卷操作由用户授权后执行：停止旧容器，保留端口/卷/环境，替换镜像，不执行 down -v。旧版 JSON 不原位改写，旧文件不搬迁；新目录能在同一挂载中共存。
回退停止新版 writer，保留新 core-v1 及新产物的副本，用已验证的旧快照在隔离或恢复的旧数据卷启动旧镜像；不得让旧进程写新版 DB，也不把新完成记录自动逆迁移为旧名称游标。新版本期间新增的下载和进度不在旧快照中，须在回退说明明确，新产物继续保留，不因回退删除。
后续重新升级时选择保留的新状态或重新预览导入，不能自动把两份进度按名称合并。发布成功、启动成功和 NAS 用户验收分别记证据。
