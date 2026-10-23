# GitHub 发布前安全审计报告

日期：2026-10-23
审计对象：`D:\lol海克斯选择`（本地项目）

## 1. 扫描范围

### 文件与目录

* 源码：`src/`、`tests/`、`tools/`、`packaging/`、`config/`
* 文档与配置：`README.md`、`pyproject.toml`、`requirements.txt`、`.python-version`
* 静态资源：`data/kiwi_augments.json`、`data/augment_aliases.json`、`certs/windows_ca_bundle.pem`
* 研究资料：`research/`（含探针脚本、报告、截图、OP.GG 样本）
* 运行时数据：`logs/*.log`、`data/*.db*`、`.pytest_tmp/`
* 构建产物：`dist/`、`dist_updater/`、`build*/`、`release/*.zip`
* 第三方参考：`research/upstream/LeagueAkari`、`research/upstream/hextech-helper`（嵌套 Git 仓库）

### Git 范围

> 审计开始时项目**不是 Git 仓库**（无 `.git`、无暂存区、无远程、无提交历史），
> 因此不存在历史泄露风险。随后创建了一个全新的 `main` 分支，只提交本次审计后的文件。
> `research/upstream/` 下两个第三方仓库没有被加入、也没有作为 submodule 引用。

## 2. 扫描方式

1. **自定义安全扫描器**：`tools/security_audit.py`
   * 文本/配置/日志：私钥块、GitHub/OpenAI/Google/AWS 令牌、JWT、
     `api_key/secret/password/token` 赋值、LCU `--remoting-auth-token`、
     `Authorization: Basic`、个人邮箱、手机号、Windows 用户目录路径、内网 IP
   * 二进制（EXE/DLL/PYD/ZIP）：按 ASCII 字符串扫描个人路径与令牌形态
   * 输出`.audit/security_raw.json`（已脱敏，gitignored）
2. **人工复核**：对每个 HIGH/MEDIUM 命中逐个判断真假；对截图做视觉检查。
3. `gitleaks`、`trufflehog` 在本机**不可用**（未安装），因此没有使用它们；
   本次结论基于上述正则扫描 + 人工复核，不代表绝对安全。
4. 最终对**暂存区文件**重新扫描：179 个文件 **0 命中**。

## 3. 风险项与处理

| 级别 | 位置 | 内容 | 处理 |
| --- | --- | --- | --- |
| HIGH（疑似） | `tests/test_lcu_credential.py` | 命令行样例里的 `--remoting-auth-token` 值（形似真实 22 位 Token） | 视为真实凭据处理：替换为 `TEST-TOKEN-NOT-REAL-02`，并同步测试断言 |
| HIGH（疑似） | 同上 + `tests/test_productization.py` | `--riotclient-auth-token` / 另一处 token 样例 | 替换为 `TEST-TOKEN-NOT-REAL-01/02` |
| MEDIUM | `research/upstream/LeagueAkari/**` | 第三方项目作者邮箱、带手机号形态的测试快照、历史本地路径 | 整个第三方克隆**排除**（不提交、不建 submodule） |
| MEDIUM | `research/opgg_js/**` | OP.GG 第三方压缩 JS，含 token 形态赋值 | 整个目录**排除** |
| LOW | `research/opgg/flight_yasuo.txt` | 某个图标文件名被邮箱规则误判 | 该目录（页面/JS 抓取物）**整体排除** |
| LOW（误报） | `release/*.zip` | 压缩数据被邮箱规则误判 | 发布包**不进入 Git**，仅作为 Release 附件 |
| 信息 | `dist/**/libcrypto-3.dll` | OpenSSL 公开库元数据里的公共联系邮箱 | 公开库信息，非个人隐私；不提交二进制 |
| 已处理 | 原完整游戏截图 | 含整屏 HUD/小地图/第三方水印 | **已删除**；改用仅含三张卡片的脱敏样本 `augment_cards_fixture.png`（140KB） |

**未发现**：真实 GitHub/云服务 Token、私钥文件、`.env` 文件、
数据库连接密码、完整 LCU Token 或 Authorization 头。

`research/lcu_cred.py` 与 `research/test_lcu.py` 只包含**运行时读取/脱敏逻辑**，
没有写入任何真实凭据。

