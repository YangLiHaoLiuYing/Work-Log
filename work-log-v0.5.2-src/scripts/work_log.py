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
from datetime import datetime
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
COOLDOWN = 135          # 同一 agent 重复告警的最小间隔（秒）
MAX_ALERTS = 200        # state.json 里保留的告警条数上限，防止无限增长
MAX_EXCHANGES = 500     # 保留的问答条数上限（未闭环的永不裁）
MAX_ACKS = 300          # 保留的用户回执条数上限
MAX_DIALOGUE = 400      # 保留的「定向对话流水」条数上限（乒乓熔断用的原始数据）
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
EXIT_PEER_DONE = 3      # 业务：对端已收工，这条答案不会来了
EXIT_BREAKER = 4        # 业务：被通信熔断/心跳预算拒绝
EXIT_INTERNAL = 70      # 工具自己坏了（sysexits 的 EX_SOFTWARE）

DONE_TAGS = {"任务完成", "完成", "done", "DONE", "TASK_DONE", "收工"}
RESERVED_AGENT = "watchdog"
# 人类用户在对话里的保留身份：agent 可以向「用户」提问（ask --to 用户），
# 人用 `reply --agent 用户 --id N` 直接回答，await 原生能等到这条答复。
# 但任何 agent 都不许叫这个名字注册/发言 —— 那会冒充人类、污染对话归属。
USER_NAME = "用户"
# 多人协作的启动条件：同时「在干活」的 agent 达到这个数量。
# 「在干活」= 已开工（写过心跳）、未收工、且静默未超阈值（心跳中 / 挂起中）。
COLLAB_MIN = 2
# 自动 UI（理想流程：第 2 个 agent 上线 → 自动起 serve + 弹浏览器）。
# WORK_LOG_NO_AUTO_UI=1 整个关掉（不自动起、不弹）；WORK_LOG_NO_UI=1 只关
# 「弹浏览器」但仍然自动起 serve —— selftest 靠它在 CI 里验证自动接入、又不开真浏览器。
UI_ALL_ENV = "WORK_LOG_NO_AUTO_UI"
UI_OPEN_ENV = "WORK_LOG_NO_UI"

ENTRY_RE = re.compile(r"^<(?P<agent>[^>\s]+)>\s+(?P<time>\d{2}:\d{2}:\d{2})\s*(?P<rest>.*)$")
DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")
TAG_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<body>.*)$", re.S)
USER_RE = re.compile(r"^\[(?P<time>\d{2}:\d{2}:\d{2})\]\s*(?P<who>[^：:]{0,12})[：:]\s*(?P<body>.*)$")
AGENT_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]{1,32}$")

_WARNED_NO_FLOCK = False

