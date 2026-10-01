# 发布手册：把这个 skill 推到 GitHub / Gitee

> 面向"第一次发布开源项目"的场景写的，每一步都可以直接复制粘贴。
> 所有 `<尖括号>` 都要换成你自己的值。**带 ⚠️ 的地方必须改，不改会出问题。**

**本目录就是仓库根目录** —— `SKILL.md` 就在根上，整个目录克隆到
`~/.workbuddy/skills/work-log/` 就能直接当 skill 用。下面所有命令都在这个目录里跑。

---

## 0. 发布前必须改的两处

### ⚠️ 0.1 填上你的名字（LICENSE 第 3 行）

```bash
cd /path/to/work-log
# macOS
sed -i '' 's/<YOUR NAME OR GITHUB HANDLE>/你的名字/g' LICENSE
# Linux
# sed -i 's/<YOUR NAME OR GITHUB HANDLE>/你的名字/g' LICENSE
head -3 LICENSE
```

MIT 协议里这一行是**版权归属声明**，留占位符等于没声明。

### ⚠️ 0.2 把 README 里的 `<you>` 换掉

```bash
grep -rn '<you>' README.md README.en.md CONTRIBUTING.md | head
sed -i '' 's#github.com/<you>/work-log#github.com/你的用户名/work-log#g' README.md README.en.md CONTRIBUTING.md
```

### 0.3 （可选）确认没有敏感信息

```bash
# 扫一遍密钥/本机路径，应当无输出
grep -rniE 'api[_-]?key|secret|token|sk-|/Users/|/home/' . --exclude-dir=.git | grep -v '^\s*#' | head
```

这个仓库本来就不需要任何密钥（`llm_agent.py` 的 key 从命令行/环境变量传入），
所以正常情况下应该是干净的 ✓

---

## 1. 初始化本地仓库

```bash
cd /path/to/work-log

git init -b main

# 第一次用 git 的话，先设置身份（只需一次）
git config --global user.name  "你的名字"
git config --global user.email "你的邮箱"

git add -A
git status --short | head -20      # 确认没有 __pycache__ / state.json 被加进来
git commit -m "feat: work-log 0.8.0 —— 多 agent 心跳看板、看门狗与定向等待图"
```

`.gitignore` 已经写好了，`__pycache__/`、`state.json`、`board.md`、`.env` 都会被忽略。

---

## 2. 建远程仓库并推送

### 路线 A：网页建仓（不需要装任何东西，最稳）

