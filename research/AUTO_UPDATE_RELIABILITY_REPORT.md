# v1.0.3 自动更新可靠性修复报告

## 实机根因

v1.0.2 客户端确实选择了增量包。日志记录：

```text
预期 19532398 字节
实际 19532398 字节
update_type=incremental
from_versions=[1.0.2]
```

第一次下载耗时约 93 秒，第二次约 132 秒，因此慢是 GitHub Release
资源在当时的网络速度问题，不是误下载完整包。

更新失败的真实原因在孤立 v1.0.2 安装副本中稳定复现：

```text
FAILED: [WinError 32] 另一个程序正在使用此文件
Updater.exe.update-new -> Updater.exe
```

旧 Updater 从安装根目录运行，而增量包包含 `Updater.exe`，导致它尝试替换
正在运行的自身体。替换失败后 Updater 直接退出，未执行新版启动，所以用户
手动打开时仍是旧版本。

## 修复

- 主程序不再直接运行安装目录中的 Updater，而是复制到
  `%LOCALAPPDATA%\LOLHexHelper\updates` 后启动。
- Updater 若被直接从安装目录运行，会先复制自身到临时目录再执行更新。
- v1.0.2 首次升级使用的新增量包不包含根目录 `Updater.exe`；新版 Updater
  作为 `_internal/Updater.exe` 新增，待新版主程序启动后自更新根目录。
- 更新器使用唯一启动 token。新版主程序写入版本、PID、EXE 路径和用户数据
  目录；Updater 必须核对成功后才判定更新完成。
- 新版启动后异常退出、版本握手缺失或版本不符时自动终止失败进程、回滚文件
  并尝试重新启动旧版。
- 下载器记录真实资产名、类型、字节数、耗时和平均速度；支持已下载文件复用、
  HTTP Range 续传和最多 3 次有限重试。
- 增量资产不存在、大小或 SHA-256 校验失败时，才允许回退完整包，并记录原因。
- 发布工具校验应用目录、拒绝带 `dist/` 或应用目录公共前缀的目标路径，并自动
  生成只包含一个应用根目录的安装 ZIP。

## 真实升级验收

使用实际 v1.0.2 安装目录的独立副本：

```text
UpdaterExitCode=0
package sha256 ok
main process exited
applied
relaunched
update successful
```

新进程日志确认：

```text
版本: 1.0.3
GUI ready
```

升级后主 EXE 与 v1.0.3 构建 SHA-256 完全一致。第二次启动后根目录
`Updater.exe` 也被替换为最新版本。SQLite 数据库、设置目录、日志和更新日志
均保留在用户数据目录，未被更新包覆盖。

## 测试

```text
400 passed, 6 skipped
```

跳过项仅为当前机器没有运行 LeagueClientUx 的在线测试。