BOARD_HEADER = """# work-log 看板

> 任务：{task}
> 行格式：`<agent> HH:MM:SS [标签] 内容`
> 铁律：每个 agent 每 <= {hb}s 至少写一条心跳
> 标签：收到 / 决定 / 执行 / 阻塞 / 建议 / 任务完成
> 看门狗：`work_log.py watch` —— 默认每 {hb}s 扫一次，静默超过 {stale}s 且未写「任务完成」即告警
> 用户喊话：`work_log.py say --text "..."`（或直接编辑 user.md）
> 用户交流：agent 可 `ask --to 用户`，人用 `reply --agent 用户 --id N` 回答；`status` 可见全部待答
> 多人协作：≥2 个 agent 同时在干活即亮起「协作」标记（这是本工具的启动条件）
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


def default_log_dir() -> Path:
    """默认把看板放桌面：日志要"看得见、点得开"才有人真的盯。

    但**再按当前目录名分一层**。只放桌面是错的 —— 两个项目会共用同一块看板，
    甲项目的看门狗会去报乙项目 agent 的「卡死」。可见性和隔离必须同时成立。
    项目里想收归本地，用 `--dir .workbuddy/work-log` 或 `$WORK_LOG_DIR` 覆盖。
    """
    desk = Path.home() / "Desktop"
    base = desk if desk.is_dir() else Path.cwd()
    proj = Path.cwd().resolve().name or "root"   # 根目录下 name 为空，兜底
    return base / DEFAULT_DIRNAME / proj


def die(msg: str, code: int = EXIT_USAGE):
    """前置条件/用法错误：打一条 ✗ 到 stderr，然后按 EXIT_USAGE 退出。

    存在的唯一理由见文件顶部退出码那段注释：`sys.exit("字符串")` 会退 1，
    而 1 在协议里是业务码。凡是不属于"业务结论"的失败都必须走这里。
    """
    print(msg if msg.startswith("✗") else f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


def resolve_dir(args) -> Path:
    d = getattr(args, "dir", None) or os.environ.get("WORK_LOG_DIR")
    p = Path(d).expanduser() if d else default_log_dir()
    if p.exists() and not p.is_dir():
        die(f"{p} 已存在且不是目录，换个 --dir")
    p = p.resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def sane_stale(v: float) -> float:
    if v < 1:
        die("--stale-after 至少 1 秒（设 0 会让所有 agent 立刻被判卡死）")
    return v


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


def acked_user_ids(st: dict) -> set:
    return {k["id"] for k in st.get("acks", [])}


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


def ensure_files(d: Path, st: dict) -> None:
    if not p_board(d).exists():
        p_board(d).write_text(
            BOARD_HEADER.format(
                task=st.get("task") or "(未命名)",
                hb=st.get("heartbeat_secs", HEARTBEAT),
                stale=STALE_AFTER,
            ),
            encoding="utf-8",
        )
    if not p_user(d).exists():
        p_user(d).write_text(USER_HEADER, encoding="utf-8")
    if not p_alerts(d).exists():
        p_alerts(d).write_text(ALERTS_HEADER, encoding="utf-8")


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

    返回 {agent: {"ts": float, "tag": str|None, "text": str, "n": int}}
    """
    out: dict = {}
    for e in board_entries(d):
        if e["agent"] == RESERVED_AGENT:     # 看门狗自己写板，不参与心跳判定
            continue
        prev = out.get(e["agent"])
        if prev is None:
            out[e["agent"]] = {"ts": e["ts"], "tag": e["tag"], "text": e["text"], "n": 1}
        else:
            prev["n"] += 1
            if e["ts"] >= prev["ts"]:
                prev.update({"ts": e["ts"], "tag": e["tag"], "text": e["text"]})
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
    al = st.get("alerts", [])
    if len(al) > MAX_ALERTS:
        st["alerts"] = al[-MAX_ALERTS:]


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
            out.append({
                "kind": "互相等待", "key": f"cycle:{pair[0]}:{pair[1]}",
                "agents": list(pair),
                "text": (f"<{pair[0]}> 与 <{pair[1]}> 正互相等对方回应（各卡在自己的 await 里）——"
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
    rows, stale, idle = [], [], []
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
        if tag in DONE_TAGS:
            ag["status"] = "done"
            ag["done_at"] = ag.get("done_at") or last
        done = ag.get("status") == "done"
        holding = float(ag.get("expected_silence_until", 0.0)) > t
        started = entries > 0 or name in board
        silence = max(0.0, t - last)
        if not started:
            # 只被注册过、从没写过心跳：这是"没派出去 / 名字打错"，不是"卡死"。
            # 两者处置方式不同，所以单独一档，绝不产生告警噪音。
            state = "待启动"
            idle.append(name)
        elif done:
            state = "完成"
        elif holding:
            state = "挂起中"
        elif silence > stale_after:
            state = "疑似卡死"
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
            "alert_open": bool(ag.get("alert_open")),
        })
    # 多人协作感知：心跳中 / 挂起中 = 「正在干活」（已开工、未收工、静默未超阈值）。
    # 这是整个工具的启动条件 —— 一个 agent 独自干活用不上看板，≥COLLAB_MIN 才是它的主场。
    working = [r["name"] for r in rows if r["state"] in ("心跳中", "挂起中")]
    res = {
        "ts": t,
        "stale_after": stale_after,
        "agents": rows,
        "stale": stale,
        "idle": idle,
        "by_name": {r["name"]: r for r in rows},
        "user_total": len(parse_user(d)),
        "alerts_unacked": unacked(st),
        "collab": {"count": len(working), "agents": working,
                   "active": len(working) >= COLLAB_MIN, "required": COLLAB_MIN},
    }
    # 危险等待要在 agent 状态算完之后才能判（要用 by_name 里的 state/silence）；
    # 通信过热也要用 res["ts"] 做时间基准。
    res["hazards"] = coordination_hazards(st, res)
    return res


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
        if ag.get("alert_open") and t - float(ag.get("last_alert_at", 0.0)) < cooldown:
            continue
        escalated = bool(ag.get("alert_open"))
        ag["alert_open"] = True
        ag["last_alert_at"] = t
        st["alert_seq"] = int(st.get("alert_seq", 0)) + 1
        aid = st["alert_seq"]
        secs = int(res["by_name"][name]["silence"])
        if escalated:
            txt = (f"<{name}> 仍无心跳（已静默 {secs}s，重复告警 #{aid}）"
                   f"→ 在线 agent 请直接接管：先看它的最后一条日志，再抢它的锁、重派它的任务")
        else:
            txt = (f"<{name}> 已静默 {secs}s（阈值 {int(res['stale_after'])}s）且未写「任务完成」"
                   f"→ 疑似卡死，请在线 agent 检查该 agent 是否卡死并修复")
        line = append_entry(d, st, "watchdog", txt, tag="告警")
        with open(p_alerts(d), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        st["alerts"].append({"id": aid, "agent": name, "ts": t, "silence": secs,
                             "text": txt, "acked_by": None})
        written.append(line)
    # 危险等待（不可达 / 死锁）也要告警：这类故障心跳全绿，只有看等待图才看得见。
    # 按 key 去重 + 冷却，并且险情一旦消失就把 key 清掉，下次复发了还能再报一次。
    haz = res.get("hazards") or []
    hseen = st.setdefault("hazard_seen", {})
    live = {h["key"] for h in haz}
    for k in list(hseen):
        if k not in live:
            hseen.pop(k, None)
    for h in haz:
        if h["key"] in hseen and t - float(hseen[h["key"]]) < max(cooldown, 60):
            continue
        hseen[h["key"]] = t
        st["alert_seq"] = int(st.get("alert_seq", 0)) + 1
        aid = st["alert_seq"]
        txt = f"[{h['kind']}] {h['text']}"
        line = append_entry(d, st, "watchdog", txt, tag="告警")
        with open(p_alerts(d), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        st["alerts"].append({"id": aid, "agent": h["agents"][0], "ts": t, "silence": 0,
                             "text": txt, "acked_by": None, "hazard": h["key"]})
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
               f"卡死阈值 {int(res['stale_after'])}s")
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
        flag = ""
        if r["state"] == "疑似卡死":
            flag = "  <= 疑似卡死"
        elif r["state"] == "待启动":
            flag = "  <= 从未写过心跳"
        elif r["alert_open"]:
            flag = "  <= 告警未确认"
        elif warn_at and r.get("posts_per_min", 0) >= warn_at:
            flag = f"  <= 心跳偏密 {r['posts_per_min']}/min"
        hold = f"  挂起至 {r['holding_until']}" if r["holding_until"] else ""
        out.append(f"{r['name']:<12} {r['state']:<6} 最后 {r['last_seen']}  静默 {r['silence']:>6.1f}s  "
                   f"条目 {r['entries']:>3}{hold}{flag}")
    if not res["agents"]:
        out.append("（还没有任何 agent 注册——先 post 一条心跳）")
    out.append("-" * 72)
    # 多人协作启动条件：用户一眼判断"这块看板该不该开"。
    col = res.get("collab") or {}
    if col:
        names = "、".join(col["agents"]) or "无"
        if col.get("active"):
            out.append(f"🤝 协作：{col['count']} 个 agent 在干活（{names}）—— 多人协作模式")
        else:
            out.append(f"🤝 协作：{col['count']} 个 agent 在干活（{names}）"
                       f"—— 未达多人协作（需 ≥{col.get('required', COLLAB_MIN)}）")
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
    for a in pending[-8:]:
        out.append(f"  #{a['id']} [{hhmmss(a['ts'])}] <{a['agent']}> 静默 {a['silence']}s 未确认")
    if res.get("idle"):
        out.append(f"待启动 {len(res['idle'])} 个：{'、'.join(res['idle'])}"
                   "（不是卡死；检查是否漏派任务或 agent 名写错）")
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


# ---------------------------------------------------------------- 子命令

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
        if getattr(a, "no_auto_ui", False):
            st["auto_ui"] = False        # 关掉"第 2 个 agent 上线自动弹协作界面"
        ensure_files(d, st)
        t = now()
        for name in a.agents.split(",") if a.agents else []:
            name = name.strip()
            if name:
                check_agent_name(name)
                ag = _get_agent(st, name, t)
                ag["note"] = "由 init 预注册"
        save_state(d, st)
    print(f"✓ 看板就绪：{d}")
    print(f"  看板   {p_board(d)}")
    print(f"  用户   {p_user(d)}")
    print(f"  告警   {p_alerts(d)}")
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
                f"→ {sys.argv[0]} reply --agent {agent} --id {q['id']} --text \"…\"")
    if not n:
        return ""
    extra = f"，其中 {len(m)} 条用户喊话" if m else ""
    return (f"有 {n} 条新动态{extra}，看一眼再往下做："
            f"{sys.argv[0]} brief --agent {agent}")


def cmd_brief(a):
    """增量投喂：只给"自你上次读过之后"发生的交流，不刷屏全板。"""
    d = resolve_dir(a)
    check_agent_name(a.agent)
    a.stale_after = sane_stale(a.stale_after)
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        ag = _get_agent(st, a.agent, now())
        entries, msgs = _feed_since(d, st, a.agent, advance=not a.peek)
        res = evaluate(d, st, a.stale_after)
        pending = unacked(st)
        locks = dict(st.get("locks", {}))
        if not a.peek:
            save_state(d, st)

    lines = []
    lines.append(f"—— work-log 增量 · {a.agent} · {hhmmss()} ——")
    # 待我回应的问题排最前：这是唯一必须由"我"来闭环的事
    for q in open_exchanges(st, to=a.agent):
        waited = int(now() - q["ts"])
        lines.append(f"❗ 待你回应 #{q['id']}（{waited}s）<{q['from']}>：{q['question']}")
        lines.append(f"   → {sys.argv[0]} reply --agent {a.agent} --id {q['id']} --text \"你的答复\"")
    for q in open_exchanges(st, frm=a.agent):
        waited = int(now() - q["ts"])
        tgt = res["by_name"].get(q["to"], {})
        warn = f"  ⚠ {q['to']} 现在「{tgt['state']}」，别干等" if tgt.get("state") in (
            "疑似卡死", "完成", "待启动") else ""
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
                     f" → 认领：{sys.argv[0]} ack-user --agent {a.agent} --id {pend_user[0]['id']} --text \"你改了什么\"")

    status = " / ".join(f"{r['name']} {r['state']}"
                        + (f"（静默 {r['silence']:.0f}s）" if r["state"] == "疑似卡死" else "")
                        for r in res["agents"])
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
    check_agent_name(a.agent)
    check_agent_name(a.to, allow_user=True)   # 提问对象可以是「用户」（人类正门）
    if a.to == a.agent:
        die("✗ 别问自己，直接决定")
    if not (a.text or "").strip():
        die("✗ 问题不能为空")
    a.stale_after = sane_stale(a.stale_after)
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
        elif target["state"] in ("疑似卡死", "完成", "待启动"):
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
        print(f"  等用户回答：{sys.argv[0]} await --agent {a.agent} --id {eid} --timeout 600"
              f"（人类响应可能慢，超时给足）")
        print(f"  用户回答方式：{sys.argv[0]} reply --agent {USER_NAME} --id {eid} --text \"…\""
              f"（跑 status 也能看到这条提问）")
    else:
        print(f"  等它回应：{sys.argv[0]} await --agent {a.agent} --id {eid} --timeout 300")
    if streak >= PINGPONG_WARN and a.to != USER_NAME:
        print(f"⚠ 你和 <{a.to}> 已经连续来回 {streak} 轮（没有第三方插入）。"
              f"如果这是在无进展地互相确认，请直接 post --tag 决定 收敛；"
              f"到 {PINGPONG_HARD} 轮会被强制熔断。")
    return 0


