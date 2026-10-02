# Docker 镜像更新检测兼容性

## 已确认的现场信息

- 飞牛版本: 用户提供为 `1.2.0203`, 无 Docker 仓库代理。
- 用户使用 `montequilla/copymanga-nasdownloader:latest`, 其他镜像可提示更新。
- 本地 RepoDigest 为 `sha256:400c99b5b3fb1bd4ca5741d45a1917033a0a113e582a30930899d26153e0dadb`。
- 从注册表按该指纹读取配置, 对应版本为 `v0.1.1`, 提交 `060ade55d72f10438b721e255a588a9847dda3a5`。
- 调查时 Docker Hub `latest` 对应 `v0.1.3`, 指纹为 `sha256:54d04218ff9b4ca5867870f55851d00a4042f31e7f3bb0cee6029d875d17a55e`。
- 新旧镜像的根索引和 AMD64 子清单指纹均不同, 本地也保留了仓库来源。

## 发布格式对比

用户提供正常提示更新的对照镜像为 `cloudflare/cloudflared`。调查其 `latest`:

| 项目 | cloudflared | 下载器 v0.1.1 / v0.1.3 |
| --- | --- | --- |
| 根清单 | Docker V2 manifest list | OCI image index |
| 可运行清单 | Docker V2 manifest | OCI image manifest |
| 构建证明附加条目 | 无 | unknown/unknown |

Docker Hub 即使收到仅接受 Docker V2 的请求头, 也会返回下载器的 OCI 索引。
这提供了一个兼容性排查方向, 但未获得飞牛更新检测日志或实现代码, 不能认定格式差异就是根因。
能搜索到镜像也不能单独证明更新检测读取清单成功。

## v0.1.4 的兼容性验证

Docker Hub 发布改为单平台 Docker V2 manifest, 显式关闭 provenance / SBOM 附加证明。
程序功能、账户、下载逻辑、容器端口和存储映射不变, 常规版本标签与 latest 自动发布规则不变。
GHCR 的证明信息和格式保持原样。
发布流程检查每个标签的清单类型、架构、版本和提交, 并实际拉取启动 Docker Hub 容器验证 WebUI。

此调整会减少 Docker Hub 镜像附带的构建溯源信息, 镜像配置仍保留 source / revision / version 标签。
它是一项待现场确认的兼容性调整, 不是已证明修复了飞牛提示。

在飞牛中验证时, 先保留本地旧镜像, 刷新镜像列表或触发界面提供的更新检查, 观察提示。
不要先手动拉取 latest 再判断提示是否修复: 拉取后本地已是新版, 没有版本差异可供检测。
检测周期和入口应以具体飞牛版本的实际界面为准, 本项目不能保证飞牛何时弹出提醒。
如果仍无提示, 应查看飞牛检测时的请求目标、返回状态和解析错误, 继续排查仓库映射、缓存或清单处理。

## 参考

- [Docker 镜像导出格式](https://docs.docker.com/build/exporters/image-registry/)
- [Docker 构建证明的存储格式](https://docs.docker.com/build/metadata/attestations/attestation-storage/)
- [Docker 标签](https://docs.docker.com/reference/cli/docker/image/tag/)

手动或自动打标签本身不影响镜像身份。更新检测应比较同一标签的新旧内容, 而不是把 latest 当成版本号。
