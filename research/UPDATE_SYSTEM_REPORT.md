# 内置自动检查更新 / 增量更新 —— 实现报告（v1.0.2）

日期：2026-10-23

## 1. 修改文件与主要实现

### 更新核心（新增 `src/update/`）

| 文件 | 作用 |
| --- | --- |
| `version.py` | 语义化版本比较（`1.0.10 > 1.0.9`，预发布低于正式版） |
| `manifest.py` | 远程清单解析与**严格校验**（HTTPS、sha256、字段、更新类型、完整包回退字段） |
| `client.py` | 异步检查更新；网络/解析失败只返回状态，绝不抛到 UI |
| `downloader.py` | 流式下载 + 进度回调 + 大小/SHA-256 校验（失败删除半包） |
| `checksum.py` | SHA-256 工具（保持 Updater 轻量） |
| `installer.py` | 文件级增量安装：路径安全、用户数据保护、备份、逐文件原子替换、失败回滚、磁盘空间检查 |
| `job.py` | 主程序 → Updater 的 JSON 握手 |
| `updater.py` | 等待主进程退出 → 校验 → 应用 → 重启 → 启动失败回滚 → `update.log` |

### UI

| 文件 | 说明 |
| --- | --- |
| `src/ui/update_dialog.py` | 「发现新版本」弹窗：版本/更新内容/包大小/更新方式；立即更新、稍后提醒、查看更新日志；下载进度、已下载/总大小、失败原因 + 重试、取消 |
| `src/ui/update_controller.py` | 主线程控制器：后台线程检查/下载 + QTimer 轮询；启动延迟检查；重复点击复用任务；手动检查反馈；增量不兼容时自动回退完整包；写入 job → 启动 Updater.exe → 退出主程序 |
| `src/ui/main_window.py` | 新增「版本与更新」区域：当前版本号 + 检查更新按钮 + 状态文字 |
| `src/ui/tray.py` | 托盘新增「检查更新」 |
| `src/ui/app_ui.py` | 创建更新控制器、启动 2.5s 后后台检查、退出时停止更新任务 |
| `src/config/settings.py` + `config/config.yaml` | 新增 `update` 配置段 |

### 打包 / 发布

| 文件 | 说明 |
| --- | --- |
| `packaging/updater.spec` | 独立 onefile `Updater.exe`（不依赖主程序 `_internal`，可安全替换主程序文件） |
| `packaging/updater_launcher.py` | Updater 入口 |
| `tools/make_release.py` | 生成文件清单、完整包、文件级增量包、manifest（或模板）、RELEASE.md |
| `README.md` / `pyproject.toml` / `src/__init__.py` | 版本号 → 1.0.2 + 更新文档 |

### 更新包格式

```text
update-*.zip
├── patch.json          # version / update_type / from_versions / operations[]
└── payload/<相对路径>   # add/replace 的新文件；delete 只需 operations
```

`operations` 支持 `add / replace / delete`；安装器只接受安装目录内的相对路径，
拒绝绝对路径、盘符、`..`、ADS(`:`)，并拒绝对用户数据（数据库/用户配置/日志/备份）的任何操作。

## 2. 更新服务配置（GitHub）

`config/config.yaml`：

```yaml
update:
  enabled: true
  # GitHub Release 的稳定清单地址（latest 永远指向最新 tag 的资产）
  manifest_url: "https://github.com/<OWNER>/<REPO>/releases/latest/download/manifest.json"
  check_on_startup: true
  timeout_seconds: 15
  allow_http: false
```

**当前仓库默认 `manifest_url` 为空 —— 没有伪造任何可用地址。**
在发布者填好真实 GitHub 地址之前：

* 启动检查：静默跳过（日志 `未配置更新源…跳过启动检查`）
* 手动检查：提示「更新源未配置，无法检查更新」

清单示例（由 `tools/make_release.py` 真实生成）：

```json
{
  "version": "1.0.2",
  "minimum_supported_version": "1.0.0",
  "release_notes": ["…"],
  "published_at": "2026-10-23",
  "update_type": "incremental",
  "from_versions": ["1.0.1"],
  "package_url": "https://github.com/<OWNER>/<REPO>/releases/download/v1.0.2/update-incremental-to-v1.0.2.zip",
  "sha256": "…",
  "package_size": 19487360,
  "full_package_url": "…/update-full-v1.0.2.zip",
  "full_sha256": "…",
  "full_package_size": 154… 
}
```

## 3. 发布新版本步骤

```powershell
# 1) 改版本号（src/__init__.py、pyproject.toml）
# 2) 打包主程序 + Updater
pyinstaller packaging\LOL海克斯助手.spec --noconfirm
pyinstaller packaging\updater.spec --noconfirm --distpath dist_updater --workpath build_updater
Copy-Item dist_updater\Updater.exe dist\LOL海克斯助手\Updater.exe

# 3) 生成文件清单 / 完整包 / 增量包 / manifest
#    --previous 可重复（支持跳过多个版本），--from-version 一一对应
python tools\make_release.py `
  --app-dir "dist\LOL海克斯助手" `
  --version 1.0.3 `
  --notes "release\notes-v1.0.3.md" `
  --out "release\1.0.3" `
  --previous "build\old-v1.0.2\LOL海克斯助手" --from-version 1.0.2 `
  --base-url "https://github.com/<OWNER>/<REPO>/releases/download/v1.0.3" `
  --repository "<OWNER>/<REPO>"

