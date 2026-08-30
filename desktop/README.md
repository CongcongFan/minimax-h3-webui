# H3 Production Studio

H3 Production Studio 是 MiniMax H3 的 Mac 日常生产工作台。项目、素材、任务快照和成片长期保存在 iCloud Drive，RunPod Pod 只在生成期间存在。

## 当前能力

- 一次导入并排序最多 9 张参考图，自动编译为 `ref2va` 任务。
- React Flow 专用画布，不加载完整 ComfyUI 前端。
- 任务加入队列时冻结不可变快照；清除未开始任务不会影响运行中任务。
- 支持快速预览、日常最佳、极致原生和 SeedVR2 交付高清四种预设。
- RunPod API Key 和会话令牌只存入 macOS 钥匙串。
- 启动前只在澳洲、加拿大、日本、冰岛和挪威读取 RunPod 实时价格与库存；备选 GPU 必须手动选择。
- 创建后再次核对实际数据中心，不符合许可地区时立即删除且不上传素材。
- 队列空闲 30 分钟后删除 Pod；RunPod 端另设创建后 3 小时强制删除，不创建持久卷。
- 按任务快照下载 Ref2VA、FL2VA 和 SeedVR2 的最小模型集合，并显示真实下载容量与当前文件。
- 模型、节点或 ComfyUI 启动失败时保存诊断包并自动删除 Pod。
- 下载回 Mac 后通过 `ffprobe`、时长和 SHA-256 校验，校验通过才标记完成。

## 项目目录

新项目默认创建在 `iCloud Drive/H3 Projects`：

```text
项目名称/
├── project.h3.json
├── Assets/
├── Jobs/
└── Outputs/
```

项目 JSON 只保存相对路径和素材 ID。缩略图不进入项目目录；项目保存使用原子替换，并保留上一版备份。

## 本地开发与打包

```bash
cd desktop
npm install
npm test
npm run tauri build
```

生成的应用位于：

```text
desktop/src-tauri/target/release/bundle/macos/H3 Production Studio.app
```

## 云端镜像发布

正式镜像由 `.github/workflows/publish-h3-worker.yml` 在 GitHub 的 Linux x86_64 环境构建并发布到 GHCR。镜像构建时会执行模型清单和 Python 启动自检，但不会下载或打包模型权重。

```bash
gh workflow run publish-h3-worker.yml \
  --ref feat/h3-production-studio \
  -f version=h3-worker-v0.1.0
```

当前应用锁定的生产镜像是 `ghcr.io/congcongfan/h3-production-worker@sha256:405dfb1853821a5f47726fb12db306a38eb802b1e8d9223381aaeb8b30d31e78`。普通用户不填写镜像地址，也不允许使用 `latest`。云端只公开带一次性令牌的 8000 端口，ComfyUI 仅监听容器内部的 `127.0.0.1:8188`。

## RunPod 连接

在应用右上角打开设置，将专用 RunPod API Key 粘贴到“连接 RunPod”。密钥只写入 macOS 钥匙串。应用会验证余额读取、GPU 报价和 Pod 管理权限；不要把密钥写入聊天、项目 JSON、iCloud 或环境文件。

## 上线前验收

本地代码、模型清单自检、前端构建和 Rust 测试已经通过。完整付费验收仍需按顺序完成：

1. 发布并锁定固定摘要的云端镜像。
2. 在应用设置中保存并测试具有 Pod 管理权限的 RunPod API Key。
3. 用“板栗 停车场”连续执行五次冷启动和四档画质测试。
4. 核对 SeedVR2 人物一致性、远端下载校验、30 分钟自动删除、3 小时强制删除和 RunPod 最终账单。

这些步骤会产生 GPU 费用，应在用户确认后执行。
