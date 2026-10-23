# v1.0.1 多局稳定性修复报告

日期：2026-10-23

## 1. 根因与源码证据

### 1.1 首要根因：常驻内层循环复用同一个 LcuClient，且断线被伪装成 UNKNOWN

```text
src/live.py: resident()
    client = await wait_for_client(...)          # 只在**外层**发现一次凭据
    while True:                                  # 内层：一局接一局
        outcome = await play_one_game(..., client, ...)   # 一直复用这个 client

src/lcu/gameflow.py: GameflowMonitor.get_phase()
    except Exception: return GamePhase.UNKNOWN   # 连接错误被吞掉

src/live.py: wait_for_game()
    只在超时后返回 False；循环里把 UNKNOWN 当作普通阶段继续 await _sleep(2)
```

国服 LCU 端口/token 是动态的（日志里 10054 之后端口从旧值变成 4695）。
客户端重启后：

* `get_phase()` 永远返回 UNKNOWN（不抛错、不报断线）；
* `wait_for_game()` 会一直空等到 `--wait-game`（打包版写死 **600 分钟**）；
* `resident()` 从不重新发现凭据、也从不关闭旧 client。

表现就是"某一局之后突然再也不工作，必须重启助手"。

### 1.2 已确认不是根因的部分

* 搜索 `回到非对局状态，结束闭环`：**当前源码已不存在**（属于旧版实现）。
  现在 `None` 只结束**单局** `play_one_game()`，`resident()` 的内层 `while True`
  会继续调用下一局。
* `rollIndex` / `old日志` 同样属于旧版；当前字段是 `offerVersion`。

### 1.3 其他确认的隐患（一并修复）

| 隐患 | 证据 | 影响 |
| --- | --- | --- |
| `_STOP[0]` 全局停止信号在 `resident()` 结束后未清理 | 同一进程第二次 resident/测试会被立刻判定"停止" | 重启监控静默失败 |
| 上一局的后台 OP.GG 预取晚到 | 无会话代际判断 | 可能覆盖新一局的 `active_opgg_stats`/HUB |
| 新 stage 没有强制全量识别 | `CardReader` 的逐槽哈希基线跨 stage 保留 | 新阶段若像素接近则可能复用旧读数 |
| 单帧 OCR 失败即累计"界面消失" | 仅看 `miss_count>=3`（约 0.75s） | 刷新动画/瞬时失败会隐藏 Overlay |
| `AugmentFlow.set_state` 每次切换都 info | WAIT_AUGMENT↔SCANNING 高频 | 日志刷屏 |
| 设置页"Windows 启动时自动运行"是禁用占位 | `settings_dialog.py` | 用户误以为功能可用 |

## 2. 修改的文件与函数

| 文件 | 修改 |
| --- | --- |
| `src/lcu/gameflow.py` | `GameflowMonitor` 增加 `consecutive_failures / last_error / healthy`；失败计数 + 日志 |
| `src/live.py` | `wait_for_game()` 返回 `ready / timeout / disconnected`；`play_one_game()` 中途连续失败返回 `disconnected`；`resident()` 在断线/手动重连时**关闭旧 client 并重新发现凭据**；新增 `resident_pause` 可注入；`finally` 清理 `_STOP` |
| `src/live.py` | 新增 `_SESSION_SEQ` 会话代际；`ChampionTracker(session_id=...)`，`_on_preload_done` 丢弃上一局/非当前英雄的回调 |
| `src/live.py` | stage 变化时 `reader.reset_slots()` + 清空本阶段快照 → **强制全量识别**；"界面消失"改为 `miss_count>=3` **且** 持续 `MISS_SECONDS=1.2s` |
| `src/opgg/provider.py` | `begin_session(session_id=...)`；`_safe_preload` 记录代际，只有同代际+仍是当前英雄才写 `active_opgg_stats`（全局缓存仍保留） |
| `src/ocr/card_reader.py` | 新增 `reset_slots(reason)`（新局/新 stage 全量识别） |
| `src/core/augment_flow.py` | 状态切换日志 10s 节流（状态本身不节流） |
| `src/ui/settings_dialog.py` | 移除禁用的"开机自启"占位（未实现功能不再暴露） |
| `src/diagnose.py` | `--selftest` 增加 cv2 / RapidOCR 真实推理 / certs 检查 |
| `packaging/LOL海克斯助手.spec` | 移除 `opencv_videoio_ffmpeg*.dll`（29.4MB）与 Qt Quick/Qml/Pdf（~16MB） |
| `README.md` / `pyproject.toml` / `src/__init__.py` | 版本号 → 1.0.1，下载名同步 |

## 3. 修复前后的生命周期行为

