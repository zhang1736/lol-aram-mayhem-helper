# LOL 海克斯助手

英雄联盟 **ARAM: Mayhem（海克斯大乱斗）** 的 OP.GG 数据助手。
游戏内透明浮层显示当前三个海克斯的**强度**与**欢迎度**，并标记欢迎度最高的那个。

程序只做四件事：

```
读取 → 识别 → 查询 → 显示
```

## 安装

1. 从 [GitHub Releases](https://github.com/zhang1736/lol-aram-mayhem-helper/releases/latest)
   下载 `LOL海克斯助手-v1.0.2.zip`
2. 解压到任意目录（例如 `D:\LOL海克斯助手`）
3. 完成

不需要安装 Python、不需要命令行、不需要 LeagueAkari。

## 使用

1. 启动英雄联盟客户端（国服 / WeGame）
2. 双击 `LOL海克斯助手.exe`
3. 正常游戏即可

程序会自动完成：

```
等待客户端 → 连接 LCU → 识别英雄 → 预取 OP.GG 数据
→ 进入对局 → 海克斯界面出现时自动识别并显示
→ 你刷新某个卡槽 → 自动重新识别与更新
→ 对局结束 → 回到等待状态，准备下一局
```

程序常驻在**系统托盘**。关闭主窗口不会退出，只会隐藏到托盘；
真正退出请用托盘菜单里的「退出」。

## 数据

```
英雄统计：
Korea / Iron–Gold

海克斯：
OP.GG Aggregate
```

> 说明：OP.GG 的英雄统计接口支持按地区与段位过滤，因此英雄数据使用 **韩国 / 黄金及以下（Iron–Gold）** 口径；
> 而海克斯统计接口**无法**按地区或段位过滤，所以它只能是 **OP.GG 聚合口径**。
> 两者口径不同，程序里也会分开标注，不会混用。

数据来源：OP.GG（`lol-api-champion.op.gg`）
海克斯名称字典：腾讯官方 `kiwi_augments.json`

## 推荐

```
当前三个海克斯中，欢迎度（popular）最高的那个
```

浮层上标记为 `★ 推荐`，并注明「按当前英雄 OP.GG Aggregate 欢迎度最高」。

程序**不会**用强度、胜率或任何加权公式计算推荐，也不会做 AI 评价。

## 注意

```
不自动点击海克斯
不自动点击刷新
不修改游戏内存
不注入游戏进程
不读取游戏内存
```

程序只做：屏幕截图 → 文字识别 → 查询公开数据 → 显示浮层。

## 数据与日志位置

程序不会往 Program Files 写任何东西。所有可写数据都在：

```
%LOCALAPPDATA%\LOLHexHelper\
├── aram_helper.db          缓存数据库（长期保留）
├── logs\app.log            日志（10MB × 5 轮转）
├── config\user_config.yaml 你的设置
└── data\                   资源覆盖（可选）
```

卸载程序只需删除程序目录；如果也想清空数据，再删掉上面这个目录。

## 设置

主界面 → `[设置]`：

| 项 | 说明 |
| --- | --- |
| 数据地区 / 段位 | 固定为 Korea / Iron–Gold（只读，改口径会产生错误数据） |
| OP.GG Cache TTL | 缓存有效期，默认 6 小时 |
| Overlay | 开关、透明度（0~100）、位置偏移 X/Y |
| Debug Mode | 打开后日志更详细 |
| Windows 启动时自动运行 | 预留，默认关闭 |

设置保存在 `config\user_config.yaml`，重启后自动恢复。

## 托盘菜单

```
显示主窗口
显示/隐藏 Overlay
设置
重新连接 LOL
查看日志
退出
```

## 常见问题

**托盘图标在，但浮层不出现？**
浮层只在海克斯选择界面出现时显示。确认游戏是**无边框窗口**模式（全屏独占模式下浮层可能不显示）。

**英雄识别不到？**
程序优先用选人阶段的数据；ARAM 选人刚开始时 `championId` 会是 0，程序会继续等待，属正常现象。
如果客户端是中途才打开的，程序会在进入对局后用游戏内数据接口补上。

**OP.GG 显示"—"？**
说明该海克斯在当前英雄的 OP.GG 数据里没有记录，或网络暂时不可用。
程序不会用 0 冒充缺失数据。

## 检查更新 / 自动更新

程序内置更新功能（主界面「版本与更新」与托盘「检查更新」）：

* 启动后后台检查线上版本（不阻塞 LCU / OCR / 浮层）
* 发现新版本 → 弹窗显示版本号、更新内容、包大小与更新方式
* 支持文件级增量更新（只下载变更文件）与完整包回退
* 下载校验 SHA-256 → 由内置 `Updater.exe` 备份/替换/重启，失败自动回滚
* 用户数据（`%LOCALAPPDATA%\LOLHexHelper` 下的数据库、设置、日志）不会被更新覆盖

**更新源配置**（发布者）：编辑 `config/config.yaml`：

```yaml
update:
  enabled: true
  # GitHub Release 的稳定清单地址（latest 会自动指向最新 tag）
  manifest_url: "https://github.com/zhang1736/lol-aram-mayhem-helper/releases/latest/download/manifest.json"
  check_on_startup: true
```

留空表示未配置更新源（不会伪造地址，手动检查会提示"更新源未配置"）。

> 本仓库的 Release 是公开的；如果改成私有仓库，朋友将无法匿名下载更新包。

## 开发者

```powershell
py -3.12 -m venv .venv
& ".venv\Scripts\python.exe" -m pip install --cert "certs\windows_ca_bundle.pem" `
    -i https://pypi.org/simple -r requirements.txt PySide6 psutil pytest

& ".venv\Scripts\python.exe" -m src.app --resident      # 开发态常驻（带托盘 GUI）
& ".venv\Scripts\python.exe" -m src.lcu.diagnostic      # LCU 诊断
& ".venv\Scripts\python.exe" -m pytest tests/ -v        # 测试

# 打包
& ".venv\Scripts\python.exe" -m PyInstaller packaging\LOL海克斯助手.spec --noconfirm
& ".venv\Scripts\python.exe" -m PyInstaller packaging\updater.spec --noconfirm `
    --distpath dist_updater --workpath build_updater
Copy-Item dist_updater\Updater.exe dist\LOL海克斯助手\Updater.exe

# 生成更新包（完整 + 增量）与 manifest 模板
& ".venv\Scripts\python.exe" tools\make_release.py `
    --app-dir "dist\LOL海克斯助手" --version 1.0.3 `
    --notes "release\notes-v1.0.3.md" --out "release\1.0.3" `
    --previous "build\old-v1.0.2\LOL海克斯助手" --from-version 1.0.2
```

> 本机 pip 需要 `--cert certs\windows_ca_bundle.pem`（由 `tools/export_windows_ca.py` 导出）。

LCU 连接说明：国服客户端**不能**依赖 lockfile（实测为 0 字节），也不要用 WMI；
程序通过 `CreateToolhelp32Snapshot` + `NtQueryInformationProcess` 读取 `LeagueClientUx.exe` 的命令行参数获取端口与 token。
