# 发布文案（可直接复制）

这里的文字都是**可直接粘贴**的。涉及事实的地方都跟 [`VALIDATION.md`](VALIDATION.md) 对齐过 ——
**不要为了好听改数字**，这个项目的可信度就建立在那几个能复现的数上。

---

## 1. 仓库设置（可直接复制）

> **About**：多 agent 协作的心跳看板 + 看门狗 + 定向等待图。零依赖纯标准库，检出「静默卡死」与「心跳全绿的死锁」，agent 之间能真的阻塞式互相提问。

**Topics**（复制这一串，用逗号或回车分隔皆可）：

```
multi-agent
ai-agents
agent-orchestration
claude-code
llmops
observability
watchdog
deadlock-detection
python
no-dependencies
```

**Website**：留空，或填你的博客。

**README 顶部一行简介**（用于各种索引站 / Awesome 列表）：

```
work-log — 多 agent 协作的心跳看板、看门狗与定向等待图。零依赖纯标准库；能检出「静默卡死」和「心跳全绿也发现不了的死锁」，还有**第二根轴**盯着「有没有提问开着没人答」（它自己就是从一次 547 秒的静默丢单里长出来的），并让 agent 之间用 ask/reply/await 真的阻塞式协商。
```

**英文一行简介**：

```
work-log — A shared heartbeat board, watchdog and wait-graph for multi-agent work. Zero dependencies (stdlib only). Catches silent stalls and the "all-green deadlock" that heartbeats alone can never see — plus a second axis for questions left open with nobody answering (born from a 547-second silently dropped request).
```

---

## 2. 中文长文案（V2EX / 掘金 / 少数派 / 博客通用）

> 标题建议：**《让多个 AI agent 在同一块看板上协作：我做了一个零依赖的看板 + 看门狗》**

---

最近在同时跑好几个 AI agent 改同一个仓库，踩了两个让我很烦的坑，就写了个小工具。

**第一个坑：agent 卡死了你不知道。**

进程还在，但已经十分钟没动静。你只能主动去问"你还在吗"。所以做了个看门狗：每个 agent 每 15s 往同一块文本看板上写一条心跳，看门狗每 15s 扫一遍，谁静默超过阈值**且没写「任务完成」**就标红 + 告警给在线的 agent 去接管。

**第二个坑更烦：心跳全绿，但团队其实已经死了。**

A 问 B 一个问题，B 也问了 A 一个问题，然后**两边都在等对方回答**。两个进程都活着、心跳都在续、日志干干净净 —— 但谁都不动了。这类故障任何"超时/崩溃"式的监控都抓不到，**因为没有任何东西超时**。

解法是顺手做的：`await` 的时候把"谁在等谁"写进共享状态，这就成了一张等待图。两张就能读出来：

- `[不可达等待]`：我等的那个人已经写「任务完成」了 → 这个答案永远不会来（直接返回，不耗超时）
- `[互相等待]`：存在 A→B 且 B→A → 真死锁，谁都不会先答

这个大概是我做的所有东西里唯一"别人没有"的部分。

**顺带做的第三件事：防止预算烧在互相客套上。**

两个 agent「好的」「收到」「那就这样」来回十几轮，或者一个 agent 陷入循环拼命刷心跳 —— 从看板看它们是**最健康的两个人**。所以加了熔断：连续交替 6 轮提醒、12 轮直接拒绝写入；心跳 60 条/分钟超了就拒，而且**不刷新存活时间**（于是它同时会被判成空转）。

**最后一个坑，是坑我自己：工具造好了，我自己没用。**

这个看板是给"多个 agent 协作"用的 —— 我写完它，转头在自己的会话里连着**四十多个小时**没写一条心跳。原因很具体：协作协议写在另一个仓库的 `AGENTS.md` 里，而我的工作目录**根本读不到那个文件**。板是好的、机制也是对的，人没接上。

所以补了一条命令 `doctor`：一次体检七件事 —— 有没有同项目的**废板**、cwd 里有没有**指令文件**、记忆里有没有**这块板的坐标 + 报到义务**、我自己的**心跳多久没写**了、看板是不是已经被**自己的告警刷满**、**名字固定住了没有**，以及**这个名字底下是不是其实塞了两个实例**（同一个名字被两个同名工具共用，⑥ 查不出来；对「一个进程承载多个会话」的形态则交给 ⑦a —— 同名被两个不相干的目录用过，`cwd` 是唯一还握在手里的证据）。退出码 `1` 表示"你这侧有病"（业务结论），`0` 表示干净，可以直接写进脚本。

真实体检长这样（第五幕，`examples/doctor-output.txt`）：一个自认为在协作的人，被查出 2 处没落好、1 处没固定住，而他自己一无所知。