def cmd_reply(a):
    d = resolve_dir(a)
    # 「用户」在这里是合法的：这就是人类回答 agent 提问的正门。
    check_agent_name(a.agent, allow_user=True)
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
    check_agent_name(a.agent)
    stale_after = sane_stale(getattr(a, "stale_after", STALE_AFTER))
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
                        elif res["by_name"].get(ex["to"], {}).get("state") == "完成":
                            lost[i] = ex["to"]          # 对端已收工，这条永远不会来
                    waiting = [i for i in ids if i not in answers and i not in lost]
                # 把自己的等待关系写进共享状态：这是「等待图」唯一的精确来源。
                # 靠"有没有 open 的提问"猜是不够的 —— 提问开着，不代表提问者此刻真的在等。
                # 多路等待会把每个还没回应的目标都记下来，等待图据此给每人建一条边。
                ag["awaiting"] = ({"ids": waiting, "id": waiting[0], "since": start}
                                  if waiting else None)
                save_state(d, st)
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
        try:
            with locked(d):
                st = load_state(d)
                ag = st["agents"].get(a.agent)
                if ag is not None:
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
    check_agent_name(a.agent)
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
    check_agent_name(a.agent)
    if not (a.text or "").strip():
        die("✗ --text 不能为空 —— 空心跳等于没写，看门狗会当它没存在过")
    t = now()
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        blocked, sent, cap = post_guard(st, a.agent, t)
        if blocked:
            # 刻意**不**刷新 last_seen：一个停不下来的 agent 不是"活着"，是"空转"。
            # 让它在 stale_after 之后同时被标成疑似卡死，两处报警比一处更难被忽略。
            save_state(d, st)
            print(blocked)
            return 4
        ag = _get_agent(st, a.agent, t)
        tag = a.tag or ("任务完成" if a.done else None)
        line = append_entry(d, st, a.agent, a.text, tag=tag, ts=t)
        ag["last_seen"] = t
        ag["entries"] = int(ag.get("entries", 0)) + 1
        ag["expected_silence_until"] = 0.0
        if ag.get("alert_open"):
            ag["alert_open"] = False
            ag["pending_recovery"] = True
        if a.done:
            ag["status"] = "done"
            ag["done_at"] = t
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
    if a.done:
        print(f"✓ {a.agent} 已标记完成，看门狗不再对它报卡死")
    if others:
        print(f"🤝 多人协作中：{'、'.join(others)} 也在干活（在线 {len(others) + 1}）")
    if ui_hint:
        print(ui_hint)
    if hint:
        print(f"💬 {hint}")
    return 0