```text
修复前
  resident: 发现 client 一次 → 内层无限 play_one_game(client)
  断线:     get_phase()=UNKNOWN → wait_for_game 空等 600 分钟 → 用户以为"挂了"
  会话:     OP.GG 后台任务无代际 → 旧结果可能写进新局
  阶段:     逐槽基线跨 stage → 新阶段不一定全量识别
  界面:     3 次不完整（0.75s）即隐藏；set_state 每次 info

修复后
  resident: 发现 client → play_one_game → 单局结束/断线 → **关闭旧 client**
            → 外层重新发现凭据（端口/token 动态）→ 继续下一局
  断线:     GameflowMonitor 连续 5 次失败 → play_one_game 返回 disconnected
            → 10s 内重连；None/Unknown 不再误判为永久断开
  会话:     每局 session_id+1；旧局晚到的预取/查询结果被丢弃
  阶段:     stage 变化 → reset_slots → 三槽全量识别
  界面:     连续失败 + 持续 1.2s 才隐藏；状态切换日志 10s 节流
```

## 4. 测试

```text
290 passed, 6 skipped
  （6 个 skip：当前没有运行中的 LeagueClientUx，真实 LCU 在线用例自动跳过）

新增 tests/test_multi_game_lifecycle.py（11 个）：
  等待链路 None→ChampSelect / 连续失败→disconnected / 结束阶段分类 /
  连打三局不退出 / game_end+no_game 混合后继续 /
  断线后新端口重连（旧连接关闭） / 手动重连关闭旧连接且不并行 /
  浮层隐藏显示不影响常驻 / 上一局晚到结果被丢弃 / 数据状态机 /
  reset_slots 强制三槽全量 OCR

扩展 tests/test_opgg_display.py：data_state 区分 LOADING/ERROR/NO_STATS/READY

注：pytest 默认临时目录当前被系统拒绝访问（WinError 5），
    因此使用 --basetemp="D:\lol海克斯选择\.pytest_tmp" -p no:cacheprovider 运行。
```

CPU 优化回归（重跑 A~E）：

```text
普通游戏 30s    0.00 核 / 0.00%   OCR 0
首次出现 10s    0.11 核 / 0.45%   OCR 3（每槽 1 次）
稳定 30s        0.00 核 / 0.00%   OCR 0
刷新一次        changed_slots=[1] → OCR slots=[1]，1 次
游戏结束        OCR 0 / 抓屏 0
```

## 5. 发布包

| 项目 | v1.0.0 | v1.0.1 |
| --- | --- | --- |
| ZIP | 170.9 MB | **142.4 MB** |
| 解压后 | 381 MB | **335 MB** |
| ffmpeg 视频 DLL | 有（29.4MB） | 已移除 |
| Qt Quick/Qml/Pdf | 有 | 已移除 |

精简后验证（打包版 `--selftest`）：

```text
PySide6 / QtCore / QtGui / QtWidgets / pyside6 / shiboken6  DLL 全部 LOAD OK
data/kiwi_augments.json / config/config.yaml / certs/windows_ca_bundle.pem 存在
cv2=5.0.0
onnx_threads={'det': (2, 1), 'cls': (2, 1), 'rec': (2, 1)}
ocr_smoke=OK
```

浮层预览自检（AUTOQUIT + PREVIEW）：`Preview finished: reason=TIMEOUT` → `Shutdown complete`，ExitCode=0。

## 6. 已验证 / 仍需实机验证

已自动化验证：

* 常驻多局（脚本化 LCU/Gameflow/OCR）：1/2/3 局连续、断线重连、手动重连、浮层开关
* 会话隔离：旧局晚到的 OP.GG 结果丢弃、新局不清全局缓存
* 识别：新 stage 全量识别、单槽刷新只更新该槽（真实截图 + 真实 RapidOCR 回放）
* 打包：依赖导入、OCR 真实推理、Qt、证书、浮层预览、退出

仍需实机验证（当前客户端处于大厅，无法开对局）：

* 真实国服客户端连续 2~3 局的完整闭环（ChampSelect→InProgress→EndOfGame→下一局）
* 真实客户端进程重启（端口变化）后的自动恢复

建议下一局观察日志：

```text
[GAMEFLOW] phase=ChampSelect / InProgress / None
[CHAMPION] 候选英雄确定 / 最终英雄(Live Client)
[AUGMENT] 进入新 stage=N，执行一次全量识别
[OPGG] active_champion_id=... / request_start / status / stats_count / match 或 MISS
```

## 7. 尚未完成 / 限制 / 建议

* **推荐模式**：OP.GG 该接口只有 `performance` 与 `popular`，没有胜率；
  因此只提供"欢迎度最高"依据，未提供"胜率优先"（不伪造指标）。
* **开机自启**：已移除禁用占位；如需要，可后续用注册表 Run 键实现并测试。
* **快捷键显隐**：未实现（托盘已有显隐入口）；优先级低于稳定性。
* **进一步减包**：`opengl32sw.dll`（19.7MB）保留作为无 GPU 环境的软件渲染兜底，
  不建议直接删除；`cv2.pyd`（82MB）包含 OCR 必需的图像处理，未拆分。