**然后是这条战线上更反直觉的一步：我把它拆成两件事，发现只有一半需要做。**

「接入」其实是**机械接入**和**认知接入**——前者（板收不收它、界面起不起）本来就自动：一条 `post` 就能注册一个新人，没有 `join` 子命令。真正会失效的是后者：**它根本不知道有这块板**。而模型没有推送通道，上下文只在它自己发起工具调用时才更新 —— 所以唯一能自动化的，是让义务句落进它**启动时一定会读到**的文件里。而这里还有一个更贵的坑：**一个工具只能问别人、不能被别人问时，交流是单向的、而且不报错。** 所以片段必须同时给两样东西 —— 一个问题入口（`ask` / `reply` / `await`），和一个**唤醒器入口**（`waker`，它退出即唤醒宿主会话）+ 一句「醒来第一件事先 `brief --peek`」。只教 `ask` 的片段装上去，对方**永远收不到你的问题**，而它安静得像一切正常。

于是补了 `onboard`（第六幕，`examples/onboard-output.txt`）：把「该报什么名、往哪报、多久报一次」写成一段可直接粘贴的协议，默认只打印、`--to` 才落盘、落盘幂等，目标里已有手写协议时**默认拒绝**。它和 `doctor` 共用同一道判据 —— **开药的和验药的必须是同一把尺**，否则就会出现"自己开的药、自己验不过"这种最难查的错。

`onboard` 刻意**不**替你做"落点判断"：对方到底读 `AGENTS.md` 还是 `CLAUDE.md`，只能靠证据。**猜错落点比不猜更糟**——那会产出一段看起来完备、却永远到不了对方手里的协议，也就是上面那次四十多小时失效的成因。

**接进来之后还有两类失效，会在没有任何人报错的情况下发生**（第七幕）：

- **名字撞了。** 引擎按名字取的是**同一个状态对象** —— 两个会话叫同一个名字时，心跳、条数、未读游标、`awaiting` 全混在一起。看门狗看到的是**两个人交替刷新**，于是**谁都不会被判卡死**，而板上**完全看不出来**。现在名字有三档解析（`--agent` → `$WORK_LOG_AGENT` 给会话钉死 → 板上 `identities.json` 按目录钉死），三档都不成立就**退 2 并打印该跑什么，绝不猜**；`identity set` 发现名字被别的目录占着会**直接拒绝**并指出占用者；`whoami` 把「我叫什么、凭什么」打出来 —— 身份固定这件事如果不可查验，就只是又一句口头约定。
- **换了机器。** 接入片段是要离开这台机器的。它以前写死引擎的绝对路径，到了第二台电脑上就是**死链**，而且失效是静默的（对方照抄、报个"文件不存在"、然后不再报到）。现在它只写**看板目录**，启动方式交给板里那个 `worklog` 引导脚本 —— 脚本自己找引擎，全找不到就**退 70 并把三条修法打出来**。整包搬到别的机器、或换个别的 agent，协议照样能用。

**几个实现上的取舍：**

- **纯标准库、零第三方依赖。** 因为想在任何机器上 `git clone` 就能跑。
- **共享文本看板，不是数据库。** 并发写靠 `flock`，看板能被反解回状态，出问题你能直接拿编辑器改。一次 `tail` 就是全局时间线。
- **不做调度。** 任务分配、优先级、重试编排是编排层的活。它只管"可见"和"可等待"。
- **没上事件流。** 本来想用 inotify 替掉 2s 轮询，先量了一下：2000 行看板下单轮扫描 34ms，其中 97% 是逐行 `datetime.strptime`。改成"日期段算一次基准 + 整数加法"后 11ms。轮询本身只占单核 2.3%（6 个并发等待者 24%，线性），而一次模型往返 30–60s —— 拿三套互不兼容的 API（Windows 还会退化）去换一个可测量的不重要，不划算。

**验证过的部分（不是"写完就发"）：**

- 698 条回归断言 / 61 个测试组，Python 3.9.6 和 3.13.12 上各自全绿
- 用真实模型驱动两个 agent 走完整协议 3 轮：双向问答全部闭环、决定里能引用对方原话、面对与已定结论冲突的用户要求走"阻塞 + 协商 + 显式折中"、看门狗全程 0 误报

**我也知道它测不出什么**（写在 README 里了）：

- 「还在动但方向错了」—— 心跳正常但一直在改错文件 / 无限重试
- 「在等外部条件」—— CI 队列、模型下载、端口占用，等待对象不是 agent

---

**60 秒能自己看到效果，不需要任何 API key：**

```bash
git clone https://github.com/YangLiHaoLiuYing/work-log.git /tmp/work-log
bash /tmp/work-log/examples/demo.sh
```

它会在临时目录里跑出三类故障的真实输出，然后自己清理干净。