1. 打开 <https://github.com/new>
2. **Repository name**: `work-log`
3. **Description**: 复制 [LAUNCH.md](LAUNCH.md#1-仓库设置可直接复制) 里的"About"
4. 选 **Public**；**不要**勾 "Add a README / .gitignore / license"（我们已经有了）
5. 建好后回到终端：

```bash
git remote add origin https://github.com/你的用户名/work-log.git
git push -u origin main
```

**推送时要密码？** GitHub 不接受账号密码，要用 **Personal Access Token**：
<https://github.com/settings/tokens> → Generate new token (classic) → 勾 `repo` → 复制 →
推送时 "Password" 处粘贴 token。或者改用路线 B 直接做认证。

### 路线 B：用 `gh` CLI（认证一次，之后都省事）

本机当前**没有装 gh**。装一下：

```bash
brew install gh
gh auth login          # 选 GitHub.com → HTTPS → 浏览器登录
```

然后一条命令建仓 + 推送：

```bash
cd /path/to/work-log
gh repo create work-log --public --source=. --remote=origin \
  --description "$(sed -n 's/^> \*\*About\*\*：//p' docs/LAUNCH.md | head -1)" \
  --push
```

### 路线 C：SSH（本机当前没有 SSH key）

```bash
ssh-keygen -t ed25519 -C "你的邮箱"      # 一路回车
pbcopy < ~/.ssh/id_ed25519.pub            # 复制公钥
# 打开 https://github.com/settings/keys → New SSH key → 粘贴

git remote add origin git@github.com:你的用户名/work-log.git
git push -u origin main
```

---

## 3. 仓库设置（直接影响别人找不找得到你）

在仓库页右上角 **⚙️ Settings** 或 About 区域的齿轮：

### 3.1 About

| 字段 | 内容 |
|---|---|
| **Description** | 见 [LAUNCH.md §1](LAUNCH.md#1-仓库设置可直接复制) |
| **Website** | 可留空；或填你的博客 |
| **Topics** | 见 [LAUNCH.md §1](LAUNCH.md#1-仓库设置可直接复制)（**这个决定搜索能不能命中**） |

> **Topics 别偷懒。** GitHub 的 topic 是主要发现渠道之一，
> `multi-agent` / `ai-agents` / `agent-orchestration` / `claude-code` / `llmops` 这几个是真正有流量的。

### 3.2 Features

- ✅ **Issues**（让人能报误报 —— 这个项目最需要的就是误报样本）
- ✅ **Discussions**（开放问答，比 issue 更适合"怎么用"）
- ⬜ Wiki（不需要，文档都在 `docs/`）
- ⬜ Projects（不需要）

### 3.3 建议打开

- **Settings → General → Pull Requests**：允许 squash merge（history 干净）
- **Settings → General → Danger Zone → Change visibility**：确认是 Public

---

## 4. 打第一个 Release

```bash
git tag -a v0.8.0 -m "v0.8.0 双向交流 + 办事轴 + 存活形态声明 + 历史可归档"
git push origin v0.8.0
```

到 <https://github.com/你的用户名/work-log/releases/new> ：

- **Choose a tag**: `v0.8.0`
- **Release title**: `v0.8.0 — 双向交流、办事轴与接入片段修正`
- **Describe this release**: 从 [`CHANGELOG.md`](../CHANGELOG.md) 里 `[0.8.0]` 那一节整段复制过去

打 tag 之后再推送，比在网页上手填靠谱。

---

## 5. 国内镜像（可选，但强烈建议）

GitHub 在国内访问不稳。Gitee 镜像能显著提高别人真正用起来的概率：

```bash
# Gitee：先在 https://gitee.com/projects/new 建一个空仓库（不要初始化 README）
git remote add gitee https://gitee.com/你的用户名/work-log.git
git push gitee main --tags

# 之后同步
git push origin main --tags && git push gitee main --tags
```

**Gitee 注意事项**：
- Gitee 对仓库有**开源审核**，README 里最好不要出现外链营销内容
- Gitee 的 Markdown 渲染对 `details` 折叠块支持一般，README 里的 `<details>` 可能显示为展开状态 —— 不影响阅读
- 国内另一个选择是 GitCode（CSDN），流程类似

---

## 6. CI：已经配好了，推上去就会跑

包里**已经带了** `.github/workflows/test.yml` 和它依赖的 `scripts/check_stdlib_only.py`，
push 之后自动生效，不需要额外操作：

| 步骤 | 作用 |
|---|---|
| `bash -n scripts/selftest.sh examples/demo.sh` | shell 语法检查 |
| `python scripts/check_stdlib_only.py` | **挡住"不小心引入第三方依赖"** —— 这是本项目卖点，必须靠 CI 守 |
| `bash scripts/selftest.sh` | 734 条断言 |
| `bash examples/demo.sh > /dev/null` | 端到端可用性（顺带验证演示脚本没坏） |

矩阵：`ubuntu-latest` + `macos-latest` × Python `3.9` + `3.13`，共 4 个组合。

`check_stdlib_only.py` 的原理是"看每个 import 的真实来源文件是否在标准库目录下"，
不用维护白名单；**反向验证过**：故意加一行 `import requests` 会退 1 并指出行号。

CI 跑绿之后，把 README 顶部的静态 badge 换成真 badge（可选）：

```markdown
[![selftest](https://github.com/你的用户名/work-log/actions/workflows/test.yml/badge.svg)](https://github.com/你的用户名/work-log/actions/workflows/test.yml)
```

> ⚠️ selftest 里有几处 `sleep 3` / `sleep 4`，单个组合约 3 分钟，4 个组合并发跑没问题。
> 但别把它挂在"每个 PR 必须 30 秒内绿"的规则上。
>
> ⚠️ 如果你的默认分支不叫 `main`，改一下 `test.yml` 里 `on.push.branches`。

---

## 7. 可选：把验收报告放进仓库

如果你想把那份 HTML 验收报告也放进去（更有说服力）：

```bash
mkdir -p docs
cp /path/to/work-log-真机验收报告.html docs/acceptance-report.html
# 先清掉里面的本机路径
sed -i '' -e 's#/Users/[^<"]*#<本机路径>#g' docs/acceptance-report.html
git add docs/acceptance-report.html
git commit -m "docs: 附真机验收报告"
```

这份报告里有原始现场路径（`/tmp/amd-test3/` 之类），**换机器会 404** ——
所以要么清掉，要么在 README 里说明"路径为作者本机，仅供佐证"。

---

## 8. 常见坑

| 坑 | 症状 | 解法 |
|---|---|---|
| **`__pycache__` 被提交** | 仓库里出现二进制 `.pyc` | `.gitignore` 已含；若已提交：`git rm -r --cached . && git add -A` |
| **`state.json` / `board.md` 被提交** | 仓库里出现你本机的运行数据（可能含项目名、任务描述） | `.gitignore` 已含；同上处理 |
| **执行权限丢失** | clone 下来 `bash selftest.sh` 报 Permission denied | 本包已 `chmod +x`；若丢了：`git update-index --chmod=+x scripts/*.sh examples/demo.sh` |
| **CRLF 换行** | Windows 上 `bash -n` 报奇怪语法错 | 加 `.gitattributes`：`*.sh text eol=lf` |
| **大文件被拒** | push 报 `file exceeds 100 MB` | 本包无大文件（最大约 90KB），如后续加了模型/录屏，用 Git LFS 或别提交 |
| **LICENSE 占位符没改** | 法律上没声明版权 | 见 §0.1 |
| **badge 显示 404** | shields.io 拼错 | 本包用的是**静态** badge（值写死），不依赖仓库，不会 404 |

---

## 9. 发布后清单

```text
[x] LICENSE 里的 <YOUR NAME> 已替换（2026-09-19 已填 YangLiHaoLiuYing）
[x] README / CONTRIBUTING 里的 <you> 已替换（已指向 github.com/YangLiHaoLiuYing/work-log）
[x] git commit 已做（2026-09-30 首次提交）；[ ] git push 成功
[ ] About 描述已填，Topics 已加（≥6 个）
[ ] Issues / Discussions 已开
[ ] v0.8.0 tag 已推，Release notes 已写
[ ] （可选）Gitee 镜像已同步
[ ] （可选）GitHub Actions 已绿
[ ] 浏览器里亲自打开一次仓库首页，确认 banner 图显示正常、代码块没乱
```

---

## 10. 之后怎么发新版本

```bash
# 1. 改代码 + 加断言
bash scripts/selftest.sh

# 2. 更新 CHANGELOG.md（新增一节，不要改历史节）

# 3. 提交 + 打 tag + 双推
git add -A && git commit -m "fix: ..."
git tag -a v0.8.1 -m "v0.8.1 一句话说清改了什么"
git push origin main --tags
git push gitee  main --tags

# 4. 网页上按 tag 建 Release，notes 从 CHANGELOG 复制
```

**版本号怎么选**（这个项目已冻结退出码契约）：

| 改了什么 | 版本 |
|---|---|
| 修 bug、改文档 | patch `0.8.x` |
| 加命令 / 加能力（如新的险情类型） | minor `0.x.0` |
| 改退出码语义 / 改看板文法 | **major `1.0.0`**（会破坏别人的脚本） |