def cmd_hold(a):
    d = resolve_dir(a)
    check_agent_name(a.agent)
    t = now()
    with locked(d):
        st = load_state(d)
        ensure_files(d, st)
        ag = _get_agent(st, a.agent, t)
        until = t + max(1, a.seconds)
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
        print("  长任务跑完记得 release，或直接 post 一条心跳（会自动解除挂起）")
    return 0


def cmd_release(a):
    d = resolve_dir(a)
    check_agent_name(a.agent)
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


def _spawn_serve(d: Path, port: int) -> int | None:
    """脱离父进程起一个 serve（终端关了也活着），日志落到看板目录 serve.log。"""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--dir", str(d),
           "serve", "--port", str(port), "--interval", "5"]
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
    for cand in range(base, base + 4):
        url = f"http://localhost:{cand}/"
        if _ui_running(cand, d):                    # 已有**本项目**的视图在跑 → 直接用
            st["ui_opened"] = True
            st["ui_port"] = cand
            opened = _open_browser(url)
            return f"🖥 协作界面：{url}" + ("（已在浏览器打开）" if opened else "")
        pid = _spawn_serve(d, cand)
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
        return (f"🖥 协作界面已自动启动：{url}"
                        + ("（浏览器已打开" if opened else "（未弹浏览器")
                        + f"；停止：kill {pid}）")
    return None