MIT 协议，随便用。**如果遇到误报请一定提 issue** —— 对这类工具来说，一个假阳性就会让人再也不看告警，所以误报样本比新功能有价值得多。

---

## 3. 社交短文案

### X / Twitter（英文，两条连着发）

```
I kept losing agents silently.

So I built work-log: agents write a heartbeat to one shared text board every 15s. A watchdog flags anyone silent past the threshold.

But the nastier bug was this: two agents each waiting for the other's answer.
Both heartbeats green. Nothing timed out. Team already dead.

Fix: `await` records who-waits-for-whom into shared state → now it's a graph,
and a 2-cycle is a provable deadlock.

Pure stdlib. Zero deps.

https://github.com/YangLiHaoLiuYing/work-log
```

```
Also added a communication breaker, because the two "healthiest looking" agents
on the board were the problem:

- one kept ping-ponging "ok" "sounds good" for 12 rounds
- the other was in a loop, spamming heartbeats

warns at 6 rounds → refuses writes at 12. Over-budget heartbeats are refused
AND don't refresh liveness.

60s demo, no API key needed:
bash examples/demo.sh
```

### 微博 / 即刻

```
做了个小工具：让多个 AI agent 在同一块文本看板上协作。

① 看门狗抓「静默卡死」——进程还在但十分钟没动静
② 等待图抓「心跳全绿的死锁」——A 等 B、B 等 A，两个心跳都是绿的，但团队已经死了。这类故障任何超时/崩溃监控都抓不到，因为没有东西超时
③ 熔断抓「互相客套烧预算」——两个 agent「好的」「收到」来回十几轮

纯标准库零依赖，Python 3.9+ / 3.13 各 698 条断言全绿。
不需要 API key，一个 bash 脚本就能看到效果 👇
github.com/YangLiHaoLiuYing/work-log
```

### 一句话版（用在各种索引 / Awesome 列表 PR 里）

```
[work-log](https://github.com/YangLiHaoLiuYing/work-log) — Heartbeat board + watchdog + wait-graph for multi-agent work. Detects silent stalls and the "all-green deadlock" (agents each waiting for the other, where nothing ever times out) that heartbeats alone can't see. Zero dependencies, stdlib only.
```

---

## 4. 英文长文案（Show HN / Reddit r/LocalLLaMA / r/LLMDevs）

**Show HN 标题**（HN 偏爱平实、不营销的标题）：

```
Show HN: work-log – a wait-graph for multi-agent work (zero deps, stdlib only)
```

正文：

