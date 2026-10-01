#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
work-log — 多 agent 心跳看板 / 看门狗 / 用户喊话通道

设计目标
  1. 每个 agent 每 <=15s 往同一块看板上写一条「我在想什么 / 我在干什么」
  2. 看门狗每 15s 扫一次：谁静默超过阈值、且没写「任务完成」→ 判定疑似卡死
     → 往看板 + alerts.md 写告警，让在线 agent 去检查、接管、修复
  3. 用户随时能往 user.md 喊话，agent 每次心跳都会被提醒取走

零依赖，只用标准库。所有写入都走 flock，多进程并发安全。
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import fcntl
    HAVE_FLOCK = True
except ImportError:                 # Windows 等无 fcntl 的平台：退化为无锁
    fcntl = None
    HAVE_FLOCK = False

DEFAULT_DIRNAME = "work-log"       # 放在桌面下的子目录名
HEARTBEAT = 15          # 协议心跳周期（秒）
STALE_AFTER = 45        # 默认卡死阈值 = 3 个心跳周期
# ★ 第二根观测轴的阈值（批次2，2026-09-29）：「提问开着多久没人办」算事故。
# 10 分钟 = 够一次真模型的往返（实测单轮 30~120s）+ 一次重试 + 一点余量。
# 为什么需要这根轴：当天心跳轴误报 33 条 / 真卡死 0，而唯一一次真事故（提问被静默
# 丢弃 547s）**告警 0 条** —— 不是阈值调错了，是观测口径与系统形态错位。
STALE_OPEN_AFTER = 600
STALE_OPEN_ENV = "WORK_LOG_STALE_OPEN_AFTER"
HOLD_DEFAULT = 600      # hold 不给 --seconds 时的默认挂起窗口（10 分钟）
COOLDOWN = 135          # 同一 agent 重复告警的最小间隔（秒）
# ---- 告警退避（P0）：静默 2 分钟可能是卡死，静默 20 小时那是"人已经不在了" ----
# 只看 alert_open 就永远按 COOLDOWN 重复，噪音会反过来把真信号埋掉：
# 实测某板上 42 小时攒出 **994 行 watchdog vs 110 行实质发言**，
# 「另一个 agent 真的停了」这件事被彻底淹没。所以重复到一定次数后**指数退避**。
# （「离线」是另一个维度 —— 按**时长**分档，见下面 OFFLINE_* 与 agent_offline()。）
ALERT_BACKOFF_AFTER = 3     # 连续重复告警达到此次数后开始退避
ALERT_BACKOFF_BASE = 135    # 退避基数（秒）。刻意**不**引用 cooldown：否则有人把
                            # cooldown 调成 0，就等于把退避也一起关掉了
ALERT_BACKOFF_FACTOR = 2    # 每次退避的倍数：135 → 270 → 540 → 1080 → …
ALERT_BACKOFF_CAP = 3600    # 退避上限（秒）：最坏情况 1 小时提醒一次，不会彻底静音
# 险情轴 / 办事轴额外要一个 60s 地板（心跳轴没有这个地板）：
# 这两根轴的 key 比"人"多得多 —— 每个提问、每对死锁各一个 —— 沿用它们原有的取值。
HAZARD_MIN_GAP = 60

# ---- 「离线」档（★ 判据只能有一处，三处共用 agent_offline()）----------------
# 静默到小时量级 ⇒ 它多半不是"卡在某一步"，而是人已经不在了。两者处置动作**相反**：
#   疑似卡死 → 去接管 / 让它补 hold；
#   离线     → 去救没意义，等它自己 post 回来，或按接管纪律替它收尾并 ack。
# 历史坑：这个档**只**落在告警正文里（而且按"报了几次"翻档，约 3~5 分钟就升格），
# 状态表与网页徽章完全不知道它 —— 于是同一个 agent 在一个地方被说成「离线」、
# 在另一个地方被永远说成「疑似卡死」，旁边还挂着"补 hold"按钮（那味解药对一个
# 已经不存在的进程毫无意义）。规格（protocol.md「措辞分档」）写的本来就是按**时长**分档。
OFFLINE_FLOOR = 3600.0   # 至少静默 1 小时（"小时量级"的下限）
OFFLINE_MULT = 20        # 且 ≥ 20× 卡死阈值（阈值被调大时跟着变，别让短阈值板误判）
MAX_ALERTS = 200        # state.json 里保留的告警条数上限，防止无限增长
MAX_EXCHANGES = 500     # 保留的问答条数上限（未闭环的永不裁）
MAX_ACKS = 300          # 保留的用户回执条数上限
MAX_DIALOGUE = 400      # 保留的「定向对话流水」条数上限（乒乓熔断用的原始数据）
HDR_RECHECK = 60        # 板头阈值最多每 60s 去核对一次（post 是热路径，board.md 可能几百 KB；
                        #  太勤会把整个板文件反复读进内存，太懒则板会说谎）
DEFAULT_PORT = 8787

# ---- 通信熔断（P1）：防止 agent 把预算烧在"互相确认"和"循环刷心跳"上 -------
# 这组数字是**守护阈值**，不是性能参数：正常的深度协商碰不到它们，
# 只有真正的失控才会撞上。撞上也是渐进处置（先告警，再拦），且总能 --force 覆盖。
PINGPONG_WARN = 6       # 同一对 agent 连续交替轮次 → 开始告警
PINGPONG_HARD = 12      # 达到此值 → 直接拒绝新的 ask/reply（--force 可覆盖）
PINGPONG_WINDOW = 600   # 连续交替的判定窗口（秒）；间隔超过它就视为"已经不是同一个来回了"
POST_CAP = 60           # 单个 agent 每分钟 post 上限；超了几乎一定是陷入了循环
POST_WARN = int(POST_CAP * 0.7)   # 预警线
POST_WINDOW = 60.0      # 心跳预算的统计窗口（秒）

# ---- 退出码：全部集中在这里，因为"哪个码代表什么"是这套协议的对外契约 -------
# 踩过的坑：以前所有前置条件错误都走 `die("✗ …")`，而 Python 对字符串参数
# 默认退出码是 **1**，恰好又是协议里"超时/对方没回"的业务码。于是
# "编号写错了" 和 "对方没答" 在调用方看来一模一样 —— 一个工具用法错误
# 直接变成了业务结论。现在用法/前置条件错误一律 EXIT_USAGE=2，
# 与 argparse 自己的用法错误码保持一致；内部崩溃另走 EXIT_INTERNAL=70。
EXIT_OK = 0
EXIT_TIMEOUT = 1        # 业务：await 超时 / check 发现卡死
EXIT_USAGE = 2          # 用法/前置条件错误（编号不存在、空文本、问自己、--id 0 …）
EXIT_PEER_DONE = 3      # 业务：**已被别人处置** —— 对端已收工 / 资源已被占 / 喊话已被认领。
                        # 刻意合并成一个码：这三种的处置动作相同（别再等、也别重做），
                        # 而调用方**不该需要知道自己在跑哪个子命令**才能读懂结论。
                        # 别把「已被别人认领」挪去 1 —— 1 的样本全是故障，这是期望结局。
EXIT_BREAKER = 4        # 业务：被通信熔断/心跳预算拒绝
# 业务：await 时发现「我要等的人正在等我」—— 互相等待，谁都不会先答。
# 这个码是 2026-09-28 真模型验收现场逼出来的：两个真模型各问各的、同时阻塞等对方，
# 干等满 5 分钟超时才动。单列一个码（而不是复用 1「超时」）是因为**处置动作相反**：
# 超时可以催/换人；互相等待必须**先回答对方**，否则再等多久都是零进展。
EXIT_DEADLOCK = 5
EXIT_INTERNAL = 70      # 工具自己坏了（sysexits 的 EX_SOFTWARE）
# 对端留下的 deadlock 标记，多久之内还算"新鲜"。
# 只需要覆盖「对端下一次轮询」（await 默认 --interval 2s，就算它正卡在一次长工具调用里，
# 几分钟也该回来了）；超时的标记一律不采信，否则会变成永久事实。
DEADLOCK_HINT_TTL = 300.0

DONE_TAGS = {"任务完成", "完成", "done", "DONE", "TASK_DONE", "收工"}
# 「反复收工」的**提醒**阈值（注意：只提醒、不作废完成状态，理由见下）。
# 一条真收工只会发生一次；但"一个回合收工一次"的交互会话（WorkBuddy / CLI）
# 会连着写多条完成标签 —— 工作期间它不发中间心跳，于是尾部就会累积好几条完成。
# 实测（2026-09-29 真板）：把"连续 ≥3 条完成"直接当成循环去作废完成状态，
# **误伤了一条正常工作的 WorkBuddy 线**（它的三条完成间隔 29~83 分钟），
# 并且当场让它开始被报「疑似卡死 2h8m」。而那种形态恰恰是本项目最主要的用法。
# 所以判据补上**时间**这一维：循环进程反复宣布收工的节奏**像心跳**（分钟级），
# 真收工的人按回合来（十分钟以上）。两维都满足才算"看着像循环"，
# 而且**只提醒、不改判**——引擎分不出"勤快的会话"和"守护进程"，那件事该由声明负责。
REPEAT_DONE_N = 3
REPEAT_DONE_WINDOW = 600.0
REPEAT_DONE_WINDOW_ENV = "WORK_LOG_REPEAT_DONE_WINDOW"
RESERVED_AGENT = "watchdog"
# 人类用户在对话里的保留身份：agent 可以向「用户」提问（ask --to 用户），
# 人用 `reply --agent 用户 --id N` 直接回答，await 原生能等到这条答复。
# 但任何 agent 都不许叫这个名字注册/发言 —— 那会冒充人类、污染对话归属。
USER_NAME = "用户"
# 「协作模式」的**标记线**（不是人数上限，也不是目标人数）：
# 同时「在干活」的 agent 达到这个数量就亮起「协作中」标记。
# 「在干活」= 已开工（写过心跳）、未收工、且静默未超阈值（心跳中 / 挂起中）。
# 为什么只做标记线：实际同时在干活的 agent **人数不固定**（常见 3~6 个，随时增减），
# 把它做成 "N/2" 这种分数式会让人误以为"只支持两个"「到顶了」——**表达成下限即可**。
# 每个板可用 `init --collab-min N` 覆盖（写进 state.json），不改这里。
COLLAB_MIN = 2


def agent_offline(silence: float, stale_after: float) -> bool:
    """静默久到不可能是「卡在某一步」⇒ 判定**离线**（人已经不在了）。

    ★ 全项目**唯一**的「离线」判据。告警措辞、`status` 状态表、网页徽章三处都必须
      调它 —— 三处各自写一遍，就会出现"同一个 agent 两套说法"这种最难查的漂移
      （实测：告警说「离线，不是卡死」，同一屏的状态表却红着「疑似卡死」+ 补 hold 按钮）。

    与「疑似卡死」共用同一个上游条件（静默 > stale_after 且未收工），
    只是**措辞与补救动作**不同；`check` 的退出码不受它影响（工作可能真的丢了）。
    """
    return silence >= max(OFFLINE_FLOOR, stale_after * OFFLINE_MULT)


def agent_is_done(tag, loop_decl: bool = False, done_flag: bool = False) -> bool:
    """「此刻这个人算不算收工了」—— **唯一判据**，`evaluate` 与 `cmd_post` 必须共用它。

    ★ 存在的理由（2026-09-29 真板实测，我自己踩的）：
    「收工」在代码里一度有两个入口、两套判据 ——
      · **看门狗**认 `tag ∈ DONE_TAGS`（也就是板上人人都在写的 `--tag 任务完成`）；
      · **`post --done` 的"收工即放手"**（放锁 + 清挂起窗口）只认 `--done` 这个 flag。
    于是按板上惯例写 `--tag 任务完成` 的 agent，收工后**照旧占着锁** ——
    「我已下线，却还卡着别人要用的资源」这个要根治的病，原样留着，而且更隐蔽：
    提示语（`✓ 已标记完成`）只在 `--done` 时打，所以按惯例写法的人连提醒都看不到。
    这不是两个 bug，是**同一件事被写了两遍**。所以抽成这个函数，两处都调它。

    参数：
      · `done_flag` —— 显式 `--done`。人亲口说"我收工了"，优先级最高（哪怕声明过循环型）。
      · `loop_decl` —— 登记表里 `identity set --loop` 的声明。**声明优先于标签**：
        循环型（守护/监听/看门狗）写「任务完成」是它的心跳习惯，不代表它要下线。
        这一点必须和 `evaluate` 一致，否则会出现"锁放了、状态却还是 active"的错配。
    """
    if done_flag:
        return True
    return (tag or "") in DONE_TAGS and not loop_decl
# 自动 UI（理想流程：第 2 个 agent 上线 → 自动起 serve + 弹浏览器）。
# WORK_LOG_NO_AUTO_UI=1 整个关掉（不自动起、不弹）；WORK_LOG_NO_UI=1 只关
# 「弹浏览器」但仍然自动起 serve —— selftest 靠它在 CI 里验证自动接入、又不开真浏览器。
UI_ALL_ENV = "WORK_LOG_NO_AUTO_UI"
UI_OPEN_ENV = "WORK_LOG_NO_UI"

# ---- 「同一个工具的多个实例」识别（2026-09-28） --------------------------------
# 现实里同一台机器上常开着**多个同名工具**：两个 opencode、两个 claude 会话……
# 板上身份只有"名字"这一维，它们会一起报 `opencode` ⇒ `_get_agent` 取到**同一个 dict**：
#   · last_seen 被合并 ⇒ 其中一个死了，另一个照样刷新，看门狗**永远测不到**；
#   · user_cursor 被合并 ⇒ 一个实例 read-user 推进游标，另一个**永远看不到**用户的喊话；
#   · entries / 心跳预算被合并 ⇒ 两个实例共用一个 60 条/分的额度，互相把对方顶到熔断；
#   · await/ask 归到同一个名字 ⇒ 答复可能被"另一个实例"先取走。
# 而在板上**完全看不出来**（和"两个会话撞同一个名字"是同一类病，但那种至少能被
# `identity set` 的 cwd 冲突挡住，**多个实例的 cwd 往往完全相同**，挡不住）。
# 所以这里给"我是哪个实例"留一个标识，并让同名多实例**当场留痕**。
INSTANCE_ENV = "WORK_LOG_INSTANCE"        # 显式指定（最准，建议给每个实例的启动器设）
INSTANCE_OFF_ENV = "WORK_LOG_NO_INSTANCE"  # =1 关掉自动探测（不想被探测时）
INSTANCE_HINTS_ENV = "WORK_LOG_HOST_HINTS"  # 追加宿主名（逗号/空格分隔）——别改代码就能扩展
# 「像 agent 宿主」的进程名片段：沿父链找第一个命中的当实例锚点。
# 只放**每个会话一个进程**的宿主；不认识的宁可认不出（返回空串）也不乱认。
#
# ⚠ **这个名单天生会落后**（2026-09-28：用户问「Hermes / Kimi Code 能识别吗」——
#    本机 `~/.local/bin/hermes`（bash 脚本）、`~/.kimi-code/bin/kimi`（Mach-O）
#    两个都装了，名单里都没有 ⇒ 探测直接返回空串）。所以配套两条：
#      ① `$WORK_LOG_HOST_HINTS=名字1,名字2` 可以**不改代码**追加（正解）；
#      ② `doctor ⑦` 会把父链逐层打出来，并告诉你该往名单里加什么。
#    已本机核实：opencode / claude(=Claude Code) / kimi / hermes 都是**一会话一进程**，
#    可以放心当锚点；`.app` 包里的进程**一律跳过**（那种是一个进程承载多个会话）。
INSTANCE_HOST_HINTS = ("opencode", "claude", "codex", "cursor-agent", "gemini-cli", "gemini",
                       "aider", "crush", "goose", "cline", "cody", "amp", "droid", "kilo",
                       "qwen", "kimi-code", "kimi", "hermes", "plandex", "gptme", "auggie",
                       "forge")
# GUI App 包里的进程一律不算宿主：它们是「一个进程承载多个会话」，
# 分不出会话 ⇒ 拿它当锚点会把所有会话算成**同一个**实例（看起来"没问题"，其实是撒谎）。
# 这是**结构性判据**（看路径），不是又一个名字名单 —— 任何 macOS App 都覆盖得到。
INSTANCE_DENY = (".app/contents/macos/",)
HOST_MIN_AGE = 20     # 候选宿主至少活了这么多秒才敢拿它当锚点。
                      # 刚起的短命进程（每次调用都新起一个的那种）锚上去会让标识乱跳 ——
                      # 宁可这次不记，也不要记一个下次就不认的。（测不出年龄时不拒绝）
INSTANCE_KEEP = 8     # 每个名字最多记几个实例（state 别无限长）
INSTANCE_TTL = 7 * 86400   # 实例标识最多记 7 天：换了锚点口径 / 实例再也不出现时，
                           # 幽灵实例不能永远挂在 board 上（doctor ⑦ 会一直报它）

# ---- onboard：把「接入片段」交到第二个 agent 手里 ------------------------------
# 「第二个 agent 接入」其实是**两件**事，自动化程度恰好相反：
#   · 机械接入（工具侧）**早就自动**：cmd_post 走 _get_agent，未知名字当场注册，
#     根本没有 join 子命令；它那一次 post 让 others 非空，顺带触发 auto_ui。
#     ⇒ 这一半不需要改任何东西。
#   · 认知接入（它那侧）**不可能自动**：模型没有推送通道，上下文只在它自己发起
#     工具调用时才更新。唯一办法是让义务句落进它**启动时一定会读到**的文件里。
# 而这一步在 SKILL.md 里是四步手工活，实测翻车点几乎全在「落点」——
# 协议写在了洞的另一侧（写进只对某仓库生效的 AGENTS.md，而当事人 cwd 不在那），
# 于是那个 agent 断心跳 42.4 小时、重复告警刷到 #966。
# 标记块存在的唯一理由是**幂等**：重复 onboard 只替换块内内容，块外一字不动。
ONBOARD_BEGIN = "<!-- work-log:begin（本块由 work_log.py onboard 维护，手改会被覆盖） -->"
ONBOARD_END = "<!-- work-log:end -->"

# ---- 身份 / 可移植性：把「我叫什么」「引擎在哪」从口头约定变成能被查的东西 ---------
# 这三条解决的是同一类失效：协议写对了、**人**却对不上。
#   · 名字：两个会话都叫 agent1 时，_get_agent 按名字取到的是**同一个 dict** ——
#     它们会共享 last_seen / 游标 / entries，板上却看不出是两个人（实测）。
#     所以名字必须能"钉死"、而且钉死这件事要能被查验。
#   · 落点：同一份 AGENTS.md 被**任何**在该目录启动的 agent 读到 ——
#     写死在里面的 `--agent X` 会变成两个人的共同身份。
#   · 引擎位置：接入片段里写死 `/Users/<某人>/…/work_log.py`，换台电脑就是死链。
AGENT_ENV = "WORK_LOG_AGENT"              # 给**这个会话**钉死身份（驱动层/launcher 设）
FORCE_ID_ENV = "WORK_LOG_FORCE_IDENTITY"  # 逃生口：明知名字被别的目录占着也要用
BIN_ENV = "WORK_LOG_BIN"                  # 引擎位置（引导脚本找不到时的兜底）
RC_NAME = ".worklogrc"                    # 看板目录里的**本机私有**配置（引擎位置/默认身份）
SHIM_NAME = "worklog"                     # init 落在看板目录里的引导脚本（会自己找引擎）
IDENT_FILE = "identities.json"            # 名字登记表：名字 -> {cwd, kind, purpose, since}
RC_IGNORE = ".gitignore"                  # 看板目录自己的，只为挡住 .worklogrc

# ---- 落点自动探测：把「对方读哪个文件」从人工取证变成一次机械观测 -------------------
# `onboard` 刻意不替人**猜**落点 —— 猜错比不猜更糟：它会产出一段看起来完备、
# 却永远到不了对方手里的协议（就是 42.4 小时那次事故）。但「不猜」不等于「必须问人」：
# 判据可以是**观测**，因为装过的工具链一定在机器上留下痕迹（配置目录 / 可执行文件）。
#
# 刻意**只认跨目录档**（`user` / `opencode-global` / `claude-global`）：它们不看 cwd，
# 于是「对方在哪个目录启动」这个我们查不到的事实被整个绕开了。仓库级档
# （`repo` / `claude` / `gemini` / `cursor`）必须知道对方的 cwd 才有意义 —— 只能显式指定。
#
# `cross=False` 的条目不是废物：它们用于**报告**。探测到了却落不了，必须说出来并给
# 替代做法，否则就是静默忽略 —— 那是本仓最不能接受的一种失败。
TOOLCHAIN_PROBES = (
    {"name": "opencode", "alias": "opencode-global", "cross": True,
     "paths": ("~/.config/opencode", "~/.opencode"), "cmd": "opencode",
     "note": "任何目录开会话都读"},
    {"name": "Claude Code", "alias": "claude-global", "cross": True,
     "paths": ("~/.claude",), "cmd": "claude",
     "note": "跨目录"},
    {"name": "WorkBuddy", "alias": "user", "cross": True,
     "paths": ("~/.workbuddy",), "cmd": "",
     "note": "每会话注入、会盖到**所有**工作区 —— auto 默认跳过"},
    {"name": "Cursor", "alias": "cursor", "cross": False,
     "paths": ("~/.cursor",), "cmd": "cursor",
     "note": "只有仓库级落点（<cwd>/.cursor/rules/）"},
    {"name": "Gemini CLI", "alias": "gemini", "cross": False,
     "paths": ("~/.gemini",), "cmd": "gemini",
     "note": "只有仓库级落点（<cwd>/GEMINI.md）"},
)

ENTRY_RE = re.compile(r"^<(?P<agent>[^>\s]+)>\s+(?P<time>\d{2}:\d{2}:\d{2})\s*(?P<rest>.*)$")
DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")
TAG_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<body>.*)$", re.S)
USER_RE = re.compile(r"^\[(?P<time>\d{2}:\d{2}:\d{2})\]\s*(?P<who>[^：:]{0,12})[：:]\s*(?P<body>.*)$")
AGENT_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]{1,32}$")
# 「看着像带了时间前缀、但 `USER_RE` 没匹配上」的行 —— 例如 `[21:37:20:20] 用户：…`
# （多打了一段秒）或 `[22:15] 优化这个 skill`（少了秒）。
# 这类行会被当成**纯文本**收下（这是对的，不该拒收用户的喊话），但旧版接下来会把它
# 的 `time` 当空、再由 snapshot 兜底成 `now()` ⇒ **伪装成"刚刚说的"**。所以要能认出来、
# 并能报出去。判据刻意收紧到"方括号里是数字与冒号"，免得把 `[重要] 大家注意` 这类
# 正常正文也标成异常（误报会让这个提示失去意义）。
TIMEISH_RE = re.compile(r"^\[\d{1,2}:\d{2}(:\d{2})*\]")

# `prune` 用来"搬家不删家"的子目录：历史在这里还能查，只是不再拖慢每个命令。
ARCHIVE_DIR = "archive"
# 已确认的老告警，正文留多长。**首选按 `→` 切**：告警正文的形状是
# `<事实> → <该怎么办>`，而"该怎么办"那半句是模板话，一旦确认过就再没有用。
# 真看板实测 200 条里 200 条都带 `→`，按它切正文平均 188 → 69 字节。
# 这个数只是"没有 `→` 时"的保底长度（免得一条超长单体把 state.json 撑大）。
ALERT_TEXT_KEEP = 60
# 截短后的标记（刻意短：它每条都要出现，太长等于没省）。
ALERT_TEXT_MARK = "…（已确认）"
# 文本里"看起来像路径"的片段（给 `_mentions_board` 的归一化比对用）。
# 从 `~` 或 `/` 起、到空白或常见中英文标点止 —— 刻意不认引号/括号/顿号，
# 免得把 `看板：/x/y` 或 `（/x/y）` 连标点一起吞进来。
#
# ⚠️ `~` 分支**必须**要求后面就跟 `/`（或 `~用户名/`）—— 即"看起来像路径"至少要有一个
#    分隔符。不能图省事写成裸 `[~|/]`：中文行文里的**约数波浪号**
#    （`~90%`、`~2.4s`、`~0.9-3s`）会被它吃成路径 token，而那种 token 展不开 ——
#    轻则 `RuntimeError` 一路冒到 main() 退 **70**（实测：真机 `~/.workbuddy/MEMORY.md`
#    里就有 `~90%`，doctor 直接整条炸掉），
#    重则被判成"提到了本板"（假阳性，机理见 `_expand()`）：
#    那正是 `_literal_forms()` 注释里立誓要避免的「永远为真的废话」。
#    裸 `~`（板恰好在家目录）刻意**不认**：判据宁可漏、不可猜 ——
#    漏了只是多跑一次 onboard，猜了是静默假绿。
_PATH_TOKEN_RE = re.compile(
    r"(?:~[A-Za-z0-9._-]*/|/)"
    r"[^\s`'\"“”‘’（）()\[\]{}<>，。；：、,;:!?！？|]*"
)

_WARNED_NO_FLOCK = False

BOARD_HEADER = """# work-log 看板

> 任务：{task}
> 行格式：`<agent> HH:MM:SS [标签] 内容`
> 铁律：每个 agent 每 <= {hb}s 至少写一条心跳
> 标签：收到 / 决定 / 执行 / 阻塞 / 建议 / 任务完成
> 看门狗：`work_log.py watch` —— 默认每 {hb}s 扫一次，静默超过 {stale}s 且未写「任务完成」即告警
> 用户喊话：`work_log.py say --text "..."`（或直接编辑 user.md）
> 用户交流：agent 可 `ask --to 用户`，人用 `reply --agent 用户 --id N` 回答；`status` 可见全部待答
> 多人协作：同时在干活的人数到标记线（本板 ≥{cmin}）即亮「协作」标记——**只是下限，不设上限，人数本来就浮动**；`init --collab-min N` 可调
"""

USER_HEADER = """# 用户喊话通道

> 用户在这里直接写：一行一条，可带 `[HH:MM:SS] 用户：` 前缀，也可直接写纯文本。
> agent 用 `work_log.py read-user --agent <name>` 取走未读消息（自动推进每 agent 游标）。
"""

ALERTS_HEADER = """# work-log 告警流水

> 由 `work_log.py watch` / `work_log.py check` 自动追加，格式同看板。
"""


# ---------------------------------------------------------------- 基础工具

def now() -> float:
    return time.time()


