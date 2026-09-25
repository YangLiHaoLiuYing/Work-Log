# 参与贡献

欢迎 issue 和 PR。这个项目的定位很窄，**先看下面的"什么会收 / 什么不会收"**，
能省掉双方很多时间。

## 最快上手

```bash
git clone https://github.com/YangLiHaoLiuYing/Work-Log.git work-log
cd work-log
bash examples/demo.sh        # 60 秒看懂它在干嘛（不需要 key，不留文件）
bash scripts/selftest.sh     # 394 条断言，本机 M1 实测 ~1m40s
```

实测一遍 demo，再读 [`docs/DESIGN.md`](docs/DESIGN.md) 里那几个「刻意不做」的决定，
你基本就掌握了这个项目的取舍。

## 硬约束（PR 必须满足）

1. **零第三方依赖。** 只用 Python 标准库。出现 `requests` / `pydantic` / `click`
   之类的 import 会被直接拒 —— 这是这个项目存在的理由之一。
2. **`bash scripts/selftest.sh` 全绿**，且在 **Python 3.9 与 3.13 上都能过**。
   （`from __future__ import annotations` 已开，但要避免在**运行期**求值的泛型/新语法。）
3. **新能力必须配回归断言。** 没有断言的功能等于没有功能 —— 尤其这个项目里的 bug
   大多是"静默的"：不崩、不报错，只是让调用方相信一个错的事实。
4. **不要破坏退出码契约。** 见 [`CHANGELOG.md`](CHANGELOG.md#契约冻结说明)。
   尤其：用法错误不许借用业务码 `1`。`[29]` 组会拦住你。
5. **不要动看板行文法。** 历史看板的可反解性依赖它。

## 什么是好贡献

- **新的"心跳全绿也发现不了"的故障模式。** 这是这个项目最有价值的方向：
  不是加功能，而是**扩大可观测的失败类型**。
  目前覆盖了 `[不可达等待]` / `[互相等待]` / `[通信过热]` / `[对话卡在中间]`。
  已知盲区（欢迎挑战，但先在 issue 里讨论可行性）：
  - 「还在动但方向错了」（心跳正常、一直在改错文件 / 无限重试）
  - 「卡在等外部条件」（CI 队列、模型下载、端口占用）
- **降低误报。** 任何一个假阳性都会让人再也不看告警。带复现的误报 issue 非常受欢迎。
- **文档里补"坑"。** 你在真实使用中踩到的坑，写进 `SKILL.md` 的「坑」一节。

## 什么不会收

- 把它改成分布式系统（多机 / 跨网络）。它刻意是单机、纯文本、`flock` 串行化的。
- 引入数据库 / 消息队列 / 服务端进程。
- 用事件流（inotify/kqueue/ReadDirectoryChangesW）替代轮询。
  **这是量过才不做的**，理由和数字见 [`docs/DESIGN.md`](docs/DESIGN.md)；
  如果你有实测数据表明值得做，欢迎带数据来推翻它。
- 调度能力（任务分配、优先级、重试编排）。那是编排层的活，这个项目只管"可见 + 可等待"。

## 提交 PR

```bash
git checkout -b fix/watchdog-false-positive
# 改代码 + 加断言
bash scripts/selftest.sh
bash examples/demo.sh > /dev/null    # 改了任何 *.sh 都要**真跑**，见下方说明
pgrep -fl 'work_log.py.*serve'       # 跑完不该多出 serve 进程（只该有你自己的那块板）
git commit -m "fix: 看门狗不再把 hold 中的 agent 判成卡死"
```

> **`bash -n` 不够，必须真跑一遍。** 它只查语法：`$变量` 后面紧跟中文标点
> （如 `echo "退出码 $rc（业务）"`）在 macOS 自带的 **bash 3.2** 下会把标点首字节吞进变量名，
> 报 `rc?: unbound variable` 当场中止 —— 而 `bash -n` 认为这完全合法。
> 0.5.1 在 `selftest.sh` 里修过 6 处这种写法，却**漏了** `examples/demo.sh`，
> 结果演示跑到一半就崩、退出码 1（0.5.2 才补上）。CI 里现在有一条静态护栏专门挡这一类。

> **演示脚本还必须"跑完不留痕"。** `demo.sh` 承诺"在临时目录里跑、退出自动清理"，
> 这条承诺有两处很容易破：
> ① 每个 `init` 都要带 `--no-auto-ui` —— 否则"第 2 个 agent 上线"会自己起一个后台 `serve`
> 并弹浏览器，而 `cleanup()` 只杀它自己记下的 PID，那些 `serve` 会留下来，
> 一直服务一个本该被删掉的临时目录（实测清出过 3 个跑了 1 天 20 小时的残留，占着 8787/8789/8790）；
> ② **后台任务不能包在函数里** —— `run ... &` 起的是一个子 shell，`$!` 拿到的是那个子 shell 的 PID，
> `kill $!` 杀不到真正的 python；它会变成孤儿跑满 timeout，然后在本脚本结束**之后**
> 把刚删掉的目录又写回来，于是"不留痕"就成了假话。
> 两处都是 2026-09-24 实测踩到才发现的。验证方式就是上面那条 `pgrep`。

commit message 用 `fix:` / `feat:` / `docs:` / `test:` / `perf:` 前缀即可。

**改动说明里请写清**：改之前是什么行为、为什么那是错的、怎么验证。
如果修的是一个"静默"bug，说清它之前是怎么伪装成正常现象的 —— 那正是这个项目最在乎的东西。

## 报告问题

提 issue 时请附：

- `python3 --version` 和操作系统
- `bash scripts/selftest.sh` 的结果（如果是误报类问题，最好附上能复现的最小 `board.md` 片段）
- 实际输出 + 你期望的输出

**不要**把真实 API key 贴进 issue。`llm_agent.py` 的 key 一律通过命令行或环境变量传入。
