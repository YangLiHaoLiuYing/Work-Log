#!/usr/bin/env bash
# waker.sh —— 让"响应式会话"保持在线：板上有冲你来的事，就叫醒你。
#
# 背景：agent 会话（WorkBuddy / 各类 CLI）没有后台轮询，只在被唤醒时运行。
# 协议层的「保持在线」= hold 住不收工 + 唤醒即读；而"醒来"需要外部驱动 —— 就是这个脚本。
# 它只**退出**（把宿主的后台任务唤醒），**不替任何 agent 回话** ⇒ 不会与别人的
# listener 抢同一条喊话、重复回执。
#
# 用法：bash waker.sh --dir <板目录> --agent <你的名字> [--window-min 45] [--interval 15]
# 停止：touch <板目录>/waker-stop
set -uo pipefail

DIR=""; AGENT=""; WINDOW_MIN=45; INTERVAL=15
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)        DIR="${2:-}"; shift 2 ;;
    --agent)      AGENT="${2:-}"; shift 2 ;;
    --window-min) WINDOW_MIN="${2:-0}"; shift 2 ;;
    --interval)   INTERVAL="${2:-0}"; shift 2 ;;
    -h|--help)    sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "✗ 未知参数 $1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

die() { echo "✗ $1" >&2; exit 2; }
[ -n "$DIR" ] || die "缺 --dir（板目录；本工具没有默认落点，不猜）"
[ -n "$AGENT" ] || die "缺 --agent（你在这块板上叫什么名字）"
case "$INTERVAL" in ''|*[!0-9]*) die "--interval 要是正整数秒（15 比较合适）" ;; esac
[ "$INTERVAL" -ge 1 ] || die "--interval 至少 1 秒"
case "$WINDOW_MIN" in ''|*[!0-9]*) die "--window-min 要是正整数分钟" ;; esac
[ "$WINDOW_MIN" -ge 1 ] || die "--window-min 至少 1 分钟"
[ -f "$DIR/user.md" ] || die "$DIR 里没有 user.md —— 这不像一块 work-log 板（先 init）"

# 「什么算一条用户消息」**一律问引擎**，不在本脚本里复刻判据。
# 教训（2026-09-29 实测）：我原先用 '^[ts] 用户：' 正则自己数，图的是"别人的回执别吵醒我"；
# 但回执其实是写进 board.md 的（实测 read-user/ack-user/post/ask/reply 都不碰 user.md），
# 而 user.md **允许不带前缀的纯文本**——引擎 parse_user 把每个非空、非 #/> 的行都算一条。
# 实测 1 条带前缀 + 1 条纯文本：引擎 user_total=2、我的正则=1
# ⇒ 用户按文档写纯文本时**永远叫不醒我**。防了一个不存在的威胁，凿出一个真缺口。
# 判据只能有一处，所以问引擎要。status 就是 check --readonly 的别名 —— 读者不该改板。
u_total() {
  "$PY" "$HERE/work_log.py" --dir "$DIR" status --json 2>/dev/null \
    | "$PY" -c 'import sys, json
try:
    print(int(json.load(sys.stdin).get("user_total") or 0))
except Exception:
    print("")'
}
# 有没有人问我 —— **问引擎的结构化字段**，不要去 grep 人话。
# 教训（2026-09-29 实测）：我原来用 `brief --peek | grep -q '待你回应'`，而 brief 的正文是
# 给人看的散文 ⇒ 别人一条 post 里只要出现「待你回应」四个字（比如在讨论本条判据本身！），
# 就会把我叫醒。实测被这么误唤醒过一次：当时板上**根本没有**任何待我回应的提问。
# brief 没有 --json，所以改取 status --json 的 open_questions[].to（status 是只读别名，
# 不推进任何游标 —— 推进游标就等于替你读了，你真醒时东西就没了）。
pend_ask() {
  "$PY" "$HERE/work_log.py" --dir "$DIR" status --json 2>/dev/null \
    | "$PY" -c 'import sys, json
me = sys.argv[1]
try:
    qs = json.load(sys.stdin).get("open_questions") or []
except Exception:
    raise SystemExit(1)
print("YES" if any((q or {}).get("to") == me for q in qs) else "NO")' "$AGENT" \
    | grep -q '^YES$'
}

U0="$(u_total)"
[ -n "$U0" ] || die "读不出 user_total（引擎没跑通）—— 先自己跑一次：$PY $HERE/work_log.py --dir $DIR status --json"
TICKS=$(( WINDOW_MIN * 60 / INTERVAL ))
echo "waker 上线：agent=${AGENT}  板=${DIR}"
echo "  盯两件事：①用户新消息（当前 ${U0} 条） ②有人 ask 到你（待你回应）"
echo "  窗口 ${WINDOW_MIN} 分钟，每 ${INTERVAL}s 看一眼；停止：touch ${DIR}/waker-stop"
i=0
while [ "$i" -lt "$TICKS" ]; do
  sleep "$INTERVAL"
  i=$(( i + 1 ))
  if [ -f "$DIR/waker-stop" ]; then
    echo "WAKER-STOPPED：看到 waker-stop，退出"
    exit 0
  fi
  U1="$(u_total)"
  if [ -z "$U1" ]; then
    # 引擎这一轮没读出来：**既不许当 0**（那会静默地永不唤醒），**也不许当有新增**
    # （那会假唤醒）。如实报一句，下个 tick 重试，别猜。
    echo "⚠ 第 ${i} 轮读不出 user_total（引擎没跑通），本轮跳过喊话判断" >&2
  elif [ "$U1" -gt "$U0" ]; then
    echo "WAKE：用户新消息（${U0} → ${U1} 条）"
    exit 0
  elif [ "$U1" -lt "$U0" ]; then
    # user.md 变短了（有人手工删过消息）：基线必须跟着降，否则从此恒有 U1<U0 ⇒ 静默失聪。
    echo "⚠ user.md 从 ${U0} 条变成 ${U1} 条（被删过？），基线已重设" >&2
    U0="$U1"
  fi
  # 每 4 个 tick（默认 60s）查一次"有没有人问我"：起 python 有成本，不必每 15s 查
  if [ $(( i % 4 )) -eq 0 ] && pend_ask; then
    echo "WAKE：有人 ask 到你（待你回应）"
    exit 0
  fi
done
echo "WAKER-WINDOW-END：${WINDOW_MIN} 分钟窗口到，该续期了"
exit 0
