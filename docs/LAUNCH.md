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
work-log — 多 agent 协作的心跳看板、看门狗与定向等待图。零依赖纯标准库；能检出「静默卡死」和「心跳全绿也发现不了的死锁」，并让 agent 之间用 ask/reply/await 真的阻塞式协商。
```

**英文一行简介**：

```
work-log — A shared heartbeat board, watchdog and wait-graph for multi-agent work. Zero dependencies (stdlib only). Catches silent stalls and the "all-green deadlock" that heartbeats alone can never see.
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

**几个实现上的取舍：**

- **纯标准库、零第三方依赖。** 因为想在任何机器上 `git clone` 就能跑。
- **共享文本看板，不是数据库。** 并发写靠 `flock`，看板能被反解回状态，出问题你能直接拿编辑器改。一次 `tail` 就是全局时间线。
- **不做调度。** 任务分配、优先级、重试编排是编排层的活。它只管"可见"和"可等待"。
- **没上事件流。** 本来想用 inotify 替掉 2s 轮询，先量了一下：2000 行看板下单轮扫描 34ms，其中 97% 是逐行 `datetime.strptime`。改成"日期段算一次基准 + 整数加法"后 11ms。轮询本身只占单核 2.3%（6 个并发等待者 24%，线性），而一次模型往返 30–60s —— 拿三套互不兼容的 API（Windows 还会退化）去换一个可测量的不重要，不划算。

**验证过的部分（不是"写完就发"）：**

- 290 条回归断言 / 32 个测试组，Python 3.9.6 和 3.13.12 上各自全绿
- 用真实模型驱动两个 agent 走完整协议 3 轮：双向问答全部闭环、决定里能引用对方原话、面对与已定结论冲突的用户要求走"阻塞 + 协商 + 显式折中"、看门狗全程 0 误报

**我也知道它测不出什么**（写在 README 里了）：

- 「还在动但方向错了」—— 心跳正常但一直在改错文件 / 无限重试
- 「在等外部条件」—— CI 队列、模型下载、端口占用，等待对象不是 agent

---

**60 秒能自己看到效果，不需要任何 API key：**

```bash
git clone https://github.com/YangLiHaoLiuYing/Work-Log.git /tmp/work-log
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

https://github.com/YangLiHaoLiuYing/Work-Log
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

纯标准库零依赖，Python 3.9+ / 3.13 各 290 条断言全绿。
不需要 API key，一个 bash 脚本就能看到效果 👇
github.com/YangLiHaoLiuYing/Work-Log
```

### 一句话版（用在各种索引 / Awesome 列表 PR 里）

```
[work-log](https://github.com/YangLiHaoLiuYing/Work-Log) — Heartbeat board + watchdog + wait-graph for multi-agent work. Detects silent stalls and the "all-green deadlock" (agents each waiting for the other, where nothing ever times out) that heartbeats alone can't see. Zero dependencies, stdlib only.
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

Verified: 290 assertions / 32 groups, green on Python 3.9 and 3.13, plus 3 rounds
of real-model validation driving two agents through the full protocol (all
exchanges closed, decisions quoted the peer's actual wording, 0 watchdog false
positives).

Known blind spots, stated up front: it cannot detect "still moving but in the
wrong direction" (heartbeating normally while editing the wrong file / retrying
forever), and it does not model waits on external conditions (CI queues,
downloads, ports).

60-second demo, no API key, leaves no files:

  git clone https://github.com/YangLiHaoLiuYing/Work-Log.git /tmp/work-log
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

---

## 6. 发布节奏建议

1. **先发 GitHub，自己用一两周**，把误报和文档缺口补一轮再宣传 —— 详见下面那句。
2. 中文先发 **V2EX / 掘金**（技术受众密度高，反馈质量好）；微博/即刻适合短文案。
3. 英文发 **Show HN**（挑工作日美西上午）或 **r/LocalLLaMA**（这个社区对"本地、零依赖"特别友好）。
4. 往 Awesome 列表提 PR（搜 `awesome ai agents`、`awesome llmops`）——流量长尾，但要先确保 README 英文版质量够。

> **最后一句实在话**：这类工具最怕的不是没人用，是**误报**。
> 一个假阳性就会让人把告警关掉，然后你所有的检测都白做了。
> 所以如果只挑一件事先做，就是把 `selftest.sh` 里那几组误报边界继续加厚。