def hhmmss(ts: float | None = None) -> str:
    return datetime.fromtimestamp(now() if ts is None else ts).strftime("%H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def user_ts(stamp: str) -> tuple[float | None, bool]:
    """把 user.md 里的 `[HH:MM:SS]` 变成时间戳。返回 `(ts, 是否可信)`。

    user.md 只写时分秒、**没有日期**，所以日期必须推。两条都要处理：

    · **晚于现在的时刻只能是昨天**。不这么办的话，跨过零点后昨天所有喊话都会被算成
      "今天 22:55"（未来 22 小时），时间线整个错乱 —— 页面上老消息排在最新位置。
    · **解析不出就返回 `(None, False)`，绝不在这里默默用 `now()`**。
      这不是"补个默认值"，是**造假**：一条 21:37 写的消息会显示成"刚刚说的"，
      而且每次刷新时间都变、永远贴在时间线末尾（2026-09-29 真板实测，见 snapshot 注释）。
    """
    if not stamp:
        return None, False
    try:
        h, mi, sec = (int(x) for x in stamp.split(":"))
        ts = datetime.strptime(f"{today()} {h:02d}:{mi:02d}:{sec:02d}",
                               "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None, False        # 含非法段（如 `21:37:20:20` 被当 4 段切）⇒ 不可信
    if ts > now():
        ts -= 86400.0
    return ts, True


def self_cmd() -> str:
    """打给用户/agent **照抄**的「本工具」写法。

    引导脚本会设 $WORK_LOG_SELF，于是提示里出现的永远是**看板自带的那个脚本**，
    而不是引擎在这一台机器上的位置 —— 换台电脑、或只是把引擎挪个目录，
    这些提示照样是能直接粘去跑的（接入片段的可移植性靠的就是同一招）。
    """
    return os.environ.get("WORK_LOG_SELF") or sys.argv[0]


def die(msg: str, code: int = EXIT_USAGE):
    """前置条件/用法错误：打一条 ✗ 到 stderr，然后按 EXIT_USAGE 退出。

    存在的唯一理由见文件顶部退出码那段注释：`sys.exit("字符串")` 会退 1，
    而 1 在协议里是业务码。凡是不属于"业务结论"的失败都必须走这里。
    """
    print(msg if msg.startswith("✗") else f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


def resolve_text(a, flag: str = "--text") -> str:
    """把 `--text <串>` 与 `--text-file <路径>` 归一到同一条字符串。

    **为什么要有 `--text-file`（2026-09-29 补）**：本工具的主战场是**长中文文本**，
    而长中文文本经 shell argv 会撞上整整一类问题，且都不报错：
      · 反引号被当命令执行 ⇒ 报 `command not found` 之后**静默替成空格**，
        帖子里少一个词而发帖人以为自己发对了（当天被三个不同的人各犯一次）；
      · `$变量` 被展开；`${var}` 后面跟中文标点还会把标点首字节吞进变量名，
        整个脚本以 rc=127 死掉（当天实测，ask_listener.sh 里 5 处）；
      · 引号嵌套、换行折叠，各有各的坑。
    这三类的共同点是**问题出在调用方的命令行，不在文本本身**，所以引擎在
    argv 里无论怎么查都查不到（查得到的那种"反引号"恰恰是已经被引号保护好的、
    本来就不会出事的）。**让文本走文件 = 让判据只面对字节，整类问题直接不存在。**

    这也是为什么没有采用「发现反引号就拒收」的写法：那样只会拒掉唯一安全的调用形态，
    对真出事的形态零覆盖 —— 就是"防了一个不存在的威胁，凿出一个真缺口"。

    `--text-file -` 表示从 stdin 读；两者同时给会退 2（谁覆盖谁不该由工具猜）。
    """
    path = (getattr(a, "text_file", None) or "").strip()
    inline = getattr(a, "text", None)
    if path and inline:
        die(f"✗ {flag} 与 --text-file 不能同时给 —— 谁覆盖谁不该由工具猜，请只留一个")
    if not path:
        return inline
    if path == "-":
        data = sys.stdin.read()
        src = "标准输入"
    else:
        p = Path(path).expanduser()
        if not p.is_file():
            die(f"✗ {flag}-file 指向的不是一个文件：{path}")
        try:
            data = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            die(f"✗ {flag}-file 读不出来（{type(e).__name__}: {e}）：{path}")
        src = str(p)
    # 只剥掉**末尾**换行（文本文件几乎都带），别动正文里的换行与空格
    data = data.rstrip("\n")
    if not data.strip():
        # 这里敢退 2，是因为「不给这个参数」已经能表达「用默认」——
        # 空文件不是一种省略方式，是一次**说出口却没内容**的调用。
        die(f"✗ {flag}-file 里没有实质内容（{src}）"
            f" —— 想用默认就别给这个参数，空文件不能代替省略")
    return data


def resolve_dir(args, mkdir: bool = True) -> Path:
    """解析 `--dir` / $WORK_LOG_DIR。**路径算法只留这一处**。

    `mkdir=False` 给「只读或只打印」的命令用（onboard / doctor）：否则一个号称
    dry-run 的命令会顺手把看板目录建出来 —— 那就不叫 dry-run 了。
    刻意用参数而不是让调用方自己拼一遍路径：拼出来的路径一旦和这里差一点
    （比如少一次 resolve），生成的片段就会和 doctor 的判据对不上。

    2026-09-28 起不再有默认落点（旧版落 ~/Desktop/work-log/<当前目录名>）：
    一个不能信任的默认值比没有默认值更费神 —— agent 不传 --dir 时会静默落到
    桌面（触发 macOS 权限弹窗），还会让两个项目共用一块板、看门狗互相报「卡死」。
    现在缺 --dir 一律退 2 + 指路，绝不当场猜。
    """
    d = getattr(args, "dir", None) or os.environ.get("WORK_LOG_DIR")
    if d:
        p = _expand(d)
        if p is None:
            # 走到这里 = `~` 后面不是真实用户名（如把约数写成 `--dir '~90%'`）。
            # 这是**用法**问题，必须退 2 —— 裸露调 expanduser 会抛 RuntimeError
            # 一路冒到 main() 退 70，把"路径写错了"伪装成"工具自己坏了"。
            die(f"--dir 这个路径展不开：{d!r}\n"
                "  `~` 后面只能是当前用户；要指家目录请直接写 `~/…` 或绝对路径。")
    else:
        die("没给 --dir，$WORK_LOG_DIR 也没设 —— 引擎**不再猜默认落点**。\n"
            "  旧版默认落 ~/Desktop/work-log/<当前目录名>：会触发桌面权限弹窗，\n"
            "  还会让两个项目共用一块板、看门狗互相报「卡死」（两个坑都实测踩过）。\n"
            "  三种修法任选：\n"
            "  ① 命令后加 `--dir <板目录>`（板目录看板头/别的 agent 的指令里有写）；\n"
            "  ② 在板目录里直接用板自带的 `./worklog <子命令> …`（已把 --dir 钉死）；\n"
            "  ③ `export WORK_LOG_DIR=<板目录>`（本会话级）。")
    if p.exists() and not p.is_dir():
        die(f"{p} 已存在且不是目录，换个 --dir")
    p = p.resolve()
    if mkdir:
        p.mkdir(parents=True, exist_ok=True)
    return p


def sane_stale(v, d=None) -> float:
    """卡死阈值的**统一解析**：显式 `--stale-after` > 看板声明的（env / `init --stale-after`）> 内置默认。

    关键点：各子命令 `--stale-after` 的 argparse 默认值**刻意是 None 而不是常量 45** ——
    否则"板里声明了 90"会被一个从没人显式写过的默认值悄悄压掉，
    于是又回到"我明明设过 90 却按 45 判卡死"的老问题（自动界面那只隐藏看门狗就是这么来的）。
    只传了 `d` 才能读到板上声明的值；读不到（板还不存在）就退回内置默认，不炸。
    """
    if v is None:
        st = {}
        if d is not None:
            try:
                st = load_state(d)
            except Exception:            # noqa: BLE001 —— 板不存在/读不到都只是"没声明"
                st = {}
        return board_stale_after(st)
    if v < 1:
        die("--stale-after 至少 1 秒（设 0 会让所有 agent 立刻被判卡死）")
    return float(v)


def id_list(v: str) -> list:
    """把 `--id 1,2,3` 解析成编号列表。

    **刻意拒绝 `--id 0`**。0 曾经是"等任何新动态"的合法写法，但它同时是
    这个项目最阴险的坑：驱动层把编号弄丢之后照传 0，就得到一个"成功"、
    而调用方以为自己拿到了答案（实测两个 agent 因此各空转 5 步）。
    现在想要那个语义就**不写 --id**，写 0 直接报错 —— 陷阱从根上拆掉。
    """
    out = []
    for part in str(v).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--id 只能是编号或用逗号分隔的编号，收到 {part!r}")
        if n < 1:
            raise argparse.ArgumentTypeError(
                f"--id 不接受 {n}：编号从 1 开始；想「等任何新动态」就不要写 --id")
        out.append(n)
    if not out:
        raise argparse.ArgumentTypeError("--id 不能为空")
    return out


def check_agent_name(name: str, allow_user: bool = False) -> str:
    """agent 名会直接进看板行首，必须能被打回来 —— 脏名字会让看板反解失效。

    「用户」是人类在对话里的保留身份：只有 reply（人类回答提问）允许用它，
    其余一律拒绝 —— 否则一个叫「用户」的 agent 会冒充人类污染对话归属。
    """
    if not AGENT_RE.match(name or ""):
        die(f"✗ agent 名不合法：{name!r}\n"
                 "  只允许 字母/数字/下划线/点/横线/中文，1~32 字符，不能含空格或 '>'")
    if name == RESERVED_AGENT:
        die(f"✗ '{RESERVED_AGENT}' 是看门狗保留名，换一个")
    if name == USER_NAME and not allow_user:
        die(f"✗ '{USER_NAME}' 是人类用户的保留身份，agent 不能叫这个名字\n"
            f"  想向用户提问：ask --agent <你> --to {USER_NAME} --text \"…\"")
    return name


def check_resource(rsrc: str) -> str:
    if not (rsrc or "").strip():
        die("✗ --resource 不能为空")
    return rsrc


def p_state(d: Path) -> Path:
    return d / "state.json"


def p_board(d: Path) -> Path:
    return d / "board.md"


def p_user(d: Path) -> Path:
    return d / "user.md"


def p_alerts(d: Path) -> Path:
    return d / "alerts.md"


def p_ident(d: Path) -> Path:
    return d / IDENT_FILE


def p_rc(d: Path) -> Path:
    return d / RC_NAME


def p_shim(d: Path) -> Path:
    return d / SHIM_NAME


@contextlib.contextmanager
def locked(d: Path):
    global _WARNED_NO_FLOCK
    d.mkdir(parents=True, exist_ok=True)
    if not HAVE_FLOCK:
        if not _WARNED_NO_FLOCK:
            print("! 当前平台没有 fcntl，并发写不做加锁保护（单进程使用无影响）", file=sys.stderr)
            _WARNED_NO_FLOCK = True
        yield
        return
    fh = open(d / ".lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def new_agent(ts: float) -> dict:
    return {
        "first_seen": ts,
        "last_seen": ts,
        "status": "active",
        "entries": 0,
        "expected_silence_until": 0.0,
        "alert_open": False,
        "last_alert_at": 0.0,
        "alert_repeat": 0,          # 连续重复告警了几次（退避用）；恢复心跳时归零
        "pending_recovery": False,
        "user_cursor": 0,
        # 此刻正在等哪一条提问的回应（await 期间才非空）。
        # 这是「等待图」的精确来源：光看"有没有 open 的提问"分不清
        # 「提问者正在干等」和「提问者早就去忙别的了」。
        # 形状：{"ids": [1,2], "id": 1, "since": ts} —— 多路 await 时 ids 有多条，
        # 等待图会给每个还没回应的目标各建一条边。
        "awaiting": None,
        "posted_ts": [],            # 最近一分钟的心跳时刻（心跳预算用）
        "note": "",
        # 这个名字下出现过的**实例**（{"标识": 最后出现时刻}）。
        # 多实例共用一个名字会静默合并 last_seen / 游标 / 预算（见 INSTANCE_ENV 处的注释），
        # 这是**唯一**能让那种情况在板上留下痕迹的地方。
        "instances": {},
        # 这个名字**第一次被写**时所在的目录（判「同名多线」用，见 `note_cwd`）。
        # 空 = 还没记过（老板升级上来也是空，第一次 post 会补上，不会误报）。
        "cwd": "",
        # 已经就"同名异目录"警告过的目录集合（含其子孙目录不再重复喊）
        "cwd_conflict": "",
        "cwd_warned": [],
    }


def blank_state(task: str = "") -> dict:
    return {
        "task": task,
        "cwd": str(Path.cwd().resolve()),   # 这块看板属于哪个项目：串项目时要能当场发现
        "created_at": now(),
        "heartbeat_secs": HEARTBEAT,
        "post_cap": POST_CAP,       # 每分钟心跳上限（init --rate-cap 可改；0 = 关闭）
        "board_date": "",
        "last_scan_ts": 0.0,        # 上次有人真正扫过看板的时间（含看门狗自检）
        "agents": {},
        "locks": {},
        "alerts": [],
        "alert_seq": 0,
        "user_seq": 0,
        "exchanges": [],            # 定向问答：{id, from, to, question, ts, status, reply, replied_at, by}
        "exchange_seq": 0,
        "acks": [],                 # 用户喊话的人工回执：{id, agent, ts, text}
        # 定向对话流水（append-only）。只记 ask/reply 这类**点对点**动作，
        # 不记 post —— 乒乓熔断要看的正是"这两个人是不是只跟对方在来回"，
        # 把广播心跳混进来只会把信号冲淡。
        "dialogue": [],             # {seq, ts, agent, peer, kind: ask|reply}
        "dialogue_seq": 0,
    }


# ------------------------------------------------------- 定向交流（问 / 答 / 等）

def open_exchanges(st: dict, to: str | None = None, frm: str | None = None) -> list:
    out = []
    for e in st.get("exchanges", []):
        if e.get("status") != "open":
            continue
        if to and e.get("to") != to:
            continue
        if frm and e.get("from") != frm:
            continue
        out.append(e)
    return out


def find_exchange(st: dict, eid: int) -> dict | None:
    for e in st.get("exchanges", []):
        if e.get("id") == eid:
            return e
    return None


def acked_user_ids(st: dict, agent: str = "") -> set:
    """谁认领过用户喊话。

    **默认是「任何人」** —— `unanswered_user()` 要的正是「还没有任何人认领」（全局）。
    传 `agent` 才是「**你**认领过」。

    2026-09-29 实测事故：`read-user` 用了全局集合去标「你已回执」，于是**别人**
    认领 #11 后，我这边打出的是「[#11] 用户你已回执 ：…」——我看到「我已处理过」
    就把一条**从没被我看过**的用户喊话跳过去了。这类误标不报错、只是让我少做一件事，
    所以它必须由断言锁住，而不是靠读代码时小心。
    """
    return {k["id"] for k in st.get("acks", [])
            if not agent or k.get("agent") == agent}


# ------------------------------------------------ 通信熔断（乒乓 / 心跳预算）

def record_dialogue(st: dict, ts: float, agent: str, peer: str, kind: str) -> None:
    """记一条**点对点**对话流水。这是乒乓熔断唯一的原始数据。

    为什么不能靠 exchanges 反推：`reply` 是就地更新已有交换的，
    交换表按 id 排出来的顺序**不是时间顺序**，用它算"来回轮次"会算错。
    """
    st["dialogue_seq"] = int(st.get("dialogue_seq", 0)) + 1
    st.setdefault("dialogue", []).append({
        "seq": st["dialogue_seq"], "ts": ts, "agent": agent, "peer": peer, "kind": kind,
    })
    d = st["dialogue"]
    if len(d) > MAX_DIALOGUE:
        st["dialogue"] = d[-MAX_DIALOGUE:]


def pair_streak(st: dict, a: str, b: str, t: float) -> int:
    """同一对 agent「连续交替」的轮次。

    从最近的对话往回数，只要每一跳都还在这两个人之间，就继续数；
    一旦出现第三方（换人对话）或时间隔断，立刻停止 —— 那说明话题已经切走了，
    再往下数就是冤枉人。所以这个数**不是**"这两人总共聊了几轮"，
    而是"这两人是不是已经陷在只有彼此的回环里"。
    """
    key = tuple(sorted((a, b)))
    n = 0
    for rec in reversed(st.get("dialogue", [])):
        if t - float(rec.get("ts", 0.0)) > PINGPONG_WINDOW:
            break
        if tuple(sorted((rec.get("agent", ""), rec.get("peer", "")))) != key:
            break
        n += 1
    return n


def chatter_hazards(st: dict, res: dict) -> list:
    """把"聊疯了"的对子捞出来。心跳全是绿的 —— 因为双方都在积极发言。"""
    t = float(res.get("ts", now()))
    out, seen = [], set()
    for rec in st.get("dialogue", []):
        if t - float(rec.get("ts", 0.0)) > PINGPONG_WINDOW:
            continue
        key = tuple(sorted((rec.get("agent", ""), rec.get("peer", ""))))
        if not all(key) or key in seen:
            continue
        if USER_NAME in key:
            continue                     # 人跟 agent 的来回不是乒乓，不进熔断视野
        seen.add(key)
        n = pair_streak(st, key[0], key[1], t)
        if n < PINGPONG_WARN:
            continue
        hard = "已熔断" if n >= PINGPONG_HARD else "已告警"
        out.append({
            "kind": "通信过热", "key": f"chatter:{key[0]}:{key[1]}",
            "agents": list(key),
            "text": (f"<{key[0]}> 与 <{key[1]}> 已连续来回 {n} 轮，期间没有任何第三方"
                     f"或用户消息插入（{hard}）。这更像互相确认而不是推进 —— "
                     f"要么收敛：post --tag 决定 写清结论；要么确认必须深挖：给命令加 --force。"),
        })
    return out


def coordination_hazards(st: dict, res: dict) -> list:
    """心跳全绿、团队却出问题的那类险情，全在这里。
    「等待险情」是等一个等不到的答案；「通信过热」是把预算烧在互相确认上。
    两者共同点：看板上一片健康。"""
    return wait_hazards(st, res) + chatter_hazards(st, res)


def chatter_guard(st: dict, agent: str, peer: str, t: float, force: bool) -> tuple[str, int]:
    """ask/reply 之前先过熔断。返回 (拦截原因, 当前轮次)。

    渐进处置：到 WARN 只提醒（让模型自己意识到），到 HARD 才真的拦。
    被拦时退出码 4 —— 刻意与"参数错误 1/2"区分开，调用方能一眼看出
    这不是它命令写错了，而是**被机制叫停了**。

    用户（USER_NAME）不参与熔断：人跟 agent 的来回不是"两个模型互相确认"，
    熔断人类只会把用户挡在对话外面。
    """
    if agent == USER_NAME or peer == USER_NAME:
        return "", 0
    n = pair_streak(st, agent, peer, t)
    if n >= PINGPONG_HARD and not force:
        return (f"✗ 通信熔断：<{agent}> 与 <{peer}> 已经连续来回 {n} 轮，"
                f"期间没有任何第三方或用户消息插入 —— 这几乎肯定是"
                f"「互相确认」而不是推进，已经替你停下。\n"
                f"  收敛：post --agent {agent} --tag 决定 --text \"结论是什么、接下来谁做什么\"\n"
                f"  确实还要深挖：在刚才那条命令后加 --force（把当前这条命令原样重发并加 --force 即可）"), n
    return "", n


def post_guard(st: dict, agent: str, t: float) -> tuple[str, int, int]:
    """心跳预算。返回 (拦截原因, 窗口内已发条数, 上限)。

    为什么值得拦：一个陷入 `while True: post("还在处理")` 的 agent，从看板上看
    是"最健康的那个"（心跳最勤），实际上一个 token 预算正在被烧穿。看门狗抓不到它，
    因为卡死判定看的是"太安静"，而它的病是"太吵"。
    """
    cap = int(st.get("post_cap", POST_CAP) or 0)
    ag = _get_agent(st, agent, t)
    keep = [x for x in ag.get("posted_ts", []) if t - float(x) <= POST_WINDOW]
    ag["posted_ts"] = keep
    if cap and len(keep) >= cap:
        return (f"✗ 心跳预算熔断：<{agent}> 在最近 {int(POST_WINDOW)}s 内已经写了 {len(keep)} 条心跳"
                f"（上限 {cap}）—— 这不是「勤快」，是循环。\n"
                f"  去看你的循环条件/重试条件，先解决为什么停不下来；"
                f"确实需要更高上限就 init --rate-cap <更大的数>（0 = 关闭）。"), len(keep), cap
    keep.append(t)
    return "", len(keep), cap


def unanswered_user(d: Path, st: dict) -> list:
    """用户喊话里，还没有任何 agent 回执的。喊话光是"被读到"不够——
    用户需要看到有人认领了它，否则他不知道自己的话有没有起作用。"""
    acks = acked_user_ids(st)
    return [dict(m, id=i + 1) for i, m in enumerate(parse_user(d)) if (i + 1) not in acks]


def load_state(d: Path) -> dict:
    f = p_state(d)
    if f.exists():
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
            for k, v in blank_state().items():
                st.setdefault(k, v)
            st.setdefault("agents", {})
            st.setdefault("locks", {})
            st.setdefault("alerts", [])
            return st
        except Exception as e:  # 损坏的 state 不该让看板瘫痪
            print(f"! state.json 解析失败（{e}），按新看板继续", file=sys.stderr)
    return blank_state()


def save_state(d: Path, st: dict) -> None:
    tmp = p_state(d).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p_state(d))


def _sync_board_header_threshold(d: Path, st: dict) -> None:
    """把 board.md 里那句「静默超过 Xs」刷成这块板**实际用的**阈值。

    只改那一行，不动任何日志内容。理由：板头是写给后来的 agent 读的协议说明，
    写着 45s 而看门狗实际按 90s 判，读的人就会照着错的数规划心跳
    （「我已经 50s 没写了，板说 45s 算卡死」——其实根本不会被报）。
    board.md 只在不存在时才整份生成，所以声明变化必须单独同步这一行。

    **只让 `init` 调它是不够的**（2026-09-28 实测踩到）：真看板声明了 `--stale-after 90`，
    板头却一直写着 45s —— 因为阈值是别的方式声明的（env / 直接改 state），
    之后没人再跑过 `init`。所以现在 `post` 每次心跳也调它，让它**自愈**。
    代价用**抽检**挡掉：`st["_hdr_stale"]` 记我们上次同步到的阈值，
    `st["_hdr_checked_at"]` 记上次**真的去核对**的时刻。两者都对上、且还没到
    `HDR_RECHECK` 秒，就直接返回 —— 稳态下**零文件 I/O**
    （board.md 可能几百 KB，每次心跳都读一遍是不能接受的）。
    为什么不干脆永久记忆：板文件可能被备份恢复/手改回去，抽检让它一分钟内自愈。
    """
    want_v = board_stale_after(st)
    try:
        last = float(st.get("_hdr_checked_at") or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    if st.get("_hdr_stale") == want_v and (now() - last) < HDR_RECHECK:
        return
    st["_hdr_checked_at"] = now()
    f = p_board(d)
    if not f.exists():
        return
    txt = f.read_text(encoding="utf-8")
    want = f"静默超过 {want_v:g}s"
    out = [
        re.sub(r"静默超过 [0-9.]+s", want, ln, count=1)
        if ln.startswith("> 看门狗：") and "静默超过" in ln else ln
        for ln in txt.split("\n")
    ]
    new = "\n".join(out)
    if new != txt:
        f.write_text(new, encoding="utf-8")
    st["_hdr_stale"] = want_v


def ensure_files(d: Path, st: dict) -> None:
    if not p_board(d).exists():
        p_board(d).write_text(
            BOARD_HEADER.format(
                task=st.get("task") or "(未命名)",
                hb=st.get("heartbeat_secs", HEARTBEAT),
                # 板头写的是**这块板实际用的**阈值（声明的优先），别写死常量 ——
                # 否则板上写着 45s、实际按 90s 判，后来人照着板头理解就错了。
                stale=f"{board_stale_after(st):g}",
                cmin=int(st.get("collab_min") or COLLAB_MIN),
            ),
            encoding="utf-8",
        )
    if not p_user(d).exists():
        p_user(d).write_text(USER_HEADER, encoding="utf-8")
    if not p_alerts(d).exists():
        p_alerts(d).write_text(ALERTS_HEADER, encoding="utf-8")
    # 引导脚本跟着**每一次**写盘一起自愈：旧版本建的板、别人拷过来的板，
    # 第一次有人 post 就把「怎么启动」补齐 —— 否则那些板永远缺这一步。
    write_bootstrap(d)


def render(agent: str, ts: float, text: str, tag: str | None = None) -> str:
    tagp = f"[{tag}] " if tag else ""
    flat = " ".join(str(text).split())
    return f"<{agent}> {hhmmss(ts)} {tagp}{flat}"


def append_entry(d: Path, st: dict, agent: str, text: str, tag: str | None = None,
                 ts: float | None = None) -> str:
    """往看板追加一行；跨天自动插日期分节。返回写入的正文行。"""
    ts = now() if ts is None else ts
    if st.get("board_date") != today():
        with open(p_board(d), "a", encoding="utf-8") as fh:
            fh.write(f"\n## {today()}\n")
        st["board_date"] = today()
    line = render(agent, ts, text, tag)
    with open(p_board(d), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


# ---------------------------------------------------------------- 看板解析

def _date_epoch(date_str: str):
    """该日期**本地 0 点**的时间戳；解析不出来返回 None。

    存在的唯一理由是性能：看板每行都带 `HH:MM:SS`，而 `datetime.strptime` 是通用解析器，
    实测在 2000 行的看板上要花 34ms —— 占了 `await` 一轮轮询的 97%。
    日期段一行一次算好基准，之后每行只是整数加法。
    （同一日期内不做夏令时处理：本项目面向国内使用，且原来用的也是 naive 本地时间。）
    """
    try:
        y, mo, dd = (int(x) for x in date_str.split("-"))
        return datetime(y, mo, dd).timestamp()
    except (ValueError, TypeError):
        return None


def board_entries(d: Path) -> list:
    """按文件顺序返回看板全部条目（含 watchdog 行）。

    实时视图和增量探针都需要"有序全量"，只有卡死判定需要"每 agent 最新一条"。
    两个需求共用这一份解析，避免出现两套时间语义。
    """
    out = []
    f = p_board(d)
    if not f.exists():
        return out
    cur_date = today()
    base = _date_epoch(cur_date)
    try:
        raw = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        s = line.strip()
        md = DATE_RE.match(s)
        if md:
            cur_date = md.group(1)
            base = _date_epoch(cur_date)
            continue
        m = ENTRY_RE.match(s)
        if not m:
            continue
        agent = m.group("agent")
        h, mi, sec = (int(x) for x in m.group("time").split(":"))
        if base is not None:
            ts = base + h * 3600 + mi * 60 + sec          # 快路径：整数运算
        else:
            try:                                          # 脏写的日期段 → 退回通用解析
                ts = datetime.strptime(f"{cur_date} {h:02d}:{mi:02d}:{sec:02d}",
                                       "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                continue
        if ts > now() + 120:             # 跨天兜底：略微超前的算昨天
            ts -= 86400
        rest = m.group("rest").strip()
        tag = None
        mt = TAG_RE.match(rest)
        if mt:
            tag = mt.group("tag")
            rest = mt.group("body").strip()
        out.append({"agent": agent, "ts": ts, "time": hhmmss(ts), "tag": tag,
                    "text": rest,
                    "kind": ("alert" if tag == "告警"
                             else "note" if tag == "协作"
                             else "agent") if agent == RESERVED_AGENT else "agent"})
    return out


def scan_board(d: Path) -> dict:
    """每个 agent 的最新一条心跳与累计条数。

    这么做的原因：agent 有可能手写看板而绕过脚本，若只看 state.json 会误报卡死、
    也会漏掉"只在看板里出现过"的 agent。取 board 与 state 两者较新的时间戳作为最后心跳。

    返回 {agent: {"ts", "tag", "text", "n", "done_streak", "prev_ts"}}

    `done_streak` 是**尾部连续的完成标签条数**（中间夹一条普通心跳就归零），
    `prev_ts` 是**上一条**的时间戳，两者合起来只服务一件事（2026-09-29 补）：
    判「它是不是在像心跳一样反复宣布收工」，见 evaluate 里那段。
    为什么需要它 ——「任务完成」这个标签同时是两个语义的开关：对心跳轴是
    「别报我卡死」，对办事轴是「我不会再答了」。一个循环进程（守护/监听）把心跳
    打成完成标签，就一次拿到两份豁免，而板上完全看不出来
    （当天 ask-listener-guard 就是这个形态，跑了半小时）。
    ⚠ 但**单靠"连续几条"是判不出来的**：一个每回合收工一次的交互会话也会连着写。
    所以判据是**两维**（条数 + 时间密度），且**只提醒不作废** —— 实测教训写在常量上。
    """
    out: dict = {}
    for e in board_entries(d):
        if e["agent"] == RESERVED_AGENT:     # 看门狗自己写板，不参与心跳判定
            continue
        prev = out.get(e["agent"])
        if prev is None:
            out[e["agent"]] = {"ts": e["ts"], "tag": e["tag"], "text": e["text"],
                               "n": 1, "prev_ts": 0.0,
                               "done_streak": 1 if e["tag"] in DONE_TAGS else 0}
        else:
            prev["n"] += 1
            # 先把"上一条"的两个字段取出来，再覆盖成当前这条 —— 顺序反了会
            # 变成"当前条的 ts 减当前条的 ts" = 恒为 0（那就成了恒真的密度判据）。
            prev_ts_of_prev = float(prev.get("ts") or 0.0)
            if e["ts"] >= prev_ts_of_prev:
                prev.update({"ts": e["ts"], "tag": e["tag"], "text": e["text"],
                             "prev_ts": prev_ts_of_prev})
            prev["done_streak"] = (int(prev.get("done_streak", 0)) + 1
                                   if e["tag"] in DONE_TAGS else 0)
    return out


def parse_user(d: Path) -> list:
    f = p_user(d)
    if not f.exists():
        return []
    msgs = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        m = USER_RE.match(s)
        if m:
            who = (m.group("who") or "").strip() or "用户"
            msgs.append({"time": m.group("time"), "who": who,
                         "body": m.group("body").strip()})
        else:
            msgs.append({"time": "", "who": "用户", "body": s})
    return msgs


# ---------------------------------------------------------------- 状态判定

def unacked(st: dict) -> list:
    return [a for a in st.get("alerts", []) if not a.get("acked_by")]


def prune_alerts(st: dict) -> None:
    """裁告警台账，但**未确认的优先保留** —— 先裁已确认的。

    2026-10-01 实测的病：原来是 `st["alerts"] = al[-MAX_ALERTS:]` —— 无条件砍掉
    窗口外的一切。真板上 `alert_seq` 已发到 3145 而台账只留得下 200 ⇒ 约 2945 条被裁，
    **裁的时候解没解，事后无从查证**。而 `prune_state` 的注释白纸黑字写着
    「只裁"已闭环"的记录 …… 丢了它就等于丢了一个还在等回应的人」——
    `exchanges` 照着这条实现了（显式保全部 open），alerts 没有照。
    同一个函数里两段代码原则相反，错的必然是不带理由的那一段。

    台账总量仍然封顶在 MAX_ALERTS（不会无限长）；区别只在于**先裁谁**。
    极端情况（未确认本身就超过上限）下仍会裁掉最老的未确认 —— 那时至少已确认的
    已经先被裁光了，而"未确认"这条我们确实没有让它无声消失的办法。

    保留原有先后顺序（不重排）：看板是给人读时间线的。
    """
    al = st.get("alerts", [])
    if len(al) <= MAX_ALERTS:
        return
    un_idx = [i for i, a in enumerate(al) if not a.get("acked_by")]
    if len(un_idx) >= MAX_ALERTS:
        st["alerts"] = [al[i] for i in un_idx[-MAX_ALERTS:]]
        return
    room = MAX_ALERTS - len(un_idx)
    acked_idx = [i for i, a in enumerate(al) if a.get("acked_by")][-room:]
    st["alerts"] = [al[i] for i in sorted(set(un_idx) | set(acked_idx))]


def prune_state(st: dict) -> None:
    """state.json 里所有会长大的数组都要有上限，否则长会话下迟早拖慢每次写入。

    注意：只裁"已闭环"的记录。未关闭的提问永远保留 —— 丢了它就等于丢了
    一个还在等回应的人。
    """
    prune_alerts(st)
    ex = st.get("exchanges", [])
    if len(ex) > MAX_EXCHANGES:
        keep = [e for e in ex if e.get("status") == "open"]
        done = [e for e in ex if e.get("status") != "open"]
        st["exchanges"] = (done[-(MAX_EXCHANGES - len(keep)):] + keep
                           if len(keep) < MAX_EXCHANGES else keep[-MAX_EXCHANGES:])
    ac = st.get("acks", [])
    if len(ac) > MAX_ACKS:
        st["acks"] = ac[-MAX_ACKS:]
    dg = st.get("dialogue", [])
    if len(dg) > MAX_DIALOGUE:
        st["dialogue"] = dg[-MAX_DIALOGUE:]


def wait_hazards(st: dict, res: dict) -> list:
    """从「谁此刻在等谁」里揪出两类危险等待 —— 心跳**永远**看不出来的那类故障。

    心跳只能证明"某个 agent 最近动过"，证明不了"它等的那个答案还会不会来"。
    于是有两种情况会让整个团队静悄悄地卡住，而看板上一片健康：

      1. 不可达等待：A 在等 B 回答，但 B 已经写「任务完成」退出了。
         这个答案永远不会来，A 却会一直等到超时。
      2. 互相等待（死锁）：A 在等 B 的回应，B 同时在等 A 的回应。
         两边都卡在 await 里，谁也不会先答 —— 典型的双向死锁。

    判定依据是 state 里 `awaiting` 标记 × 交换记录的 to/from 关系，组成一张等待图。
    注意只信"正在等待且自身还活着"的标记：进程被 kill 会留下过期的 awaiting，
    所以要求该 agent 最近还在刷新心跳（静默 <= 阈值）才采信。

    `awaiting.ids` 是多路等待（`await --id 1,2,3`）时，会给**每个**还没回应的目标
    各建一条边 —— 一次等三个人，就有三条边，任一目标收工都能立刻发现。
    """
    by = res.get("by_name", {})
    ex_by_id = {e["id"]: e for e in st.get("exchanges", [])}
    limit = float(res.get("stale_after", STALE_AFTER))
    edges, out = {}, []

    for name, ag in st["agents"].items():
        aw = ag.get("awaiting") or {}
        ids = aw.get("ids") or ([aw["id"]] if aw.get("id") else [])
        if not ids:
            continue
        row = by.get(name, {})
        if row.get("silence", 0.0) > limit:
            continue                       # 过期标记：它已经不在等了
        for wid in ids:
            ex = ex_by_id.get(wid)
            if not ex or ex["status"] != "open" or ex["from"] != name:
                continue                   # 已被回应 / 不是它在问 → 它没在等这个
            peer = ex["to"]
            edges.setdefault(name, set()).add(peer)
            if by.get(peer, {}).get("state") == "完成":
                out.append({
                    "kind": "不可达等待", "key": f"unreachable:{wid}",
                    "agents": [name, peer],
                    "text": (f"<{name}> 正在等 <{peer}> 回答 #{wid}，"
                             f"但 <{peer}> 已经写了「任务完成」→ 这个答案不会来了。"
                             f"<{name}> 会白等到超时；换人问，或自己拍板。"),
                })

    seen_cycle = set()
    for a in sorted(edges):
        for b in sorted(edges[a]):
            pair = tuple(sorted((a, b)))
            if pair in seen_cycle or a not in edges.get(b, set()):
                continue
            seen_cycle.add(pair)
            # 两边都已各自收手（await 因互相等待退了 5，标记是**故意**留着的）时，
            # 措辞必须改：它们已经不在 await 里了，此刻卡住的是**那两个没人回答的提问**。
            # 不分两种说法的话，看板会一直催"让一方先 reply"，而那一方早就不在场了。
            both_out = all((st["agents"].get(x, {}).get("awaiting") or {}).get("deadlock")
                           for x in pair)
            out.append({
                "kind": "互相等待", "key": f"cycle:{pair[0]}:{pair[1]}",
                "agents": list(pair),
                "text": (f"<{pair[0]}> 与 <{pair[1]}> 互相等对方回应，双方都已各认出这条真死锁"
                         f"并收手退出（await 退 5）——但那些提问仍然没人答。"
                         f"必须有一方先 reply，否则它会以「两个未闭环提问」的形式一直挂着。"
                         if both_out else
                         f"<{pair[0]}> 与 <{pair[1]}> 正互相等对方回应（各卡在自己的 await 里）——"
                         f"谁都不会先答，这是一条真死锁。需要有一方先 brief 看到"
                         f"「待你回应 #N」并 reply，或由在线 agent 介入。"),
            })
    return out


def evaluate(d: Path, st: dict, stale_after: float = STALE_AFTER) -> dict:
    t = now()
    board = scan_board(d)
    # 只在看板里出现过的 agent（手写看板）也要纳入监督，否则"绕过脚本"就绕过了看门狗。
    # 「用户」除外：人类写在看板上的行（如 UI 里回答提问）不是心跳，不该被监督。
    for name in sorted(set(board) - set(st["agents"])):
        if name == USER_NAME:
            continue
        b = board[name]
        ag = _get_agent(st, name, b["ts"])
        ag["entries"] = b["n"]
        ag["last_seen"] = b["ts"]
        ag["note"] = "仅出现在看板（手写）"
    # ★ 存活形态有三种，看门狗原来只认一种（批次2）。
    # 「心跳停了」这条判据有个隐含前提：**agent 的存在 = 它在自报心跳**。
    # 而实盘上至少有三种形态：常驻心跳型 / hold 声明的长任务型 / **spawn 即活型**
    # （被提问才起一次，平时零心跳 —— 如被监听器唤醒的一次性实例）。
    # 第三种会被稳定地误读成「疑似卡死」：2026-09-29 一天误报 33 条、真卡死 0 条，
    # 用户看到的抱怨是「你们老是警告」。所以第三种必须在**板上声明一次**（写在
    # identities.json 里，跟着名字走），而不是每次现场解释一遍。
    # 声明要付代价：必须写清它是被谁唤醒的（`identity set --spawn` 强制要 --purpose），
    # 否则「点一下就把自己静音」会成为藏身处。
    _ids = load_identities(d)
    # 「反复收工」提醒用的时间窗：env > 内置。**在循环外解析一次** ——
    # 放循环里会让"没有任何 agent"时 `win` 未定义，而它还要进 `res` 供展示。
    win = REPEAT_DONE_WINDOW
    _w = os.environ.get(REPEAT_DONE_WINDOW_ENV)
    if _w:
        try:
            _wf = float(_w)
            if _wf > 0:
                win = _wf
        except ValueError:
            pass
    rows, stale, idle, loop_lies, repeat_done = [], [], [], [], []
    for name in sorted(st["agents"]):
        if name == USER_NAME:          # 人类不进 agent 状态表、不计入协作数
            continue
        ag = st["agents"][name]
        last = float(ag.get("last_seen", 0.0))
        tag = None
        if name in board:
            b = board[name]
            tag = b["tag"]
            if b["ts"] > last:
                last = b["ts"]
        entries = int(ag.get("entries", 0))
        # ★ 两个声明必须在**被使用之前**解析完。
        # 这里踩过一次：`loop_decl` 原本写在下面（和 spawn_decl 一起），而它在上面
        # 就被 `if done and loop_decl:` 用到了 —— 靠 `done` 短路侥幸没抛
        # UnboundLocalError，**但它读到的是上一个 agent 留下的值**（for 循环不产生作用域）。
        # 症状是"这一行明明显示 loop=True，判断却按 False 走"，而且只有循环型身份
        # 排在前面时才会暴露。凡是"声明声明"都要在使用点之前解析，别信短路的运气。
        #
        # spawn 即活型：把判活的权力交回它的驱动层（这就是声明的全部含义）。
        # 它**不进 stale**（不产生心跳告警），但照样留在状态表里、照样被事故轴盯着 ——
        # 「少报」由第二根轴补回来，不是靠这一条自己兜。
        spawn_decl = str((_ids.get(name) or {}).get("spawn") or "").strip()
        # 循环型（守护 / 监听 / 看门狗）：它**永远不收工**，所以「任务完成」对它不成立。
        # 理由不是洁癖 —— 完成状态同时是**办事轴的开关**（`_peer_can_answer` 里
        # "完成 ⇒ 不会答了"）。一个循环进程把自己的心跳打上完成标签，就等于把
        # "所有问它的提问"一次性标成"该结案"，而这件事在板上**看不出来**
        # （当天 ask-listener-guard 就是这个形态）。声明优先于标签。
        loop_decl = bool((_ids.get(name) or {}).get("loop"))
        # --- 「完成」状态的唯一推导点 -------------------------------------
        # 完成不是"写过的标签"，是**此刻是否成立的事实**。它会被两件事推翻：
        #   ① 它自己又说了话（最新一条不是完成标签）⇒ 它回来了，完成作废；
        #   ② 它声明过自己是循环型（`identity set --loop`）⇒ 声明优先于标签。
        # 为什么这两条非加不可：完成状态同时是**两个语义的开关** ——
        # 对心跳轴是「别报我卡死」，对办事轴是「我不会再答了」。一个循环进程
        # 把心跳打成完成标签，一次性拿到两份豁免，**而板上看不出来**
        # （当天 ask-listener-guard 就是这个形态，跑了半小时）。
        #
        # ★ 这里**故意没有**「连续写多条完成 ⇒ 当作废」这条。2026-09-29 真板实测：
        # 加过、然后撤了 —— 因为"一个回合收工一次"的交互会话（WorkBuddy / CLI，
        # 工作期间不发中间心跳）尾部本来就会累积好几条完成，它被误判成循环、
        # 当场开始被报「疑似卡死 2h8m」，而那种形态恰恰是本项目最主要的用法。
        # **一条会惩罚主要使用形态的规则，比它堵的那个洞更糟。**
        # 判不出来那部分不靠猜，靠两件事：③ 声明（`--loop`，精确但要人记得）、
        # 以及把"看着像循环"的情形**报出来提醒**（只提醒、不改判，见 repeat_done）。
        prev_ts = float((board.get(name) or {}).get("prev_ts") or 0.0)
        streak = int((board.get(name) or {}).get("done_streak") or 0)
        # 「反复收工」的提醒：条数 + **时间密度**两维都要满足。
        # 循环进程反复宣布收工的节奏像心跳（分钟级）；真收工的人按回合来（十分钟以上）。
        # 只提醒不改判 —— 引擎分不出"勤快的会话"和"守护进程"，那件事该由声明负责。
        dense_done = bool(last and prev_ts and (last - prev_ts) < win)
        if tag in DONE_TAGS and streak >= REPEAT_DONE_N and dense_done and not loop_decl:
            repeat_done.append(name)
        if tag in DONE_TAGS and loop_decl:
            loop_lies.append(name)
        if agent_is_done(tag, loop_decl=loop_decl):
            ag["status"] = "done"
            ag["done_at"] = ag.get("done_at") or last
        elif ag.get("status") == "done":
            ag["status"] = "active"
            ag.pop("done_at", None)
        done = ag.get("status") == "done"
        holding = float(ag.get("expected_silence_until", 0.0)) > t
        started = entries > 0 or name in board
        silence = max(0.0, t - last)
        if ag.get("retired"):
            # 有人**宣告过**「它不会再回来」（retire）。与「离线」的本质区别：
            # 离线是引擎按时长**猜**的（工作可能真丢了，照样告警 —— 每只按 1h 退避
            # 上限永远重复下去）；离场是人**拍板**的：不再判卡死、不再发告警、
            # 它名下的未确认告警一并确认。复位必须便宜：它 post 一条即自动撤销。
            state = "已离场"
        elif not started:
            # 只被注册过、从没写过心跳：这是"没派出去 / 名字打错"，不是"卡死"。
            # 两者处置方式不同，所以单独一档，绝不产生告警噪音。
            state = "待启动"
            idle.append(name)
        elif done:
            state = "完成"
        elif holding:
            state = "挂起中"
        elif spawn_decl:
            state = "spawn 型"
        elif silence > stale_after:
            # 静默到小时量级 ⇒ 多半是进程没了，不是卡在某一步。
            # 仍进 stale（工作可能真丢了，check 照样退 1、告警照样发），
            # 只是**措辞与补救动作**换成「离线」那一套 —— 判据与告警/网页共用 agent_offline()。
            state = "离线" if agent_offline(silence, stale_after) else "疑似卡死"
            stale.append(name)
        else:
            state = "心跳中"
        # 「它此刻在等谁」—— 多路 await 时是一个列表；waiting_on 保留"第一条"，
        # 方便只想看一眼的人，waiting_ids 给需要完整等待关系的人（如等待图/看板）。
        aw_ids = list((ag.get("awaiting") or {}).get("ids") or [])
        if not aw_ids:
            one = (ag.get("awaiting") or {}).get("id")
            aw_ids = [one] if one else []
        if silence > stale_after:
            aw_ids = []                    # 过期标记不算数
        rows.append({
            "name": name,
            "state": state,
            "started": started,
            "silence": round(silence, 1),
            "last_seen": hhmmss(last),
            "entries": entries,
            "posts_per_min": sum(1 for x in ag.get("posted_ts", []) if t - float(x) <= POST_WINDOW),
            "holding_until": hhmmss(ag["expected_silence_until"])
            if holding else None,
            "waiting_on": aw_ids[0] if aw_ids else None,
            "waiting_ids": aw_ids,
            # True = 它不是"正在等"，而是**已因互相等待收手退出**，标记留着是为了让对端
            # 也退出来（以及告诉看门狗这条环是真的）。别把它读成"它还卡在 await 里"。
            "waiting_deadlock": bool((ag.get("awaiting") or {}).get("deadlock")),
            "alert_open": bool(ag.get("alert_open")),
            # 这个名字下有几个**实例**在写（>1 = 多个同名工具共用一个身份，见 note_instance）
            "instances": len(ag.get("instances") or {}),
            # 「同名多线」：这个名字被**另一个不是它子目录的地方**写过（判据见 note_cwd）
            "cwd_conflict": str(ag.get("cwd_conflict") or ""),
            # spawn 即活型：判活交给它的驱动层。非空时是"被谁唤醒"的说明，
            # 空串 = 普通（按心跳判活）。**不要**把它读成"它更可信/更不重要"——
            # 它只是换了判据，事故轴对它一样有效。
            "spawn": spawn_decl,
            # 循环型（守护/监听）：声明后「任务完成」标签对它无效 ——
            # 它不是"更可信/更不重要"，只是**不允许用完成标签关掉办事轴**。
            "loop": loop_decl,
        })
    # ★ 第二根观测轴：办事维度（批次2）。心跳轴问「它还在吗」，这根问「该办的事还在不在」。
    # 2026-09-29 的一组对偶定死了它的必要性：同一个下午、同一块板 ——
    # 心跳轴误报 33 条 / 真卡死 0；而唯一一次真事故（提问被静默丢弃 547s）**告警 0 条**。
    # 调阈值救不了这个，因为错的是观测口径，不是数字。
    state_by_name = {r["name"]: r["state"] for r in rows}

    def _peer_can_answer(who: str) -> bool:
        """「对方还答得出来吗」—— 只认一个依据：它当前的 state 是不是「完成」。

        挂起中 = **可答**（hold 只挡看门狗，不挡读消息，这条今天踩过）；
        spawn 型 = **可答**（它就是被问才醒的）；
        完成 / 名字根本不在板上 = 不会答了。判据全部来自引擎自己的状态，
        不解析看板**文本**、不猜 —— 今天已经有两处翻车在「猜一个字段/自己数一遍」上。

        ⚠ 这根轴能不能成立，**完全押在「完成」这个 state 靠不靠得住上**。
        所以「完成」的推导被收紧过了（见 evaluate 里「完成状态的唯一推导点」）：
        循环进程写完成标签、连续写两次完成、完成之后又说话 —— 三种都已作废。
        这条注释是给下一个改 `_peer_can_answer` 的人看的：**别在这里加判据**，
        该改的是 state 的推导点，否则两个轴会用两套不同的"完成"定义。
        """
        stt = state_by_name.get(who)
        # 已离场 = 有人宣告过它不会再回来 —— 答案不会来了，与「完成」同待遇
        return stt is not None and stt not in ("完成", "已离场")

    soa = board_stale_open_after(st)
    stale_opens, unreachable_opens = [], []
    for q in open_exchanges(st):
        waited = max(0.0, t - float(q.get("ts") or t))
        if waited < soa:
            continue                       # 第一层：开够久了没有
        rec = {"id": q.get("id"), "from": str(q.get("from") or ""),
               "to": str(q.get("to") or ""), "waited": int(waited),
               "question": " ".join(str(q.get("question") or "").split())[:120]}
        if _peer_can_answer(rec["to"]):
            stale_opens.append(rec)        # 第二层：对方还答得出来 ⇒ 这是**事故**
        else:
            unreachable_opens.append(rec)  # 对方已收工 ⇒ 不是事故，是「提问者该自己结案」
    # 多人协作感知：心跳中 / 挂起中 = 「正在干活」（已开工、未收工、静默未超阈值）。
    # 这是整个工具的启动条件 —— 一个 agent 独自干活用不上看板，≥COLLAB_MIN 才是它的主场。
    # 门槛优先取 state 里的（`init --collab-min N` 可调）：agent 数量本来就不固定，
    # 写死一个数会把"3 个人在干活"呈现得像勉强凑数。
    cmin = int(st.get("collab_min") or COLLAB_MIN)
    working = [r["name"] for r in rows if r["state"] in ("心跳中", "挂起中")]
    # 喊话只解析一次：下面既要数条数、又要挑出格式写坏的那些。
    _umsgs = parse_user(d)
    res = {
        "ts": t,
        "stale_after": stale_after,
        "agents": rows,
        "stale": stale,
        "idle": idle,
        # 办事轴：**事故**（对方还答得出来）与**不可达**（对方已收工、该结案）分开。
        # 两类的处置完全相反，混在一起报就会变成噪音 —— 这是 22:35 被纠正后加的。
        "stale_opens": stale_opens,
        "unreachable_opens": unreachable_opens,
        "stale_open_after": soa,
        # 声明了循环型、却写了「任务完成」的身份：标签已被忽略（声明赢），
        # 但要报出来 —— 否则他下次还会这么写，只是这次恰好没造成后果。
        "loop_lies": loop_lies,
        # 反复写「任务完成」的身份（**只提醒、不改判完成状态**，见 evaluate 那段）：
        # **条数（≥REPEAT_DONE_N）+ 时间密度（相邻两条 <「办事轴」窗口）两维都满足**
        # 才列出 —— 只有条数会把"一个回合收工一次"的交互会话全打成循环。
        # 附带阈值 `repeat_done_window`，让读者不必翻代码就能复核判定。
        "repeat_done": repeat_done,
        "repeat_done_window": win,
        "by_name": {r["name"]: r for r in rows},
        "user_total": len(_umsgs),
        # 「看着像带了时间戳、却没解析出来」的喊话。这类行会被**当纯文本收下**
        # （不该拒收用户的喊话），代价是时间丢失、页面上的位置只能靠推断 ——
        # 属于**静默降级**。所以在这里报出来：让写坏格式的人有机会发现，
        # 而不是让它悄悄变成一条"没有时间的消息"。
        "user_malformed": [m["body"][:60] for m in _umsgs
                           if not m["time"] and TIMEISH_RE.match(m["body"].lstrip())],
        "alerts_unacked": unacked(st),
        "collab": {"count": len(working), "agents": working,
                   "active": len(working) >= cmin, "required": cmin},
    }
    # 危险等待要在 agent 状态算完之后才能判（要用 by_name 里的 state/silence）；
    # 通信过热也要用 res["ts"] 做时间基准。
    res["hazards"] = coordination_hazards(st, res)
    return res


def fmt_dur(secs: float) -> str:
    """把秒数说成人话：90 → '1m30s'，153000 → '42h30m'。

    告警正文里写 `已静默 152778s` 没人能一眼读出是多久 —— 而"静默了多久"
    恰恰是区分「卡死」和「离线」的唯一依据，所以它必须可读。
    """
    s = int(max(0, secs))
    h, m = divmod(s // 60, 60)
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s % 60}s"
    return f"{s}s"


def alert_gap(repeat: int, cooldown: float, floor: float = 0.0) -> float:
    """第 `repeat` 次**连续重复**告警要间隔多久。三根轴共用这一处判据。

    为什么必须收成一处（2026-10-01 实测）：退避最早只长在心跳轴上，注释里那条理由也
    早就写明了 ——「只看 alert_open 就永远按 COOLDOWN 重复，噪音会反过来把真信号埋掉」
    （当时某板 42 小时攒出 994 行 watchdog vs 110 行实质发言）。
    但后加的**险情轴**（不可达等待 / 互相等待）与**办事轴**（提问没人办）各自用了
    恒定的 `max(cooldown, 60)`：于是一个**永久开着**的提问每 135s 报一次、永远报下去。
    真板上实测：7 个僵尸提问在最近 200 条台账里占了 170 条（85%），`alert_seq` 已发到
    3145 而台账只留得下 200 ⇒ 约 2945 条被挤出窗口，窗口外的信号就永久丢了。
    ⇒ 「记住结论」没能变成「长在同一条轴上」，所以把它收成唯一一处。

    `floor` 给险情轴 / 办事轴用（它们原有 60s 地板，见 HAZARD_MIN_GAP）；
    心跳轴不传，保持它原来的 `max(退避, cooldown)`。
    """
    gap = (cooldown if repeat < ALERT_BACKOFF_AFTER else
           min(ALERT_BACKOFF_BASE * ALERT_BACKOFF_FACTOR ** (repeat - ALERT_BACKOFF_AFTER + 1),
               ALERT_BACKOFF_CAP))
    return max(gap, cooldown, floor)


def _seen_last(v) -> float:
    """读 `hazard_seen` / `open_seen` 里记的那个时刻（拿不准就当没记过）。

    这两个表的值**故意保持裸时间戳**（float），不换成 `{"last":…, "n":…}`：
    同一块板上可能**同时跑着新旧两个版本**的进程 —— `serve` 的 `watchdog_loop` 是长驻的，
    升级引擎后它不会自己重启（真板实测连续跑过 1 天以上），而旧版的读法是
    `float(hseen[key])`。值一旦变成 dict，**旧进程下一轮就抛 TypeError**，
    看门狗从此静默 —— 而"板子不再报警"这件事没有任何人会立刻发现。
    所以"报过几次"另存一张表（`hazard_repeat` / `open_repeat`），两张表同生共死。
    """
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def emit(d: Path, st: dict, res: dict, cooldown: float = COOLDOWN) -> list:
    """写告警 / 恢复通知 / 协作状态切换事件。返回本次新写入的告警行。"""
    written = []
    t = now()
    # 多人协作模式切换要进看板：这是整个工具的"启动条件"——
    # 第二个 agent 开始干活的那一刻，所有人（和用户）都该看见协作已成立。
    # 用状态标记保证"开启"只报一次；全员停了才允许下次重新报。
    col = res.get("collab") or {}
    if col.get("active") and not st.get("collab_started"):
        names = "、".join(col["agents"])
        line = append_entry(d, st, "watchdog",
                            f"现在有 {col['count']} 个 agent 在干活（{names}）"
                            f"—— 多人协作模式开启", tag="协作")
        st["collab_started"] = True
        written.append(line)
    elif not col.get("count") and st.get("collab_started"):
        st["collab_started"] = False
        st["ui_opened"] = False          # 协作结束，下次协作允许重新自动弹 UI
        line = append_entry(d, st, "watchdog",
                            "所有 agent 都已停止干活 —— 多人协作模式结束", tag="协作")
        written.append(line)
    for name in res["stale"]:
        ag = st["agents"][name]
        # 退避按「连续重复了几次」算，而不是只看 alert_open：后者只能说明"报过"，
        # 说明不了"报过几次"，算不出递增的间隔。间隔判据见 alert_gap()（三根轴共用）。
        rep = int(ag.get("alert_repeat", 0))
        gap = alert_gap(rep, cooldown)
        if ag.get("alert_open") and t - float(ag.get("last_alert_at", 0.0)) < gap:
            continue
        escalated = bool(ag.get("alert_open"))
        ag["alert_open"] = True
        ag["last_alert_at"] = t
        ag["alert_repeat"] = rep + 1
        st["alert_seq"] = int(st.get("alert_seq", 0)) + 1
        aid = st["alert_seq"]
        secs = int(res["by_name"][name]["silence"])
        if agent_offline(secs, res["stale_after"]):
            # 静默到了小时量级，它多半已经不是"卡在某一步"，而是根本不在了。
            # 必须换措辞：继续说「疑似卡死」会让人去救一个不存在的进程。
            # ★ 判据用 agent_offline()（与状态表 / 网页徽章共用），**不是** rep>=3 ——
            #   规格写的是按"时长"分档；按"报了几次"翻档会让措辞几分钟内就升格，
            #   把一个可能真卡死的 agent 过早说成"人已经不在了"。退避间隔仍按 rep 算。
            txt = (f"<{name}> 已静默 {fmt_dur(secs)}（{secs}s）—— 判定为**离线**，不是卡死；"
                   f"退避提醒（下次约 {int(gap)}s 后）"
                   f"→ 要它回来就补一条 post；或按接管纪律替它收尾并 ack")
        elif escalated:
            txt = (f"<{name}> 仍无心跳（已静默 {fmt_dur(secs)}，重复告警 #{aid}）"
                   f"→ 在线 agent 请直接接管：先看它的最后一条日志，再抢它的锁、重派它的任务；"
                   f"若它只是在跑长任务，让它补 `hold --agent {name}` 并 post 一条心跳（告警会自动撤回）")
        else:
            txt = (f"<{name}> 已静默 {fmt_dur(secs)}（阈值 {int(res['stale_after'])}s）且未写「任务完成」"
                   f"→ 疑似卡死，请在线 agent 检查该 agent 是否卡死并修复；"
                   f"**先分清两种可能**：①真卡死 → 接管；"
                   f"②在跑长任务没声明 → 补 `hold --agent {name}`（默认 10 分钟），"
                   f"post 一条心跳即可撤回本条告警")
        line = append_entry(d, st, "watchdog", txt, tag="告警")
        with open(p_alerts(d), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        st["alerts"].append({"id": aid, "agent": name, "ts": t, "silence": secs,
                             "text": txt, "acked_by": None})
        written.append(line)
    # 危险等待（不可达 / 死锁）也要告警：这类故障心跳全绿，只有看等待图才看得见。
    # 按 key 去重 + 退避，并且险情一旦消失就把 key 连同「报过几次」一起清掉 ——
    # 复发要从头开始算，而不是接着上一轮的间隔（否则一次偶发会长期压低灵敏度）。
    # ★ 两张表同生共死：值表保持裸时间戳（旧进程读得动，见 _seen_last），次数表是新加的。
    haz = res.get("hazards") or []
    hseen = st.setdefault("hazard_seen", {})
    hrep = st.setdefault("hazard_repeat", {})
    live = {h["key"] for h in haz}
    for k in list(hseen):
        if k not in live:
            hseen.pop(k, None)
    for k in list(hrep):
        if k not in live:
            hrep.pop(k, None)
    for h in haz:
        last = _seen_last(hseen.get(h["key"]))
        rep = int(hrep.get(h["key"]) or 0)
        if last and t - last < alert_gap(rep, cooldown, HAZARD_MIN_GAP):
            continue
        hseen[h["key"]] = t
        hrep[h["key"]] = rep + 1
        st["alert_seq"] = int(st.get("alert_seq", 0)) + 1
        aid = st["alert_seq"]
        txt = f"[{h['kind']}] {h['text']}"
        if rep + 1 > ALERT_BACKOFF_AFTER:
            # 与办事轴同一理由：同一句话重复到第 N 次时，看板上必须**看得出这是重复**，
            # 否则读者以为又出了一件事（那正是它刷屏 170/200 的原因之一）。
            txt += (f"\n  （这是第 {rep + 1} 次重复提醒，已退避；"
                    f"下次约 {int(alert_gap(rep + 1, cooldown, HAZARD_MIN_GAP))}s 后）")
        line = append_entry(d, st, "watchdog", txt, tag="告警")
        with open(p_alerts(d), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        st["alerts"].append({"id": aid, "agent": h["agents"][0], "ts": t, "silence": 0,
                             "text": txt, "acked_by": None, "hazard": h["key"]})
        written.append(line)
    # ★ 办事轴的告警（批次2）：**「有一件事开着没人办」**。
    # 与心跳轴并列，但只报「对方还答得出来」那一类 —— 对方已收工的那些不是事故，
    # 是「提问者该自己结案」（await 对它们本来就立刻退 3），报出来只是噪音。
    # 两类分开处置，正是它们该被拆成两类的理由。去重按提问编号，闭环即清。
    # ★ 与险情轴同样：值表保持裸时间戳（旧进程读得动），次数另存一张表、同生共死。
    oseen = st.setdefault("open_seen", {})
    orep = st.setdefault("open_repeat", {})
    live_open = {f"open:#{r['id']}" for r in res.get("stale_opens") or []}
    for k in list(oseen):
        if k not in live_open:
            oseen.pop(k, None)
    for k in list(orep):
        if k not in live_open:
            orep.pop(k, None)
    for r in res.get("stale_opens") or []:
        key = f"open:#{r['id']}"
        last = _seen_last(oseen.get(key))
        rep = int(orep.get(key) or 0)
        if last and t - last < alert_gap(rep, cooldown, HAZARD_MIN_GAP):
            continue
        oseen[key] = t
        orep[key] = rep + 1
        st["alert_seq"] = int(st.get("alert_seq", 0)) + 1
        aid = st["alert_seq"]
        peer_state = str((res["by_name"].get(r["to"]) or {}).get("state") or "?")
        txt = (f"[办事轴] 提问 #{r['id']} 开了 {fmt_dur(r['waited'])} 没人办："
               f"<{r['from']}> 问 <{r['to']}>，而对方**还答得出来**（当前：{peer_state}）。"
               f"\n  心跳轴看不到这件事（全员的 last_seen 都是新的），所以它单独报。"
               f"\n  这多半是「消息丢了」或「没人认领」——不是「大家都闲着」。"
               f"\n  处置：答它 `reply --agent {r['to']} --id {r['id']} --text …`；"
               f"或催它 `ask --agent {r['from']} --to {r['to']} --text …`；"
               f"确实不办了就由提问者说明一句，别让它一直挂着（阈值 "
               f"{int(res.get('stale_open_after') or 0)}s，见 `init --stale-open-after`）")
        if rep + 1 > ALERT_BACKOFF_AFTER:
            # 同一句话重复到第 N 次时，看板上必须**看得出这是重复** ——
            # 否则读者以为又出了一件事（这正是它刷屏 170/200 的原因之一）。
            txt += (f"\n  （这是第 {rep + 1} 次重复提醒，已退避；"
                    f"下次约 {int(alert_gap(rep + 1, cooldown, HAZARD_MIN_GAP))}s 后）")
        line = append_entry(d, st, "watchdog", txt, tag="告警")
        with open(p_alerts(d), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        st["alerts"].append({"id": aid, "agent": r["to"], "ts": t, "silence": 0,
                             "text": txt, "acked_by": None, "stale_open": key})
        written.append(line)
    for name in sorted(st["agents"]):
        ag = st["agents"][name]
        if ag.get("pending_recovery"):
            ag["pending_recovery"] = False
            auto = 0
            for al in st.get("alerts", []):
                if al["agent"] == name and not al.get("acked_by"):
                    al["acked_by"] = "watchdog:auto-recovery"
                    al["acked_at"] = t
                    auto += 1
            line = append_entry(
                d, st, "watchdog",
                f"<{name}> 恢复心跳，撤回卡死告警" + (f"（自动关闭 {auto} 条待确认告警）" if auto else ""),
                tag="恢复")
            written.append(line)
    prune_state(st)
    return written


def format_check(d: Path, st: dict, res: dict) -> str:
    out = []
    out.append(f"work-log · {hhmmss(res['ts'])} · {p_board(d)}")
    out.append(f"任务：{st.get('task') or '(未命名)'}   心跳周期 {st.get('heartbeat_secs', HEARTBEAT)}s   "
               f"卡死阈值 {int(res['stale_after'])}s   "
               f"办事轴 {int(res.get('stale_open_after') or STALE_OPEN_AFTER)}s")
    # 先报"看门狗自己还活着没有" —— 否则"全员健康"和"根本没人看"长得一模一样
    last_scan = float(st.get("last_scan_ts", 0.0))
    if last_scan:
        idle = res["ts"] - last_scan
        limit = max(HEARTBEAT * 4, res["stale_after"] * 2)
        out.append(f"上次扫描：{hhmmss(last_scan)}（{int(idle)}s 前）"
                   + ("  ⚠ 看门狗自身已静默，下面状态不可信！" if idle > limit else ""))
    else:
        out.append("上次扫描：从未 —— 没有任何看门狗跑过，下面只是当前快照，没有人会告警")
    out.append("-" * 72)
    # 心跳预警线要跟着这块看板自己的上限走（init --rate-cap 可能设得很小），
    # 否则小上限下已经撞线了、看板还一声不吭。
    _cap = int(st.get("post_cap", POST_CAP) or 0)
    warn_at = max(2, int(_cap * 0.7)) if _cap else 0
    for r in res["agents"]:
        if r["state"] == "已离场":
            # 宣告离场 = 人拍板「不会再回来」⇒ 人看的表里**直接不出现**（用户 2026-09-30 拍板：
            # 都说了不会再启动，就别再占板面）。数据层不删：--json 仍含它（retired 语义可查），
            # `retire --undo` 与驱动层还要用；它 post 一条即自动复位、重新出现在表里。
            continue
        flag = ""
        if r["state"] == "离线":
            flag = "  <= 已离线（多半是进程没了，不是卡死）"
        elif r["state"] == "疑似卡死":
            flag = "  <= 疑似卡死"
        elif r["state"] == "待启动":
            flag = "  <= 从未写过心跳"
        elif r["alert_open"]:
            flag = "  <= 告警未确认"
        elif warn_at and r.get("posts_per_min", 0) >= warn_at:
            flag = f"  <= 心跳偏密 {r['posts_per_min']}/min"
        hold = f"  挂起至 {r['holding_until']}" if r["holding_until"] else ""
        multi = (f"  ⚠同名 {r['instances']} 实例（共用身份，见 doctor ⑦）"
                 if int(r.get("instances") or 0) > 1 else "")
        if r.get("cwd_conflict"):
            multi += "  ⚠ 同名多线（另一条从别处来，见 doctor ⑦）"
        if r.get("spawn"):
            # 说清它为什么安静：否则下一个人还是会把它读成"卡死"
            multi += "  ⓘ spawn 型（判活交给它的驱动层，不按心跳判）"
        if r.get("loop"):
            # 说清它为什么不会被「任务完成」停掉：否则下一个人会以为"标签写错了"
            multi += "  ⓘ 循环型（守护/监听，永不收工；「任务完成」对它不生效）"
        out.append(f"{r['name']:<12} {r['state']:<6} 最后 {r['last_seen']}  静默 {r['silence']:>6.1f}s  "
                   f"条目 {r['entries']:>3}{hold}{multi}{flag}")
    if not res["agents"]:
        out.append("（还没有任何 agent 注册——先 post 一条心跳）")
    out.append("-" * 72)
    # 多人协作标记：用户一眼判断"这块看板该不该开"。
    # ⚠ 只报"现在有几个在干活"，**不写分母** —— 实际人数不固定（3~6 个很常见、随时增减），
    # 写成 "N/2" 会被读成"上限两人""还有空位"，把不定量说成定量（用户明确要求改掉）。
    col = res.get("collab") or {}
    if col:
        names = "、".join(col["agents"]) or "无"
        req = col.get("required", COLLAB_MIN)
        if col.get("active"):
            out.append(f"🤝 协作中：{col['count']} 个 agent 在干活（{names}）")
        else:
            out.append(f"协作：{col['count']} 个 agent 在干活（{names}）"
                       f"—— 未到「协作模式」标记线（本板设 ≥{req}）；"
                       f"人数不设上限也不固定，`init --collab-min` 可调")
    # ★ 办事轴（批次2）：心跳全绿也可能有事。2026-09-29 的一组对偶是它的存在理由 ——
    # 同一个下午：心跳轴误报 33 条 / 真卡死 0；而唯一一次真事故（提问被静默丢弃 547s）
    # 告警 0 条。**两类分开列**，因为处置完全相反：一个该催/该答，一个该结案。
    so = res.get("stale_opens") or []
    uo = res.get("unreachable_opens") or []
    if so or uo:
        out.append(f"⚠ 办事轴（阈值 {int(res.get('stale_open_after') or 0)}s）："
                   f"心跳全绿也发现不了的那类")
        for r in so:
            out.append(f"  [事故] #{r['id']} 已开 {fmt_dur(r['waited'])}："
                       f"<{r['from']}> 问 <{r['to']}>，对方**还答得出来**"
                       f" —— 催它，或自己答掉")
        for r in uo:
            out.append(f"  [该结案] #{r['id']} 已开 {fmt_dur(r['waited'])}："
                       f"<{r['to']}> 已收工，答案不会来了"
                       f"（提问者 `await` 会立刻退 3）；由提问者说一句收掉它")
        out.append("-" * 72)
    ll = res.get("repeat_done") or []
    if ll:
        # ★ 只提醒，不改判 —— 见 evaluate 里「完成状态的唯一推导点」那段
        # （实测：把"连续多条完成"直接当循环去作废，误伤了正常的交互会话）。
        out.append(f"⚠ 反复写「任务完成」的身份：{'、'.join(ll)}")
        out.append(f"  它尾部连续写了 {REPEAT_DONE_N} 条以上完成标签、且间隔在 "
                   f"{int(res.get('repeat_done_window') or 0)}s 内 —— **像心跳一样在宣布收工**。")
        out.append("  完成状态**未被改动**（引擎分不出'勤快的会话'和'守护进程'，所以不猜）。"
                   "若它确实是守护/监听类，请声明一次："
                   "`identity set --agent <名字> --loop` —— 那之后「任务完成」对它不生效。")
        out.append("  若它是「一个回合收工一次」的会话（工作期间不发中间心跳），忽略本条即可。")
        out.append("-" * 72)
    ll = res.get("loop_lies") or []
    if ll:
        # 声明与标签打架：声明赢（完成已忽略），但必须说出来。
        # 静默忽略等于把"他下次还会这么写"这件事藏起来 —— 只是这次恰好没造成后果。
        out.append(f"⚠ 循环型身份写了「任务完成」：{'、'.join(ll)}")
        out.append("  它声明过自己是守护/监听（永不收工），所以完成标签**已忽略**、"
                   "它照样按心跳判活。")
        out.append("  请改掉：循环进程的自报心跳不要带「任务完成」标签"
                   "（它等于用一条标签换取看门狗和办事轴的双重豁免）。")
        out.append("-" * 72)
    # 危险等待要放在最显眼的位置：这类故障每个 agent 的心跳都是绿的，
    # 不看等待图就完全发现不了（实测：对端已收工，提问者白等满超时，全程零告警）。
    haz = res.get("hazards") or []
    if haz:
        out.append(f"⚠ 协作险情 {len(haz)} 条（心跳全绿也发现不了的那类）：")
        for h in haz:
            out.append(f"  [{h['kind']}] {h['text']}")
        out.append("-" * 72)
    pending = unacked(st)
    out.append(f"用户喊话 {res['user_total']} 条 · 未确认告警 {len(pending)} 条")
    # 格式写坏的喊话要看得见 —— 它被当纯文本收下了（时间丢失、页面上位置靠推断），
    # 这属于**静默降级**：不报出来，写坏的人永远不知道自己写坏了。
    _um = res.get("user_malformed") or []
    if _um:
        out.append(f"⚠ {len(_um)} 条喊话的时间前缀解析不出来（已按纯文本收下、时间按位置推断）：")
        for _s in _um[:3]:
            out.append(f"    {_s}")
        out.append("  正确格式：[HH:MM:SS] 用户：内容 —— 时分秒各两位；多一段或少一段都解析不了")
    for a in pending[-8:]:
        out.append(f"  #{a['id']} [{hhmmss(a['ts'])}] <{a['agent']}> 静默 {a['silence']}s 未确认")
    if res.get("idle"):
        out.append(f"待启动 {len(res['idle'])} 个：{'、'.join(res['idle'])}"
                   "（不是卡死；检查是否漏派任务或 agent 名写错）")
    _off = [r["name"] for r in res["agents"] if r["state"] == "离线"]
    if _off:
        out.append(f"离线 {len(_off)} 个：{'、'.join(_off)}"
                   "（静默已到小时量级，多半是进程没了 —— 不是卡死；要它回来等它 post，或替它收尾并 ack）")
    # 问向「用户」的提问也要显示 —— 用户跑 status 就能看到 agent 在等自己回答。
    all_open = open_exchanges(st)
    pend_in = [q for q in all_open if q["to"] == USER_NAME
               or q["to"] in {r["name"] for r in res["agents"]}]
    pend_user = unanswered_user(d, st)
    if pend_in:
        out.append(f"未闭环的提问 {len(pend_in)} 条：")
        for q in pend_in[-6:]:
            if q["to"] == USER_NAME:
                out.append(f"  #{q['id']} <{q['from']}> 问 <{USER_NAME}>（{int(res['ts'] - q['ts'])}s，"
                           f"等你回答 → reply --agent {USER_NAME} --id {q['id']} --text \"…\"）"
                           f"：{q['question'][:38]}")
            else:
                tgt = res["by_name"].get(q["to"], {})
                out.append(f"  #{q['id']} <{q['from']}> 问 <{q['to']}>（{int(res['ts'] - q['ts'])}s，"
                           f"对方「{tgt.get('state', '?')}」）：{q['question'][:38]}")
    if pend_user:
        out.append(f"用户喊话还没人认领 {len(pend_user)} 条：" +
                   "、".join(f"#{u['id']}" for u in pend_user) +
                   "  → agent 用 ack-user 认领")
    locks = st.get("locks", {})
    if locks:
        out.append("占用中的锁：")
        for rsrc, info in sorted(locks.items()):
            out.append(f"  {rsrc} <- {info.get('agent')} @ {hhmmss(info.get('ts'))}"
                       f"{' （' + info['note'] + '）' if info.get('note') else ''}")
    return "\n".join(out)


# ------------------------------------------------- 身份登记 / 引擎自举（可移植）

def _my_cwd() -> str:
    return str(Path.cwd().resolve())


def load_identities(d: Path) -> dict:
    f = p_ident(d)
    if not f.exists():
        return {}
    try:
        v = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:      # 坏文件不该让所有命令都瘫掉
        print(f"! {f} 解析失败（{e}），按「没有登记」继续", file=sys.stderr)
        return {}
    return v if isinstance(v, dict) else {}


def save_identities(d: Path, ids: dict) -> None:
    tmp = p_ident(d).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p_ident(d))


def _ident_holders(ids: dict, cwd: str) -> list:
    return sorted(n for n, v in ids.items()
                  if str((v or {}).get("cwd") or "") == cwd)


def resolve_agent(a, d: Path, allow_user: bool = False, strict: bool = True) -> str:
    """把「我在板上叫什么」定下来 —— 固定身份不该靠每次手打 `--agent`。

    三档，从**最明确**到**最自动**，任何一档成立就停：
      1. 命令行 `--agent`          —— 一次性覆盖，永远最优先
      2. `$WORK_LOG_AGENT`        —— 给这个**会话**钉死（驱动层/launcher 设）
      3. 板上 `identities.json`    —— cwd == 我当前目录 的那一条（换会话也还在）

    三档都不成立就 **退 2** 并把可抄的一行打出来 —— 绝不猜。
    猜错的代价不是"名字不好看"：`_get_agent` 按名字取的是**同一个 dict**，
    两个会话撞进同一个名字会共享 last_seen / 游标 / entries，
    而且在板上完全看不出来（实测）。这种"看起来在协作、其实是一个人"比报错难查得多。

    刻意**不**用"板上只有一个名字就用它"这条规则：那会在第二个 agent 还没登记的
    窗口期把它静默塞进第一个人的身份里 —— 正好是最该拦住的时候。
    """
    explicit = (getattr(a, "agent", "") or "").strip()
    if explicit:
        return check_agent_name(explicit, allow_user=allow_user)
    env = (os.environ.get(AGENT_ENV) or "").strip()
    if env:
        a.agent_why = f"来自 ${AGENT_ENV}"
        return check_agent_name(env, allow_user=allow_user)
    ids = load_identities(d)
    cwd = _my_cwd()
    mine = _ident_holders(ids, cwd)
    if len(mine) == 1:
        a.agent_why = f"板上登记：{mine[0]} 的 cwd 就是这里（{cwd}）"
        return check_agent_name(mine[0], allow_user=allow_user)
    if len(mine) > 1:
        if not strict:
            return ""
        die(f"✗ 这个目录在板上登记了不止一个名字：{', '.join(mine)}\n"
            f"  它们 cwd 相同，工具不替你选（选错 = 两个会话共用一个身份）。\n"
            f"  显式指定：--agent <名字>；或给这个会话钉死：export {AGENT_ENV}=<名字>")
    if not strict:
        return ""
    known = "、".join(sorted(ids)) or "（一个都没有）"
    die(f"✗ 没告诉我在板上叫什么，也没法唯一确定。\n"
        f"  板上已登记：{known}\n"
        f"  三条路任选一条：\n"
        f"    ① 这次显式传：--agent <名字>\n"
        f"    ② 给这个会话钉死（推荐，之后所有命令都不用再传）：\n"
        f"         export {AGENT_ENV}=<名字>\n"
        f"    ③ 把「名字 + 当前目录」登记到板上（以后在这个目录自动认出）：\n"
        f"         {self_cmd()} --dir {d} identity set --agent <名字>")


def _try_resolve(a, d: Path):
    """`resolve_agent` 的「不炸」版本：解析不出来返回 `("", "")`，交给调用方自己判。

    给 `doctor` / `whoami` 这类**诊断**命令用 —— 诊断命令的职责是"把现状说出来"，
    不是"因为现状不合规就当场退出"。它们要能在一无所有的板上一路走完。
    """
    try:
        return resolve_agent(a, d, strict=False), getattr(a, "agent_why", "")
    except SystemExit:
        return "", ""


SHIM_TMPL = """#!/bin/sh
# work-log 引导脚本 —— 由 `work_log.py init` 生成，可以手改。
#
# 它做三件事：
#   1. 把 --dir 钉死在这块板上（调用方不用再拼路径，也就不会指错板）
#   2. **自己找引擎** —— 所以接入协议里不需要出现任何本机绝对路径，换台电脑也还能用
#   3. 告诉引擎「提示里该把哪个写法打给人抄」（$WORK_LOG_SELF）—— 否则那些提示
#      会写成引擎在本机的位置，换台电脑就成死链
# 引擎位置的查找顺序（先命中先用）：$WORK_LOG_BIN → .worklogrc 的 WORK_LOG_ENGINE
#   → 本目录/work_log.py → 本目录/scripts/ → 用户级 skill 安装位 → $PATH
B=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$B/%(rc)s" ]; then . "$B/%(rc)s"; fi
WORK_LOG_SELF="$B/%(shim)s"
export WORK_LOG_SELF
for E in "$WORK_LOG_BIN" "$WORK_LOG_ENGINE" \\
         "$B/work_log.py" "$B/scripts/work_log.py" \\
         "$HOME/.workbuddy/skills/work-log/scripts/work_log.py"; do
    if [ -n "$E" ] && [ -f "$E" ]; then exec python3 "$E" --dir "$B" "$@"; fi
done
E=$(command -v work_log.py 2>/dev/null)
if [ -n "$E" ]; then exec python3 "$E" --dir "$B" "$@"; fi
echo "✗ 找不到 work-log 引擎（work_log.py）。任选一条修好它：" >&2
echo "    ① export %(bin)s=/path/to/work_log.py" >&2
echo "    ② 把引擎路径写进 $B/%(rc)s： export WORK_LOG_ENGINE=/path/to/work_log.py" >&2
echo "    ③ 把 work_log.py 拷到 $B/ 旁边" >&2
echo "  看板目录：$B" >&2
exit 70
"""

RC_TMPL = """# work-log 本机私有配置（由 `work_log.py init` 生成）
#
# ⚠ 这个文件里的路径是**本机**的，跟着看板目录走但不该进版本库 ——
#   换台电脑时要么改掉下面这行，要么直接删了它（引导脚本会自己去别处找引擎）。
# ${WORK_LOG_ENGINE:-…} 的写法是刻意的：**已经在环境里显式给的路径优先**，
#   否则会出现"我明明 export 了新的引擎位置，它却还在跑 rc 里那个旧的"——最难查的一类。
export WORK_LOG_ENGINE="${WORK_LOG_ENGINE:-%(engine)s}"
#
# 想给「用这块板的那个会话」钉死名字，把下面这行取消注释并改成你的名字。
# ⚠ 谨慎：同一块板上的**所有** agent 会共用这一行；多人协作时改用环境变量，
#   或让每个 agent 各自 `identity set`（按 cwd 区分）。
# export WORK_LOG_AGENT="${WORK_LOG_AGENT:-%(agent)s}"
"""


def write_bootstrap(d: Path, agent: str = "") -> None:
    """在看板目录里落「引导脚本 + 本机配置」—— 让这块板自带启动方式。

    存在的理由是**跨机器**：`onboard` 生成的协议是要被抄进别的 agent 的记忆里的，
    里面一旦写死引擎的绝对路径，换台电脑（或只是挪了安装位置）那段协议就变成
    一条死链 —— 而且失效方式是静默的：对方照抄、报个"文件不存在"，然后不再报到。
    协议里只留**看板目录**（本来就是数据、本来就跟着板走），引擎位置交给这里的
    引导脚本现场解析。副作用是接入片段对"引擎装在哪"彻底中立。
    """
    eng = str(Path(__file__).resolve())
    shim = p_shim(d)
    body = SHIM_TMPL % {"rc": RC_NAME, "bin": BIN_ENV, "shim": SHIM_NAME}
    if not shim.exists() or shim.read_text(encoding="utf-8", errors="replace") != body:
        shim.write_text(body, encoding="utf-8")
    try:
        os.chmod(shim, 0o755)       # 引擎可能换了位置，可执行位不能只在第一次设
    except OSError:
        pass
    rc = p_rc(d)
    if not rc.exists():             # 本机私有：**已存在就一字不动**（别覆盖别人的机器配置）
        rc.write_text(RC_TMPL % {"engine": eng, "agent": agent or "agent1"}, encoding="utf-8")
    ign = d / RC_IGNORE
    if not ign.exists():
        ign.write_text(f"{RC_NAME}\n", encoding="utf-8")


def cmd_identity(a):
    """名字登记表：谁在这块板上叫什么、从哪个目录来。

    这张表存在的唯一理由是**让抢名字变成一件会被拒绝的事**。
    一块板上两个会话叫同一个名字时，`_get_agent` 拿到的是同一个 dict ——
    心跳、游标、entries 全混在一起，板上看着像"一个人干得挺欢"。
    """
    d = resolve_dir(a)
    action = (getattr(a, "action", "") or "list").strip()
    with locked(d):
        ids = load_identities(d)
        if action == "set":
            if not (a.agent or "").strip():
                die("✗ identity set 必须给 --agent <名字>（那才是「我要叫什么」）")
            name = check_agent_name(a.agent.strip())
            cwd = _my_cwd()
            old = ids.get(name) or {}
            held = str(old.get("cwd") or "")
            force = getattr(a, "force", False) or bool(os.environ.get(FORCE_ID_ENV))
            if held and held != cwd and not force:
                others = "、".join(_ident_holders(ids, cwd)) or "（还没有）"
                die(f"✗ '{name}' 已经被 {held} 占着（{old.get('since') and hhmmss(float(old['since'])) or '更早'}）。\n"
                    f"  一个名字对应一个会话；两个会话共用它 = 共享 last_seen / 游标，板上看不出来。\n"
                    f"  换一个名字，或（确实是你自己换了目录时）加 --force。\n"
                    f"  本目录现有登记：{others}")
            purpose = (a.purpose or "").strip() or str(old.get("purpose") or "")
            if getattr(a, "no_spawn", False):
                spawn_why = ""
            elif getattr(a, "spawn", False):
                # 强制写清「被谁唤醒」：这条声明的效果是**让看门狗不再对它报卡死**，
                # 而不带理由的静音就是藏身处 —— 代价必须付在前面。
                if not (a.purpose or "").strip():
                    die("✗ --spawn 必须同时写 --purpose「它是被谁唤醒的、什么时候会醒」。\n"
                        "  理由：这条声明的效果是「看门狗不再按心跳判它卡死」，"
                        "不写清唤醒来源，它就变成了一个把自己静音的开关。")
                spawn_why = purpose
            else:
                spawn_why = str(old.get("spawn") or "")
            if getattr(a, "no_loop", False):
                loop_flag = False
            elif getattr(a, "loop", False):
                loop_flag = True
            else:
                loop_flag = bool(old.get("loop"))
            ids[name] = {
                "cwd": cwd,
                "kind": (a.kind or "").strip() or str(old.get("kind") or ""),
                "purpose": purpose,
                "spawn": spawn_why,
                "loop": loop_flag,
                "since": float(old.get("since") or now()),
            }
            save_identities(d, ids)
            print(f"✓ 已登记：{name} ← {cwd}")
            # 这句登记正是 ⑦a 让你做的补救动作**本身**（"给每条线起名字"）——
            # 所以它必须顺手把过期的同名旗标清掉，否则医生永远好不了。
            _st = load_state(d)
            _cleared = clear_stale_cwd_conflicts(_st, cwd, name)
            if _cleared:
                save_state(d, _st)
            if spawn_why:
                print(f"  ⓘ 已声明为 **spawn 即活型**：{spawn_why}")
                print(f"    含义：看门狗**不再**按心跳判它卡死（今天 33 条误报全来自这里）。"
                      f"判活交给它的驱动层 —— 所以驱动层挂了没人会替它喊，"
                      f"这一侧的空缺由「事故轴」（有提问开着没人答）补。")
                print(f"    撤销：identity set --agent {name} --no-spawn")
            if loop_flag:
                print(f"  ⓘ 已声明为 **循环型**（守护/监听/看门狗：永远不收工）")
                print(f"    含义：「任务完成」标签对它**不生效**，它照样按心跳判活。"
                      f"这不是不信任它，是因为完成状态同时是**办事轴的开关**："
                      f"循环进程把自己的心跳打成「任务完成」，会把所有问它的提问"
                      f"一次性标成「该结案」，而这件事在板上看不出来。")
                print(f"    撤销：identity set --agent {name} --no-loop")
            print(f"  以后在这个目录跑命令，不传 --agent 也能认出你；"
                  f"想换个会话也钉死：export {AGENT_ENV}={name}")
            if _cleared:
                print(f"  ⓘ 顺手清掉 {len(_cleared)} 条过期的「同名多线」旗标："
                      f"{'、'.join(_cleared)} —— 本目录已有自己的名字，"
                      f"不会再顶着它们的名字发帖（doctor ⑦a 会随之转绿）")
            return EXIT_OK
        if action == "rm":
            if not (a.agent or "").strip():
                die("✗ identity rm 必须给 --agent <名字>")
            if a.agent.strip() not in ids:
                print(f"（板上没有 '{a.agent.strip()}' 这条登记）")
                return EXIT_OK
            ids.pop(a.agent.strip())
            save_identities(d, ids)
            print(f"✓ 已删除登记：{a.agent.strip()}")
            return EXIT_OK
        if action != "list":
            die(f"✗ identity 只认 list / set / rm，收到 {action!r}")
    # list（锁外打印就够，反正只是读）
    ids = load_identities(d)
    print(f"—— 名字登记 · {d} ——")
    if not ids:
        print("  （一个都没有）")
        print(f"  登记一个：{self_cmd()} --dir {d} identity set --agent <名字>")
        return EXIT_OK
    cwd = _my_cwd()
    for name in sorted(ids):
        v = ids[name] or {}
        rc_cwd = str(v.get("cwd") or "")
        here = "  ← 你现在就在这个目录" if rc_cwd == cwd else ""
        print(f"  {name}")
        if rc_cwd:
            print(f"     登记目录：{rc_cwd}{here}")
        else:
            print("     登记目录：（无 —— 只能靠 --agent 或 $%s）" % AGENT_ENV)
        extra = [x for x in (v.get("kind"), v.get("purpose")) if x]
        # 存活形态声明也要在这一行看得见 —— 否则"它为什么不被判卡死"只能靠翻文档。
        if str(v.get("spawn") or "").strip():
            extra.append("spawn 即活型")
        if v.get("loop"):
            extra.append("循环型（永不收工）")
        since = v.get("since")
        print(f"     自 {hhmmss(float(since)) if since else '?'} 起"
              + (f" · {' / '.join(extra)}" if extra else ""))
    clashes = {}
    for name, v in ids.items():
        c = str((v or {}).get("cwd") or "")
        if c:
            clashes.setdefault(c, []).append(name)
    for c, names in sorted(clashes.items()):
        if len(names) > 1:
            print(f"  ⚠ {c} 登记了 {len(names)} 个名字（{', '.join(sorted(names))}）——"
                  f" 它们没法靠目录区分，那两个会话必须显式传 --agent")
    return EXIT_OK


def cmd_claim(a):
    """多实例起名：让 agent 自己认领 <base>1、<base>2、…，不用人指派。

    针对「同名多实例静默合并」：两个会话共用一个名字时，last_seen / 用户喊话游标 /
    心跳预算全混在同一行，板上看不出来。claim 在**锁内**挑一个没人占的号码并当场
    登记（占座）—— 两个会话同时启动也拿不到同一个号。
    幂等：同一个目录重复认领拿到**同一个**名字 —— 重跑启动脚本不会每次冒新号。
    号码**永不复用**（包括已离场的）：回收旧号码等于让一个带着陈年 last_seen 的
    名字复活，当场被判离线/卡死 —— 名字便宜，历史干净，退役号码永远退役。
    """
    d = resolve_dir(a)
    base = (getattr(a, "base", "") or "").strip()
    if not base:
        # 让 agent **自己认**：框架名 + 序号（opencode1 / claude1 / …），不写死任何工具名。
        # 三档：显式 --base > $WORK_LOG_INSTANCE（人显式给的名字最可信）> 父链上的宿主名。
        base = (os.environ.get(INSTANCE_ENV) or "").strip()
        if "@" in base:                        # detect_instance 形态的锚点（pid@时刻）不是名字
            base = ""
    if not base:
        base = host_name()
    if not base:
        die("✗ 认不出我是哪个工具（--base 没给，父链上也没有像宿主的进程）。\n"
            "  显式给一个：claim --base opencode；或启动器里 export WORK_LOG_INSTANCE=<名字>")
    check_agent_name(f"{base}1")     # 前缀合法性随 1 号一并校验
    as_json = bool(getattr(a, "json", False))
    t = now()
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        ids = load_identities(d)
        cwd = _my_cwd()
        # 幂等：本目录已经认领过这个前缀的名字 ⇒ 返回同一个，不重复占号
        for h in _ident_holders(ids, cwd):
            if h == base or (h.startswith(base) and h[len(base):].isdigit()):
                if as_json:
                    print(f'{{"name": "{h}"}}')
                else:
                    print(f"✓ 本目录已认领过 <{h}>，不重复占号（幂等：重跑启动脚本不会冒新名字）")
                    print(f"  以后在这个目录跑命令不传 --agent 也认得你；"
                          f"想跨会话钉死：export {AGENT_ENV}={h}")
                return EXIT_OK
        # 占用 = state 里的（不管在不在场/离没离场）∪ 看板上出现过的（手写路径也算）
        #        ∪ 别的目录登记过的（identity 表是「抢名字会被拒绝」的那张表）
        taken = set(st["agents"]) | set(scan_board(d)) | set(ids)
        n = 1
        while f"{base}{n}" in taken:
            n += 1
        name = f"{base}{n}"
        st["agents"][name] = new_agent(t)      # entries=0 ⇒ 「待启动」，第一条心跳才点亮
        ids[name] = {"cwd": cwd, "kind": "", "purpose": f"claim --base {base}（自认领）",
                     "spawn": "", "loop": False, "since": t}
        save_identities(d, ids)
        save_state(d, st)
    if as_json:
        print(f'{{"name": "{name}"}}')
        return EXIT_OK
    print(f"✓ 认领 <{name}>（这块板上第 {n} 条 {base} 线；认领后是「待启动」，第一条心跳才点亮）")
    print(f"  以后在这个目录跑命令不传 --agent 也认得你；想跨会话钉死：export {AGENT_ENV}={name}")
    return EXIT_OK


def cmd_whoami(a):
    """把「我是谁、凭什么」打出来 —— 身份固定要能被查验，不然就是又一句口头约定。"""
    d = resolve_dir(a, mkdir=False)
    print(f"—— whoami · {d} ——")
    if not p_state(d).exists():
        print(f"  ⚠ 这块板还没 init（{p_state(d)} 不存在）")
    try:
        name = resolve_agent(a, d)
    except SystemExit as e:                 # 解析不出来就是"还没法确定身份"，如实退 2
        if e.code == EXIT_USAGE:
            return EXIT_USAGE
        raise
    why = getattr(a, "agent_why", "") or "来自命令行 --agent"
    print(f"  我在这块板上叫：{name}")
    print(f"  依据：{why}")
    print(f"  钉死它（换会话也不用再传 --agent）：export {AGENT_ENV}={name}")
    return EXIT_OK


# ---------------------------------------------------------------- 子命令

def _etime_secs(s: str):
    """`ps -o etime=` → 秒数；认不出返回 None（**不要拿"测不出"当拒绝理由**）。

    macOS/BSD 的格式：`MM:SS` / `HH:MM:SS` / `DD-HH:MM:SS`。
    """
    if not s:
        return None
    try:
        days = 0
        if "-" in s:
            d, s = s.split("-", 1)
            days = int(d)
        f = [int(x) for x in s.split(":")]
        while len(f) < 3:
            f.insert(0, 0)
        return days * 86400 + f[0] * 3600 + f[1] * 60 + f[2]
    except (ValueError, AttributeError):
        return None


def _ps_row(pid: int):
    """`ps` 一行：返回 (父 pid, 启动时刻, 命令行, 已存活秒数)；拿不到返回 None。

    只用 `ps` 这一个外部命令（POSIX 都有），不做平台分支 —— 少一处会漂移的实现。
    启动时刻字符串只当**不透明标识**用（比 pid 号本身可靠：pid 会被系统复用，
    单看 pid 会让"昨天那个实例"和"今天这个实例"撞成一个）。

    **强制 LC_ALL=C**：`lstart` 的月份是**按 locale 渲染**的字符串，用户 LANG 一变
    （`9月` / `Sep` / `sept.`）同一个进程就会算成两个实例。钉成 C 才跨环境稳定。
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        out = subprocess.run(["ps", "-o", "ppid=,lstart=,etime=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5, env=env).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    # ppid, 星期, 月, 日, 时:分:秒, 年, etime, 命令
    parts = out.split(None, 7)
    if len(parts) < 8:
        return None
    try:
        ppid = int(parts[0])
    except ValueError:
        return None
    return ppid, " ".join(parts[1:6]), parts[7], _etime_secs(parts[6])


_INTERPRETERS = ("node", "bun", "deno", "npx", "pnpm", "yarn",
                 "python", "python3", "env")
_SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "ash")


def _base_low(p: str) -> str:
    return os.path.basename(p or "").lower()


def host_hint_names():
    """内置宿主名单 + `$WORK_LOG_HOST_HINTS` 追加的（逗号或空格分隔）。

    为什么要这个：**手工名单天生会落后** —— 新 agent 工具层出不穷，而改代码才能加名字
    意味着"我用的那个工具永远不被识别"。有了它，用户一句
    `export WORK_LOG_HOST_HINTS=myagent,foo-code` 就接上了，不用碰代码。
    """
    names = list(INSTANCE_HOST_HINTS)
    raw = (os.environ.get(INSTANCE_HINTS_ENV) or "").replace(",", " ")
    for t in raw.split():
        t = t.strip().lower()
        if len(t) >= 2 and t not in names:
            names.append(t)
    return names


def host_hint(cmd: str):
    """命令行长啥样才算「agent 宿主」？命中返回那个关键词，否则 None。

    ⚠ 三条铁律，全是实测踩出来的（2026-09-28）：

    ① **绝不对整条命令行做子串匹配。** WorkBuddy 自己那个 Electron 辅助进程会把
       **整块环境变量 JSON 塞进命令行**，里面有个 `CLAUDE_PLUGIN_ROOT` ——
       一句 `"claude" in cmd.lower()` 就把**宿主自己**认成了 claude，
       于是实例锚点挂在每次重启都换号码的 Electron 辅助进程上，探测结果每次都变。

    ② **对「跑脚本的进程」，真身在 argv[1] 而不是 argv[0]。** macOS 上带 shebang 的脚本，
       `ps` 显示的是 `/bin/sh /路径/opencode …`（argv[0] 是 `sh`，脚本在 argv[1]）。
       `opencode` 完全可能以脚本方式安装 ⇒ 只认 argv[0] 会**漏掉真实宿主**
       （实测：假 opencode 宿主探测返回空串，就是栽在这里）。
       同理 `node …/claude-code/cli.js` 的真身也在 argv[1]。
       但 `-c` / `-e` 这类开关不是路径，**必须跳过** —— 否则
       `bash -c "… --text 'opencode 卡住了' …"` 又会把一次性 shell 认成宿主
       （那种 shell 每次 post 都换一个 pid，认它等于每次都换锚点）。

    ③ 候选与关键词做**全等或 `关键词-` 前缀**匹配（`claude` 认 `claude-code`），
       不做任意子串匹配。
    """
    toks = cmd.split()
    if not toks:
        return None
    a0 = _base_low(toks[0]).lstrip("-")      # 登录 shell 的 argv[0] 是 '-zsh'
    if not a0:
        return None
    cands = [_base_low(os.path.dirname(toks[0]))]
    nxt = None
    if a0 in _SHELLS or a0 in _INTERPRETERS:
        # 这两类都可能是「在跑某个脚本」⇒ 去 argv[1] 找真身（见上面 ②）
        if len(toks) > 1 and not toks[1].startswith("-"):
            nxt = toks[1]
    else:
        cands.append(a0)                     # argv[0] 自己就是那个程序
    if nxt:
        cands.append(_base_low(nxt))
        cands.append(_base_low(os.path.dirname(nxt)))
    for low in cands:
        if not low:
            continue
        for h in host_hint_names():
            if low == h or low.startswith(h + "-"):
                return h
    return None


def _is_app_bundle(cmd: str) -> bool:
    """是不是 macOS GUI App 包里的进程？是的话**一律不能当实例锚点**。

    这是**结构性判据**（看路径 `.app/Contents/MacOS/`），不是又一个名字名单 ——
    任何 macOS App 都覆盖得到。为什么必须排除：那种 App 是「**一个进程承载多个会话**」，
    分不出会话；拿它当锚点会把好几个会话算成**同一个**实例，
    也就是「看起来一切正常，其实在撒谎」。宁可返回空串（=我认不出），也不撒谎。
    （实测：WorkBuddy 自己就是这种，一个 Electron 进程承载全部会话。）
    """
    low = cmd.lower()
    return any(t in low for t in INSTANCE_DENY)


def host_name(explain: list = None) -> str:
    """尽力认出「我是哪个工具」（opencode / claude / …）—— 返回**宿主名**，不是实例标识。

    与 `detect_instance`（返回 `pid@启动时刻`）的区别：实例标识必须锚在**稳定**的进程上
    （所以有 HOST_MIN_AGE），而起名只要**工具名** —— 刚起了 2s 的 opencode 也叫 opencode，
    年龄无关紧要；GUI App 包照样跳过（一个进程承载多个会话，认不出「哪条线」）。
    认不出返回空串 —— 名字宁可让人 `--base` 显式给，也不猜（猜错的名字会合并两条线）。
    `WORK_LOG_NO_INSTANCE=1` 一并关掉（不想被起名探测时）。
    """
    if os.environ.get(INSTANCE_OFF_ENV):
        return ""
    pid = os.getpid()
    for _ in range(16):                        # 父链深度上限，防环
        row = _ps_row(pid)
        if row is None:
            return ""
        ppid = row[0]
        if ppid <= 1:
            return ""
        prow = _ps_row(ppid)
        if prow is None:
            return ""
        pid = ppid
        _pp, _started, p_cmd, _age = prow
        if _is_app_bundle(p_cmd):
            continue
        hint = host_hint(p_cmd)
        if hint:
            return hint
    return ""


def detect_instance(explain: list = None) -> str:
    """尽力认出「我是哪个实例」。认不出就返回空串（**绝不用猜的当判据**）。

    优先 `$WORK_LOG_INSTANCE`（最准，多实例场景建议显式设）。
    否则从本进程沿父链往上找**第一个像 agent 宿主**的进程（见 `host_hint`），
    用 `pid@启动时刻` 当标识。

    为什么必须沿父链而不是只看父进程：agent 起我们通常是
    `bash -c "… worklog post …"`，父进程是**一次性的 shell**，每次都不一样；
    只有再往上找到 opencode / claude / kimi / hermes 这种**常驻宿主**，标识才稳定。

    沿途会被跳过的三类（都会被记进 `explain`，`doctor ⑦` 靠它解释"为什么认不出"）：
      · `.app` 包里的进程 —— 一个进程承载多个会话，分不出会话（`_is_app_bundle`）；
      · 存活不足 `HOST_MIN_AGE` 秒的 —— 锚在短命进程上，标识下次就变；
      · 不像宿主的 —— 见 `host_hint`（只看 argv 可执行名 / 脚本名）。

    认不出时：**宁可返回空串，也不能返回一个乱跳的标识**（乱跳会把同一个会话
    报成多个实例，比不报还糟）；但这时 `explain` 会把父链逐层打出来，
    并告诉你要么 `export $WORK_LOG_HOST_HINTS=<你的工具名>`，要么直接
    `export $WORK_LOG_INSTANCE=<名字>`。

    `explain`：传一个 list，就会把每层的判断结果 append 进去（给 `doctor` 用）。
    """
    def note(pid2, verdict, cmd=""):
        if explain is not None:
            explain.append({"pid": pid2, "verdict": verdict, "cmd": cmd})

    if os.environ.get(INSTANCE_OFF_ENV):
        note(0, f"{INSTANCE_OFF_ENV} 已设 ⇒ 整体关掉自动探测")
        return ""
    env = (os.environ.get(INSTANCE_ENV) or "").strip()
    if env:
        note(0, f"显式指定 {INSTANCE_ENV}={env}（最准）")
        return env
    pid = os.getpid()
    for _ in range(12):                       # 父链最多爬 12 层，防止异常情况下死循环
        row = _ps_row(pid)
        if row is None:
            note(pid, "读不到这一层（ps 失败或进程已退出）")
            break
        row_ppid = row[0]
        if row_ppid <= 1:
            note(row_ppid, "到了顶层（ppid ≤ 1），父链走完")
            break
        # ⚠ 必须**重新读父进程自己那一行**：pid 和启动时刻要来自**同一个进程**。
        # （旧实现先 `pid = ppid` 再拿**上一个进程**的 started 拼字符串，
        #   于是返回的是「父的 pid + 子的启动时刻」—— 一个不存在的组合，
        #   锚点挂在短命的 Electron 辅助进程上，每次调用都在变。2026-09-28 踩过。）
        prow = _ps_row(row_ppid)
        pid = row_ppid
        if prow is None:
            note(pid, "读不到这一层（ps 失败或进程已退出）")
            break
        _pp, _started, p_cmd, p_age = prow
        if _is_app_bundle(p_cmd):
            note(pid, "跳过：GUI App 包里的进程（一个进程承载多个会话，分不出会话）", p_cmd)
            continue
        hint = host_hint(p_cmd)
        if hint is None:
            note(pid, "跳过：不像 agent 宿主（只看 argv 可执行名 / 脚本名）", p_cmd)
            continue
        if p_age is not None and p_age < HOST_MIN_AGE:
            note(pid, f"跳过：刚起了 {p_age:g}s（< {HOST_MIN_AGE}s），锚在短命进程上会乱跳",
                 p_cmd)
            continue
        note(pid, f"✅ 认定为宿主（命中关键词 `{hint}`）", p_cmd)
        return f"{pid}@{prow[1]}"
    return ""


def instance_alive(inst: str) -> bool:
    """这个实例标识对应的**宿主进程还活着吗**？

    为什么必须有这条：锚点是 `pid@启动时刻`，而宿主会重启（你关掉 opencode 再开一个、
    或用 `someagent run "…"` 这种"一次一个进程"的用法）。若不看死活，旧锚点会**一直留在
    state 里**，于是下一次 post 就被读成"又来了一个新实例" —— **满屏假的多实例告警**。

    判据：
      · 不是 `pid@…` 形式（比如显式 `WORK_LOG_INSTANCE=oc-eval`）⇒ **查不了，一律当活着**
        （显式钉的名字是我们唯一确知的东西，不能因为查不到就当成死了）；
      · pid 不在了 ⇒ 死；
      · pid 在但**启动时刻对不上** ⇒ 那个 pid 已经被系统**复用**给别的进程了 ⇒ 死。
    """
    if "@" not in inst:
        return True
    head, _, started = inst.partition("@")
    if not head.isdigit() or not started:
        return True
    row = _ps_row(int(head))
    if row is None:
        return False
    return row[1] == started


def note_instance(ag: dict, inst: str, t: float) -> bool:
    """把实例标识记到 agent 上；返回「这是一个**新出现**的实例（该提醒了）」。

    只在"已经有过实例、又来一个不同的**还活着的**实例"时返回 True ——
    第一个实例不算多实例，否则每个新 agent 第一次 post 都会喊一嗓子（最容易被忽略的噪音）。
    宿主已经死掉的旧锚点先清掉再说，否则重启一次就假报一次（见 `instance_alive`）。
    """
    seen = ag.setdefault("instances", {})
    # 先清"宿主已经不在"的锚点：那不是并发实例，是同一个会话换了宿主。
    for k in [k for k in seen if k != inst and not instance_alive(k)]:
        seen.pop(k, None)
    if not inst:
        return False
    fresh = bool(seen) and inst not in seen
    seen[inst] = t
    for k in [k for k, v in seen.items() if t - v > INSTANCE_TTL]:
        seen.pop(k, None)                      # 过期幽灵实例先清掉，再按数量裁剪
    if len(seen) > INSTANCE_KEEP:              # 只留最近的几个
        for k in sorted(seen, key=lambda x: seen[x])[:-INSTANCE_KEEP]:
            seen.pop(k, None)
    return fresh


def same_project(a: str, b: str) -> bool:
    """两个目录是不是"同一个项目的里外"（相等，或一个在另一个里面）。

    判「同名多线」时用它排掉最容易被误判的一类：agent 换到子目录再 post
    （`repo/` → `repo/backend/`）不是两条线，不该喊。
    """
    if not a or not b:
        return True
    if a == b:
        return True
    return a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def note_cwd(ag: dict, t: float) -> str:
    """记下"这个名字是从哪个目录发帖的"；返回「刚发现一条**别的目录**的同名线」。

    为什么需要它（2026-09-28 的现场）：同名会把 last_seen / entries / **用户喊话游标**
    / 心跳预算全部静默合并，而唯一的痕迹 `instances` 靠**进程探测** —— WorkBuddy /
    Electron 这类「一个进程承载多个会话」的形态探测不出、按设计返回空串不猜。
    于是那种情况下合并**一点痕迹都没有**。而 cwd 是工具本来就握着的判据：
    同一名字从两个不互为子目录的地方发帖 ⇒ 几乎不可能是同一个人。

    只回报"新出现的那个目录"（`cwd_conflict` 记住上一次警告过谁）——
    否则同一个人持续从第二个目录发帖，每一条心跳都会再刷一条板，那是最容易被忽略的噪音。
    """
    my = _my_cwd()
    known = str(ag.get("cwd") or "")
    if not known:
        ag["cwd"] = my                       # 第一次见到这个名字：记下它从哪来
        return ""
    if same_project(known, my):
        return ""
    warned = ag.setdefault("cwd_warned", [])
    # 已经喊过的目录（含它的子目录）一律不再喊 —— 只记住"上一个"是不够的：
    # 一个人真在三个目录之间来回切换时，每次换回第一个目录都会再刷一条板。
    if any(same_project(w, my) for w in warned):
        return ""
    warned.append(my)
    ag["cwd_warned"] = warned[-5:]      # 只留最近几个，别让 state.json 跟着长
    ag["cwd_conflict"] = my
    return known


def claimed_name_for_dir(d: Path, cwd: str) -> str:
    """本目录在名字登记表里**认领**的名字（没认领则空串）。

    「认领」是 `identity set` 这个动作，不是"从这儿发过帖" —— 后者显式 `--agent`
    从任何目录发帖都成立，拿它当判据会误判（见 clear_stale_cwd_conflicts）。
    """
    for nm, v in (load_identities(d) or {}).items():
        c = str((v or {}).get("cwd") or "")
        if c and same_project(c, cwd):      # c 为空时 same_project 恒真，必须先挡掉
            return nm
    return ""


def clear_stale_cwd_conflicts(st: dict, cwd: str, except_name: str) -> list:
    """清掉「冲突目录就是 cwd」的那些**过期** cwd_conflict 旗标，返回被清的名字。

    **调用方必须先确认前提**：本目录已经**认领**了自己的名字，且这次**就是用它**
    在说话。两个条件缺一不可 —— 缺了就是误清（本函数自己看不出来，所以写在契约里）：
      · 只凭「从本目录发过帖」不够：显式 `--agent` 从任何目录发帖都成立。
        实测（`[42](j)`）：a2 只是从目录 c 发了一条帖，就把 a1 在 c 上的冲突旗标清了。
      · 只凭「登记过名字」也不够：同一台机器上仍可能显式用别的名字发帖。
    两个都成立时才意味着「本目录不会再顶着别人的名字发帖」。

    背景（2026-10-01 实测）：cwd_conflict 当初为「同一个人持续从第二个目录发帖时
    别每条心跳都刷一遍板」而设计成写下就不再改（见 note_cwd 注释），但**全文件
    没有任何清除路径** —— grep 只有一处赋值。后果是：⑦a 提示你做的那两件补救
    动作（`identity set --agent <名字>` / `export WORK_LOG_AGENT=<名字>`）做完之后，
    doctor 的 ⑥ 已经转绿、⑦a 却永远 ✗，总结论继续挂着「通道没落好，我随时可能
    人间蒸发」。一个**永远好不了**的告警等于没有告警 —— 它训练人去忽略医生。

    判据故意窄：只清「某个名字的 cwd_conflict 指的正是本目录」的那些；
    冲突方在**别的**目录的名字一律不动 —— 那种冲突可能还是真的。

    同时把 cwd 从 cwd_warned 里摘掉：万一以后本目录又顶着这个名字发帖，
    note_cwd 能重新喊一次，而不是被"已经喊过"永久静音 —— 清掉不等于免报。
    """
    out = []
    for n2, v2 in (st.get("agents") or {}).items():
        if n2 == except_name or not isinstance(v2, dict):
            continue
        cf = str(v2.get("cwd_conflict") or "")
        if cf and same_project(cf, cwd):
            v2["cwd_conflict"] = ""
            v2["cwd_warned"] = [w for w in (v2.get("cwd_warned") or [])
                                if not same_project(str(w), cwd)]
            out.append(n2)
    return out


def _get_agent(st: dict, name: str, t: float) -> dict:
    """取或注册一个 agent。注意：新注册的 agent entries=0，会被判为「待启动」，
    不参与卡死告警 —— 这样写错 agent 名不会制造误报。

    「用户」是人类，不是 agent：返回一个**不入 state 的临时对象**，
    调用方对它的 last_seen/entries 更新会随对象一起被丢弃 ——
    否则人会被拉进 agent 监督表、甚至被看门狗判"卡死"（实测踩到）。
    """
    if name == USER_NAME:
        return new_agent(t)
    ag = st["agents"].get(name)
    if ag is None:
        ag = new_agent(t)
        st["agents"][name] = ag
    return ag


def cmd_init(a):
    d = resolve_dir(a)
    here = str(Path.cwd().resolve())
    foreign = None
    names = [n.strip() for n in (a.agents or "").split(",") if n.strip()]
    if getattr(a, "collab_min", None) is not None and int(a.collab_min) < 1:
        # 校验放在加锁之前：die() 是 SystemExit，在 with locked(d) 里抛会把异常
        # 穿锁而出，虽然 locked 用了 try/finally 能释放，但"参数错"本就该在动手前拒掉。
        die("--collab-min 至少 1（0/负数会让「协作中」永远不亮）")
    if getattr(a, "stale_after", None) is not None and float(a.stale_after) <= 0:
        die("--stale-after 必须为正数（0/负数会让所有 agent 立刻被判卡死）")
    # 引导脚本要在 ensure_files 之前落：它决定 .worklogrc 里那行示例身份写谁。
    write_bootstrap(d, names[0] if len(names) == 1 else "")
    with locked(d):
        st = load_state(d)
        prev = st.get("cwd")
        if prev and prev != here:
            foreign = prev          # 手动 --dir 指到了别的项目的看板
        st["cwd"] = here
        if a.task:
            st["task"] = a.task
        if getattr(a, "rate_cap", None) is not None:
            st["post_cap"] = max(0, int(a.rate_cap))
        if getattr(a, "collab_min", None) is not None:
            # 门槛写进 state 而不是常量：agent 数量本来就"几个都有可能"，
            # 写死 2 会把"3 个人在干活"也显示成勉强达标的样子，也剥夺了
            # "我们这条线平时就是 4 个人"的团队把它调高的能力。
            st["collab_min"] = int(a.collab_min)
        if getattr(a, "stale_after", None) is not None:
            # 同理：卡死阈值要能被**声明一次、两个看门狗都听**。
            # 自动界面起的那只看门狗对用户是不可见的，它原来写死用内置默认 45s，
            # 让"我显式传了 --stale-after 90"变成一句空话（实测被误报）。
            st["stale_after"] = float(a.stale_after)
            _sync_board_header_threshold(d, st)   # 板头那句也得跟着改，否则板在说谎
        if getattr(a, "stale_open_after", None) is not None:
            if float(a.stale_open_after) <= 0:
                die("--stale-open-after 必须为正数（它决定「提问开多久算没人办」）")
            # 与卡死阈值同理：声明一次，check / watch / serve / 自动界面全都听。
            # 别做成第二套平级配置 —— 那正是「阈值声明一次」这条纪律要防的事。
            st["stale_open_after"] = float(a.stale_open_after)
        if getattr(a, "no_auto_ui", False):
            st["auto_ui"] = False        # 关掉"第 2 个 agent 上线自动弹协作界面"
        ensure_files(d, st)
        t = now()
        for name in names:
            check_agent_name(name)
            ag = _get_agent(st, name, t)
            ag["note"] = "由 init 预注册"
        # 预注册的名字同时进**身份登记表**：只有一个名字时顺手把 cwd 也绑上
        #（等于"这块板在这个目录上就是它"）；给了多个名字则不绑 —— 绑谁都等于瞎猜。
        ids = load_identities(d)
        for name in names:
            rec = ids.get(name) or {}
            if not rec.get("cwd") and len(names) == 1:
                rec["cwd"] = here
            rec.setdefault("since", t)
            if not rec.get("kind"):
                rec["kind"] = "init 预注册"
            ids[name] = rec
        if names:
            save_identities(d, ids)
        save_state(d, st)
    print(f"✓ 看板就绪：{d}")
    print(f"  看板   {p_board(d)}")
    print(f"  用户   {p_user(d)}")
    print(f"  告警   {p_alerts(d)}")
    print(f"  引导   {p_shim(d)}   ← 任何 agent 都能用它，它自己找引擎（换台电脑也不用改）")
    for name in names:
        print(f"  agent  {name}（已登记）")
    if names:
        print(f"  发给对方：export {AGENT_ENV}=<名字>，然后统一用 `{p_shim(d)} <子命令>`")
    if foreign:
        print(f"⚠ 这块看板原本属于 {foreign}")
        print(f"  你现在从 {here} 用它 —— 两个项目的 agent 会混进同一块看板。")
        print("  默认目录已按项目名隔离；出现这行说明是手动 --dir 指过来的，换个目录或确认无妨。")
    return 0


def _feed_since(d: Path, st: dict, agent: str, advance: bool = True):
    """按 agent 自己的游标取出"新动态"：别人的心跳 + 看门狗告警 + 用户喊话。

    这就是"探针"的本体。模型没有推送通道，上下文只在它发起工具调用时更新，
    所以只能做成"拉"——但可以把它挂在心跳的必经路径上，让 agent 想不调都难。
    """
    entries = board_entries(d)
    msgs = parse_user(d)
    ag = st["agents"].get(agent) or {}
    bc = int(ag.get("board_cursor", 0))
    uc = int(ag.get("user_cursor", 0))
    # 协作开/关事件（kind=note）不投喂给 agent：那是写给人看的状态标记，
    # 而且是 agent 自己的行为触发的 —— 投喂只会制造无意义的"新动态"噪声。
    # agent 感知协作走的是 post 时的 🤝 提示和 status。
    new_entries = [e for e in entries[bc:] if e["agent"] != agent and e["kind"] != "note"]
    new_msgs = msgs[uc:]
    if advance and ag:
        ag["board_cursor"] = len(entries)
        ag["user_cursor"] = len(msgs)
    return new_entries, new_msgs


def _brief_hint(d: Path, st: dict, agent: str) -> str:
    e, m = _feed_since(d, st, agent, advance=False)
    mine = open_exchanges(st, to=agent)          # 别人在等我回答
    n = len(e) + len(m)
    if mine:
        q = mine[0]
        return (f"❗ <{q['from']}> 在等你回应 #{q['id']}：「{q['question'][:40]}」"
                f"→ {self_cmd()} reply --agent {agent} --id {q['id']} --text \"…\"")
    if not n:
        return ""
    extra = f"，其中 {len(m)} 条用户喊话" if m else ""
    return (f"有 {n} 条新动态{extra}，看一眼再往下做："
            f"{self_cmd()} brief --agent {agent}")


def cmd_brief(a):
    """增量投喂：只给"自你上次读过之后"发生的交流，不刷屏全板。

    `--from-now` 是给**新接入的 agent** 的一道正门：一块长期在用的板上，
    新人首次 brief 会拿到**全量历史**（实测本机这块板 882 条），上下文当场被淹没，
    等于没法交流。这个开关把心跳读游标对齐到当前，跳过全部历史。
    """
    d = resolve_dir(a)
    if getattr(a, "from_now", False) and getattr(a, "peek", False):
        # 两个语义相反（一个"推进到当前"、一个"绝不推进"），同时给一定是想错了。
        die("✗ --peek（只看不推进）与 --from-now（推进到当前）语义相反，二选一")
    a.agent = resolve_agent(a, d)
    a.stale_after = sane_stale(a.stale_after, d)
    skipped = 0
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        ag = _get_agent(st, a.agent, now())
        if getattr(a, "from_now", False):
            # ⚠ 只推进**心跳流水**的游标，**绝不动 `user_cursor`**：
            #   用户喊话是人类的指令，哪怕发生在我"对齐"之前也必须被看到 ——
            #   宁可我多看一条，也不能吞掉用户说过的话。
            n_all = len(board_entries(d))
            skipped = max(0, n_all - int(ag.get("board_cursor", 0) or 0))
            ag["board_cursor"] = n_all
        entries, msgs = _feed_since(d, st, a.agent, advance=not a.peek)
        res = evaluate(d, st, a.stale_after)
        pending = unacked(st)
        locks = dict(st.get("locks", {}))
        if not a.peek:
            save_state(d, st)

    lines = []
    lines.append(f"—— work-log 增量 · {a.agent} · {hhmmss()} ——")
    if getattr(a, "from_now", False):
        lines.append(f"✓ 已对齐到当前：跳过 {skipped} 条历史心跳，此后只看新动态"
                     f"（你未读的用户喊话不受影响，仍会投喂）")
    # 待我回应的问题排最前：这是唯一必须由"我"来闭环的事
    for q in open_exchanges(st, to=a.agent):
        waited = int(now() - q["ts"])
        lines.append(f"❗ 待你回应 #{q['id']}（{waited}s）<{q['from']}>：{q['question']}")
        lines.append(f"   → {self_cmd()} reply --agent {a.agent} --id {q['id']} --text \"你的答复\"")
    for q in open_exchanges(st, frm=a.agent):
        waited = int(now() - q["ts"])
        tgt = res["by_name"].get(q["to"], {})
        warn = f"  ⚠ {q['to']} 现在「{tgt['state']}」，别干等" if tgt.get("state") in (
            "已离场", "离线", "疑似卡死", "完成", "待启动") else ""
        lines.append(f"… 等 <{q['to']}> 回答 #{q['id']}（已等 {waited}s）：{q['question']}{warn}")
    if not entries and not msgs:
        lines.append("（无新动态）")
    for m in msgs:
        stamp = f"{m['time']} " if m["time"] else ""
        lines.append(f"[用户喊话] {stamp}{m['who']}：{m['body']}")
    for e in entries:
        if e["kind"] == "alert":
            lines.append(f"⚠ 告警 {e['time']} {e['text']}")
        else:
            tag = f"[{e['tag']}] " if e["tag"] else ""
            lines.append(f"<{e['agent']}> {e['time']} {tag}{e['text']}")

    pend_user = unanswered_user(d, st)
    if pend_user:
        ids = "、".join(f"#{u['id']}" for u in pend_user[-5:])
        lines.append(f"· 用户喊话还没人认领：{ids}"
                     f" → 认领：{self_cmd()} ack-user --agent {a.agent} --id {pend_user[0]['id']} --text \"你改了什么\"")

    status = " / ".join(f"{r['name']} {r['state']}"
                        + (f"（静默 {r['silence']:.0f}s）"
                           if r["state"] in ("疑似卡死", "离线") else "")
                        for r in res["agents"] if r["state"] != "已离场")
    lines.append(f"· 当前：{status or '无 agent'}")
    mine = [k for k, v in locks.items() if v.get("agent") == a.agent]
    others = [f"{k} ← {v['agent']}" for k, v in locks.items() if v.get("agent") != a.agent]
    if mine:
        lines.append(f"· 你持有：{'、'.join(mine)}")
    if others:
        lines.append(f"· 被占用：{'；'.join(others)} —— 别去动这些")
    if pending:
        lines.append(f"· 有 {len(pending)} 条未确认告警（#"
                     + "、#".join(str(p["id"]) for p in pending[-5:])
                     + f"），处置流程见 SKILL.md；确认用 ack --by {a.agent}")
        lines.append(f"· 卡死者的锁可以抢：lock --agent {a.agent} --resource <名字> --force")
    if a.peek:
        lines.append("· peek 模式：游标未推进")
    print("\n".join(lines))
    return 0


def cmd_ask(a):
    """定向提问。这是"交流"区别于"广播日记"的最小单位：有人问、指定谁答、
    答案会回到提问者手里。"""
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    check_agent_name(a.to, allow_user=True)   # 提问对象可以是「用户」（人类正门）
    a.text = resolve_text(a)
    if a.to == a.agent:
        die("✗ 别问自己，直接决定")
    if not (a.text or "").strip():
        die("✗ 问题不能为空")
    a.stale_after = sane_stale(a.stale_after, d)
    force = bool(getattr(a, "force", False))
    t = now()
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        blocked, streak = chatter_guard(st, a.agent, a.to, t, force)
        if blocked:
            print(blocked)
            return 4
        res = evaluate(d, st, a.stale_after)
        target = res["by_name"].get(a.to)
        warn = ""
        if a.to == USER_NAME:
            # 用户是人类：不注册、不心跳，「从未出现过」的警告在这里是误报。
            pass
        elif target is None:
            # 必须只是警告、不能报错：正常协作里 A 经常比 B 先启动，
            # 提问早于对方第一次心跳是合法的（实测第二轮就是 01:53:02 问、01:53:03 对方才发声）。
            # 但"从未出现过"比"待启动"更可能是名字拼错，所以这里必须提醒。
            warn = (f"{a.to} 至今从未出现过（没写过心跳、也没被 init 预注册）——"
                    f"若它是拼错了的名字，你的 await 会一直等到超时。先确认拼写。")
        elif target["state"] in ("已离场", "离线", "疑似卡死", "完成", "待启动"):
            warn = f"{a.to} 当前是「{target['state']}」，大概率等不到回应——考虑换人或自己拍板"
        st["exchange_seq"] = int(st.get("exchange_seq", 0)) + 1
        eid = st["exchange_seq"]
        st.setdefault("exchanges", []).append({
            "id": eid, "from": a.agent, "to": a.to,
            "question": " ".join(a.text.split()), "ts": t,
            "status": "open", "reply": None, "replied_at": None, "by": None,
        })
        record_dialogue(st, t, a.agent, a.to, "ask")
        ag = _get_agent(st, a.agent, t)
        append_entry(d, st, a.agent, f"#{eid} {a.text}", tag=f"提问→{a.to}", ts=t)
        ag["last_seen"] = t
        ag["entries"] = int(ag.get("entries", 0)) + 1
        save_state(d, st)
    print(f"✓ 已向 <{a.to}> 提问 #{eid}")
    if warn:
        print(f"⚠ {warn}")
    if a.to == USER_NAME:
        print(f"  等用户回答：{self_cmd()} await --agent {a.agent} --id {eid} --timeout 600"
              f"（人类响应可能慢，超时给足）")
        print(f"  用户回答方式：{self_cmd()} reply --agent {USER_NAME} --id {eid} --text \"…\""
              f"（跑 status 也能看到这条提问）")
    else:
        print(f"  等它回应：{self_cmd()} await --agent {a.agent} --id {eid} --timeout 300")
    if streak >= PINGPONG_WARN and a.to != USER_NAME:
        print(f"⚠ 你和 <{a.to}> 已经连续来回 {streak} 轮（没有第三方插入）。"
              f"如果这是在无进展地互相确认，请直接 post --tag 决定 收敛；"
              f"到 {PINGPONG_HARD} 轮会被强制熔断。")
    return 0


def _clear_waiting(st: dict, who: str, wid) -> None:
    """把 <who> 的等待标记里的 #wid 摘掉（摘空了就整条置 None）。

    正常路径下 await 进程自己会在退出前清标记，但那清不掉两种情况：
      · 进程被 SIGKILL（finally 没机会跑）；
      · 它因**互相等待**提前退出了 —— 那种情况下标记是**故意**留下来给对端看的。
    这两种都会让看板长期显示「它在等 #N」，而那个答案其实早就到了。
    所以「回应即撤掉等待」在这里兜底：答案一到，等待关系就不成立。
    """
    ag = st["agents"].get(who)
    if ag is None:
        return
    aw = ag.get("awaiting") or {}
    if not aw:
        return
    one = aw.get("id")
    ids = [x for x in (aw.get("ids") or ([one] if one else [])) if x != wid]
    if ids:
        aw["ids"] = ids
        aw["id"] = ids[0]
        ag["awaiting"] = aw
    else:
        ag["awaiting"] = None


def cmd_reply(a):
    d = resolve_dir(a)
    # 「用户」在这里是合法的：这就是人类回答 agent 提问的正门。
    a.agent = resolve_agent(a, d, allow_user=True)
    a.text = resolve_text(a)
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        ex = find_exchange(st, a.id)
        if ex is None:
            die(f"✗ 没有编号 #{a.id} 的提问（先 brief 看有哪些待回应）")
        if ex["status"] != "open":
            die(f"✗ #{a.id} 已由 <{ex.get('by')}> 回应过了")
        if ex["to"] != a.agent:
            die(f"✗ #{a.id} 是 <{ex['from']}> 问 <{ex['to']}> 的，不该你来答")
        if not (a.text or "").strip():
            die("✗ 回应不能为空")
        t = now()
        blocked, streak = chatter_guard(st, a.agent, ex["from"], t, bool(getattr(a, "force", False)))
        if blocked:
            print(blocked)
            return 4
        ex.update({"status": "closed", "reply": " ".join(a.text.split()),
                   "replied_at": t, "by": a.agent})
        _clear_waiting(st, ex["from"], a.id)   # 答案到了 ⇒ 提问者的等待关系不再成立
        record_dialogue(st, t, a.agent, ex["from"], "reply")
        ag = _get_agent(st, a.agent, t)
        append_entry(d, st, a.agent, a.text, tag=f"回应#{a.id}", ts=t)
        ag["last_seen"] = t
        ag["entries"] = int(ag.get("entries", 0)) + 1
        save_state(d, st)
    print(f"✓ 已回应 #{a.id}，提问者 <{ex['from']}> 下次 brief 就会看到")
    if streak >= PINGPONG_WARN:
        print(f"⚠ 你和 <{ex['from']}> 已经连续来回 {streak} 轮（没有第三方插入）。"
              f"如果这是在无进展地互相确认，请直接 post --tag 决定 收敛；"
              f"到 {PINGPONG_HARD} 轮会被强制熔断。")
    return 0


def cmd_await(a):
    """阻塞等待回应。把「等它搞完我再行动」从口头约定变成机制。

    等待期间持续刷新自己的 last_seen —— 等待中的 agent 是活的，
    不能因为它在等就被判卡死。

    但"活着"和"等得到"是两件事：如果对方已经「完成」退出，这个答案永远不会来。
    所以这里会在等待中主动判定「对方已收工」，立刻收手（退出码 3），
    而不是把整个 --timeout 干耗完（实测踩到：对端早已收工，白等满 60s 且全程无告警）。

    多路等待：`--id 1,2,3` 可以一次等多条。默认"全部都要"；
    加 `--any` 变成"任一回应即可"（问了三个人，谁先答都能往下走）。
    """
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    stale_after = sane_stale(a.stale_after, d)
    ids = [int(x) for x in (getattr(a, "ids", None) or [])]
    any_mode = bool(getattr(a, "any_mode", False))
    if any_mode and not ids:
        die("✗ --any 只在同时等多条（--id 1,2,3）时有意义；只等一条不用加")
    with locked(d):                        # 编号先校验，别等了三分钟才发现写错了
        st0 = load_state(d)
        for i in ids:
            ex0 = find_exchange(st0, i)
            if ex0 is None:
                die(f"✗ 没有编号 #{i} 的提问（先 brief 看有哪些待回应）")
            if ex0["from"] != a.agent:
                die(f"✗ #{i} 是 <{ex0['from']}> 问 <{ex0['to']}> 的，不是你问的，等不到你头上")
    start = now()
    deadline = start + max(1, a.timeout)
    collected, last_report = [], start
    answers, lost = {}, {}                 # 已拿到的回应 / 已收工的目标（id → 对端名）
    dl_note = None                         # 非 None = 本次是因互相等待退出，要把标记留给对端
    try:
        while True:
            waiting = [i for i in ids if i not in answers and i not in lost]
            with locked(d):
                st = load_state(d)
                ensure_files(d, st)
                entries, msgs = _feed_since(d, st, a.agent, advance=True)
                collected.extend(("msg", m) for m in msgs)
                collected.extend(("entry", e) for e in entries)
                ag = _get_agent(st, a.agent, now())
                ag["last_seen"] = now()
                if ids:
                    res = evaluate(d, st, stale_after)
                    for i in ids:
                        ex = find_exchange(st, i)
                        if ex is None:
                            continue
                        if ex["status"] != "open":
                            answers[i] = ex            # 已被回应
                        elif res["by_name"].get(ex["to"], {}).get("state") in ("完成", "已离场"):
                            lost[i] = ex["to"]          # 对端已收工/已离场，这条永远不会来
                    waiting = [i for i in ids if i not in answers and i not in lost]
                # 把自己的等待关系写进共享状态：这是「等待图」唯一的精确来源。
                # 靠"有没有 open 的提问"猜是不够的 —— 提问开着，不代表提问者此刻真的在等。
                # 多路等待会把每个还没回应的目标都记下来，等待图据此给每人建一条边。
                ag["awaiting"] = ({"ids": waiting, "id": waiting[0], "since": start}
                                  if waiting else None)
                # ★ 互相等待：你要等的人，正在等你（2026-09-28 真模型验收首次撞到）。
                #   两个 agent 都"问完就阻塞等答案" ⇒ 谁都走不到 reply 那一步，
                #   双方稳定刷心跳、看起来最健康，实际上整个团队已经死了。
                #   险情判定原本只活在 `evaluate()` 里 —— 也就是说**只有看门狗扫到才有人知道**，
                #   而 await 自己会白耗满整个 --timeout（实测：两个真模型各干等 5 分钟）。
                #   判据是等待图里的**环**，不猜：对方 awaiting 的提问里，有没有一条是问我的。
                # 两条判据，各管一件事：
                #   (α) 环 —— 对方**此刻真的在等**（它的 await 写的、不带 deadlock 的标记），
                #       且它等的提问里有一条是问我的、还没被回应。
                #   (β) 对端已先收手 —— 对方认出了这条死锁、退了 5，把带 deadlock 的标记
                #       留在 state 里给我看。只在这一种情况下采信，且**只递一次**：
                #       标记的 at 必须晚于我这次 await 的开始（= 我等的过程中新发生的事）。
                #       否则上一轮死锁留下的旧标记会把之后的**单向等待**误判成死锁
                #       （[43] (e) 实测：a2 的旧标记让 a1 几秒后的新 await 白白退 5）。
                #   要求 status == "open" 是关键：标记一旦被 reply 摘掉那条 id，环就不成立。
                dead = []
                for i in waiting:
                    exw = find_exchange(st, i)
                    if exw is None:
                        continue
                    tgt = str(exw.get("to") or "")
                    aw = (st["agents"].get(tgt) or {}).get("awaiting") or {}
                    dk = aw.get("deadlock") or {}
                    if dk:
                        if (dk.get("with") == a.agent
                                and float(dk.get("at") or 0.0) > start
                                and now() - float(dk.get("at") or 0.0) < DEADLOCK_HINT_TTL):
                            dead.append((tgt, dk.get("id") or (aw.get("id") or ""), i))
                            break
                        continue
                    for j in (aw.get("ids") or []):
                        ej = find_exchange(st, j)
                        if ej and str(ej.get("to") or "") == a.agent \
                                and ej.get("status") == "open":
                            dead.append((tgt, j, i))
                            break
                save_state(d, st)
            if dead:
                # 立刻收手：等下去不会有任何一方先答，白耗的每一秒都是真的卡住。
                tgt, j, i = dead[0]
                print(f"⚠ 互相等待：<{tgt}> 正在等你回应 #{j}，而你正在等它的 #{i} ——"
                      f"谁都不会先答，这是一条真死锁（等 {int(now() - start)}s 发现的）。")
                print(f"  先回答它：{self_cmd()} --dir {d} reply --agent {a.agent} "
                      f"--id {j} --text \"…\"，再回来等 #{i}；或自己拍板往下做。")
                # ★ 留给对端一个带 deadlock 的标记（见上面 (β)）：
                #   两边都退出来，这条死锁才算真的解开；只救一边等于没救。
                dl_note = {"with": tgt, "id": j, "at": now(), "ids": list(waiting)}
                # 板上留一条：先发现的一方会立刻撤掉自己的等待标记（下面的 finally），
                # 于是另一侧再扫时「环」已经没了 —— 不留痕它就永远不知道刚才发生了什么。
                # 这也是"不依赖看门狗"的一层：险情判定原本只在 watchdog 扫的那一次。
                try:
                    with locked(d):
                        st2 = load_state(d)
                        append_entry(
                            d, st2, RESERVED_AGENT,
                            f"[互相等待] <{a.agent}> 等 #{i} 时发现 <{tgt}> 正在等它回 #{j} ——"
                            f"已让 <{a.agent}> 先回答 #{j}。两边都问完就阻塞等答案会形成真死锁，"
                            f"谁都不会先答；需要有一方先 reply。",
                            tag="协作", ts=now())
                except Exception:              # noqa: BLE001 - 留痕失败不能掩盖返回码
                    pass
                return EXIT_DEADLOCK
            if ids:
                if any_mode and answers:
                    break                      # --any：有人答了就够
                if not waiting:
                    break                      # 全都有结果了（回应或收工）
            elif collected:
                break                          # 没指定 #id：一有新动态就返回，不干等
            if now() >= deadline:
                break
            if now() - last_report >= a.report:
                waited = int(now() - start)
                if ids:
                    print(f"[await] 已等 {waited}s，还差 "
                          + "、".join(f"#{i}" for i in waiting)
                          + ("（任一即算达成）" if any_mode else "（要全部）"))
                else:
                    print(f"[await] 已等 {waited}s，暂无新动态")
                last_report = now()
            time.sleep(a.interval)
    finally:
        # 无论怎么退出（含被 Ctrl-C / 被杀前最后一次机会），都要撤掉等待标记，
        # 否则看板会一直显示它在等一个早就没人等的答案。
        # 唯一例外是**因互相等待退出**：那一刻必须把标记留下来（带 deadlock 说明），
        # 好让对端下一轮扫到时一起收手 —— 撤掉它等于把对端锁在黑箱里干等到超时。
        # 留下来是安全的：evaluate() 只采信"还在刷新心跳"的等待标记，
        # 对端收工/静默超过阈值后这条标记自动作废，不会变成永久噪声。
        try:
            with locked(d):
                st = load_state(d)
                ag = st["agents"].get(a.agent)
                if ag is not None:
                    if dl_note is not None:
                        wids = list(dl_note.pop("ids", []))
                        ag["awaiting"] = {"ids": wids,
                                          "id": (wids or [None])[0],
                                          "since": start,
                                          "deadlock": dl_note}
                    else:
                        ag["awaiting"] = None
                    save_state(d, st)
        except Exception:                  # noqa: BLE001 - 清理失败不能掩盖真正的返回码
            pass

    for kind, item in collected:
        if kind == "msg":
            print(f"[用户喊话] {item['time']} {item['who']}：{item['body']}")
        else:
            tag = f"[{item['tag']}] " if item["tag"] else ""
            print(f"<{item['agent']}> {item['time']} {tag}{item['text']}")
    waited = int(now() - start)

    def _dump() -> None:
        for i in sorted(answers):
            ex = answers[i]
            print(f"  ✓ #{i} 已由 <{ex['by']}> 回应：{ex['reply']}")
        for i in sorted(lost):
            print(f"  ✗ #{i} 不会来了：<{lost[i]}> 已收工")

    if not ids:
        return 0          # 没指定 #id：等到任何新动态就算达成

    # 单条等待保留原来的文案：它是最常用的路径，措辞已经被文档和测试引用。
    only = ids[0] if len(ids) == 1 else None

    if any_mode and answers:
        first = min(answers)
        ex = answers[first]
        print(f"✓ #{first} 已由 <{ex['by']}> 回应：{ex['reply']}")
        if len(answers) > 1 or lost:
            _dump()
        print(f"  等了 {waited}s（--any：任一回应即达成）")
        return 0

    if len(answers) == len(ids):
        if only is not None:
            ex = answers[only]
            print(f"✓ #{only} 已由 <{ex['by']}> 回应：{ex['reply']}")
            print(f"  等了 {waited}s")
        else:
            print(f"✓ {len(ids)} 条全部拿到回应（等了 {waited}s）：")
            _dump()
        return 0

    pending = [i for i in ids if i not in answers and i not in lost]
    if not pending:
        # 结构性地等不齐了：剩下的都已收工 —— 这不是超时，再等也没用
        if only is not None:
            print(f"✗ 对方已收工：<{lost[only]}> 写了「任务完成」，#{only} 的答案不会来了"
                  f"（只等了 {waited}s，没白耗完 {int(a.timeout)}s）")
        else:
            print(f"✗ 等不齐了：拿到 {len(answers)} 条，还有 {len(lost)} 条的目标已收工"
                  f"—— 答案不会来了（只等了 {waited}s，没白耗完 {int(a.timeout)}s）")
            _dump()
        print("  别干等：自己拍板往下做，或换一个还在线的 agent 问。")
        return 3

    # 注意：不能因为"期间有别的心跳路过"就返回成功 ——
    # 提问者会以为自己拿到了答案。没等到回应就是没等到，必须明确失败。
    if only is not None:
        print(f"✗ 等待超时（{int(a.timeout)}s）：#{only} 始终没等到回应")
    else:
        left = "、".join(f"#{i}" for i in pending)
        print(f"✗ 等待超时（{int(a.timeout)}s）：{left} 始终没等到回应")
        if answers or lost:
            _dump()
    print("  别继续干等：自己拍板往下做，或换一个在线的 agent 问。")
    return 1


def cmd_ack_user(a):
    """回执用户喊话。光"读到"不算数，用户要看到有人认领。"""
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    a.text = resolve_text(a)
    # 回执的正文就是给用户看的全部信息（页面上「谁认领了 + 认领时说了什么」）。
    # 空的回执在页面上长得和"漏填"一模一样，而用户拿到的信息量是 0 ——
    # 它比"没认领"更差：没认领时用户知道还没人管，空认领时用户以为有人管了。
    if not (a.text or "").strip():
        die(f"✗ 认领 #{a.id} 时要写清你打算做什么（--text）"
            f" —— 空认领让用户以为有人管了，却不知道谁在管什么")
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        msgs = parse_user(d)
        if a.id < 1 or a.id > len(msgs):
            die(f"✗ 没有第 {a.id} 条用户喊话（当前共 {len(msgs)} 条）")
        for k in st.get("acks", []):
            if k["id"] == a.id and k["agent"] == a.agent:
                print(f"（你已回执过 #{a.id}）")
                return 0
        # 先到先得。板上有多个应答方时（人工 agent + 面板自动应答），同一条喊话会被
        # 各自回执一遍，而**重复回执在板上完全看不出来** —— 用户看到两条"我在管"，
        # 无法分辨谁真的在动。
        #
        # 退 **3** 而不是 1：3 这一族的含义是「已被别人处置，别再等、也别重做」
        # （`lock` 抢锁失败退的也是 3）。而 1 的样本全是**故障**（超时/卡死/险情），
        # "别人已经认领了"不是故障、是期望结局 —— 塞进 1 会让 `check || 告警`
        # 把良性结论当异常。这是「用法错误绝不借用 1」的镜像：良性结论也别借用故障码。
        holder = next((k for k in st.get("acks", [])
                       if k["id"] == a.id and k.get("agent") != a.agent), None)
        if holder is not None and not getattr(a, "force", False):
            t = now()
            print(f"✗ 用户喊话 #{a.id} 已被 <{holder['agent']}> 认领"
                  f"（{hhmmss(holder.get('ts'))}）："
                  f"{holder.get('text') or ''}\n"
                  f"  认领的目的是让用户知道「有人在管了」—— 已经有人管了，这条就不再需要你认领。\n"
                  f"  若你只是想让大家看见你也动过，用 `post`（不受认领约束）；"
                  f"确实要接管这一条，加 `--force`")
            # 抢不到也进看板：让"谁想管但已经有人管了"变成共享可见的事实（同 lock）
            append_entry(d, st, a.agent,
                         f"想认领用户喊话 #{a.id}，但 <{holder['agent']}> 已认领，让给它",
                         tag="阻塞", ts=t)
            ag = _get_agent(st, a.agent, t)
            ag["last_seen"] = t
            ag["entries"] = int(ag.get("entries", 0)) + 1
            save_state(d, st)
            return 3
        t = now()
        st.setdefault("acks", []).append({"id": a.id, "agent": a.agent, "ts": t,
                                          "text": " ".join(a.text.split())})
        ag = _get_agent(st, a.agent, t)
        append_entry(d, st, a.agent, a.text, tag=f"回执#{a.id}", ts=t)
        ag["last_seen"] = t
        ag["entries"] = int(ag.get("entries", 0)) + 1
        save_state(d, st)
    print(f"✓ 已回执用户喊话 #{a.id}，用户会在页面上看到是谁认领的")
    return 0


def cmd_post(a):
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    a.text = resolve_text(a)
    if not (a.text or "").strip():
        die("✗ --text 不能为空 —— 空心跳等于没写，看门狗会当它没存在过")
    t = now()
    multi_inst, multi_n, inst, other_cwd = False, 0, "", ""
    freed_locks: list = []
    dropped_hold = False
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        # 板头那句「静默超过 Xs」要跟本板**实际**阈值一致 —— 否则板在说谎：
        # 后面接手的 agent 会照着错的数规划心跳（实测：真看板声明了 90s，
        # 板头却一直写着 45s，因为原来只有 `init` 会刷这一行）。
        # 放在 post（最热的路径）里自愈；函数内部有记忆化，稳态下零文件 I/O。
        _sync_board_header_threshold(d, st)
        blocked, sent, cap = post_guard(st, a.agent, t)
        if blocked:
            # 刻意**不**刷新 last_seen：一个停不下来的 agent 不是"活着"，是"空转"。
            # 让它在 stale_after 之后同时被标成疑似卡死，两处报警比一处更难被忽略。
            save_state(d, st)
            print(blocked)
            return 4
        ag = _get_agent(st, a.agent, t)
        # 同名多实例识别：第一次见到「第二个实例」时当场留痕。
        # 这件事本身不能失败、也不能拦人（同一逻辑 agent 从两个终端驱动是合法的），
        # 所以只做"说出来 + 记进 state"，让 doctor/status/brief 都能看见。
        inst = detect_instance()          # 只探测一次：它要爬父链，别在热路径上重复调
        multi_inst = note_instance(ag, inst, t)
        # 同名异目录 ⇒ 两条线共用一个名字。判据用 cwd，不用进程探测：
        # 后者对「一进程多会话」的形态（WorkBuddy / Electron 桌面版）探测不出、
        # 按设计返回空串不猜，于是那种情况下合并**一点痕迹都没有**（实测 2026-09-28）。
        other_cwd = note_cwd(ag, t)
        # 本目录**已认领**自己的名字、且这次**就是用它**在说话 ⇒「本目录顶着别人的
        # 名字发帖」这个前提不存在了（不清的话 doctor ⑦a 会永远 ✗）。
        # 两个条件缺一不可，理由见 clear_stale_cwd_conflicts 的契约 ——
        # 只凭"从本目录发过帖"会误清（[42](j) 实测：a2 从 c 发一条帖清了 a1 的旗标）。
        _mine = claimed_name_for_dir(d, _my_cwd())
        if _mine and _mine == a.agent:
            clear_stale_cwd_conflicts(st, _my_cwd(), a.agent)
        tag = a.tag or ("任务完成" if a.done else None)
        # 「完成」的判据只有一处（agent_is_done）：看门狗与这里的"收工即放手"必须同一套。
        # 曾经这里写的是 `if a.done:`，于是板上按惯例发 `--tag 任务完成` 的 agent
        # 收工后**照旧占着锁**（我自己在真板上撞到的那一回，锁没放掉、还看不出来）。
        # 循环型声明者写完成标签不算放手 —— 和 evaluate 保持一致，避免"锁放了状态却 active"。
        loop_decl = bool((load_identities(d).get(a.agent) or {}).get("loop"))
        is_done = agent_is_done(tag, loop_decl=loop_decl, done_flag=a.done)
        line = append_entry(d, st, a.agent, a.text, tag=tag, ts=t)
        if multi_inst:
            # 用 watchdog 名写板：`scan_board` 会跳过保留名，不会把它变成一个"幽灵 agent"。
            n_inst = len(ag.get("instances") or {})
            multi_n = n_inst
            append_entry(
                d, st, RESERVED_AGENT,
                f"<{a.agent}> 这个名字下现在有 {n_inst} 个实例在写（刚出现的那个是 "
                f"{inst}）。多实例共用一个名字会**合并** last_seen / 用户喊话游标 / "
                f"心跳预算：一个死了另一个会替它刷新（看门狗测不到），"
                f"用户喊话也会被其中一个先读走（另一个永远看不到）。"
                f"建议给每个实例起不同的名字：`export {AGENT_ENV}={a.agent}-<实例名>`"
                f"（驱动层启动器里设，之后该实例不用再传 --agent）",
                tag="协作", ts=t)
        if other_cwd:
            append_entry(
                d, st, RESERVED_AGENT,
                f"<{a.agent}> 这个名字现在从**两个不同的目录**发帖：{other_cwd} 与 "
                f"{_my_cwd()}。它们会共用 last_seen / entries / 用户喊话游标 / 心跳预算，"
                f"**板上完全看不出来**（实例探测对「一进程多会话」的形态给不出答案）。"
                f"请给每条线起不同名字：`identity set --agent <名字>`（按目录登记），"
                f"或启动器里 `export {AGENT_ENV}=<名字>`。若确实是同一个人换了目录，忽略即可。",
                tag="协作", ts=t)
        ag["last_seen"] = t
        ag["entries"] = int(ag.get("entries", 0)) + 1
        # post = 活性信号本身。曾宣告过离场的 agent 一旦发心跳，「离场」就该当场撤销 ——
        # 复位不能指望人记得跑 retire --undo（那个宣告的人多半早忘了）。
        retired_reset = bool(ag.pop("retired", None))
        for _rk in ("retired_at", "retired_by", "retired_note"):
            ag.pop(_rk, None)
        # ★ 2026-09-28 起不再清零 expected_silence_until（hold 与 post 解耦）。
        # 旧版这里写 0.0：任何一条 post 都会当场把 hold 窗口拆掉 —— 于是
        # 「每步都要 post 心跳」和「长任务先 hold」互相打架，得靠一段文档
        # 绕口令（"hold 别紧接着 post"）去解释掉一个坑。该修状态机，不该写文档。
        # 新语义：挂起窗口只被 hold 设置、被 release / `hold --seconds 0` 清除、
        # 随时间自然失效；post 只刷新 last_seen（对看门狗而言它本来就是活性信号）。
        # done 优先于挂起判定（evaluate 里 done 分支在前），已完成者身上的残留窗口无害。
        if ag.get("alert_open"):
            ag["alert_open"] = False
            ag["alert_repeat"] = 0     # 退避计数必须一起归零，否则它下次静默会直接从「离线」档开始
            ag["pending_recovery"] = True
        if is_done:
            ag["status"] = "done"
            ag["done_at"] = t
            # ★ 批次2：**收工 = 我不再占任何东西**。
            # 「我已下线」在语义上本来就包含「我不再持有任何资源」，旧版却要手工
            # `unlock` + `release` 两步 —— 忘了任何一步，别人就被一个**已经不在了**的
            # agent 挡住，而且板上看不出是它挡的（今天真撞过：陈旧锁只能靠猜 >15 分钟）。
            # 所以让收工顺手做掉：能省掉的那两步，就不该留给记性。
            freed_locks = sorted(k for k, v in (st.get("locks") or {}).items()
                                 if (v or {}).get("agent") == a.agent)
            for k in freed_locks:
                st["locks"].pop(k, None)
            dropped_hold = float(ag.get("expected_silence_until") or 0.0) > t
            ag["expected_silence_until"] = 0.0
        elif ag.get("status") == "done":
            ag["status"] = "active"
            ag.pop("done_at", None)
        hint = _brief_hint(d, st, a.agent)
        # 协作提示：你不是一个人在干活。用 last_seen 新鲜度判断别人是否在线
        #（窗口 = 4 个心跳周期，与"卡死"判定错开，避免边缘抖动）。
        fresh = float(st.get("heartbeat_secs", HEARTBEAT)) * 4
        others = sorted(n for n, o in st["agents"].items()
                        if n != a.agent and o.get("status") != "done"
                        and t - float(o.get("last_seen", 0.0)) <= fresh)
        # 理想流程：多人开始协作 → 自动起协作界面并弹到用户面前。
        # 这里只在锁内**占位**（ui_opened=True 防并发 post 双开），
        # 真正起服务/探测必须在锁外 —— serve 的健康检查也要拿这把锁，
        # 持锁探测 = 自己等自己（实测锁死 2.5s）。
        need_ui = (bool(others) and not st.get("ui_opened")
                   and st.get("auto_ui", True) and not os.environ.get(UI_ALL_ENV))
        if need_ui:
            st["ui_opened"] = True
        save_state(d, st)
    ui_hint = None
    if need_ui:
        with locked(d):
            st2 = load_state(d)
        ui_hint = auto_ui(d, st2)          # 锁外：serve 子进程要能拿到锁响应探活
        with locked(d):
            st3 = load_state(d)
            if ui_hint is None:
                st3["ui_opened"] = False   # 四个端口全失败 → 允许下次再试
            else:
                for k in ("ui_port", "ui_pid"):
                    if k in st2:
                        st3[k] = st2[k]
            save_state(d, st3)
    print(line)
    if multi_inst:
        # 触发者必须当场看到（板上那条是给别人看的，不提示它自己等于没说）。
        print(f"⚠ 同名多实例：<{a.agent}> 这个名字下已经有 {multi_n} 个实例在写（我刚认出的"
              f"是 {inst}）。它们会共用 last_seen / 用户喊话游标 / 心跳预算 ——"
              f"**板上看不出来**（一个死了另一个会替它刷新，用户喊话会被先读到的人取走）。")
        print(f"  建议：给每个实例起不同名字，用环境变量钉死在启动器里 ——"
              f"export {AGENT_ENV}={a.agent}-<实例名>（如 {a.agent}-eval / {a.agent}-ui）。")
    if other_cwd:
        print(f"⚠ 同名多线：<{a.agent}> 这个名字现在从两个不同目录发帖 ——"
              f"{other_cwd} 与 {_my_cwd()}。它们会共用 last_seen / 用户喊话游标 / 心跳预算，"
              f"**板上看不出来**（实例探测对「一进程多会话」形态给不出答案）。")
        print(f"  建议：给这条线起个自己的名字 —— 在本目录跑一次 "
              f"`identity set --agent <名字>`，或启动器里 export {AGENT_ENV}=<名字>。"
              f"若确实只是你自己换了目录，忽略本条（不会再刷）。")
    if is_done:
        print(f"✓ {a.agent} 已标记完成，看门狗不再对它报卡死")
        # 收工顺手做的两件事必须**当面报出来**：不说，用户和协作者就以为还要手工那一步，
        # 于是继续保留「收工后手工 unlock」的习惯 —— 那这条机制的收益就白丢了。
        if freed_locks:
            print(f"✓ 顺手放掉了 {len(freed_locks)} 把锁：{'、'.join(freed_locks)}"
                  f"（已收工的人不该再占着资源；正在等它们的人现在就能拿到）")
        if dropped_hold:
            print("✓ 顺手清掉了挂起窗口（已收工者不该再挂着「我在跑长任务」）")
    if retired_reset:
        print(f"↩ <{a.agent}> 曾被宣告离场 —— 它现在发心跳了，离场标记已自动复位（重新纳入监督）")
    if others:
        # 只报"还有谁在干活"和当前总人数，**不写分母** —— 人数不固定。
        print(f"🤝 协作中：{'、'.join(others)} 也在干活（当前 {len(others) + 1} 个 agent 在干活）")
    if ui_hint:
        print(ui_hint)
    if hint:
        print(f"💬 {hint}")
    return 0


def cmd_hold(a):
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    # 这里**故意不拒空文本**，与 post/ask/reply/say/ack-user 不同。
    # 判据不是"空文本一律是用法错误"，而是「空文本在这个命令里有没有默认含义」：
    #   · post/ask/reply/say/ack-user：文本**就是载荷**，空 = 没有内容 ⇒ 退 2；
    #   · hold/release：文本是**可选标签**，空 = 用下面那句内置默认标签 ⇒ 合法。
    # 而且 argparse 里 `--text` 的 default 就是 ""，`--text ''` 与"根本没给"
    # **逐字节相同**、工具分不出来 —— 对 hold 退 2 等于把"用默认标签"这条路堵死。
    # （真正该拦的是 `--text-file <空文件>`：省略能表达的语义，不必靠一个空文件表达。）
    a.text = resolve_text(a, "--text") or ""
    t = now()
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        ag = _get_agent(st, a.agent, t)
        if a.seconds < 0:
            die("--seconds 不能为负；0 = 立即解除挂起（等价 release）")
        if a.seconds == 0:
            # 显式解除：post 已与 hold 解耦（不再自动清窗口），
            # 提前收工的人需要一个不写绕口令的出口。
            ag["expected_silence_until"] = 0.0
            if getattr(a, "quiet", False):
                ag["last_seen"] = t
            else:
                append_entry(d, st, a.agent, a.text or "挂起提前解除", tag="执行", ts=t)
                ag["last_seen"] = t
                ag["entries"] = int(ag.get("entries", 0)) + 1
            save_state(d, st)
            print(f"✓ {a.agent} 挂起已解除（--seconds 0）")
            return 0
        until = t + a.seconds
        ag["expected_silence_until"] = until
        if getattr(a, "quiet", False):
            # 静默挂起：只刷新存活 + 声明静默窗口，不往看板写条目。
            # 驱动层（LLM agent 每步都要等模型 30~120s）需要这个——
            # 否则每个模型调用都留一条「进入长任务」，看板会被垃圾冲掉。
            ag["last_seen"] = t
        else:
            label = a.text or f"进入长任务，{a.seconds}s 内不写心跳也不会被误报"
            append_entry(d, st, a.agent, f"{label}（挂起至 {hhmmss(until)}）", tag="阻塞", ts=t)
            ag["last_seen"] = t
            ag["entries"] = int(ag.get("entries", 0)) + 1
        save_state(d, st)
    if getattr(a, "quiet", False):
        print(f"✓ {a.agent} 静默挂起至 {hhmmss(until)}（未写心跳条目）")
    else:
        print(f"✓ {a.agent} 挂起至 {hhmmss(until)}；看门狗在此期间不判它卡死")
        print("  post 不会解除挂起（两者已解耦）；提前收工用 `release` 或 `hold --seconds 0`，不解除也会到点自动失效")
    return 0


def cmd_release(a):
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    # 空文本合法，理由同 cmd_hold（文本是可选标签，"挂起结束，回到心跳" 是默认标签）。
    a.text = resolve_text(a, "--text") or ""
    t = now()
    with locked(d):
        st = load_state(d)
        ag = _get_agent(st, a.agent, t)
        ag["expected_silence_until"] = 0.0
        if getattr(a, "quiet", False):
            # 静默解除：驱动层退出前清掉自己最后一次 hold 留下的窗口。
            # 不清的话，一个**已经退出**的 agent 会以「挂起中」的健康面貌继续挂着
            # 最多一个窗口那么久（实测：agent 收工后看板仍显示它挂起中 180s）。
            ag["last_seen"] = t
        else:
            append_entry(d, st, a.agent, a.text or "挂起结束，回到心跳", tag="执行", ts=t)
            ag["last_seen"] = t
            ag["entries"] = int(ag.get("entries", 0)) + 1
        save_state(d, st)
    print(f"✓ {a.agent} 已解除挂起" + ("（静默，未写心跳条目）" if getattr(a, "quiet", False) else ""))
    return 0


def user_say(d: Path, st: dict, text: str, who: str = "用户") -> str:
    """往用户喊话通道写一条。cmd_say 与 serve 的 HTTP 接口共用这一份实现。

    who 必须清洗：user.md 是「一行一条」的格式，名字里混进换行会伪造出
    多条喊话；混进冒号会冒充别的说话人。两边入口（CLI --as / HTTP who）
    都从这里过一遍，不靠调用方自觉。
    """
    who = re.sub(r"[\s：:]+", " ", str(who)).strip()[:12] or "用户"
    line = f"[{hhmmss()}] {who}：{' '.join(text.split())}"
    with open(p_user(d), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    st["user_seq"] = int(st.get("user_seq", 0)) + 1
    return line


def user_reply_exchange(d: Path, st: dict, eid: int, text: str) -> tuple[bool, str]:
    """人类在 UI / CLI 里回答一条问向自己的提问。返回 (是否成功, 消息)。"""
    ex = find_exchange(st, eid)
    if ex is None:
        return False, f"没有编号 #{eid} 的提问"
    if ex.get("status") != "open":
        return False, f"#{eid} 已由 <{ex.get('by')}> 回应过了"
    if ex.get("to") != USER_NAME:
        return False, f"#{eid} 是 <{ex['from']}> 问 <{ex['to']}> 的，只能由被问者回答"
    ex.update({"status": "closed", "reply": " ".join(text.split()),
               "replied_at": now(), "by": USER_NAME})
    append_entry(d, st, USER_NAME, " ".join(text.split()), tag=f"回应#{eid}")
    return True, f"✓ 已回答 #{eid}，<{ex['from']}> 的 await 会立刻拿到"


def _ui_running(port: int, want_dir: Path | None = None) -> bool:
    """端口上有没有一个**本项目**的 work-log serve。

    want_dir 给定时必须核对 /api/health 返回的 dir：光看"端口有服务应答"就复用，
    两个项目同时跑时会互相弹对方的看板（串台，实测级别的事故）。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.6) as r:
            if r.status != 200:
                return False
            if want_dir is None:
                return True
            got = (json.loads(r.read().decode("utf-8")) or {}).get("dir")
            return str(want_dir) == got
    except Exception:                  # noqa: BLE001 - 探活失败一律视为"没在跑"
        return False


def _open_browser(url: str) -> bool:
    if os.environ.get(UI_OPEN_ENV):
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:                  # noqa: BLE001 - 无桌面/无浏览器的环境静默跳过
        return False


def board_stale_after(st: dict) -> float:
    """看板自己声明的卡死阈值：env > state（`init --stale-after N`）> 内置默认。

    为什么需要它：`post` 触发的**自动界面**会再起一个 serve，而那只 serve 对用户
    完全不可见（提示行里只有一句"协作界面已自动启动"）。它原来写死用内置默认 45s ——
    于是"我明明用 `serve --stale-after 90` 起的"照样会被这个隐藏的看门狗按 45s
    判卡死。真 LLM agent 单轮 30~120s，必然中招。
    阈值必须能被**声明一次、两个看门狗都听**（state 或环境变量都行）。
    """
    env = os.environ.get("WORK_LOG_STALE_AFTER")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    try:
        v = float(st.get("stale_after") or 0)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return float(STALE_AFTER)


def board_stale_open_after(st: dict) -> float:
    """办事轴阈值的统一解析：env > 看板声明的（`init --stale-open-after`）> 内置默认。

    **刻意不做成第二个独立旋钮**：今天已经有一条纪律是「阈值声明一次」（卡死阈值那套
    被四份驱动层各配各的坑过）。再开一套平级配置，就是把同一个错误犯第二次。
    所以它跟着 `st` 走 —— 谁读到板，谁就拿到同一个值；连隐藏的自动 serve 也一样。
    """
    env = os.environ.get(STALE_OPEN_ENV)
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    try:
        v = float(st.get("stale_open_after") or 0)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return float(STALE_OPEN_AFTER)


def _spawn_serve(d: Path, port: int, stale_after: float | None = None) -> int | None:
    """脱离父进程起一个 serve（终端关了也活着），日志落到看板目录 serve.log。"""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--dir", str(d),
           "serve", "--port", str(port), "--interval", "5"]
    if stale_after is not None:
        # 不传的话子进程用内置默认 45s，会和用户声明的阈值打架（见 board_stale_after）
        cmd += ["--stale-after", f"{float(stale_after):g}"]
    try:
        logf = open(d / "serve.log", "a", encoding="utf-8")
    except OSError:
        return None
    try:
        p = subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError:
        return None
    finally:
        logf.close()
    return p.pid


def auto_ui(d: Path, st: dict) -> str | None:
    """协作 UI 自动接入：起 serve（必要时）+ 弹浏览器。返回提示行，无事可做返回 None。

    **调用约定**：「是否需要弹」由调用方判定（cmd_post 在锁内占位 ui_opened=True
    防并发双开）——本函数不做这个判断，否则占位标记会把自己挡在门外（实测踩到）。
    本函数必须在**不持看板锁**的前提下运行：serve 子进程的健康检查要拿这把锁，
    持锁探测 = 自己等自己。全部端口失败时返回 None，调用方负责复位 ui_opened。
    """
    if os.environ.get(UI_ALL_ENV):
        return None
    # 端口可被 WORK_LOG_UI_PORT 覆盖（selftest 用它保证并行实例互不撞口）
    base = int(os.environ.get("WORK_LOG_UI_PORT")
               or st.get("ui_port") or DEFAULT_PORT)
    # 自动起的看门狗必须听**同一个**卡死阈值，否则它会比用户显式起的那个更严，
    # 而它又不可见 —— 用户会以为"阈值设了没用"（见 board_stale_after 的注释）。
    sa = board_stale_after(st)
    for cand in range(base, base + 4):
        url = f"http://localhost:{cand}/"
        if _ui_running(cand, d):                    # 已有**本项目**的视图在跑 → 直接用
            st["ui_opened"] = True
            st["ui_port"] = cand
            opened = _open_browser(url)
            return (f"🖥 协作界面：{url}" + ("（已在浏览器打开）" if opened else "")
                    + f"〔卡死阈值 {sa:g}s〕")
        pid = _spawn_serve(d, cand, sa)
        if pid is None:
            continue
        up = False
        for _ in range(5):                          # 最多等 2.5s 起服务
            time.sleep(0.5)
            if _ui_running(cand, d):
                up = True
                break
        if not up:
            # 健康检查没就绪不等于它没起来 —— 慢机器上它可能晚 1s 才绑定成功。
            # 不杀掉就 continue 换端口，会留下一只"迟到启动"的孤儿 serve
            # （实测：8787-8790 连开 4 只就是这么来的）。
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            continue
        st["ui_opened"] = True
        st["ui_port"] = cand
        st["ui_pid"] = pid
        opened = _open_browser(url)
        # 阈值必须打出来：这只看门狗是**自动起的**，用户看不见它，
        # 不打出来就没法解释"为什么我设了 90s 还被按 45s 判卡死"。
        return (f"🖥 协作界面已自动启动：{url}"
                        + ("（浏览器已打开" if opened else "（未弹浏览器")
                        + f"；卡死阈值 {sa:g}s；停止：kill {pid}）")
    return None


def cmd_say(a):
    d = resolve_dir(a)
    a.text = resolve_text(a)
    # 空喊话要当场拦住 —— 它的代价不在"这条没内容"，而在**它会把所有 agent 叫起来**：
    # `user_say` 会推进 user_seq，每个 agent 下一次 `brief` 都会被告知
    # 「用户新增喊话（N → N+1）」，于是全体跑来 `read-user` 看一条空行。
    # 一次空喊话 = 全场一次空跑。**HTTP 的 /api/say 早就拒空（"内容不能为空"）**，
    # 只有 CLI 这条入口漏了 —— 同一件事两个入口判据不一致，正是本 skill 今天在查的那类病。
    if not (a.text or "").strip():
        die("✗ 喊话内容不能为空 —— 空喊话会推进用户消息序号，把所有 agent 叫起来看一条空行")
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        line = user_say(d, st, a.text, a.as_ or "用户")
        save_state(d, st)
    print(line)
    print("✓ 已投递到用户喊话通道；在线 agent 下一次心跳就会被提醒取走")
    return 0


def cmd_read_user(a):
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        ag = _get_agent(st, a.agent, now())
        msgs = parse_user(d)
        cursor = 0 if a.all else int(ag.get("user_cursor", 0))
        unread = msgs[cursor:]
        if not a.peek:
            ag["user_cursor"] = len(msgs)
        save_state(d, st)
    if not unread:
        print("（无新消息）")
        return 0
    for off, m in enumerate(unread):
        n = cursor + off + 1
        stamp = f"[{m['time']}] " if m["time"] else ""
        # 标记一律放**行尾**：原来插在 `who` 与 `：` 之间，打出来是
        # 「[#11] 用户你已回执 ：…」，读着像用户的名字叫「用户你已回执」。
        mark = "  （你已回执过）" if n in acked_user_ids(st, a.agent) else ""
        others = sorted({k.get("agent", "") for k in st.get("acks", [])
                         if k["id"] == n and k.get("agent") != a.agent})
        if others:
            # 「已被别人认领」必须看得见：今天板上有三个 agent 都能 ack 同一条，
            # 重复回执在板上完全看不出来。先做到"看得见"，再谈要不要退码拦。
            mark += "  （<" + "、".join(others) + "> 已认领）"
        print(f"[#{n}] {stamp}{m['who']}：{m['body']}{mark}")
    if a.peek:
        print(f"—— peek 模式，游标未推进（共 {len(unread)} 条）")
    return 0


def cmd_check(a):
    d = resolve_dir(a)
    a.stale_after = sane_stale(a.stale_after, d)
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        res = evaluate(d, st, a.stale_after)
        written = [] if a.readonly else emit(d, st, res, a.cooldown)
        res["alerts_unacked"] = unacked(st)
        if not a.readonly:              # --readonly 承诺不落盘（ensure_files 除外）
            st["last_scan_ts"] = res["ts"]      # 留下"有人守过"的证据
            prune_state(st)
            save_state(d, st)
    if a.json:
        print(json.dumps({"stale_after": res["stale_after"], "stale": res["stale"],
                          "post_cap": int(st.get("post_cap", POST_CAP) or 0),
                          "agents": res["agents"],
                          "user_total": res["user_total"],
                          # 时间前缀写坏、只能当纯文本收下的喊话（原文片段）：
                          # 让驱动层也能"看见降级"，而不是只看到条数对得上。
                          "user_malformed": res.get("user_malformed", []),
                          "collab": res.get("collab"),
                          "open_questions": [{"id": q["id"], "from": q["from"], "to": q["to"],
                                              "question": q["question"],
                                              "waited": int(res["ts"] - q["ts"])}
                                             for q in open_exchanges(st)],
                          "hazards": res.get("hazards", []),
                          "stale_open_after": res.get("stale_open_after"),
                          "stale_opens": res.get("stale_opens", []),
                          "unreachable_opens": res.get("unreachable_opens", []),
                          # 声明了循环型、却写了「任务完成」的身份（标签已忽略）。
                          # 放进 JSON 是为了让驱动层也能自检 —— 不必去 grep 看板文本。
                          "loop_lies": res.get("loop_lies", []),
                          "repeat_done": res.get("repeat_done", []),
                          # 阈值一并给出来 —— 否则驱动层看到 repeat_done 非空，
                          # 却不知道是按哪个时间窗判的，没法自己复核。
                          "repeat_done_window": res.get("repeat_done_window"),
                          "alerts_unacked": len(res["alerts_unacked"])}, ensure_ascii=False, indent=2))
    else:
        print(format_check(d, st, res))
        for line in written:
            print(f"\n⚠ {line}")
    # 0 的含义是"全员健康"。协作险情显然不属于健康 —— 尤其 [互相等待] 是
    # 一条真死锁：心跳全绿、没有任何进程崩溃，但整个团队已经不往前走了。
    # 如果这种状态还退 0，脚本里 `check || 告警` 就会把它整个漏掉，
    # 而它偏偏是这套东西最想抓的那类故障。
    #
    # 办事轴（stale_opens）同样计入：**「有人在等一个能来的答案」和「有人在等一个
    # 不会来的答案」是两回事** —— 后者（unreachable_opens）不计入，它不是事故，
    # 是提问者该自己结案，报出来就是噪音。这就是「两层判据缺一不可」的落点。
    unhealthy = (bool(res["stale"]) or bool(res.get("hazards"))
                 or bool(res.get("stale_opens")))
    return 1 if unhealthy else 0


# 「我每会话都会被注入」的地方 —— 也正是协议唯一真正生效的地方。
DOCTOR_MEM_CANDIDATES = (
    "~/.workbuddy/MEMORY.md",             # WorkBuddy：用户级长期记忆
    "~/.claude/CLAUDE.md",                # Claude Code 系
    "~/.config/opencode/AGENTS.md",       # opencode 全局指令
)

# 「报到义务」的祈使性措辞。**光有坐标不算数** ——
# 坐标只说明"我知道有这个工具"，义务才说明"我必须去报到"。
# 实测（2026-09-24）：坐标写进了用户级记忆，义务却只写在某个项目工作区里，
# 于是"换个目录开新会话就断"；而只查坐标的旧判据照样判 ✓，**完全看不见这个洞**。
# 刻意不收「报到」这种词：它太容易出现在**诊断句**里（"我这条线没有在报到"），
# 会让"有病"被判成"有义务"—— 假阴性可以接受，假阳性不行。
DUTY_MARKERS = (
    "每个动作后", "每次动作", "收工", "--done",
    "post --dir", "必须 post", "要 post",
)


def _literal_forms(d: Path) -> set:
    """一块看板在文本里可能出现的**字面**形态（快路径，不碰文件系统）。

    记忆/协议里一般写成 `~/…`，而 `--dir` 传的是展开后的绝对路径 —— 两种都得认。
    但**不能**只认 `/.workbuddy/work-log` 这种尾巴：那会命中任何一块板，
    于是"我有坐标"退化成一句永远为真的废话。

    ⚠️ **别直接拿它当判据。** 它只认字面量，而同一个目录可以有多种写法：
    `resolve_dir()` 总是把 `d` 归一成已解析形态，于是手写协议里那种
    "未解析拼法"**永远匹配不上**。实测（2026-09-25）：板在 `/private/tmp/x`，
    手写文本写 `/tmp/x`（macOS 上 `/tmp` 是符号链接）⇒ `onboard` 本该拒绝
    「这里已有一份指向本板的协议」，实际退 0 并**静默追加出第二份**；
    `doctor ③` 也看不见那份已经写好的坐标。判据请统一走 `_mentions_board()`。
    """
    forms = {str(d)}
    try:
        forms.add("~/" + str(d.relative_to(Path.home())))
    except ValueError:                      # 板在家目录之外（如外置盘）
        pass
    return forms


def _expand(raw: str):
    """把外部文本里的 `~` 展开成 `Path`；**展不开返回 None**（不原样保留）。

    ⚠️ 别裸露地用 `Path(raw).expanduser()`。`~` 后面只要是**非真实用户名**，
    pathlib 就抛 `RuntimeError("Could not determine home directory.")` ——
    它不是 `OSError`，`except OSError` 接不住，于是一路冒到 `main()` 变成
    **70（内部错误）**。而这句话最常出现在两种正常场景里：

      · **中文行文的约数**：真机 `~/.workbuddy/MEMORY.md` 里写着 `~90%`、`~2.4s`；
      · 用户手打**路径**时写错（`--dir '~90%'`）。

    两种都是"输入压根不是路径"，退 70 等于把用法问题伪装成工具自己坏了。

    展不开时**返回 None 而不是原样保留**，这条更要紧 —— 那是假阳性的根源：
    原样保留的 `~90%` 会被后续 `resolve()` 当成**相对路径**接到 cwd 上，
    变成 `<cwd>/~90%`，于是"板正好建在当前目录"时它与板前缀匹配 ⇒
    一句「模型延迟 ~2.4s」就能把这份记忆判成"提到了本板"。
    实测（2026-09-25）：`_is_same_dir("~90%", 板=cwd)` 返回 **True**。

    所以这里只认"能确定指向"的：展不开就 `None`，让调用方走"不知道"这条路。

    ⚠️ 也别改成按异常**文案**匹配（`"Could not determine home directory."`）：
    实测同一件事两个版本措辞不同 —— 3.9.6 是
    `Can't determine home directory for '90%'`，3.13.12 是 `Could not determine home directory.`。
    按类型捕才跨版本成立。
    """
    try:
        return Path(raw).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_same_dir(raw: str, target: Path) -> bool:
    """`raw` 指的是不是 `target` 这个目录（或它里面的东西）。

    两级判据，从**最可靠**到**最兜底**：

    ① `os.path.samefile()` —— 按 inode + 设备号比。一次覆盖符号链接、
       **大小写差异**（macOS 默认文件系统大小写不敏感，而 `resolve()` 归不了大小写 ——
       实测 `resolve("/tmp/hole/casetest")` 仍是 `casetest`，但 `samefile` 认得出
       它与 `CaseTest` 是同一个）、以及挂载别名。代价是两边都必须存在。
    ② `resolve()` 归一后比字符串 —— 给不存在的路径兜底（手写协议里常出现还没建的目录）。
       代价是它归不了大小写。

    刻意**只认"完全相同"或"在其之下"**，不认"尾巴相同" —— 见 `_literal_forms` 的注释。

    `raw` 是**从行文里猜出来的片段**（不是用户显式给的路径），所以多一道闸：
    展不开、或展开后仍**不是绝对路径**的，一律判 False。
    展不开的 `~xxx` 会原样保留成相对形态，`resolve()` 再把它接到 cwd 上 ——
    不拦就会在"板 = cwd"时产生假阳性（见 `_expand()`）。
    """
    p = _expand(raw)
    if p is None or not p.is_absolute():
        return False
    t = str(target)
    try:
        if os.path.samefile(p, target):
            return True
    except OSError:
        pass
    try:
        c = str(p.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return c == t or c.startswith(t + "/")


def _mentions_board(text: str, d: Path) -> bool:
    """这段文本有没有提到**这块板** —— 「开药」与「验药」共用的唯一判据。

    两条路，任一命中即可：

    ① 字面形态（`_literal_forms`）：快路径，不碰文件系统，覆盖绝大多数情况。
    ② 把文本里每一段像路径的 token 归一再比：修的是「同一块板、两种拼法」。
       手写 `/tmp/x` 与引擎拿到的 `/private/tmp/x` 是同一个目录，但字面量永远对不上。

    两条路的**共同前提**是"这看起来确实是个路径"：`~` 后面必须跟分隔符（见
    `_PATH_TOKEN_RE`），且必须展开得开、展开后是绝对的（见 `_expand()`）。
    行文里的约数波浪号（`~90%`）两条都不满足 ⇒ 不算提到本板。
    这个方向是刻意的：**宁可漏、不可猜** —— 漏了只是多跑一次 onboard，
    猜了会让一份根本没接上的记忆判绿。

    这个洞的代价不对称 —— 对 `onboard` 是**静默多出一份互相打架的协议**，
    对 `doctor ③` 是**看不见已经写好的坐标**。两者都不会报错。
    """
    if any(f in text for f in _literal_forms(d)):
        return True
    for m in _PATH_TOKEN_RE.finditer(text):
        if _is_same_dir(m.group(0), d):
            return True
    return False


def _is_global_mem(q: Path) -> bool:
    """这个记忆文件是不是「跨工作区、每会话都注入」的那一份。

    判据是**路径形态**，不是文件名：项目级记忆都长在
    `<某工作区>/.workbuddy/memory/MEMORY.md` 下 —— 换个工作区就是另一个文件，
    写在那里约等于写在一次性文件里。只有用户级那份换了目录还在。
    """
    parts = q.parts
    return not (".workbuddy" in parts and "memory" in parts)


def _board_ratio(d: Path):
    """看板里 watchdog 告警行 vs 实质发言行。

    比值远大于 1 ⇒ 板上已有 agent 长期离线，**真信号被自己的噪音埋了**。
    实测：某板 42 小时攒出 994 行告警 / 110 行发言（≈9:1）。
    """
    try:
        lines = p_board(d).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, 0
    wd = sum(1 for ln in lines if ln.startswith("<watchdog>"))
    real = sum(1 for ln in lines if ln.startswith("<") and not ln.startswith("<watchdog>"))
    return wd, real


def _path_on_path(cmd: str) -> str:
    """在 $PATH 里找一个可执行文件；找不到返回空串。

    刻意不用 `shutil.which`：本工具卖的「零第三方依赖」由
    `scripts/check_stdlib_only.py` 守着，但它只挡得住第三方，挡不住「顺手多
    import 一个标准库模块」。这里 os + os.path 就够了，不值得为它开一个模块。
    """
    if not cmd:
        return ""
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not d:
            continue
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def detect_toolchains() -> list:
    """探测本机装了哪些工具链，每条都带**证据**。

    命中判据只有两条，都是观测而非推测：配置目录存在（装过就跑不掉），
    或可执行文件在 $PATH 上。证据必须打出来、断言要正面命中 ——
    「我探测到了」如果不可查验，就只是把口头约定换了个地方。
    """
    found = []
    for probe in TOOLCHAIN_PROBES:
        hits = []
        for raw in probe["paths"]:
            p = Path(raw).expanduser()
            if p.exists():
                hits.append(str(p))
        exe = _path_on_path(probe["cmd"])
        if exe:
            hits.append(exe)
        found.append({**probe, "hits": hits, "found": bool(hits)})
    return found


def render_detection(found: list, include_user: bool) -> tuple:
    """打印探测表；返回 (拟落的条目, 探测到却落不了的 (条目, 原因))。

    打印与决策合在一处是刻意的：**表里写的就一定是将要发生的**。拆成两处的话，
    迟早出现「表上说了、实际没落」这种最糟的静默不一致。
    """
    plan, blocked = [], []
    for it in found:
        if not it["found"]:
            print(f"  – {it['name']:<12} 没探测到（{'、'.join(it['paths'])} 都不存在）")
            continue
        print(f"  ✓ {it['name']:<12} {it['note']}")
        print(f"    {'':<12} 证据  " + "；".join(it["hits"]))
        if not it["cross"]:
            blocked.append((it, f"本机没有跨目录落点，只能显式给 --to {it['alias']}"))
            continue
        if it["alias"] == "user" and not include_user:
            blocked.append((it, "user 档会注入**所有**工作区，默认跳过（要就说 --include-user）"))
            continue
        plan.append(it)
    return plan, blocked


def to_targets() -> dict:
    """`onboard --to` 的落点别名表。

    它回答的是**「那个 agent 启动时一定会读到哪个文件」** —— 也就是「接入任何 agent」
    这件事真正难的地方：不同工具链读的启动文件根本不是同一个，而这些差异没人记得住
    （实测翻车点几乎全在落错地方：协议写进了只对某仓库生效的 AGENTS.md，
    而当事 agent 的 cwd 不在那儿，于是它断心跳 42.4 小时）。

    只是**别名**，不是白名单：`--to <任意文件路径>` 一直可用。
    判据用「换个目录开会话还认不认」分两档 —— 带 `-global` / `user` 的跨目录，
    其余只对在该目录启动的会话生效。
    """
    cwd, home = Path.cwd(), Path.home()
    return {
        "user": (str(home / ".workbuddy" / "MEMORY.md"),
                 "WorkBuddy 用户级记忆：跨工作区、每会话注入"),
        "opencode-global": (str(home / ".config" / "opencode" / "AGENTS.md"),
                            "opencode 全局指令：**任何目录**开会话都读"),
        "claude-global": (str(home / ".claude" / "CLAUDE.md"),
                          "Claude Code 全局记忆：跨目录"),
        "repo": (str(cwd / "AGENTS.md"),
                 "仓库级：opencode / Codex / Cursor 等在**该目录**启动时读"),
        "claude": (str(cwd / "CLAUDE.md"), "Claude Code 项目级"),
        "gemini": (str(cwd / "GEMINI.md"), "Gemini CLI 项目级"),
        "cursor": (str(cwd / ".cursor" / "rules" / "work-log.mdc"), "Cursor 项目规则"),
    }


def _onboard_snippet(d: Path, agent: str) -> str:
    """接入片段本体。

    两条硬约束，缺一条就退货：

    1. **必须能让 doctor ③ 通过** —— 也就是同时满足那三层里的前两层：既有看板的
       **坐标**（绝对路径，与 doctor 的 `forms` 逐字一致），又有**报到义务**
       （DUTY_MARKERS 里的措辞）。这样 `onboard` 就是「doctor 给病人开的药」，
       一开一个准；selftest 第 35 组就是这么验的。

    2. **不许出现引擎的本机路径**（这句是 2026-09-25 补的）。片段的作用是**离开这台
       机器**：它会被抄进对方的记忆文件、被拷到别的电脑。写死
       `/Users/<某人>/…/work_log.py` 的那一刻，这段协议在第二台机器上就是一条死链，
       而且失效是静默的 —— 对方照抄、报个"文件不存在"，然后不再报到。
       现在只留**看板目录**（它本来就是数据、本来就跟着板走），启动方式交给
       板目录里那个 `worklog` 引导脚本现场解析引擎位置。
       selftest 第 36 组用「片段里不许出现 /Desktop/work-log- 或 .workbuddy/skills/work-log」
       把这条钉死。
    """
    me = agent or "<你在这块板上的名字>"
    return f"""{ONBOARD_BEGIN}
## 协作看板（work-log）：你要报到

本机有一块 agent 协作看板，别的 agent 正靠它互相通报进度：

    看板目录：{d}
    启动方式：{d / SHIM_NAME}      （这块板自带的引导脚本，它自己找引擎）

**义务：每个动作后都写一条心跳；收工必须写 `--done`。** 这是硬性的 ——
不写就等于你从未接入过（名单里有没有你的名字都不算数），看门狗会把你标成
「从未接入」或「疑似卡死」，你的沉默会以告警的形式出现在别人眼前。
`--dir` 一律显式传上面那个绝对路径：引擎**没有默认落点**（缺 --dir 会退 2 并指路），别靠猜。

```bash
D="{d}"
WL="$D/{SHIM_NAME}"        # 板自带；已把 --dir 钉死在 $D，所以换台电脑也能直接跑
"$WL" post --dir "$D" --agent {me} --tag 执行 --text "做了什么 / 结论是什么"
"$WL" brief --dir "$D" --agent {me}    # 取别人的新动态 + 谁在等我回答
"$WL" post --dir "$D" --agent {me} --tag 任务完成 --done --text "收工"
```

- **名字要钉死**：`--agent {me}` 里的名字就是「你」。如果这块板上可能有两个会话，
  别把名字留在共享文件里 —— 给每个会话各自设
  `export WORK_LOG_AGENT=<名字>`，或按目录登记一次：
  `"$WL" identity set --agent <名字>`（之后在这个目录不传 `--agent` 也能认出你）。
  两个会话用同一个名字 = 共用一份心跳和游标，板上看不出来。
- **长命令前先占位**：超过约 60s 的命令要先跑
  `... hold --agent {me} --seconds 600 --text "跑 X（约 N 分钟）"`，
  否则看门狗只看静默时长，会把正在干活的人判成疑似卡死（实测被连报两次）。
- **收到告警先分清两种**：「疑似卡死」⇒ 它多半在跑长命令（见上一条）或真卡了，看日志 / 接管；
  「判定为**离线**，不是卡死」⇒ 别去救（进程多半早没了）。**确定它不会再回来**（进程关了、
  任务砍了）⇒ `"$WL" retire --agent <它> --note "为什么判定不回来了"` 宣告离场，告警就此停；
  宣告错了它一条 post 就自动复位。
- `ask` / `reply` / `await`：`await` 退 **0**=有答复 · **1**=超时（可以自己拍板）·
  **3**=对方已收工（别等了）· **5**=**互相等待**（你在等的人正在等你 ⇒ **先回答它**）。
  这四个码的处置各不相同（1 是再等等、3 是自己拍板、**5 与 1 相反**），别混为一谈。
- **只刷心跳不算在线 —— 你还得能被叫醒。** 上面那条讲的是**你问别人**；要让别人问你时
  你真收得到，必须挂一个**唤醒器**（响应式会话没有后台轮询，没人叫就不会醒）：
  `"$WL" waker --dir "$D" --agent {me} --window-min 45`（放后台跑）。
  板上一有冲你来的事（用户新喊话 / 有人 `ask` 到你）它就**退出** —— 退出即唤醒你这个会话；
  **它不替你回话**。窗口到点会自己退出，你被唤醒后重新挂上，这就是「保持在线」的续期循环
  （靠的是这个循环，不是某个进程永不退出）。
  **醒来第一件事**：`"$WL" brief --dir "$D" --agent {me} --peek` 查有没有「待你回应」，
  收尾再 `read-user` 取用户喊话 —— 漏了这一步，别人 `ask` 到你就是**断的，而且不报错**。
- 退出码：**2**=参数错 · **3**=抢锁失败/对方已收工 · **4**=熔断（写太快）· **70**=工具坏了。
- 动共享资源（文件、端口、数据库）前先 `lock`，做完 `unlock`。
- **拿不到对方的回答就如实上报、并允许自己拍板；编造对方的话是明确禁止的。**
{ONBOARD_END}
"""


def _onboard_target(tgt: Path, block: str, d: Path, force: bool, dry: bool = False) -> dict:
    """落点写入的**唯一**实现：判据、备份、写盘全在这里。

    `dry=True` 只走到「判据通过、算出新内容」为止，**一个字节都不碰磁盘**。
    `--to auto` 靠它做**预检**：先在内存里把所有落点验一遍，任何一个会被拒就
    一个文件都不写。批量落盘最怕留下「落了一半」——半落状态比全不落更难收拾，
    而且没人知道哪几处生效了。

    读盘只发生一次（`old` 同时供判据和备份使用），所以读与写之间不存在第二个
    时间点，也就没有 TOCTOU 的余地。
    """
    if tgt.is_dir():
        die(f"✗ 落点指向的是目录：{tgt}\n"
            f"  给一个文件路径，或者用 {' / '.join(to_targets())}")
    if tgt.exists() and tgt.resolve() == Path(__file__).resolve():
        die("✗ 落点就是工具自己 —— 片段写进 work_log.py 会把脚本弄坏")

    old = ""
    if tgt.exists():
        try:
            old = tgt.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            die(f"✗ 读不动 {tgt}：{e}")

    has_block = ONBOARD_BEGIN in old and ONBOARD_END in old
    # 没标记块、这份文件却已经在讲**同一块看板**（给了这个板的坐标，或提过工具名）
    # ⇒ 多半是**手写**的协议：默认拒绝。硬追加会让两份协议互相打架，而且没人知道
    # 哪份是权威 —— 比不做更糟。
    # ⚠ 判据不能只认字面量 "work-log"：实测手写的那份只写了「看板：<路径>」，
    #   压根没提工具名，于是被静默追加成两份（自测第 35 组抓到的真洞）。
    if old and not has_block and not force:
        if _mentions_board(old, d) or "work-log" in old or "work_log" in old:
            die(f"✗ {tgt} 里已经有一份指向这块看板的协议，但它不是 onboard 维护的"
                "（没有标记块）。\n"
                "  直接追加会出现两份互相打架的协议，而且没人知道哪份是权威。\n"
                "  先手工合并；确认就是要追加时再加 --force。")

    if has_block:
        head, rest = old.split(ONBOARD_BEGIN, 1)
        _mid, tail = rest.split(ONBOARD_END, 1)
        new = head + block + tail.lstrip("\n")
        action = "替换原标记块"
    else:
        sep = "" if (not old or old.endswith("\n\n")) else ("\n" if old.endswith("\n") else "\n\n")
        new = old + sep + block
        action = "新增"

    if dry:
        return {"action": action, "bak": None, "size": len(new), "wrote": False}

    bak = None
    if tgt.exists():
        # 备份内容直接用**已经读进内存的 `old`**，不再去 copy 一遍：
        # ① 不引额外模块（本工具零依赖，自测里那条断言是真护栏）；
        # ② 读盘与备份之间没有第二个时间点，也就没有 TOCTOU 的余地。
        bak = tgt.with_name(tgt.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        try:                                     # 备份失败就中止，绝不留半个改动的文件
            bak.write_text(old, encoding="utf-8")
        except OSError as e:
            die(f"✗ 备份失败，已中止：{e}")

    try:
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.write_text(new, encoding="utf-8")
    except OSError as e:
        die(f"✗ 写不动 {tgt}：{e}")

    return {"action": action, "bak": bak, "size": len(new), "wrote": True}


def cmd_onboard(a):
    """把「接入片段」生成 / 写进**对方真的会读**的文件里。

    默认**只打印不落盘**（等于 dry-run）；给了 `--to` 才写，且写是幂等的：
    标记块内替换、块外一字不动。

    刻意**不做**「自动发现对方是谁、它读哪个文件」—— 那要去翻对方二进制里的
    字符串（SKILL.md 里那一步）。猜错落点比不猜更糟：它会产出一段看起来完备、
    实际永远到不了对方手里的协议，也就是本节存在的那个历史事故。这里只承诺
    一件事：**给我一个落点，我给你一段一定能通过 doctor ③ 的协议。**
    """
    d = resolve_dir(a, mkdir=False)      # **不建目录**：默认只打印，就得是干净的读
    wl = Path(__file__).resolve()
    if a.agent:
        check_agent_name(a.agent)
    block = _onboard_snippet(d, a.agent or "")
    uninit = not p_state(d).exists()
    no_shim = not p_shim(d).exists()
    targets = to_targets()

    # 只探测、不写任何文件。存在的理由是「换台机器」：新机器上你本来也不知道装了什么，
    # 而这个判断必须能在**不落任何盘**的前提下做出来 —— 否则"先看看"就成了"先改了"。
    if getattr(a, "detect", False):
        print("—— 落点自动探测（本机）——")
        plan, blocked = render_detection(detect_toolchains(), a.include_user)
        print()
        for it, why in blocked:
            print(f"  ⚠ {it['name']}：{why}")
        if plan:
            print(f"  可自动落 {len(plan)} 处：" + "；".join(
                str(Path(targets[it["alias"]][0]).expanduser()) for it in plan))
            print("  真去落盘：onboard --to auto"
                  + ("" if a.include_user else "（要连 user 档一起落：--include-user）"))
        else:
            print("  ⚠ 没有可自动落的跨目录落点 —— 显式指定：--to " + " / ".join(targets))
        return EXIT_OK

    to = (a.to or "").strip()
    if not to:
        print(block)
        print("── 以上仅打印（默认不落盘）。写入用：--to <落点>")
        print("   落点（「那个 agent 启动时一定会读到哪个文件」）：")
        for k, (path, note) in targets.items():
            print(f"     {k:<17}{path}")
            print(f"     {'':<17}{note}")
        print("   也可以直接给一个文件路径。")
        print("  ⚠ 三个必踩的坑：① 往仓库跑 /init 会覆盖 AGENTS.md；")
        print("     ② **已开着的会话不会捡起新写的文件**，对方要新开会话才生效；")
        print("     ③ 写在某个仓库里的协议只对**在该目录启动**的 agent 生效 ——")
        print("        换个目录开会话就断，所以跨工具链优先用 user / opencode-global。")
        if uninit:
            print(f"  ⚠ 这块板还没 init（{p_state(d)} 不存在）—— 对方照片段 post 会当场把板建出来，"
                  "但你先跑一次 init 更稳。")
        if no_shim:
            print(f"  ⚠ 板里还没有引导脚本（{p_shim(d)}）—— 片段里让它调 "
                  f"`{d / SHIM_NAME}`，跑一次 init（或让对方 post 一次）会补上。")
        return EXIT_OK

    if to == "auto":
        # 探测 → 预检 → 落盘。顺序不能换：预检的意义就是**一个文件都不写**之前
        # 就发现"其中某一处会被拒"，否则批量落盘会留下半落状态。
        print("—— 落点自动探测（--to auto）——")
        plan, blocked = render_detection(detect_toolchains(), a.include_user)
        print()
        for it, why in blocked:
            print(f"  ⚠ {it['name']}：{why}")
        if not plan:
            die("✗ 一个可自动落的跨目录落点都没探测到 —— **没有写任何文件**。\n"
                "  「探测不到」不是用法错误，是这台机器上确实没有能自动落的工具链。\n"
                "  三条路任选：\n"
                "    ① 先看看这台机器上到底有什么：onboard --detect\n"
                f"    ② 显式给落点：--to {' / '.join(targets)}\n"
                "    ③ 想连 user 档一起落：--include-user",
                EXIT_TIMEOUT)

        def _tgt_of(item):
            return Path(targets[item["alias"]][0]).expanduser()

        for it in plan:                              # 预检：只读不写
            _onboard_target(_tgt_of(it), block, d, a.force, dry=True)
        print()
        for it in plan:
            tgt = _tgt_of(it)
            res = _onboard_target(tgt, block, d, a.force)
            print(f"✓ {it['name']} ⇒ {res['action']}：{tgt}（{res['size']} 字）")
            if res["bak"]:
                print(f"  改前备份：{res['bak']}")
        print(f"\n—— 共落 {len(plan)} 处 ——")
    else:
        if to in targets:
            tgt = Path(targets[to][0]).expanduser()
        else:
            tgt = _expand(to)
            if tgt is None:
                # 同 `--dir`：展不开 = 路径写错，退 2 而不是 70。
                die(f"--to 这个路径展不开：{to!r}\n"
                    "  `~` 后面只能是当前用户；要指家目录请直接写 `~/…` 或绝对路径。")
        res = _onboard_target(tgt, block, d, a.force)
        print(f"✓ 接入片段已{res['action']}：{tgt}（{res['size']} 字）")
        if res["bak"]:
            print(f"  改前备份：{res['bak']}")
        if tgt.name == "MEMORY.md" and res["size"] > 3800:
            print(f"  ⚠ 这个记忆文件已 {res['size']} 字、逼近 4000 字上限 ——"
                  " 建议只留「坐标 + 义务」那几行，其余按主题挪出去。")
    print(f"  自查（跑完这条必须绿）："
          + (f"{p_shim(d)} doctor" if not no_shim else f"python3 {wl} --dir {d} doctor")
          + (f" --agent {a.agent}" if a.agent else ""))
    print("  ⚠ 已开着的会话不会捡起新写的文件 —— 对方要**新开会话**才生效。")
    print(f"  钉名字：让那个 agent 的会话设 `export {AGENT_ENV}="
          f"{a.agent or '<名字>'}`，或按目录登记 `{d / SHIM_NAME} identity set --agent <名字>`。")
    if uninit:
        print(f"  ⚠ 这块板还没 init（{p_state(d)} 不存在）—— 对方照片段 post 会当场把板建出来，"
              "但你先跑一次 init 更稳。")
    if no_shim:
        print(f"  ⚠ 板里还没有引导脚本（{p_shim(d)}）—— 片段里让它调 "
              f"`{d / SHIM_NAME}`，跑一次 init（或让对方 post 一次）会补上。")
    return EXIT_OK


def cmd_prune(a):
    """把看板里**过老的历史**挪进 `archive/` —— 让这几个文件不无限长。默认只报告。

    为什么要有它：`board.md` / `alerts.md` / `state.alerts` 全是**只增不减**的。
    实测真看板（2026-09-28）：board.md 606KB、alerts.md 400KB、state.json 89KB
    —— 其中 alerts 一项占 47KB，因为每条都带着 ~230 字节的长中文正文。
    工具自己 doctor ⑤ 会报「告警 : 实质发言 ≈ 5:1 —— 真信号已被自己的噪音埋掉」，
    却**原来没有任何手段处置**，只能眼看它涨。所以补这个。

    三条规则（都只动"历史"，不动"当前"）：

      ① `state.alerts`：**已确认**且超过 `--days` 天的整条丢掉（那是历史审计痕迹，
         `alerts.md` 里还有）；已确认但超过 1 天的把长正文截短 —— 只留 `→` 之前
         那半句（`→` 之后是「该怎么办」的模板话，确认过就没用了）。
         **没确认的永不触碰** —— 那是待办，不是噪音。
      ② `alerts.md`：只留最近 `--keep-alerts-md` 条，其余**移进** `archive/`。
        （这份文件没有日期分段，只有 HH:MM:SS，所以只能按条数切，不能按天切。）
      ③ `board.md`：把 `--days` 天之前的**整段日期块**移进 `archive/`。

    **为什么按整段日期块搬是安全的**：解析器（`board_entries`）靠每一段自己的
    `## YYYY-MM-DD` 定位，搬走一整段不会让剩下任何一条时间算错；
    而 `user_cursor` 是 `user.md` 的**消息索引**，与 board 行号无关。
    反过来，如果按"行数"随便截，剩下那些没有日期段的条目时间就会错 —— 所以不这么干。

    ⚠ **但"时间算得对"不等于"没人受影响"**：每个 agent 的 `board_cursor` 存的是
    `len(entries)` —— 一个**数组下标**。板头少掉 N 条后，下标还指着原来的位置 ⇒
    `entries[bc:]` 变空 ⇒ 那个 agent **从此再也收不到任何新动态，且不报任何错**。
    所以搬历史时必须同步前移所有游标（`--apply` 时自动做，报告里会写移了几个）。

    ⚠ 另一条：整个"读板 → 改 → 写回"必须在**同一把锁**里（`post` 的追加也在锁内）。
    否则在这个窗口里恰好追加的那条心跳会被旧内容覆盖掉 —— 它自己收到 ✓，板上却没有。

    默认**只报告**；确认无误再加 `--apply`。归档是搬家（写进 `archive/`），不是删除。
    """
    d = resolve_dir(a, mkdir=False)
    if not (d / "state.json").exists():
        die(f"板不存在或还没 init：{d}")
    try:
        days = float(a.days)
    except (TypeError, ValueError):
        die("--days 要是数字（天）")
    if days < 0:
        die("--days 不能为负")
    try:
        keep_md = int(a.keep_alerts_md)
    except (TypeError, ValueError):
        die("--keep-alerts-md 要是整数")
    if keep_md < 0:
        die("--keep-alerts-md 不能为负")
    apply = bool(getattr(a, "apply", False))
    adir = d / ARCHIVE_DIR

    rep = {"dir": str(d), "apply": apply, "stale_days": days,
           "keep_alerts_md": keep_md, "items": []}

    nowt = now()
    cut_drop = nowt - days * 86400
    cut_shrink = nowt - 86400
    cut_date = (date.fromisoformat(today()) - timedelta(days=int(days))).isoformat()

    # ⚠ 三件事必须在**同一把锁**里做完。理由不是性能，是正确性：
    #   `post` 的"读板 → 追加一行 → 写回"也在锁内。如果 prune 在锁外
    #   `read_text` 完之后有个 post 恰好追加了一条，prune 再把**旧的**内容写回去，
    #   那条心跳就被**静默吃掉**了 —— 它自己收到了 ✓，板上却没有。
    #   （同族错误：读到的版本被后来的写覆盖，且没有任何一方报错。）
    with locked(d):
        st = load_state(d)
        st_dirty = False

        # ---- ① state.alerts：已确认的老告警瘦身 ----------------------------
        al = st.get("alerts") or []
        before_bytes = len(json.dumps(al, ensure_ascii=False))
        kept, dropped, shrunk = [], 0, 0
        for rec in al:
            try:
                ts = float(rec.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            if not rec.get("acked_by"):
                kept.append(rec)                     # 没确认的=待办，永不碰
                continue
            if ts < cut_drop:
                dropped += 1
                continue
            txt = rec.get("text")
            if ts < cut_shrink and isinstance(txt, str) and len(txt) > ALERT_TEXT_KEEP:
                # 只留 `→` 之前的"事实"那半句（见 ALERT_TEXT_KEEP 的注释）。
                head_txt = txt.split("→", 1)[0].rstrip() or txt
                if len(head_txt) > ALERT_TEXT_KEEP:
                    head_txt = head_txt[:ALERT_TEXT_KEEP]
                rec["text"] = head_txt + ALERT_TEXT_MARK
                shrunk += 1
            kept.append(rec)
        after_bytes = len(json.dumps(kept, ensure_ascii=False))
        if apply and (dropped or shrunk):
            st["alerts"] = kept
            st_dirty = True
        rep["items"].append({
            "what": "state.alerts（已确认的）",
            "drop": dropped, "shrink": shrunk,
            "bytes_before": before_bytes, "bytes_after": after_bytes,
            "moved_to": None})

        # ---- ② alerts.md：只留最近 N 条，其余搬进 archive/ -----------------
        af = p_alerts(d)
        if af.exists():
            raw = af.read_text(encoding="utf-8", errors="replace")
            lines = raw.split("\n")
            head = [ln for ln in lines if ln.startswith("#") or ln.startswith(">")]
            body = [ln for ln in lines if ln.strip() and not ln.startswith(("#", ">"))]
            n_move = max(0, len(body) - keep_md)
            moved, leftover = body[:n_move], body[n_move:]
            arch = adir / f"alerts-rotated-{today()}.md"
            tail_txt = "\n".join(head + leftover) + "\n"
            if apply and moved:
                adir.mkdir(parents=True, exist_ok=True)
                with open(arch, "a", encoding="utf-8") as fh:
                    fh.write(f"\n<!-- 归档自 alerts.md · {today()} -->\n")
                    fh.write("\n".join(moved) + "\n")
                af.write_text(tail_txt, encoding="utf-8")
            rep["items"].append({
                "what": "alerts.md", "drop": n_move, "shrink": 0,
                "bytes_before": len(raw.encode("utf-8")),
                "bytes_after": len(tail_txt.encode("utf-8")),
                "moved_to": str(arch) if moved else None})

        # ---- ③ board.md：搬走 --days 天之前的**整段日期块** ----------------
        bf = p_board(d)
        if bf.exists():
            braw = bf.read_text(encoding="utf-8", errors="replace")
            blines = braw.split("\n")
            starts = [i for i, ln in enumerate(blines) if DATE_RE.match(ln.strip())]
            moved_sections, keep_lines = [], []
            if starts:
                keep_lines = blines[:starts[0]]        # 板头（任务/协议那几句）
                for k, s0 in enumerate(starts):
                    s1 = starts[k + 1] if k + 1 < len(starts) else len(blines)
                    sec = blines[s0:s1]
                    sec_date = DATE_RE.match(blines[s0].strip()).group(1)
                    # 最后一段（今天那段）无论如何留着；否则就按天数切
                    if sec_date < cut_date and k != len(starts) - 1:
                        moved_sections.append((sec_date, sec))
                    else:
                        keep_lines.extend(sec)
            arch2 = adir / f"board-rotated-{today()}.md"
            tail_txt2 = "\n".join(keep_lines).rstrip("\n") + "\n"
            # 搬走的**条目**条数（不是行数）—— 下面修游标要用它。
            n_entries = sum(1 for _dt, sec in moved_sections
                            for ln in sec if ENTRY_RE.match(ln.strip()))
            shifted = 0
            # ⚠ 必须跟着搬走的历史**同步前移**每个 agent 的 `board_cursor`。
            #   它是 `board_entries()` 的**数组下标**（存的是 `len(entries)`），
            #   不是时间戳。板头少掉 N 条后，原来读到 2000 的 agent 游标还指着
            #   2000，而板只剩 200 条 ⇒ `entries[2000:]` 永远是空 ⇒
            #   **它从此再也收不到任何新动态，而且不报任何错**。
            #   （这就是"按整段日期块搬很安全"这句话的例外：时间戳安全，
            #     下标不安全。实测真看板 2606 行 / 1800 条属历史。）
            #   试算时**也要数**（否则报告写"前移 0 个"，让人以为没事）。
            for ag in (st.get("agents") or {}).values():
                if not isinstance(ag, dict):
                    continue
                bc = int(ag.get("board_cursor", 0) or 0)
                if bc <= 0:
                    continue
                nb = max(0, bc - n_entries)
                if nb != bc:
                    shifted += 1
                    if apply:
                        ag["board_cursor"] = nb
            if apply and moved_sections:
                adir.mkdir(parents=True, exist_ok=True)
                with open(arch2, "a", encoding="utf-8") as fh:
                    for _dt, sec in moved_sections:
                        fh.write("\n".join(sec).rstrip("\n") + "\n")
                bf.write_text(tail_txt2, encoding="utf-8")
                if shifted:
                    st_dirty = True
            rep["items"].append({
                "what": "board.md（整段日期块）",
                "drop": sum(len(s) for _dt, s in moved_sections), "shrink": 0,
                "unit": "行",
                "entries": n_entries, "cursors": shifted,
                "bytes_before": len(braw.encode("utf-8")),
                "bytes_after": len(tail_txt2.encode("utf-8")),
                "moved_to": str(arch2) if moved_sections else None,
                "sections": [dt for dt, _ in moved_sections]})

        if apply and st_dirty:
            save_state(d, st)

    # ---- 报告 --------------------------------------------------------------
    if getattr(a, "json", False):
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"{'（已动手）' if apply else '（试算，未改动任何文件）'} prune · {d}")
    print(f"  规则：已确认告警留 {days:g} 天；alerts.md 留最近 {keep_md} 条；"
          f"board.md 搬走 {days:g} 天前的日期块")
    tot = 0
    for it in rep["items"]:
        if not it["drop"] and not it["shrink"]:
            print(f"  · {it['what']}：无需处理")
            continue
        parts = []
        if it["drop"]:
            parts.append(f"搬走/清掉 {it['drop']} {it.get('unit', '条')}")
        if it["shrink"]:
            parts.append(f"截短 {it['shrink']} 条的正文")
        b0, b1 = it["bytes_before"], it["bytes_after"]
        size = f"（{b0} 字节" + (f" → {b1} 字节）" if b1 is not None else "）")
        print(f"  · {it['what']}：{'，'.join(parts)} {size}")
        if it.get("sections"):
            print(f"      日期块：{', '.join(it['sections'])}")
        if it.get("entries"):
            print(f"      搬走 {it['entries']} 条心跳 ⇒ 同步前移 {it['cursors']} 个 agent 的"
                  f"读取游标（不然它们会静默收不到新动态）")
        if it["moved_to"]:
            print(f"      → {it['moved_to']}")
        tot += it["drop"]
    if not apply and tot:
        print("  确认没问题就加 `--apply` 真动手（**搬家不是删除**：历史都在 archive/ 里）")
    elif not tot:
        print("  干净，没有需要归档的东西。")
    return EXIT_OK


def cmd_doctor(a):
    """「我这侧的拉取点在哪？」—— 把「协议要落在对方**真的会读**的文件里」这条规矩，
    **对称地**用在自己身上。只读：不建目录、不写任何文件。

    为什么需要它：一个协作看板最常见的失效**不是引擎坏了**，而是协议从来没到过
    某个 agent 手里 —— 板建好、serve 起上、协议写进**对方**的 `AGENTS.md`，
    然后自己 42 小时没写一条心跳（实测）。而 `AGENTS.md` **只对在该仓库启动的
    会话生效**：cwd 在别处的 agent 永远读不到它。所以「我到底被拉起来了没有」
    必须能当场验，不能靠"应该会看到"。
    """
    # doctor 自己不调 load 前先走 resolve_dir(mkdir=False)：与所有命令**同一个解析路径**，
    # 展不开退 2、缺 --dir 退 2 并指路 —— 诊断命令不该自己另养一套路径算法。
    d = resolve_dir(a, mkdir=False)
    stale_after = sane_stale(a.stale_after, d)
    # 名字用**非致命**解析：诊断命令的职责是把现状说出来，不是当场退出。
    # 而且能自动认出来时本来就不该逼人再写一遍 --agent。
    me, me_why = _try_resolve(a, d)
    print(f"work-log doctor · {hhmmss(now())}")
    print(f"看板目录：{d}")
    print(f"卡死阈值：{int(stale_after)}s（判「我还在不在报到」用它）")
    if not p_state(d).exists():
        print("  ✗ 这里没有 state.json —— 板还没 init，或 --dir 指错了。")
        print("    引擎**没有默认落点**：必须显式 `--dir <板目录>`（或 $WORK_LOG_DIR / 板自带 ./worklog）。")
        return EXIT_USAGE
    st = load_state(d)
    t = now()
    owner = str(st.get("cwd") or "")
    print(f"任务    ：{st.get('task') or '(未命名)'}")
    print(f"板主 cwd：{owner or '(未记录)'}")
    print(f"我是    ：{me or '(没定下来 —— 见 ⑥)'}"
          + (f"   [{me_why}]" if me and me_why else ""))
    print(f"引导    ：{p_shim(d)}"
          + ("" if p_shim(d).exists()
             else "   ⚠ 不存在（执行一次 init，或让任何 agent post 一次即可补上）"))
    bad = 0

    # ① 同一项目会不会有两块板：早期按默认值落桌面 + 后来 --dir 落仓库内。
    #    在废板上排查是真实踩过的坑（对着 40 小时前的两条心跳下结论）。
    #    判据不能只看目录名 —— 废板的目录名取的是**当时那个 cwd 的 basename**，
    #    与现在的板常常不同（实测：桌面那块板的目录名与活板 cwd 完全不同）。
    mynames = set(st["agents"])
    twins = []
    desk = Path.home() / "Desktop" / DEFAULT_DIRNAME
    if desk.is_dir():
        for sub in sorted(desk.iterdir()):
            if not sub.is_dir() or sub.resolve() == d or not p_state(sub).exists():
                continue
            try:
                other = load_state(sub)
            except (Exception, SystemExit):        # 坏 state 不该让体检本身崩掉
                continue
            same_cwd = bool(owner) and str(other.get("cwd") or "") == owner
            shared = mynames & set(other.get("agents") or {})
            if not (same_cwd or shared):
                continue
            try:
                mt = max((q.stat().st_mtime for q in sub.glob("*.md")), default=0.0)
            except OSError:
                mt = 0.0
            why = "同一个 cwd" if same_cwd else f"板上是同一批人（{', '.join(sorted(shared))}）"
            twins.append((sub, mt, why))
    if twins:
        bad += 1
        print("\n① 同项目第二块看板：✗ 有废板（在那里下的结论可能是几小时前的）")
        for sub, mt, why in twins:
            print(f"     {sub}   最后写入 {hhmmss(mt)}   （判据：{why}）")
        print("     ⇒ 以本目录为准；给 agent 写指令时一律显式 --dir")
    else:
        print("\n① 同项目第二块看板：✓ 未发现同项目的桌面废板")

    # ② 我的 cwd 里有没有指令文件。没有 ⇒ 任何"写在别人仓库里的协议"对我都无效。
    here = Path.cwd()
    found = [q.name for q in (here / "AGENTS.md", here / "CLAUDE.md", here / "CONTEXT.md")
             if q.is_file()]
    if found:
        print(f"② 当前 cwd 的指令文件：✓ {', '.join(found)}")
    else:
        bad += 1
        print(f"② 当前 cwd 的指令文件：✗ {here} 下没有 AGENTS.md / CLAUDE.md / CONTEXT.md")
        print("     ⇒ 写在别的仓库里的那份协议对我**永远不生效**"
              "（AGENTS.md 只对在该仓库启动的会话生效）")

    # ③ 真正决定成败的一条：每会话会被注入的地方，有没有这条通道的**坐标 + 义务**。
    #    三层判据，缺一不可 ——
    #      · 坐标：记忆里一般写成 `~/…` 而不是展开后的绝对路径，两种形态都要认；
    #        但**不能**只认 `/.workbuddy/work-log` 这种尾巴：那会命中任何一块板，
    #        于是"我有坐标"变成一句永远为真的废话。
    #      · 义务：**光有坐标不算数**。坐标说明"我知道有这个工具"，
    #        义务才说明"我必须去报到"。实测 2026-09-24：坐标写进了用户级记忆，
    #        义务却只写在某个项目工作区里 ⇒ 换个目录开会话就断；
    #        而只查坐标的旧判据照样判 ✓ —— 判据本身看不见这个洞，这才是最该记的。
    #      · 落点：只有**跨工作区**那份（如 ~/.workbuddy/MEMORY.md）换了目录还在；
    #        项目级那份长在 <某工作区>/.workbuddy/memory/ 下，换工作区就是另一个文件。
    rows = []          # (path, 有坐标, 有义务, 是否跨工作区)
    cand = [Path(x).expanduser() for x in DOCTOR_MEM_CANDIDATES]
    if owner:
        cand.append(Path(owner) / ".workbuddy" / "memory" / "MEMORY.md")
    cand.append(here / ".workbuddy" / "memory" / "MEMORY.md")
    seen = set()
    scanned = 0
    for q in cand:
        if str(q) in seen or not q.is_file():
            continue
        seen.add(str(q))
        scanned += 1
        try:
            body = q.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        has_path = _mentions_board(body, d)
        has_duty = has_path and any(w in body for w in DUTY_MARKERS)
        if has_path or "work_log.py" in body or "work-log" in body:
            rows.append((q, has_path, has_duty, _is_global_mem(q)))

    ok = [r for r in rows if r[1] and r[2]]           # 坐标 + 义务都齐
    no_duty = [r for r in rows if r[1] and not r[2]]  # 有坐标、没义务
    only_tool = [r for r in rows if not r[1]]         # 只提过这个工具
    gok = [r for r in ok if r[3]]
    if ok:
        print(f"③ 通道坐标 + 报到义务：✓ {len(ok)} 处齐备"
              f"（共扫描 {scanned} 个记忆 / 指令文件）")
        for q, _hp, _hd, isg in ok:
            print(f"     {q}   [{'跨工作区（换目录也认）' if isg else '仅本工作区'}]")
        if not gok:
            # 刻意**不**计 bad：当前工作区是能连上的，不算"现在就失效"。
            # 但这正是最容易踩的洞（换个目录开新会话就断），所以必须显式打出来，
            # 并由 selftest 断言这行文字存在 —— 否则它会变成没人看的装饰。
            print("     ⚠ 但**跨工作区**那一份里没有它 —— 换个目录开新会话就断。")
            print("       项目级记忆长在 <某工作区>/.workbuddy/memory/ 下，换工作区就是另一个文件；")
            print("       补进 ~/.workbuddy/MEMORY.md（用户级：跨工作区、每会话必注入）。")
    elif no_duty:
        bad += 1
        print(f"③ 通道坐标 + 报到义务：⚠ 有坐标、**没有报到义务**"
              f"（共扫描 {scanned} 个文件）")
        for q, _hp, _hd, _isg in no_duty:
            print(f"     {q}")
        print("     ⇒ 这只说明「我知道有这个工具」，不说明「我必须去报到」—— 等于没有。")
        print(f"       补上：`{d}` + 我在板上的名字 + 「每个动作后 post / 收工 --done」。")
    elif only_tool:
        bad += 1
        print(f"③ 通道坐标 + 报到义务：⚠ 只提到工具、**没有这块板的坐标**"
              f"（共扫描 {scanned} 个文件）")
        for q, _hp, _hd, _isg in only_tool:
            print(f"     {q}")
        print("     ⇒ 通道只存在于「我造过的工具清单」里，不在我的义务里 —— 等于没有。")
        print(f"       把 `{d}` + 我在板上的名字 + 「每个动作后 post」写进上面那个文件。")
    else:
        bad += 1
        print(f"③ 通道坐标 + 报到义务：✗ 一处都没有（共扫描 {scanned} 个记忆 / 指令文件）")
        print(f"     ⇒ 把 `{d}` + 我在板上的名字 + 「每个动作后 post」写进**每会话都被注入**的那个文件。")
        print("       （WorkBuddy 系：~/.workbuddy/MEMORY.md 与 <工作区>/.workbuddy/memory/MEMORY.md）")

    # ④ 板上每个人现在什么状态 —— 「我」单独标出来
    print("\n④ 板上的人（判在线只看 last_seen）：")
    if not st["agents"]:
        print("     （一个都没有）")
    for name in sorted(st["agents"]):
        ag = st["agents"][name]
        last = float(ag.get("last_seen", 0.0))
        sil = max(0.0, t - last)
        rep = int(ag.get("alert_repeat", 0))
        note = f"  连续告警 {rep} 次（已退避）" if rep >= ALERT_BACKOFF_AFTER else ""
        star = "  ← 我" if name == me else ""
        print(f"     <{name}> 最后心跳 {fmt_dur(sil)}前（{hhmmss(last)}）"
              f"  entries={ag.get('entries', 0)}{note}{star}")
        if name == me and sil > stale_after and not ag.get("expected_silence_until", 0) > t:
            bad += 1
            print("          ✗ 静默已过阈值 —— 我这条线**没有在报到**。这就是本命令要抓的病。")

    # ⑤ 告警 / 实质发言 比值
    wd, real = _board_ratio(d)
    ratio = (wd / real) if real else float(wd)
    if wd >= 20 and ratio >= 3:
        bad += 1
        print(f"\n⑤ 告警 / 实质发言：✗ {wd} : {real}（≈{ratio:.1f}:1）—— 真信号已被自己的噪音埋掉")
        print("     ⇒ 补一条 post 会自动撤回并关闭该 agent 的全部待确认告警"
              "（实测一次关掉 127 条）。")
    else:
        print(f"\n⑤ 告警 / 实质发言：✓ {wd} : {real}")

    # ⑥ 名字固定住了没有 —— 「我知道我叫什么」不能是一次性的。
    #    为什么单列一条：③ 保证的是「我能找到这块板」，⑥ 保证的是「我在这块板上
    #    有**专属**身份」。两者可以各自独立地成立/失效，混在一起会互相遮住 ——
    #    实测：通道全绿，两个会话却共用一个名字，心跳与游标混在一起，板上看不出来。
    #    判据刻意只把「连自己叫什么都定不下来」算病：显式传 --agent 是能当场工作的，
    #    只是换了会话要重来 —— 那是提醒，不是故障（与 ④ 的静默判定同一套口径）。
    ids = load_identities(d)
    print("\n⑥ 名字固定：")
    if not me:
        bad += 1
        print(f"     ✗ 定不下我在板上叫什么（没给 --agent，${AGENT_ENV} 没设，"
              f"板上也没登记本目录）")
        print(f"       ⇒ 任选一条：`--agent <名字>` · `export {AGENT_ENV}=<名字>` · "
              f"`{p_shim(d)} identity set --agent <名字>`")
    else:
        rec = ids.get(me) or {}
        rc_cwd = str(rec.get("cwd") or "")
        if rc_cwd == _my_cwd():
            print(f"     ✓ {me} —— 板上登记的就是这个目录，换个会话也认得出"
                  "（**按目录**认的，同一个目录开两个会话仍要显式区分）")
        elif os.environ.get(AGENT_ENV):
            print(f"     ✓ {me} —— 来自 ${AGENT_ENV}，这个会话钉死了")
        else:
            print(f"     ⚠ {me} —— 这次是显式传进来的，板上没有「{me} ← 本目录」这条登记"
                  f"（现有：{rc_cwd or '没登记目录'}）")
            print("       换个会话、或哪次忘了写 --agent，就认不出来（会退 2）。钉死任选一条：")
            print(f"         export {AGENT_ENV}={me}                      # 这个会话")
            print(f"         {p_shim(d)} identity set --agent {me}   # 这个目录")
    clash = {}
    for n2, v2 in ids.items():
        c2 = str((v2 or {}).get("cwd") or "")
        if c2:
            clash.setdefault(c2, []).append(n2)
    for c2, n2s in sorted(clash.items()):
        if len(n2s) > 1:
            print(f"     ⚠ {c2} 上登记了 {len(n2s)} 个名字（{', '.join(sorted(n2s))}）——"
                  " 目录分不开它们，那两个会话必须显式传 --agent")

    # ⑦ 同一个名字下有几个**实例**在写 —— 「多个同名工具」这一类。
    #    为什么单列：⑥ 管的是「我有没有专属名字」，管不了「开着两个 opencode
    #    却都叫 opencode」。那种情况 ⑥ 全绿（名字确实固定住了），
    #    但两个实例共用 last_seen / 用户喊话游标 / 心跳预算 —— 一个死了另一个
    #    照样刷新（看门狗测不到），用户喊话会被先读到的人取走（另一个永远看不到）。
    #    它的 cwd 往往完全相同，所以 `identity set` 的目录冲突**也**挡不住它。
    #    判据用状态里记的实例集合（`post` 时留痕），不猜、不看进程表。
    print("\n⑦ 同名多实例（多个同名工具共用一个身份？）：")
    multi = {n2: sorted((v2 or {}).get("instances") or {})
             for n2, v2 in st["agents"].items()
             if len((v2 or {}).get("instances") or {}) > 1}
    if multi:
        bad += 1
        for n2, insts in sorted(multi.items()):
            print(f"     ✗ <{n2}> 下有 {len(insts)} 个实例在写：")
            for i2 in insts:
                print(f"         {i2}")
        print("       ⇒ 它们会共用 last_seen / 用户喊话游标 / 心跳预算，**板上看不出来**。")
        print(f"         给每个实例起不同名字（在各自启动器里钉死）："
              f"`export {AGENT_ENV}=<工具>-<实例名>`，如 opencode-eval / opencode-ui。")
    else:
        n_known = sum(1 for v2 in st["agents"].values() if (v2 or {}).get("instances"))
        if n_known:
            print(f"     ✓ 没有同名多实例（{n_known} 个名字的实例标识已记下）")
            print(f"       想知道本机能不能认出宿主，加 --explain 看父链逐层判定")
        else:
            print("     · 还没有实例标识可查（新板，或探测不到宿主进程）")

    # ⑦b 「我这台机器上到底能不能认出来」—— 把父链逐层判定的过程打出来。
    #     加它的原因：光说「探测不到宿主进程」没法让人自救 ——
    #     用户真正要知道的是「换 Hermes / Kimi Code 还行不行」以及**该怎么补**。
    # ⑦a 同名多线：同一个名字被**两个不互为子目录的地方**用过。
    #    为什么它必须和「多实例」分开列：`instances` 靠进程探测，
    #    对「一个进程承载多个会话」的形态（WorkBuddy / Electron 桌面版）给不出答案 ——
    #    那种情况 ⑦ 全绿，而合并照样发生（实测 2026-09-28：两条线混在同一行、
    #    实例数 0）。cwd 是同一个判据里唯一还握在手里的证据。
    split = {n2: (str((v2 or {}).get("cwd_conflict") or ""), str((v2 or {}).get("cwd") or ""))
             for n2, v2 in st["agents"].items() if (v2 or {}).get("cwd_conflict")}
    if split:
        bad += 1
        print("\n⑦a 同名多线（一个名字被两个不相干的目录用过）：")
        for n2, (other, home) in sorted(split.items()):
            print(f"     ✗ <{n2}>：{home} 与 {other}")
        print("       ⇒ 两条线共用 last_seen / entries / **用户喊话游标** / 心跳预算，"
              "板上看不出这是两个人。")
        print(f"         给每条线起名字：本目录 `identity set --agent <名字>`，"
              f"或 `export {AGENT_ENV}=<名字>`。")
    if getattr(a, "explain", False):
        print("\n⑦b 实例探测逐层判定（我是哪个实例？）：")
        trace = []
        got = detect_instance(explain=trace)
        me = os.getpid()
        print(f"     从本进程 pid {me} 往上爬（只看 argv 可执行名 / 脚本名，不做整句子串匹配）：")
        for t2 in trace:
            pid2 = t2["pid"]
            cmd2 = (t2["cmd"] or "").replace("\n", " ")
            if len(cmd2) > 96:
                cmd2 = cmd2[:93] + "…"
            who = f"pid {pid2}" if pid2 > 1 else "—"
            print(f"       {who:>10s}  {t2['verdict']}")
            if cmd2:
                print(f"                  $ {cmd2}")
        if got:
            print(f"     ✓ 结论：这个会话的实例标识 = {got}")
            print(f"       （同一台机器上另一个同名工具会是**不同的** pid@启动时刻，因此能被认出来）")
        else:
            print("     ✗ 结论：认不出宿主 ⇒ 同名多实例**不会**被告警（不是报错，是静默不检测）。")
            print(f"       两条补救，任选一条：")
            print(f"         ① 你的工具名不在名单里/名字对不上 ⇒ 追加（不用改代码）：")
            print(f"            export {INSTANCE_HINTS_ENV}=<你的工具名>[,<另一个>]")
            print(f"         ② 你的宿主是「一个进程承载多个会话」（桌面 App、tmux 等）⇒ 分不出会话，")
            print(f"            必须显式钉死每个会话：export {INSTANCE_ENV}=<这个名字>-<实例名>")
            print(f"       当前内置名单（{len(host_hint_names())} 个）：{', '.join(host_hint_names())}")

    print("")
    print("✗ 结论：通道没落好 —— 我随时可能「人间蒸发」而不自知。" if bad
          else "✓ 结论：通道已落好，新开一个会话也能自己找到这块板。")
    print("  判据不是「我记得有这块板」，而是「下次开新会话，不用任何人提醒就会 post」。")
    return 1 if bad else 0


def cmd_status(a):
    a.readonly = True
    # 注意：这里**不能**把 a.json 强行置 False —— 那会让 `status --json`
    # 静默退化成人类可读输出（脚本里按 JSON 解析会当场炸），
    # 而 --json 明明在 status 的 usage 里列着。
    return cmd_check(a)


def cmd_waker(a):
    """跑外部唤醒器 —— 把它包成引擎的一个子命令，**只为让接入片段能写出它**。

    为什么需要这一层（2026-09-30，用户实测「两个 agent 都装上了却交流不通」）：
    `onboard` 的片段刻意**不含任何引擎本机路径**（`[36]` 组用断言钉死），它只能通过
    看板自带的 `worklog` 引导脚本去调引擎。而 `waker.sh` 住在引擎旁边 ——
    片段既然写不出它的路径，当初就**一个字都没提**。后果：照片段装上的响应式会话
    **永远不会被 `ask` 叫醒**，只能问别人、不能被别人问，交流是**单向**的，
    而且这个缺失**不报错**（对方只会看到「问了没人答」）。
    包成子命令后，片段里写 `"$WL" waker --agent <你>` 就够了，路径问题不复存在。

    用 `exec` 而不是 subprocess：唤醒器要一直跑到「板上有冲你来的事」为止，
    中间不该多一个父进程占着（也免得 Ctrl-C 只杀父、留个孤儿 —— 本项目踩过）。
    """
    d = resolve_dir(a)
    shunt = Path(__file__).resolve().parent / "waker.sh"
    if not shunt.is_file():
        die(f"✗ 找不到唤醒器：{shunt}\n"
            "  它应与 work_log.py 同目录（脚本包不完整，或被单独拷走了）。")
    argv = ["bash", str(shunt), "--dir", str(d)]
    if (a.agent or "").strip():
        argv += ["--agent", a.agent.strip()]
    for flag, val in (("--window-min", a.window_min), ("--interval", a.interval)):
        if val is not None:
            argv += [flag, str(val)]
    os.execv("/bin/bash", argv)


def cmd_watch(a):
    d = resolve_dir(a)
    a.stale_after = sane_stale(a.stale_after, d)
    interval = a.interval
    tick, last_user = 0, None
    print(f"watchdog 启动 · {d}")
    print(f"每 {interval}s 扫描一次；静默超过 {a.stale_after}s 且未写「任务完成」→ 告警")
    print("Ctrl-C 停止\n")
    with locked(d):
        st0 = load_state(d)
        ensure_files(d, st0)
        known = list(st0["agents"])
    if not known:
        print("⚠ 这个日志目录还没有任何 agent 记录，看门狗会空转。")
        print(f"  目录：{d}")
        print("  若 agent 在别处 post，请用 --dir 指定同一目录；或先在项目根跑一次 init。\n")
    try:
        while True:
            tick += 1
            with locked(d):
                st = load_state(d)
                ensure_files(d, st)
                res = evaluate(d, st, a.stale_after)
                written = [] if a.readonly else emit(d, st, res, a.cooldown)
                pending = len(unacked(st))
                st["last_scan_ts"] = res["ts"]
                msgs = parse_user(d)
                if last_user is None:
                    last_user = 0 if a.replay_user else len(msgs)
                fresh = msgs[last_user:] if (a.replay_user or last_user < len(msgs)) else []
                last_user = len(msgs)
                if not a.readonly:
                    prune_state(st)
                    save_state(d, st)
            for line in written:
                print(f"⚠ {line}")
            for m in fresh:
                print(f"[用户喊话] {m['who']}：{m['body']}  <= 请相关 agent 立刻响应")
            if not a.quiet or written or fresh:
                flags = ",".join(f"{r['name']}:{r['state']}" for r in res["agents"]
                                 if r["state"] != "已离场") or "无 agent"
                col = res.get("collab") or {}
                collab = f" | 在线干活 {col.get('count', 0)}" + ("（多人协作）" if col.get("active") else "")
                print(f"[{hhmmss()}] tick {tick:>5} | {flags} | 用户 {res['user_total']} 条 "
                      f"| 未确认告警 {pending}{collab}")
            if a.ticks and tick >= a.ticks:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nwatchdog 已停止")
    return 0


def ack_alerts(d: Path, ids=None, agent=None, by="unknown") -> int:
    """确认告警，返回条数。**CLI 的 ack 与看板上的「确认」按钮共用这一份实现** ——
    同一件事有两个入口就必然漂移（work-log 自己的老毛病：一处改了另一处忘），
    所以内核只留一份，两个入口都薄薄地包一层。
    """
    want = {int(x) for x in (ids or [])}
    hit = 0
    with locked(d):
        st = load_state(d)
        for al in st.get("alerts", []):
            if al.get("acked_by"):
                continue
            if want and al["id"] not in want:
                continue
            if agent and al["agent"] != agent:
                continue
            al["acked_by"] = by
            al["acked_at"] = now()
            hit += 1
        prune_state(st)
        save_state(d, st)
    return hit


def cmd_ack(a):
    d = resolve_dir(a)
    hit = ack_alerts(d, ids=[a.id] if a.id else None,
                     agent=a.agent or None, by=a.by or "unknown")
    print(f"✓ 已确认 {hit} 条告警" + (f"（by {a.by}）" if a.by else ""))
    return 0 if hit else 1


def cmd_retire(a):
    """宣告某 agent「已离场」（不会再回来）—— 给永不 post 的 agent 一个生命周期出口。
    与「离线」的本质区别：离线是引擎按时长**猜**的（工作可能真丢了，照样告警，
    每只按 1h 退避上限永远重复下去）；离场是人**拍板**的 —— 不再判卡死、不再发告警、
    它名下未确认告警一并确认。复位必须便宜：它 post 一条即自动撤销（cmd_post），
    或显式 `retire --undo`。离场不是删除：state 里的条目原样留着，随时可撤。"""
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    t = now()
    if getattr(a, "undo", False):
        with locked(d):
            st = load_state(d)
            ag = (st.get("agents") or {}).get(a.agent)
            # ★ 这两条分支原来合并成一句 `return 1`（2026-10-01 修）。它有两个毛病，
            #   而且两个都不是"风格"问题：
            #   ① **同一个命令的两条路对同一种输入给了不同的码**：正向路径里
            #      「名字没出现过」是 2（"名字打错了？"），这里却是 1。
            #   ② 1 在这套协议里的定义是「业务：超时 / 对方没回」，**样本全是故障**
            #      （见 EXIT_PEER_DONE 的注释："别把一个期望结局挪去 1"）。
            #      于是调用方把「本来就没什么可撤销」读成了「对方不配合」，去走重试/换人。
            if ag is None:
                print(f"✗ <{a.agent}> 还没在这块板上出现过（state 里没有它）——名字打错了？")
                print("  --undo 只对「出现过、且被宣告过离场」的 agent 有意义。")
                return EXIT_USAGE
            if not ag.get("retired"):
                # 幂等 no-op：与正向那条「已在离场名单里 …… 无需重复」对称。
                # 已经是目标状态 = 成功，不是故障。
                print(f"• <{a.agent}> 本来就没被宣告过离场，无需撤销——它一直在心跳监督下")
                return EXIT_OK
            for k in ("retired", "retired_at", "retired_by", "retired_note"):
                ag.pop(k, None)
            save_state(d, st)
        print(f"✓ 已撤销 <{a.agent}> 的离场标记：它重新纳入心跳监督")
        return EXIT_OK
    with locked(d):
        st = load_state(d)
        ag = (st.get("agents") or {}).get(a.agent)
        if ag is None:
            # 不替你创建条目：retire 一个不存在的名字多半是拼错了，
            # 静默创建会让错名字永远挂在板上 —— 那正是本命令要消灭的东西。
            print(f"✗ <{a.agent}> 还没在这块板上出现过（state 里没有它）——名字打错了？")
            print("  先确认拼写；retire 只对「出现过」的 agent 有意义。")
            return 2
        if ag.get("retired"):
            print(f"• <{a.agent}> 已在离场名单里（{hhmmss(float(ag.get('retired_at') or t))} 宣告过），无需重复")
            return 0
        ag["retired"] = True
        ag["retired_at"] = t
        ag["retired_by"] = getattr(a, "by", None) or "用户"
        if a.note:
            ag["retired_note"] = a.note
        # 离场的人不该再挂任何「活性」残迹：挂起窗口、告警退避计数一并清掉
        ag["expected_silence_until"] = 0.0
        ag.pop("alert_open", None)
        ag.pop("alert_repeat", None)
        ag.pop("pending_recovery", None)
        save_state(d, st)
    hit = ack_alerts(d, agent=a.agent, by="retire")   # 锁外调：ack_alerts 自己拿锁
    print(f"✓ <{a.agent}> 已宣告离场：不再判卡死、不再发告警"
          + (f"；它名下 {hit} 条未确认告警已一并确认" if hit else ""))
    if a.note:
        print(f"  备注：{a.note}")
    print(f"  复位方式：它 post 一条心跳（自动撤销），或 retire --agent {a.agent} --undo")
    return 0


def cmd_lock(a):
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    check_resource(a.resource)
    a.stale_after = sane_stale(a.stale_after, d)
    t = now()
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        ag = _get_agent(st, a.agent, t)
        cur = st["locks"].get(a.resource)
        if cur and cur.get("agent") != a.agent:
            holder_last = st["agents"].get(cur["agent"], {}).get("last_seen", 0.0)
            dead = (t - holder_last) > a.stale_after * 2
            if not a.force:
                print(f"✗ {a.resource} 已被 <{cur['agent']}> 占用（{hhmmss(cur.get('ts'))} 起"
                      f"{'，' + cur['note'] if cur.get('note') else ''}）")
                if dead:
                    print(f"  ⚠ 持有者已静默 {int(t - holder_last)}s，疑似卡死"
                          f"→ 可以先 `ack` 它的告警，再 `lock --force` 抢锁")
                # 抢锁失败也进看板：让"谁在等谁的锁"变成共享可见的事实
                append_entry(d, st, a.agent,
                             f"想占用 {a.resource}，但 <{cur['agent']}> 正持有，先等它释放",
                             tag="阻塞", ts=t)
                ag["last_seen"] = t
                ag["entries"] = int(ag.get("entries", 0)) + 1
                save_state(d, st)
                return 3
            print(f"! 强制抢锁：{a.resource} 原属 <{cur['agent']}>")
        st["locks"][a.resource] = {"agent": a.agent, "ts": t, "note": a.note or ""}
        append_entry(d, st, a.agent, f"抢占资源 {a.resource}"
                     + (f"：{a.note}" if a.note else ""), tag="执行", ts=t)
        ag["last_seen"] = t
        ag["entries"] = int(ag.get("entries", 0)) + 1
        save_state(d, st)
    print(f"✓ {a.agent} 持有 {a.resource}")
    return 0


def cmd_unlock(a):
    d = resolve_dir(a)
    a.agent = resolve_agent(a, d)
    check_resource(a.resource)
    t = now()
    with locked(d):
        st = load_state(d)
        ag = _get_agent(st, a.agent, t)
        cur = st["locks"].get(a.resource)
        if not cur:
            print(f"（{a.resource} 本来就没被占用）")
            return 0
        if cur.get("agent") != a.agent and not a.force:
            print(f"✗ {a.resource} 属于 <{cur['agent']}>，想替它解要用 --force")
            return 3
        st["locks"].pop(a.resource, None)
        append_entry(d, st, a.agent, f"释放资源 {a.resource}", tag="执行", ts=t)
        ag["last_seen"] = t
        ag["entries"] = int(ag.get("entries", 0)) + 1
        save_state(d, st)
    print(f"✓ 已释放 {a.resource}")
    return 0


def cmd_locks(a):
    d = resolve_dir(a)
    a.stale_after = sane_stale(a.stale_after, d)
    st = load_state(d)
    locks = st.get("locks", {})
    if not locks:
        print("（无占用）")
        return 0
    for rsrc, info in sorted(locks.items()):
        last = st["agents"].get(info.get("agent"), {}).get("last_seen", 0.0)
        silence = int(now() - last) if last else -1
        warn = "  ⚠ 持有者疑似卡死，可 --force 抢" if silence > a.stale_after * 2 else ""
        print(f"{rsrc:<16} <- {info.get('agent'):<12} {hhmmss(info.get('ts'))}  "
              f"持有者静默 {silence}s{warn}")
    return 0


def cmd_tail(a):
    d = resolve_dir(a)
    f = p_board(d)
    if not f.exists():
        print("（看板还没建）")
        return 0
    lines = [l for l in f.read_text(encoding="utf-8", errors="replace").splitlines()
             if l.strip() and not l.lstrip().startswith(("#", ">"))]
    for l in lines[-a.n:]:
        print(l)
    return 0


def snapshot(d: Path, stale_after: float = STALE_AFTER) -> dict:
    """一屏所需的全部数据：把看板行、用户喊话、状态、锁合并成一条时间线。

    合并放在服务端做，前端只负责渲染 —— 时间语义只有一处，不会前后端各算一套。
    """
    st = load_state(d)
    res = evaluate(d, st, stale_after)
    items = board_entries(d)
    # ── 用户喊话落在时间线的哪里 ───────────────────────────────────────────
    # user.md 是**手写**的，所以"没带时间前缀""时间格式写坏了"都必须容忍 ——
    # 但**容忍不等于假装它是最新的**。旧版这里两种情况都写 `ts = now()`，
    # 后果是真板实测出来的（2026-09-29，截图里肉眼可见）：一条 21:37 的老消息
    # 在页面上显示成"刚刚说的"，而且**每次刷新时间都变、永远贴在时间线最末尾**
    # —— 读的人只会得出"用户刚说了话"这个错误结论。
    # 现在：算不出时间的**继承上一条的时间**（user.md 是顺序流水，顺序本身就是信息），
    # 页面 time 留空、明确表示"这条没标时间"；日期推算全部交给 `user_ts`。
    prev_ts = None
    malformed: list = []
    for m in parse_user(d):
        ts, exact = user_ts(m["time"])
        if ts is None:
            ts = prev_ts if prev_ts is not None else now()
            if not m["time"] and TIMEISH_RE.match(m["body"].lstrip()):
                malformed.append(m["body"][:60])
        prev_ts = ts
        items.append({"agent": m["who"], "ts": ts,
                      "time": hhmmss(ts) if exact else "",
                      "tag": "喊话", "text": m["body"], "kind": "user"})
    items.sort(key=lambda e: e["ts"])
    for e in items:
        e.pop("ts", None)
    # 看门狗自身是否还活着：页面必须能区分"全员健康"和"根本没人看"
    last_scan = float(st.get("last_scan_ts", 0.0))
    wd_idle = (now() - last_scan) if last_scan else None
    wd_limit = max(int(st.get("heartbeat_secs", HEARTBEAT)) * 4, int(res["stale_after"]) * 2)
    return {
        "task": st.get("task") or "",
        "generated_at": hhmmss(),
        "heartbeat_secs": st.get("heartbeat_secs", HEARTBEAT),
        "stale_after": res["stale_after"],
        "collab": res.get("collab"),
        "entries": items,
        # 看着像带了时间戳、却没解析出来的喊话（原文片段）。**报出来而不是咽下去**：
        # 这类行的代价是"静默降级"（时间丢失、顺序可疑），只有看得见才修得掉。
        "user_malformed": malformed,
        "agents": res["agents"],
        "idle": res["idle"],
        "hazards": res.get("hazards", []),
        "locks": st.get("locks", {}),
        "unacked": len(unacked(st)),
        "alerts": [{"id": a["id"], "agent": a["agent"], "time": hhmmss(a["ts"]),
                    "text": a["text"]} for a in unacked(st)],
        "last_scan_at": hhmmss(last_scan) if last_scan else None,
        "watchdog_idle": int(wd_idle) if wd_idle is not None else None,
        "watchdog_stale": bool(wd_idle is not None and wd_idle > wd_limit),
        # 最近告警（含已确认的），否则"已自动恢复"这件事在前端会凭空消失
        "alerts_recent": [{"id": a["id"], "agent": a["agent"], "time": hhmmss(a["ts"]),
                           "text": a["text"], "acked": bool(a.get("acked_at"))}
                          for a in st.get("alerts", [])[-6:]],
        # 页面上要能一眼看到"有人在等回答"和"用户的话还没人认领"
        "questions": [{"id": q["id"], "from": q["from"], "to": q["to"],
                       "question": q["question"], "waited": int(now() - q["ts"])}
                      for q in open_exchanges(st)],
        "pending_user": [{"id": u["id"], "who": u["who"], "body": u["body"]}
                         for u in unanswered_user(d, st)],
    }


def cmd_serve(a):
    """起一个本地实时视图：浏览器里直接看 agent 之间的交流。"""
    d = resolve_dir(a)
    a.stale_after = sane_stale(a.stale_after, d)
    viewer = Path(__file__).resolve().parent.parent / "assets" / "viewer.html"
    if not viewer.exists():
        die(f"✗ 找不到查看器：{viewer}\n  （assets/viewer.html 应与 scripts/ 同级）")

    def read_viewer() -> bytes:
        """每次请求**重新读盘**，而不是启动时读一次就常驻内存。

        原来是 `html = viewer.read_bytes()` 在进程启动时读一遍。后果是改前端界面
        必须重启这个 serve 进程才生效 —— 实测：改了 assets/viewer.html，页面上
        怎么刷新都是旧版（curl 到的还是 19082 字节的老文件），排查半天才想到是
        服务端把字节缓冲住了。看板是"边跑边看"的东西，改一行就得重启一次不划算；
        这个文件只有十几 KB，每次请求读一次的代价可以忽略。
        """
        try:
            return viewer.read_bytes()
        except OSError as e:                   # 文件被删/被改坏时别把整个看板打挂
            return f"<!DOCTYPE html><html><body style='font:14px sans-serif;padding:24px'>\
viewer.html 读取失败：{e}</body></html>".encode("utf-8")

    stop = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):        # 静音访问日志，别刷屏
            pass

        def _send(self, code, ctype, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            """只认本机 Host。缺这层的话，DNS rebinding（恶意域名解析到 127.0.0.1）
            可以把浏览器的同源策略整个绕掉——请求看起来跨域，Host 却是攻击者域名。"""
            h = (self.headers.get("Host") or "").strip()
            h = h.rsplit(":", 1)[0].strip("[]").lower()
            return h in ("127.0.0.1", "localhost", "::1")

        def _json(self, code, obj):
            self._send(code, "application/json; charset=utf-8",
                       json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def do_POST(self):
            """UI 的写通道：/api/say（用户插话）、/api/reply（回答问向自己的提问）。

            只开放这两个口，不提供"执行任意命令"——看板是协作场所，不是 shell。
            写路径全部走 locked() + 与 CLI 相同的函数，不会绕过任何一致性保护。

            **必须校验 Content-Type**：浏览器把 `application/json` 视为需预检的请求，
            而 `text/plain` 是"简单请求"直接放行——不校验的话，你浏览的任何网页都能
            用 text/plain 夹带 JSON 打进这里，**冒充「用户」给 agent 下指令**。
            （serve 只绑本机挡不住这种事，出事的是浏览器发来的跨站请求。）
            """
            if not self._host_ok():
                self.close_connection = True
                self._json(403, {"ok": False, "error": "拒绝非本机 Host"})
                return
            path = self.path.split("?")[0]
            ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct != "application/json":
                self.close_connection = True
                self._json(415, {"ok": False, "error": "Content-Type 必须是 application/json"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 65536:
                    # 拒绝时不再读 body，必须断开连接：HTTP/1.1 keep-alive 下，
                    # 残留的 body 字节会被当成下一条请求的请求行（协议错位）
                    self.close_connection = True
                    self._json(413, {"ok": False, "error": "请求体过大（上限 64KB）"})
                    return
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                if not isinstance(body, dict):
                    raise ValueError
            except Exception:              # noqa: BLE001 - 坏请求体一律 400
                self.close_connection = True
                self._json(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
                return
            text = " ".join(str(body.get("text") or "").split())
            if path == "/api/say":
                if not text:
                    self._json(400, {"ok": False, "error": "内容不能为空"})
                    return
                with locked(d):
                    st = load_state(d)
                    ensure_files(d, st)
                    line = user_say(d, st, text, str(body.get("who") or "用户")[:12])
                    save_state(d, st)
                self._json(200, {"ok": True, "line": line})
            elif path == "/api/reply":
                try:
                    eid = int(body.get("id"))
                except (TypeError, ValueError):
                    self._json(400, {"ok": False, "error": "缺少提问编号 id"})
                    return
                if not text:
                    self._json(400, {"ok": False, "error": "回答不能为空"})
                    return
                with locked(d):
                    st = load_state(d)
                    ensure_files(d, st)
                    ok, msg = user_reply_exchange(d, st, eid, text)
                    if ok:
                        save_state(d, st)
                self._json(200 if ok else 409, {"ok": ok, "error": None if ok else msg,
                                                "message": msg})
            elif path == "/api/ack":
                # 人在页面上看到「未确认告警 N」应该能直接点掉，不该被迫去敲 CLI
                #（--id 不给就是全确认，与 CLI 的 `ack` 语义一致）。
                raw_ids = body.get("ids")
                ids = []
                if raw_ids is not None:
                    seq = raw_ids if isinstance(raw_ids, list) else [raw_ids]
                    try:
                        ids = [int(x) for x in seq]
                    except (TypeError, ValueError):
                        self._json(400, {"ok": False, "error": "ids 必须是编号或编号数组"})
                        return
                hit = ack_alerts(d, ids=ids or None,
                                 agent=str(body.get("agent") or "") or None,
                                 by=str(body.get("by") or "用户")[:12])
                self._json(200, {"ok": True, "hit": hit})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def do_GET(self):
            if not self._host_ok():
                self.close_connection = True
                self._json(403, {"ok": False, "error": "拒绝非本机 Host"})
                return
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", read_viewer())
                elif path == "/api/board":
                    with locked(d):
                        body = json.dumps(snapshot(d, a.stale_after),
                                          ensure_ascii=False).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                elif path == "/api/health":
                    # 带 dir：auto_ui 复用端口前要核对"这是不是**本项目**的 serve"，
                    # 否则两个项目同时跑，B 项目会把 A 项目的看板弹给用户（串台）
                    self._json(200, {"ok": True, "dir": str(d)})
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def watchdog_loop():
        while not stop.is_set():
            try:
                with locked(d):
                    st = load_state(d)
                    ensure_files(d, st)
                    res = evaluate(d, st, a.stale_after)
                    emit(d, st, res, a.cooldown)
                    st["last_scan_ts"] = res["ts"]
                    prune_state(st)
                    save_state(d, st)
            except Exception as e:            # 看门狗线程绝不能把服务带崩
                print(f"! 内置看门狗异常：{e}", file=sys.stderr)
            stop.wait(a.interval)

    try:
        httpd = http.server.ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        die(f"✗ 端口 {a.port} 起不来：{e}\n  换个端口：--port {a.port + 1}")
    httpd.daemon_threads = True

    if a.watch:
        threading.Thread(target=watchdog_loop, daemon=True).start()
    host = "localhost" if a.host in ("127.0.0.1", "0.0.0.0", "::1", "") else a.host
    url = f"http://{host}:{a.port}/"
    print(f"实时视图   {url}")
    print(f"数据目录   {d}")
    # 阈值必须打出来：serve 自带的看门狗默认 45s，而真 LLM agent 单轮就要 30~120s，
    # 不写出来的话用户会以为"我已经给 watch 传了 --stale-after 90"就没事了，
    # 实际上这里这个更严的看门狗会先告警（实测踩到）。
    print(f"内置看门狗 {'已启动' if a.watch else '未启动'}"
          f"（每 {a.interval:g}s 扫一次，卡死阈值 {a.stale_after:g}s）")
    # 启动横幅里给出协作判定：这个视图的核心价值就是"看着多个 agent 干活"。
    # 同样**只报人数、不写分母**：人数不固定，写死 "N/2" 会误导成"上限两人"。
    with locked(d):
        st0 = load_state(d)
        res0 = evaluate(d, st0, a.stale_after)
    col0 = res0.get("collab") or {}
    names0 = "、".join(col0.get("agents") or []) or "无"
    if col0.get("active"):
        print(f"🤝 协作中：{col0['count']} 个 agent 在干活（{names0}）")
    else:
        print(f"协作：{col0.get('count', 0)} 个 agent 在干活（{names0}）"
              f"—— 未到「协作模式」标记线（本板设 ≥{col0.get('required', COLLAB_MIN)}）；"
              f"人数不设上限也不固定，任何 agent 开工都会即时计入，"
              f"`init --collab-min` 可调")
    if a.watch:
        print(f"  提示：若同时另跑 watch 且阈值不同，两个看门狗会取**更严**的那个。"
              f"要一致就两边传同样的 --stale-after。")
    print("Ctrl-C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        stop.set()
        httpd.server_close()
    return 0


# ---------------------------------------------------------------- CLI

# 自由文本类选项：值以 "-" 开头时 argparse 会误判为新选项，需要预先合并。
FREE_TEXT_OPTS = ("--text", "--note")
# `--text-file` 的定位：让**文本**从文件走进来，而不是从 argv 挤进来。
# 长中文帖经 shell argv 会撞上整类不报错的问题（反引号被当命令执行后静默替成空格、
# `$变量` 被展开、`${var}` 后跟中文标点吞字节致脚本 rc=127、引号嵌套），
# 而这些**都出在调用方的命令行**，引擎在 argv 里查不到（查得到的那个反引号
# 恰恰是已经被引号保护好的）。走文件 = 判据只面对字节，整类问题不存在。
TEXT_FILE_HELP = ("从文件读正文，替代 --text。**长帖 / 含反引号或 $ 的帖走这条**："
                  "shell 的引号、反引号执行、$ 展开、${} 吞字节整类问题都不会再发生。"
                  "`-` = 从标准输入读；与 --text 同时给会退 2（谁覆盖谁不该由工具猜）")
# `--agent` 从「必填」改成「可缺省」是为了**固定身份**：
# 名字要么从 $WORK_LOG_AGENT 来（给会话钉死），要么从板上 identities.json 按 cwd 认出来。
AGENT_HELP = ("；缺省时先看 $WORK_LOG_AGENT，再看板上 identities.json 里 cwd 匹配的那条，"
              "都不成立就退 2 并把该跑的命令打出来（不猜）")
KNOWN_OPTS = {
    "--dir", "--agent", "--text", "--tag", "--done", "--task", "--agents",
    "--seconds", "--peek", "--all", "--json", "--readonly", "--stale-after",
    "--from-now",
    "--cooldown", "--interval", "--ticks", "--quiet", "--replay-user",
    "--id", "--by", "--note", "--resource", "--force", "--help", "-h", "-n",
    # 下面这些原本漏登记：`--text --to` 这种"漏传值"会被误当成文本吞掉，
    # 于是用法错误伪装成"提问内容就是 --to"。缺一个就等于给 `--text` 开了个后门。
    "--to", "--rate-cap", "--no-auto-ui", "--port", "--host", "--no-watch",
    "--set", "--kind", "--purpose", "--detect", "--include-user",
    # 又漏了 5 个（2026-09-28 用"把所有 add_argument 拉出来和这里对账"的方法查出来的）：
    # 手动维护一份"选项名单"迟早会漏 —— 于是补了 selftest 里的**结构化护栏**，
    # 以后再加选项忘了登记，自测当场红，不再靠人记得。
    "--any", "--as", "--collab-min", "--report", "--timeout",
    "--explain", "--undo", "--base",
    # prune 的三个（同样别再靠人记得 —— 漏登记会让 `--days 3` 被当成别的东西吞掉）
    "--days", "--keep-alerts-md", "--apply",
    # 批次2 新增（同样：漏登记会让 `--text --text-file` 这种被悄悄吞掉）
    "--text-file", "--stale-open-after", "--spawn", "--no-spawn",
    "--loop", "--no-loop",
    # `waker` 子命令带来的（2026-09-30）。★ 就是这条漏登记被结构性护栏当场抓出来的：
    # 加了子命令但没同步这份手工名单 ⇒ `[29]` 组报 `MISSING --window-min`。
    # 这正说明"名单靠人记"必然漏，而"从 argparse 枚举 + 对账"必然抓得到。
    "--window-min",
}


def _fix_dash_values(argv: list) -> list:
    """把 `--text - 我先停一下` 合并成 `--text=- 我先停一下`。

    只对自由文本选项生效，且下一个 token 不是已知选项名时才合并，
    这样 `--text --done` 依然按"漏传值"报错，不会被悄悄当成文本。
    """
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if (tok in FREE_TEXT_OPTS and i + 1 < len(argv)
                and argv[i + 1].startswith("-") and len(argv[i + 1]) > 1
                and argv[i + 1] not in KNOWN_OPTS):
            out.append(f"{tok}={argv[i + 1]}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return out

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS 是关键：子解析器没写 --dir 时不会用 None 覆盖主解析器已解析的值，
    # 这样 `work_log.py --dir X post …` 和 `work_log.py post --dir X …` 两种写法都能用。
    common.add_argument("--dir", default=argparse.SUPPRESS,
                        help="日志目录（**必填**，或设 $WORK_LOG_DIR；缺了退 2 并指路，不再默认桌面）")

    p = argparse.ArgumentParser(prog="work_log.py", description="多 agent 心跳看板 / 看门狗 / 用户喊话通道")
    p.add_argument("--dir", default=None,
                   help="日志目录，放在子命令前后都行（**必填**，或设 $WORK_LOG_DIR；缺了退 2 并指路）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", parents=[common], help="初始化看板")
    sp.add_argument("--task", default="", help="任务名")
    sp.add_argument("--agents", default="", help="预注册 agent，逗号分隔")
    sp.add_argument("--rate-cap", type=int, default=None,
                    help=f"单个 agent 每分钟心跳上限（默认 {POST_CAP}；0 = 关闭熔断）")
    sp.add_argument("--collab-min", type=int, default=None,
                    help=f"「协作模式」的启动门槛：同时在干活的 agent 达到几人算成立"
                         f"（默认 {COLLAB_MIN}）。人多少不固定时按**平时的下限**填即可 ——"
                         f"这只是一个「够不够热闹」的标记，不限制人数上限，几个 agent 都能接")
    sp.add_argument("--stale-after", type=float, default=None,
                    help=f"卡死阈值（秒）。**建议在这里声明一次**：之后 `serve` / `watch` / `check` "
                         f"以及 `post` 自动起的看门狗**都取这个值**（解析顺序：显式参数 > 本处声明 "
                         f"/ $WORK_LOG_STALE_AFTER > 内置默认 {STALE_AFTER}）。为什么必须能声明："
                         f"`post` 触发的自动界面会另起一个看门狗，它原来只会用内置默认值 ——"
                         f"于是「我明明用 --stale-after 90 起的 serve」照样被它按 45s 判卡死。"
                         f"真 LLM agent 单轮 30~120s，建议 90 以上")
    sp.add_argument("--stale-open-after", type=float, default=None,
                    help=f"**办事轴**阈值（秒）：一个提问开着多久没人办就算事故"
                         f"（默认 {STALE_OPEN_AFTER}；解析顺序同上，"
                         f"env $WORK_LOG_STALE_OPEN_AFTER > 本处声明 > 内置默认）。"
                         f"它和卡死阈值是**两根轴**：卡死阈值问「它还在吗」，这根问"
                         f"「该办的事还在不在」—— 当天心跳轴误报 33 条 / 真卡死 0，"
                         f"而唯一一次真事故告警 0 条，就是缺了这根")
    sp.add_argument("--no-auto-ui", action="store_true",
                    help="关闭自动协作界面（默认：第 2 个 agent 开工时自动起 serve 并弹浏览器）")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("post", parents=[common], help="写一条心跳（核心命令）")
    sp.add_argument("--agent", default="", help="agent 名字，如 agent1。" + AGENT_HELP)
    sp.add_argument("--text", default="", help="想了什么 / 干了什么（长帖用 --text-file）")
    sp.add_argument("--text-file", default="", help=TEXT_FILE_HELP)
    sp.add_argument("--tag", default=None, help="标签：收到/决定/执行/阻塞/建议/任务完成")
    sp.add_argument("--done", action="store_true", help="等同 --tag 任务完成")
    sp.set_defaults(func=cmd_post)

    sp = sub.add_parser("hold", parents=[common], help="声明要跑长任务，期间不判卡死")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--seconds", type=int, default=HOLD_DEFAULT,
                    help=f"挂起多久（秒），默认 {HOLD_DEFAULT}；0 = 立即解除挂起。"
                         "凡是要跑几分钟以上的活（装依赖 / 构建 / 渲染 / 训练 / 大批量下载 / 长时间模型往返），"
                         "先 hold 再开工；估不准就写大一点。"
                         "post 不再解除挂起（已解耦），提前收工用 `release` 或 `--seconds 0`")
    sp.add_argument("--text", default="")
    sp.add_argument("--text-file", default="", help=TEXT_FILE_HELP)
    sp.add_argument("--quiet", action="store_true",
                    help="只挂起不写心跳：给「每秒都在跑长任务」的驱动层用（如 LLM agent 每步都要调模型），"
                         "避免把看板刷满「进入长任务」这种无信息量的阻塞条目")
    sp.set_defaults(func=cmd_hold)

    sp = sub.add_parser("release", parents=[common], help="解除挂起")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--text", default="")
    sp.add_argument("--text-file", default="", help=TEXT_FILE_HELP)
    sp.add_argument("--quiet", action="store_true",
                    help="静默解除：只清挂起窗口不写心跳条目（驱动层退出前清理自己用）")
    sp.set_defaults(func=cmd_release)

    sp = sub.add_parser("say", parents=[common], help="用户喊话（也用于 agent 以用户身份留言）")
    sp.add_argument("--text", default="")
    sp.add_argument("--text-file", default="", help=TEXT_FILE_HELP)
    sp.add_argument("--as", dest="as_", default="用户")
    sp.set_defaults(func=cmd_say)

    sp = sub.add_parser("ask", parents=[common],
                        help="定向提问某个 agent（会等它回应，不是广播）")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--to", required=True, help="问谁")
    sp.add_argument("--text", default="", help="问题")
    sp.add_argument("--text-file", default="", help=TEXT_FILE_HELP)
    sp.add_argument("--stale-after", type=float, default=None)
    sp.add_argument("--force", action="store_true",
                    help=f"明知已连续来回 {PINGPONG_HARD} 轮也要继续问（默认会被熔断拦住）")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("reply", parents=[common], help="回应别人对你的提问")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--id", type=int, required=True, help="提问编号，见 brief")
    sp.add_argument("--text", default="")
    sp.add_argument("--text-file", default="", help=TEXT_FILE_HELP)
    sp.add_argument("--force", action="store_true", help="明知已被熔断也要继续答")
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("await", parents=[common],
                        help="阻塞等回应／等别人动（把「等它搞完」变成机制）")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--id", dest="ids", type=id_list, default=[],
                    help="等这些提问的回应，逗号分隔（如 1,2,3）；不给则等到任何新动态")
    sp.add_argument("--any", dest="any_mode", action="store_true",
                    help="多路等待时：任一回应即算达成（默认要全部）")
    sp.add_argument("--timeout", type=float, default=300)
    sp.add_argument("--interval", type=float, default=2)
    sp.add_argument("--report", type=float, default=15, help="每隔多少秒报一次进度")
    sp.add_argument("--stale-after", type=float, default=None,
                    help="判定「对方已收工 → 立刻收手」用的阈值；缺省取看板声明的值")
    sp.set_defaults(func=cmd_await)

    sp = sub.add_parser("ack-user", parents=[common], help="认领用户喊话（用户才知道有人管了）")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--id", type=int, required=True, help="喊话编号，见 read-user")
    sp.add_argument("--text", default="", help="你改了什么")
    sp.add_argument("--text-file", default="", help=TEXT_FILE_HELP)
    sp.add_argument("--force", action="store_true",
                    help="已被别人认领时也强行认领（默认退 3，先到先得）")
    sp.set_defaults(func=cmd_ack_user)

    sp = sub.add_parser("brief", parents=[common],
                        help="探针：取上次读过之后的交流增量（别人的心跳 + 用户喊话 + 告警）")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--peek", action="store_true", help="只看不推进游标")
    sp.add_argument("--from-now", action="store_true",
                    help="把心跳读游标对齐到当前：跳过全部历史，此后只看新动态"
                         "（新接入的 agent 用这个干净起步；未读的用户喊话不受影响）")
    sp.add_argument("--stale-after", type=float, default=None)
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("read-user", parents=[common], help="取走用户未读喊话")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--peek", action="store_true", help="只看不推进游标")
    sp.add_argument("--all", action="store_true", help="从头发全部")
    sp.set_defaults(func=cmd_read_user)

    sp = sub.add_parser("check", parents=[common], help="扫一次并补告警（有卡死则退出码 1）")
    sp.add_argument("--stale-after", type=float, default=None)
    sp.add_argument("--cooldown", type=float, default=COOLDOWN)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--readonly", action="store_true", help="只读，不写告警")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("status", parents=[common], help="check --readonly 的别名（只看不改）")
    sp.add_argument("--stale-after", type=float, default=None)
    sp.add_argument("--cooldown", type=float, default=COOLDOWN)
    sp.add_argument("--readonly", action="store_true", default=True)
    sp.add_argument("--json", action="store_true", default=False)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("watch", parents=[common], help="看门狗：每 15s 扫一次并告警")
    sp.add_argument("--interval", type=float, default=HEARTBEAT)
    sp.add_argument("--stale-after", type=float, default=None)
    sp.add_argument("--cooldown", type=float, default=COOLDOWN)
    sp.add_argument("--ticks", type=int, default=0, help="跑多少轮后退出（0=不限）")
    sp.add_argument("--quiet", action="store_true", help="只在有变化时打印")
    sp.add_argument("--replay-user", action="store_true", help="启动时把历史喊话也播一遍")
    sp.add_argument("--readonly", action="store_true")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("waker", parents=[common],
                        help="跑唤醒器：板上一有冲你来的事就退出（退出即唤醒宿主会话）")
    sp.add_argument("--agent", default="", help="你的名字（片段里会写成 --agent <你>）")
    sp.add_argument("--window-min", type=int, default=None,
                    help="窗口分钟数（默认 45；到点自己退出，被唤醒后重新挂上就是续期）")
    sp.add_argument("--interval", type=int, default=None, help="轮询秒数（默认 15）")
    sp.set_defaults(func=cmd_waker)

    sp = sub.add_parser("ack", parents=[common], help="确认告警（在线 agent 接管后调用）")
    sp.add_argument("--id", type=int, default=0)
    sp.add_argument("--agent", default="", help="只确认该 agent 的告警")
    sp.add_argument("--by", default="", help="谁确认的")
    sp.set_defaults(func=cmd_ack)

    sp = sub.add_parser("retire", parents=[common],
                        help="宣告某 agent 已离场（不会再回来）：不再判卡死、不再发告警；它 post 一条即自动复位")
    sp.add_argument("--agent", default="", help="要宣告离场的 agent 名")
    sp.add_argument("--note", default=None, help="离场备注（可选，写明为什么判定它不会回来了）")
    sp.add_argument("--by", default="", help="谁宣告的（默认「用户」）")
    sp.add_argument("--undo", action="store_true", help="撤销离场标记（它其实还活着）")
    sp.set_defaults(func=cmd_retire)

    sp = sub.add_parser("claim", parents=[common],
                        help="多实例自己认领名字：<框架名>1、2、…（base 缺省自动认宿主；同目录幂等；号码永不复用）")
    sp.add_argument("--base", default="", help="名字前缀；缺省自动认宿主（$WORK_LOG_INSTANCE > 父链工具名），认不出退 2")
    sp.add_argument("--json", action="store_true", help='机器可读输出（{"name": …}）')
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("lock", parents=[common], help="抢占共享资源（GPU/端口/文件）")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--resource", required=True)
    sp.add_argument("--note", default="")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--stale-after", type=float, default=None)
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser("unlock", parents=[common], help="释放资源")
    sp.add_argument("--agent", default="", help=AGENT_HELP)
    sp.add_argument("--resource", required=True)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_unlock)

    sp = sub.add_parser("locks", parents=[common], help="列出占用情况")
    sp.add_argument("--stale-after", type=float, default=None)
    sp.set_defaults(func=cmd_locks)

    sp = sub.add_parser("serve", parents=[common], help="起本地实时视图，浏览器里看 agent 交流")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--host", default="127.0.0.1", help="默认只绑本机，别改成 0.0.0.0")
    sp.add_argument("--interval", type=float, default=HEARTBEAT, help="内置看门狗扫描间隔")
    sp.add_argument("--stale-after", type=float, default=None)
    sp.add_argument("--cooldown", type=float, default=COOLDOWN)
    sp.add_argument("--no-watch", dest="watch", action="store_false", default=True,
                    help="只开视图，不跑内置看门狗")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("onboard", parents=[common],
                        help="生成「接入片段」，让第二个 agent 知道这块板存在（默认只打印）")
    sp.add_argument("--agent", default="",
                    help="对方在这块板上的名字（写进片段，让它照抄）")
    sp.add_argument("--to", default="", metavar="落点",
                    help="写到哪里。别名：user · opencode-global · claude-global · "
                         "repo(=cwd/AGENTS.md) · claude · gemini · cursor；也可以直接给文件路径。"
                         "给 auto 则**探测本机装了哪些工具链**再逐个落跨目录档。"
                         "缺省只打印，不落盘")
    sp.add_argument("--detect", action="store_true",
                    help="只探测本机装了哪些工具链、该往哪落（**不写任何文件**）。"
                         "换台机器时先跑这个")
    sp.add_argument("--include-user", action="store_true",
                    help="--to auto 时把 user 档（~/.workbuddy/MEMORY.md）也算进去。"
                         "默认跳过，因为它会注入**所有**工作区")
    sp.add_argument("--force", action="store_true",
                    help="目标里已有手写的 work-log 内容、又没有标记块时，仍要追加（默认拒绝）")
    sp.set_defaults(func=cmd_onboard)

    sp = sub.add_parser("identity", parents=[common],
                        help="名字登记表：谁在这块板上叫什么（两个会话抢同一个名字会被拒绝）")
    sp.add_argument("action", nargs="?", default="list", choices=["list", "set", "rm"],
                    help="list（默认）| set 登记「名字 + 当前目录」| rm 删除一条")
    sp.add_argument("--agent", default="", help="set / rm 时的名字" + AGENT_HELP)
    sp.add_argument("--kind", default="", help="set 时记一句「这是什么工具/角色」，如 workbuddy")
    sp.add_argument("--purpose", default="", help="set 时记一句这个会话负责什么")
    sp.add_argument("--spawn", action="store_true",
                    help="set 时把这条线声明为 **spawn 即活型**：它平时零心跳、被提问/事件"
                         "唤醒才起一次（如被监听器 spawn 的一次性实例）。声明后看门狗**不再**"
                         "按心跳判它卡死。**必须同时给 --purpose 写清它是被谁唤醒的** —— "
                         "不带理由的静音就是藏身处。判活从此交给它的驱动层；"
                         "「有提问开着没人答」仍由事故轴盯着，不是没人管")
    sp.add_argument("--no-spawn", action="store_true",
                    help="撤销上面的声明，让它回到按心跳判活")
    sp.add_argument("--loop", action="store_true",
                    help="set 时把这条线声明为 **循环型**（守护/监听/看门狗：它永远不收工）。"
                         "声明后「任务完成」标签对它**不生效**、照样按心跳判活。"
                         "必要性：完成状态同时是**办事轴的开关**（「完成 ⇒ 不会再答了」），"
                         "循环进程把心跳打成「任务完成」就会把所有问它的提问一次性标成"
                         "「该结案」，而板上完全看不出来（当天真有一个这样的守护进程）")
    sp.add_argument("--no-loop", action="store_true",
                    help="撤销循环型声明，让「任务完成」标签重新生效")
    sp.add_argument("--force", action="store_true",
                    help=f"明知这个名字被别的目录占着也要登记（等价 ${FORCE_ID_ENV}=1）")
    sp.set_defaults(func=cmd_identity)

    sp = sub.add_parser("whoami", parents=[common],
                        help="我在这块板上叫什么、凭什么 —— 按哪一档认出来的（只读）")
    sp.add_argument("--agent", default="", help="显式声明身份，用来验证它合法" + AGENT_HELP)
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("doctor", parents=[common],
                        help="自查「我这侧的拉取点」有没有落好（只读；有病退 1）")
    sp.add_argument("--agent", default="",
                    help="我在板上的名字 —— 用来把「我」的状态单独标出来")
    # 真 LLM agent 一次「模型思考 + 工具调用」往返能到 30~60s，默认 45s 会误判，
    # 所以这个阈值必须可调（与 check/watch 同一套口径）。
    sp.add_argument("--stale-after", type=float, default=None)
    # ⑦b：把「实例探测」的父链逐层判定打出来 —— 回答「换 Hermes/Kimi Code 还认不认得出」
    # 以及「认不出该往哪补」。默认关闭，因为要读进程表（有 ps 调用）。
    sp.add_argument("--explain", action="store_true",
                    help="额外打 ⑦b：实例探测的父链逐层判定 + 认不出时怎么补救")
    sp.set_defaults(func=cmd_doctor)

    # prune：把**过老的历史**搬进 archive/ —— 治 board.md / alerts.md / state.alerts
    # 只增不减。默认只报告，确认无误再加 --apply。
    # 为什么叫 prune 而不是 clean/delete：它是**搬家**（历史留在 archive/ 里可查），
    # 名字要让人一眼知道"不是删"。
    sp = sub.add_parser("prune", parents=[common],
                        help="归档过老的历史（board.md/alerts.md/alerts）；默认只报告，--apply 才动手")
    sp.add_argument("--days", type=float, default=7,
                    help="多少天之前的算「历史」（默认 7；board 的整段日期块 + 已确认告警都按它切）")
    sp.add_argument("--keep-alerts-md", type=int, default=200,
                    help="alerts.md 至少留最近这么多条（默认 200；其余搬进 archive/）")
    sp.add_argument("--apply", action="store_true",
                    help="真动手（不传 = 只试算并打印将要发生什么）")
    sp.add_argument("--json", action="store_true", help="按 JSON 输出（给脚本用）")
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("tail", parents=[common], help="看最近几条日志")
    sp.add_argument("-n", type=int, default=20)
    sp.set_defaults(func=cmd_tail)

    return p


def main(argv=None):
    # 后台跑 watch / serve 是常态，stdout 一旦进管道或文件，Python 默认全缓冲：
    # 会出现"看门狗在跑、视图在跑，但一个字都看不到"。在入口统一切行缓冲，
    # 比在每个命令里各打一次补丁可靠。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    # 注意：try 必须把 build_parser() 也包进来 —— 解析器构建时会**按名字引用**每个
    # cmd_* 函数（set_defaults(func=cmd_xxx)），哪个函数头在并发编辑中被吞掉，
    # NameError 是在**这里**炸的，不是在 args.func(args) 里。只包后者的话，
    # 契约定义者自己会把"工具坏了"裸 traceback 成退出码 1（2026-09-28 实测，
    # opencode 撞上后以为是自己参数写错）。argparse 的用法错误走 SystemExit(2)，
    # 在下面被原样 re-raise，不会误伤成 70。
    try:
        argv = _fix_dash_values(list(sys.argv[1:] if argv is None else argv))
        args = build_parser().parse_args(argv)
        return args.func(args)
    except BrokenPipeError:
        return 0
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:                  # noqa: BLE001 - 工具自己的 bug 不能伪装成业务结论
        # 关键：内部崩溃**绝不能**复用 1。1 在这套协议里的含义是
        # 「超时 / 对方没回」——一个明确的业务结论，调用方会据此继续往下做。
        # 工具自己坏了是另一回事，用 70（sysexits 的 EX_SOFTWARE）单独标出来。
        import traceback
        print(f"✗ 内部错误：{type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