def cmd_say(a):
    d = resolve_dir(a)
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
    check_agent_name(a.agent)
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
        who = "你已回执 " if n in acked_user_ids(st) else ""
        print(f"[#{n}] {stamp}{m['who']}{who}：{m['body']}")
    if a.peek:
        print(f"—— peek 模式，游标未推进（共 {len(unread)} 条）")
    return 0


def cmd_check(a):
    d = resolve_dir(a)
    a.stale_after = sane_stale(a.stale_after)
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
                          "collab": res.get("collab"),
                          "open_questions": [{"id": q["id"], "from": q["from"], "to": q["to"],
                                              "question": q["question"],
                                              "waited": int(res["ts"] - q["ts"])}
                                             for q in open_exchanges(st)],
                          "hazards": res.get("hazards", []),
                          "alerts_unacked": len(res["alerts_unacked"])}, ensure_ascii=False, indent=2))
    else:
        print(format_check(d, st, res))
        for line in written:
            print(f"\n⚠ {line}")
    # 0 的含义是"全员健康"。协作险情显然不属于健康 —— 尤其 [互相等待] 是
    # 一条真死锁：心跳全绿、没有任何进程崩溃，但整个团队已经不往前走了。
    # 如果这种状态还退 0，脚本里 `check || 告警` 就会把它整个漏掉，
    # 而它偏偏是这套东西最想抓的那类故障。
    unhealthy = bool(res["stale"]) or bool(res.get("hazards"))
    return 1 if unhealthy else 0