# 4) 发布到 GitHub
gh release create v1.0.3 --title "v1.0.3" --notes-file "release\1.0.3\RELEASE.md"
gh release upload v1.0.3 `
  "release\1.0.3\update-full-v1.0.3.zip" `
  "release\1.0.3\update-incremental-to-v1.0.3.zip" `
  "release\1.0.3\manifest.json" --clobber
```

> 首次安装用普通完整 ZIP（`LOL海克斯助手-v1.0.3.zip`）；
> 老用户由 `releases/latest/download/manifest.json` 自动发现更新。

## 4. 增量包生成方式

* 对新版本目录逐文件计算 `size + SHA-256` → `files-v<ver>.json`
* 与每个 `--previous` 目录做**文件级并集 diff**：
  * 新增 → `add`
  * 内容不同 → `replace`
  * 只存在于旧版 → `delete`
* 增量包只包含发生变化的文件；运行时依赖（ONNX/Qt/cv2 等未变）不会重复下载
* `from_versions` 记录所有可增量升级的旧版本；当前版本不在列表 →
  UI 自动改用清单里的 `full_package_url`
* 本次 v1.0.1 → v1.0.2 的增量包就是真实构建 diff（不是手工清单）

## 5. 测试结果

```text
366 passed, 6 skipped
（6 个 skip：当前没有运行中的 LeagueClientUx，真实 LCU 在线用例自动跳过）
其中更新功能专项：76 个测试（tests/test_update_*.py）
```

覆盖：

* 语义化版本（1.0.10/1.0.9、预发布）
* 清单校验（HTTPS、sha256、字段、update_type、from_versions）
* 下载（本地真实 HTTP 服务器）：成功/进度/404/SHA 不符/大小不符/取消
* 安装：新增/替换/删除、路径穿越/盘符/ADS/用户数据拒绝、损坏包拒绝、注入失败回滚、多旧版本兼容、备份清理
* Updater：等待退出、SHA 校验、应用+重启、新版本启动失败自动回滚
* UI：弹窗字段/日志/进度/错误重试、启动最新不弹窗、启动新版本弹窗、稍后提醒、下次启动再次提示、手动检查最新/未配置、重复点击去重、下载成功启动 Updater、下载失败重试

非自动化验证（已完成）：

* 打包版自检：Qt/OCR/certs 正常（`ocr_smoke=OK`）
* **真实 Updater.exe 端到端**：复制 v1.0.2 安装目录 → 应用 1.0.3 测试包 →
  README 被替换、新文件出现、主程序被重新启动、`update.log` 记录
  `applied → relaunched → update successful`，备份目录生成
* **增量包正确性**：v1.0.1 应用增量包后，对 307 个文件逐一 SHA-256 比对，
  与 v1.0.2 新构建**完全一致**

## 6. 发布产物体积（实际）

| 产物 | 体积 |
| --- | --- |
| 首次安装完整 ZIP | **151.0 MB** |
| 解压后安装目录 | **343 MB** |
| 完整更新包 `update-full-v1.0.2.zip` | **147.2 MB** |
| 增量更新包 `update-incremental-to-v1.0.2.zip`（v1.0.1 → v1.0.2） | **18.6 MB** |

增量包含 5 个操作：新增 `Updater.exe`（8.7MB），替换主 EXE（9.9MB）、
`base_library.zip`（1.3MB）、`config.yaml`、`README.md`。

## 7. 仍需真实 Windows 发布环境验证

1. **真实 GitHub 发布链路**：当前没有可用的实际仓库地址，`manifest_url` 默认空；
   配置真实 `owner/repo` 后，用 `releases/latest/download/manifest.json`
   实测自动检查与下载（需要发布者账号）。
2. **数字签名**：当前只做 SHA-256 完整性校验；哈希不能证明发布者身份。
   正式分发建议对主 EXE 与 Updater.exe 做代码签名（任务书中明确允许后续）。
3. **杀毒/SmartScreen**：未签名 onefile Updater.exe 可能在部分机器弹警告。
4. **真实升级场景**：在游戏客户端运行、托盘常驻、正在对局时点击更新，
   确认主程序退出、Updater 替换、重启后 LCU/OCR/浮层恢复（自动化只覆盖了
   无游戏进程的端到端）。
5. **文件被占用/断电**：已实现备份与回滚，但仍需实机验证极端中断场景。

## 8. 已知限制

* 文件级增量（符合要求），未做二进制差分
* 更新后必须重启主程序（不做热替换）
* 新版本启动后 10 秒内崩溃会自动回滚；更晚的运行时崩溃不会自动回滚
* 只支持 Windows；Updater 与主程序同目录分发