```
I run several coding agents in parallel on the same repo. Two problems kept biting me.

1. Silent stalls. The process is alive but hasn't moved in 10 minutes. You only
   find out by asking.

2. The all-green deadlock. Agent A asked B a question and B asked A a question,
   then both blocked waiting for the other's answer. Both processes alive, both
   heartbeats green, clean logs — and nothing ever times out. No amount of
   timeout/crash monitoring finds this, because nothing is timing out.

Fix for #2 was to make `await` write the waiting edge into shared state. That
turns waiting into a graph, and a 2-cycle in it is a provable deadlock. It also
lets it distinguish "unreachable wait" (the peer already wrote "done" — that
answer is never coming) from a real deadlock.

The last one bit me personally: I built the tool, then didn't use it. My own
session posted zero heartbeats for ~42 hours — because the collaboration
protocol lived in a different repo's `AGENTS.md`, and my working directory
could not read that file. The board was fine; my side was never wired up.

So there's `doctor`: seven checks in one command — stale duplicate boards for the
same project, instruction files in cwd, this board's path **and the duty to report**
in memory, how long since *I* posted, whether alerts have buried the real signal,
whether my *name* is pinned down, and whether that name is secretly shared by two
instances (a collision the name check alone cannot see; for the "one process, many
sessions" case it falls back to a cwd-based check). Exit code 1 means "your side is
broken" (a business verdict, not a crash); 0 means clean.

Then the more counter-intuitive half. "Joining" is really two things: *mechanical*
joining (does the board accept it, does the UI come up) has always been automatic —
one `post` registers a newcomer, there is no `join` subcommand. What actually fails
is *cognitive* joining: **the new agent has no idea the board exists.** Models have
no push channel; context only updates when they make a call themselves. So the only
automatable move is to get the duty sentence into a file it is *guaranteed to read
at startup*. And there is a costlier trap hiding inside that: **when a tool can ask
but cannot be asked, the conversation is one-way — and it fails silently.** So the
snippet must hand over two things: a way to ask (`ask` / `reply` / `await`), and a
**waker hook** (`waker`; its exit is what wakes a reactive session) plus one rule —
"first thing after waking, `brief --peek`." Wire up a snippet that teaches only
`ask`, and the other side **never receives your question** — and nothing complains.

That's `onboard` (scene 6, `examples/onboard-output.txt`): it emits a
paste-ready protocol — prints only by default, writes on `--to`, idempotent, and it
refuses to clobber a hand-written one. It shares its predicate with `doctor`, because
**whoever prescribes and whoever verifies must use the same ruler.**

`onboard` deliberately does *not* pick the landing spot for you — whether the other
side reads `AGENTS.md` or `CLAUDE.md` can only be settled with evidence. **Guessing
wrong is worse than not guessing**: it yields a protocol that looks complete and
never reaches anyone, which is exactly how that 42-hour outage happened.

Two more failure modes come *after* an agent is wired up, and both of them are
silent (scene 7). **Name collisions**: the engine looks an agent up by name and
gets back *the same state object*, so two sessions sharing a name merge their
heartbeats, entry counts, read cursors and `awaiting` state — the watchdog sees two
people taking turns and **never flags either as stalled**, and the board shows
nothing wrong. Names now resolve in three tiers (`--agent` → `$WORK_LOG_AGENT`
pin per session → `identities.json` pin per directory); if none applies it exits
**2** and prints the command to run rather than guessing, `identity set` **refuses**
a name another directory already holds and names the holder, and `whoami` prints
*which rule* decided. **Moving machines**: the snippet used to hard-code the
engine's absolute path, which is a dead link on machine two — and it fails
silently (the peer copies it, reports "file not found", and stops reporting). It
now contains only the board directory; the `worklog` launcher inside that
directory finds the engine itself, and exits **70 with three fixes** if it can't.
The whole bundle still works on another machine, or with a different agent.

Design choices worth mentioning:

- Shared plain-text board, not a DB. One `tail` is the global timeline, `flock`
  serializes writes, and you can fix it with an editor.
- No scheduler. Task assignment and retry orchestration belong in the
  orchestration layer; this only makes state visible and waiting blockable.
- No event stream. I measured first: with a 2000-line board a scan took 34ms and
  97% of it was per-line `datetime.strptime`. Fixing that got it to 11ms, and
  polling is then 2.3% of one core (24% with 6 concurrent waiters, linear). A
  model round-trip is 30–60s. Not worth three incompatible OS APIs, especially
  with a Windows fallback.

Verified: 698 assertions / 61 groups, green on Python 3.9 and 3.13, plus 3 rounds
of real-model validation driving two agents through the full protocol (all
exchanges closed, decisions quoted the peer's actual wording, 0 watchdog false
positives).

Known blind spots, stated up front: it cannot detect "still moving but in the
wrong direction" (heartbeating normally while editing the wrong file / retrying
forever), and it does not model waits on external conditions (CI queues,
downloads, ports).

60-second demo, no API key, leaves no files:

  git clone https://github.com/YangLiHaoLiuYing/work-log.git /tmp/work-log
  bash /tmp/work-log/examples/demo.sh

MIT. Feedback welcome — especially false positives. For this kind of tool one
false alarm means people stop reading the alerts entirely, so false-positive
reports are worth more than feature requests.
```

---

## 5. 配图建议

仓库里已经有 `assets/banner.svg`（README 顶部自动显示）。
如果要在社交平台发，建议**另存成 PNG**（部分平台不渲染 SVG）：

```bash
# 需要 rsvg-convert（brew install librsvg）或直接用浏览器截图
rsvg-convert -w 1600 assets/banner.svg > /tmp/work-log-banner.png
```

如果要做 GIF/录屏，最值得录的是 **`examples/demo.sh` 的第二段（互相等待那一段）**——
它把一个"看不见的故障"变成了屏幕上的一行红字，这是最难用文字说服人的地方。

如果只截一张静态图，比功能列表更有说服力的是一屏 `doctor` 的输出 ——
全是"我以为我接好了，其实没有"（见 `examples/doctor-output.txt`）。

---

## 6. 发布节奏建议

1. **先发 GitHub，自己用一两周**，把误报和文档缺口补一轮再宣传 —— 详见下面那句。
2. 中文先发 **V2EX / 掘金**（技术受众密度高，反馈质量好）；微博/即刻适合短文案。
3. 英文发 **Show HN**（挑工作日美西上午）或 **r/LocalLLaMA**（这个社区对"本地、零依赖"特别友好）。
4. 往 Awesome 列表提 PR（搜 `awesome ai agents`、`awesome llmops`）——流量长尾，但要先确保 README 英文版质量够。

> **最后一句实在话**：这类工具最怕的不是没人用，是**误报**。
> 一个假阳性就会让人把告警关掉，然后你所有的检测都白做了。
> 所以如果只挑一件事先做，就是把 `selftest.sh` 里那几组误报边界继续加厚。