def cmd_status(a):
    a.readonly = True
    # 注意：这里**不能**把 a.json 强行置 False —— 那会让 `status --json`
    # 静默退化成人类可读输出（脚本里按 JSON 解析会当场炸），
    # 而 --json 明明在 status 的 usage 里列着。
    return cmd_check(a)


def cmd_watch(a):
    d = resolve_dir(a)
    a.stale_after = sane_stale(a.stale_after)
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
                flags = ",".join(f"{r['name']}:{r['state']}" for r in res["agents"]) or "无 agent"
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


def cmd_ack(a):
    d = resolve_dir(a)
    with locked(d):
        st = load_state(d)
        hit = 0
        for al in st.get("alerts", []):
            if al.get("acked_by"):
                continue
            if a.id and al["id"] != a.id:
                continue
            if a.agent and al["agent"] != a.agent:
                continue
            al["acked_by"] = a.by or "unknown"
            al["acked_at"] = now()
            hit += 1
            if a.id:
                break
        prune_state(st)
        save_state(d, st)
    print(f"✓ 已确认 {hit} 条告警" + (f"（by {a.by}）" if a.by else ""))
    return 0 if hit else 1


def cmd_lock(a):
    d = resolve_dir(a)
    check_agent_name(a.agent)
    check_resource(a.resource)
    a.stale_after = sane_stale(a.stale_after)
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
    check_agent_name(a.agent)
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
    a.stale_after = sane_stale(a.stale_after)
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
    for m in parse_user(d):
        if m["time"]:
            try:
                h, mi, sec = (int(x) for x in m["time"].split(":"))
                ts = datetime.strptime(f"{today()} {h:02d}:{mi:02d}:{sec:02d}",
                                       "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                ts = now()
        else:
            ts = now()               # 手写且没带时间的，当"刚说的"排在末尾
        items.append({"agent": m["who"], "ts": ts, "time": hhmmss(ts), "tag": "喊话",
                      "text": m["body"], "kind": "user"})
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
    a.stale_after = sane_stale(a.stale_after)
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
    with locked(d):
        st0 = load_state(d)
        res0 = evaluate(d, st0, a.stale_after)
    col0 = res0.get("collab") or {}
    names0 = "、".join(col0.get("agents") or []) or "无"
    if col0.get("active"):
        print(f"🤝 协作：{col0['count']} 个 agent 在干活（{names0}）—— 多人协作模式")
    else:
        print(f"🤝 协作：{col0.get('count', 0)} 个 agent 在干活（{names0}）"
              f"—— 未达多人协作（需 ≥{col0.get('required', COLLAB_MIN)}），"
              f"第二个 agent 开工后这里会亮起")
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
KNOWN_OPTS = {
    "--dir", "--agent", "--text", "--tag", "--done", "--task", "--agents",
    "--seconds", "--peek", "--all", "--json", "--readonly", "--stale-after",
    "--cooldown", "--interval", "--ticks", "--quiet", "--replay-user",
    "--id", "--by", "--note", "--resource", "--force", "--help", "-h", "-n",
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
                        help="日志目录（默认 ~/Desktop/work-log/<当前目录名>；可用 $WORK_LOG_DIR 覆盖）")

    p = argparse.ArgumentParser(prog="work_log.py", description="多 agent 心跳看板 / 看门狗 / 用户喊话通道")
    p.add_argument("--dir", default=None,
                   help="日志目录，放在子命令前后都行（默认 ~/Desktop/work-log/<当前目录名>；可用 $WORK_LOG_DIR 覆盖）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", parents=[common], help="初始化看板")
    sp.add_argument("--task", default="", help="任务名")
    sp.add_argument("--agents", default="", help="预注册 agent，逗号分隔")
    sp.add_argument("--rate-cap", type=int, default=None,
                    help=f"单个 agent 每分钟心跳上限（默认 {POST_CAP}；0 = 关闭熔断）")
    sp.add_argument("--no-auto-ui", action="store_true",
                    help="关闭自动协作界面（默认：第 2 个 agent 开工时自动起 serve 并弹浏览器）")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("post", parents=[common], help="写一条心跳（核心命令）")
    sp.add_argument("--agent", required=True, help="agent 名字，如 agent1")
    sp.add_argument("--text", required=True, help="想了什么 / 干了什么")
    sp.add_argument("--tag", default=None, help="标签：收到/决定/执行/阻塞/建议/任务完成")
    sp.add_argument("--done", action="store_true", help="等同 --tag 任务完成")
    sp.set_defaults(func=cmd_post)

    sp = sub.add_parser("hold", parents=[common], help="声明要跑长任务，期间不判卡死")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--seconds", type=int, required=True)
    sp.add_argument("--text", default="")
    sp.add_argument("--quiet", action="store_true",
                    help="只挂起不写心跳：给「每秒都在跑长任务」的驱动层用（如 LLM agent 每步都要调模型），"
                         "避免把看板刷满「进入长任务」这种无信息量的阻塞条目")
    sp.set_defaults(func=cmd_hold)

    sp = sub.add_parser("release", parents=[common], help="解除挂起")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--text", default="")
    sp.add_argument("--quiet", action="store_true",
                    help="静默解除：只清挂起窗口不写心跳条目（驱动层退出前清理自己用）")
    sp.set_defaults(func=cmd_release)

    sp = sub.add_parser("say", parents=[common], help="用户喊话（也用于 agent 以用户身份留言）")
    sp.add_argument("--text", required=True)
    sp.add_argument("--as", dest="as_", default="用户")
    sp.set_defaults(func=cmd_say)

    sp = sub.add_parser("ask", parents=[common],
                        help="定向提问某个 agent（会等它回应，不是广播）")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--to", required=True, help="问谁")
    sp.add_argument("--text", required=True, help="问题")
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER)
    sp.add_argument("--force", action="store_true",
                    help=f"明知已连续来回 {PINGPONG_HARD} 轮也要继续问（默认会被熔断拦住）")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("reply", parents=[common], help="回应别人对你的提问")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--id", type=int, required=True, help="提问编号，见 brief")
    sp.add_argument("--text", required=True)
    sp.add_argument("--force", action="store_true", help="明知已被熔断也要继续答")
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("await", parents=[common],
                        help="阻塞等回应／等别人动（把「等它搞完」变成机制）")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--id", dest="ids", type=id_list, default=[],
                    help="等这些提问的回应，逗号分隔（如 1,2,3）；不给则等到任何新动态")
    sp.add_argument("--any", dest="any_mode", action="store_true",
                    help="多路等待时：任一回应即算达成（默认要全部）")
    sp.add_argument("--timeout", type=float, default=300)
    sp.add_argument("--interval", type=float, default=2)
    sp.add_argument("--report", type=float, default=15, help="每隔多少秒报一次进度")
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER,
                    help="判定「对方已收工 → 立刻收手」用的阈值")
    sp.set_defaults(func=cmd_await)

    sp = sub.add_parser("ack-user", parents=[common], help="认领用户喊话（用户才知道有人管了）")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--id", type=int, required=True, help="喊话编号，见 read-user")
    sp.add_argument("--text", required=True, help="你改了什么")
    sp.set_defaults(func=cmd_ack_user)

    sp = sub.add_parser("brief", parents=[common],
                        help="探针：取上次读过之后的交流增量（别人的心跳 + 用户喊话 + 告警）")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--peek", action="store_true", help="只看不推进游标")
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER)
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("read-user", parents=[common], help="取走用户未读喊话")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--peek", action="store_true", help="只看不推进游标")
    sp.add_argument("--all", action="store_true", help="从头发全部")
    sp.set_defaults(func=cmd_read_user)

    sp = sub.add_parser("check", parents=[common], help="扫一次并补告警（有卡死则退出码 1）")
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER)
    sp.add_argument("--cooldown", type=float, default=COOLDOWN)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--readonly", action="store_true", help="只读，不写告警")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("status", parents=[common], help="check --readonly 的别名（只看不改）")
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER)
    sp.add_argument("--cooldown", type=float, default=COOLDOWN)
    sp.add_argument("--readonly", action="store_true", default=True)
    sp.add_argument("--json", action="store_true", default=False)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("watch", parents=[common], help="看门狗：每 15s 扫一次并告警")
    sp.add_argument("--interval", type=float, default=HEARTBEAT)
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER)
    sp.add_argument("--cooldown", type=float, default=COOLDOWN)
    sp.add_argument("--ticks", type=int, default=0, help="跑多少轮后退出（0=不限）")
    sp.add_argument("--quiet", action="store_true", help="只在有变化时打印")
    sp.add_argument("--replay-user", action="store_true", help="启动时把历史喊话也播一遍")
    sp.add_argument("--readonly", action="store_true")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("ack", parents=[common], help="确认告警（在线 agent 接管后调用）")
    sp.add_argument("--id", type=int, default=0)
    sp.add_argument("--agent", default="", help="只确认该 agent 的告警")
    sp.add_argument("--by", default="", help="谁确认的")
    sp.set_defaults(func=cmd_ack)

    sp = sub.add_parser("lock", parents=[common], help="抢占共享资源（GPU/端口/文件）")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--resource", required=True)
    sp.add_argument("--note", default="")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER)
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser("unlock", parents=[common], help="释放资源")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--resource", required=True)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_unlock)

    sp = sub.add_parser("locks", parents=[common], help="列出占用情况")
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER)
    sp.set_defaults(func=cmd_locks)

    sp = sub.add_parser("serve", parents=[common], help="起本地实时视图，浏览器里看 agent 交流")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--host", default="127.0.0.1", help="默认只绑本机，别改成 0.0.0.0")
    sp.add_argument("--interval", type=float, default=HEARTBEAT, help="内置看门狗扫描间隔")
    sp.add_argument("--stale-after", type=float, default=STALE_AFTER)
    sp.add_argument("--cooldown", type=float, default=COOLDOWN)
    sp.add_argument("--no-watch", dest="watch", action="store_false", default=True,
                    help="只开视图，不跑内置看门狗")
    sp.set_defaults(func=cmd_serve)

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
    argv = _fix_dash_values(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    try:
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