## 4. 文件处理

### 忽略（不提交）

`.venv/`、`venv/`、`__pycache__/`、`.pytest_cache/`、`.pytest_tmp/`、
`build/`、`build_updater/`、`dist/`、`dist_updater/`、`release/`、
`logs/`、`*.log`、`data/*.db*`、`data/_*`、`tools/*.exe`（Python 安装包）、
`.audit/`、`.env*`、`*.key`、`*.p12`、`*.pfx`、
`research/upstream/`、`research/opgg/`、`research/opgg_js/`。

### 保留

* 源码 / 测试 / 打包配置 / 发布与更新工具
* `data/kiwi_augments.json`（腾讯 KIWI 静态字典）与 `data/augment_aliases.json`
* `certs/windows_ca_bundle.pem`（**公开 CA 根证书包**，非私钥）
* `config/config.yaml`（`update.manifest_url` 为空，不含任何地址/凭据）
* `research/*.md`、`research/*.py`、`research/captures/augment_select_*.png`、
  `research/opgg_api/augments_157.json`（测试所需的 OP.GG 公开响应样本）

### 示例配置

未提供 `.env.example`；本项目通过 `config/config.yaml`（默认值）与
`%LOCALAPPDATA%\LOLHexHelper\config\user_config.yaml`（用户覆盖）配置，
两者均不含凭据。

## 5. Git 历史

* 审计前**无 Git 仓库/历史**，因此没有历史提交中的敏感信息。
* 新建 `main`，使用仓库级提交身份
  `zhang1736 <zhang1736@users.noreply.github.com>`，**未使用本机全局邮箱**。
* 没有执行 `git push --force`、历史重写或删除已有提交。

## 6. 未解决 / 待确认

1. **GitHub 仓库名与可见性**：尚未创建远程仓库，也未推送。
   账号 `zhang1736` 当前只有一个无关的 public 仓库 `lol-death-switcher`。
   需要确认新仓库是 **public** 还是 **private**。
2. **Release 附件**：`release/1.0.2/` 下的完整包/增量包/manifest 尚未上传。
3. **代码签名**：目前只有 SHA-256 完整性校验，未做 Authenticode 签名。
4. 若选择 private 仓库，`releases/latest/download` 对匿名用户不可下载，
   朋友无法匿名自动更新；需要 public 仓库 + public Release，或单独公开发布仓库。

## 7. 最终提交范围

180 个文件（含本报告与两处测试 Token 替换），主要包含：

| 目录 | 内容 |
| --- | --- |
| `src/` | 全部源码（LCU/OCR/OP.GG/Overlay/更新） |
| `tests/` | 全部自动化测试 |
| `tools/` | 发布/审计/复现工具（不含 Python 安装包） |
| `packaging/` | PyInstaller 主程序与 Updater 配置 |
| `config/` | 默认配置（无凭据、无更新地址） |
| `data/` | 仅两个静态字典 |
| `certs/` | 公开 CA bundle |
| `research/` | 报告/探针脚本 + 2 个测试样本 |
| 根目录 | README、pyproject、requirements、.python-version、.gitignore |

## 8. GitHub 仓库地址与实际可见性

**尚未创建**（等待确认名称与可见性），因此没有地址、没有 visibility。

## 9. 是否推送

**未推送**。仅完成本地审计、清理与本地提交。

## 10. GitHub Releases 自动更新还需配置

上传附件（首次安装包 + 更新包）：

```text
release/LOL海克斯助手-v1.0.2.zip                （首次安装，151.0 MB）
release/1.0.2/update-full-v1.0.2.zip           （完整更新，147.2 MB）
release/1.0.2/update-incremental-to-v1.0.2.zip （增量更新，18.6 MB）
release/1.0.2/manifest.json                    （需用真实 --base-url 重新生成）
```

并在 `config/config.yaml` 设置稳定地址：

```yaml
update:
  manifest_url: "https://github.com/<OWNER>/<REPO>/releases/latest/download/manifest.json"
```

清单必须由 `tools/make_release.py --base-url ...` 用真实 GitHub 下载地址重新生成，
不能直接使用当前的 `manifest.template.json`（其中的 `<SET_...>` 只是占位符）。
