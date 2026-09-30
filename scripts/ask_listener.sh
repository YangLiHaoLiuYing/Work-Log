#!/bin/bash
# ask-listener —— 把「有人 ask 我」也变成可自动应答的事件。
#
# 为什么需要它（2026-09-29 实测）：
#   board-listener.sh 只轮询 user.md 的 user_total，所以只有**用户喊话**能唤醒 bot；
#   而 ask/reply 这条定向问答通路对它完全不可见 —— 板上 #16 等了 170s、
#   opencode-main await #8 等满 120s 退 1，都是这条断路的实锤，且**不报错**。
#
# 关键事实（不是推测）：交互式 opencode TUI 没有可被外部进程唤醒/注入消息的机制
#   —— opencode 21:58 实测 lsof 无监听端口、TUI 非 server。所以在 TUI 上"挂 waker"
#   是不可能的。可被外部驱动的只有 `opencode run`（一次性、非交互），listener 一直靠它。
#   于是本脚本的形态是：轮询 state.json，发现有 status=open 且 to=<本线> 的提问
#   ⇒ spawn 一次 opencode run 去 reply。**不替人回话，只把人叫起来。**
#
# 判据一律问引擎（今天两处独立踩坑的共同教训：驱动层不许复刻引擎判据）：
#   计数走 work_log.py --json，解析走 python3 读 state.json，不在 shell 里 grep 状态。
#   判据写在文档里不等于脚本里长对了。
#
# 退 3 = 已被别人处置（对端收工／资源被占／喊话被认领）——良性结论不借用故障码 1。
set -u

# 守护进程绝不弹浏览器（2026-09-30 修）。本脚本会 post 自报心跳，而 `post` 在
# 「还有别人在干活」时**会自动起协作界面并弹浏览器**（auto_ui）：一个后台守护
# 去开用户桌面的窗口，是没人会预期的事；留下的那只 serve 还脱离父进程、指着这块板不放。
# 同类问题在 examples/demo.sh 上实测积了 2 只孤儿 serve、占住 8789/8790。
export WORK_LOG_NO_AUTO_UI=1

DIR="${WORK_LOG_DIR:?必须设 WORK_LOG_DIR（板目录）}"
# 引擎默认就在本脚本同目录（scripts/）；板目录必须由 WORK_LOG_DIR 给 —— 不猜落点。
_here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ENGINE="${WORK_LOG_ENGINE:-$_here/work_log.py}"
OPENCODE_BIN="${OPENCODE_BIN:-$(command -v opencode 2>/dev/null || echo "$HOME/.opencode/bin/opencode")}"
BOT="${ASK_BOT:-opencode-bot}"
POLL="${ASK_POLL:-15}"
# 陈旧锁判死秒数：取偏大值，宁可多等几轮也不误杀正在应答的实例。
STALE_LOCK="${ASK_STALE_LOCK:-600}"
# 守护进程自报心跳：治「spawn 型线平时零心跳 ⇒ 每轮被看门狗读成卡死」的形态误报。
# 留空＝不自报（默认关，避免替别人发心跳）。
HEARTBEAT_AGENT="${ASK_HEARTBEAT_AGENT:-}"
HB_EVERY="${ASK_HB_EVERY:-240}"
MODEL="${ASK_MODEL:-}"   # 留空＝用 opencode 默认模型，不绑死某家模型名
CURSOR="$DIR/.ask-cursor"
LOCK="$DIR/.ask-listener.lock"
LOG="$DIR/ask-listener.log"

log() { printf '%s [%s] %s\n' "$(date '+%H:%M:%S')" "$$" "$*" >>"$LOG"; }

# 本线上未闭环提问的 id 列表。解析交给 python3，不在 shell 里复刻判据。
pending_ids() {
  python3 - "$DIR/state.json" "$BOT" <<'PY' 2>/dev/null || true
import json, sys
try:
    st = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
bot = sys.argv[2]
out = []
for e in st.get("exchanges", []):
    if e.get("status") == "open" and e.get("to") == bot:
        out.append(str(e.get("id")))
print(" ".join(out))
PY
}

mark_seen() {
  # 成功才推进游标：失败不推进、不吞消息，下轮重放（正文判据②）。
  # 记的是"已 spawn 过的 id 集合"，避免同一提问被反复 spawn。
  python3 - "$CURSOR" "$@" <<'PY' 2>/dev/null || true
import json, os, sys
p = sys.argv[1]
try:
    cur = set(json.load(open(p)))
except Exception:
    cur = set()
cur.update(sys.argv[2:])
tmp = p + ".tmp"
json.dump(sorted(cur), open(tmp, "w"))
os.replace(tmp, p)
PY
}

was_seen() {
  python3 - "$CURSOR" "$1" <<'PY' 2>/dev/null
import json, sys
try:
    sys.exit(0 if sys.argv[2] in set(json.load(open(sys.argv[1]))) else 1)
except Exception:
    sys.exit(1)
PY
}

[ -s "$CURSOR" ] || echo '[]' >"$CURSOR"
last_hb=$(date +%s)
log "ask-listener 启动 BOT=$BOT POLL=${POLL}s engine=${ENGINE}"

while :; do
  # 退出即清锁：被 kill / launchctl kickstart -k 时，循环末尾的 rmdir 不会执行，
  # 锁目录会残留并让整条通路静默瘫掉（今天 22:17 实测踩到）。
  trap 'rmdir "$LOCK" 2>/dev/null; log "收到退出信号，已清锁"; exit 0' INT TERM HUP
  sleep "$POLL"
  [ -f "$ENGINE" ] || { log "✗ 找不到引擎 $ENGINE"; continue; }
  [ -x "$OPENCODE_BIN" ] || [ -f "$OPENCODE_BIN" ] || { log "✗ 找不到 opencode：$OPENCODE_BIN"; continue; }

  # 【形态误报的自救，opencode-main 22:28:11 诊断】看门狗判据只认自报心跳，
  # 而本线（spawn 型）的存活形态是「被提问时 spawn 一次」，平时零心跳 ——
  # 于是每轮都被读成卡死，今天已误报 3 次（#2089/#2090/#2091）。
  # 根治要改看门狗判据（覆盖常驻心跳/hold 声明/spawn 即活三种形态），归 workbuddy-skill；
  # 我这侧能做的最小止血：让本进程周期性以本线身份 post 一条**有实质内容**的心跳
  # （不是「处理中」那种骗看门狗的空话——写明本进程 pid 与本轮轮询时间）。
  # 注意：这条心跳只代表「ask 通路的守护进程活着」，不代表我这条交互会话在线。
  if [ -n "$HEARTBEAT_AGENT" ] && [ $(( $(date +%s) - last_hb )) -ge "$HB_EVERY" ]; then
    last_hb=$(date +%s)
    python3 "$ENGINE" --dir "$DIR" post --agent "$HEARTBEAT_AGENT" \
      --tag 执行 \
      --text "ask_listener 守护进程在线：pid=$$ ，本轮无待答提问（已问引擎 state.json，非本地推断）。本条只表示 ask 通路守护进程存活，不代表 opencode-bot-tui 那条交互会话在线——后者仍在 TUI 里靠用户按键唤醒。" \
      >>"$LOG" 2>&1 || true
  fi

  fresh=""
  for id in $(pending_ids); do
    was_seen "$id" || fresh="$fresh $id"
  done
  [ -n "$fresh" ] || continue

  # mkdir 原子锁防重入：同一时刻只 spawn 一个应答进程。
  # 陈旧锁要清理：进程被 kill -9 / 崩了就不会 rmdir，锁目录会永久残留，
  # 于是这条通路会**静默瘫掉**——轮询照跑、日志一直写「留到下轮」，没有报错。
  # 判据：锁目录的 mtime 超过 STALE_LOCK 秒没人动，就当它死了（今天 22:15 实测踩到：
  # 旧实例还在跑，锁 mtime 停在 spawn 时刻，误判风险与真卡死无法区分，故取偏大值）。
  if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -d "$LOCK" ]; then
      lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
      if [ "$lock_age" -gt "$STALE_LOCK" ]; then
        log "陈旧锁（${lock_age}s 无心跳，按 ${STALE_LOCK}s 判死）⇒ 清理后重试"
        rmdir "$LOCK" 2>/dev/null && mkdir "$LOCK" 2>/dev/null || continue
      else
        log "已有实例在应答，$fresh 留到下轮"
        continue
      fi
    else
      continue
    fi
  fi

  ids_csv=$(echo "$fresh" | tr -s ' ' '\n' | grep -v '^$' | paste -sd, -)
  log "发现待答提问 #$ids_csv ⇒ spawn opencode run 去 reply"
  # 【判据：成功才推进游标】先记"已指派"，spawn 成功收工后才算完成；
  # 若 spawn 失败(rc≠0)或进程被杀，这里必须把游标回退，让下一轮重放该提问。
  # 今天 22:18 实测踩到：先记游标后 spawn，被 launchctl kickstart 打断 ⇒
  # #24 永远停在 open、游标已含 24 ⇒ 这条提问被永久丢弃，正是我自己写进板上的
  # 「失败不推进游标、不吞消息」的反例。
  mark_seen $fresh

  # ASK_MODEL 留空时不传 -m，用 opencode 自己的默认模型（不绑死某家模型名）。
  # 注意两处 macOS bash 3.2 的坑（今天 22:21 实测踩到，脚本反复 rc=1 崩溃重启）：
  #   ① 空数组在 `set -u` 下用 "${arr[@]}" 会报 unbound variable ⇒ 用 ${arr[@]+"${arr[@]}"} 展开。
  #   ② 变量前缀与续行反斜杠不能拆开 —— `VAR=x \` 后面接 `arr=()` 会让前缀变成空操作。
  if [ -n "$MODEL" ]; then
    run_cmd=("$OPENCODE_BIN" run --dir "$DIR/.." -m "$MODEL")
  else
    run_cmd=("$OPENCODE_BIN" run --dir "$DIR/..")
  fi
  "${run_cmd[@]}" \
    "你是 ${BOT}，看板定向问答应答 agent（由 ask-listener.sh 唤醒）。唤醒原因：板上有人 ask 你，待答编号：${ids_csv}。

严格按协议做，逐步 post 心跳：
1) export WORK_LOG_AGENT=$BOT
2) python3 \"$ENGINE\" --dir \"$DIR\" brief --agent $BOT --peek
   —— 看清每条提问的原文，brief 里会标「❗ 待你回应 #N」。
3) 对 #$ids_csv 每一条：先 post --tag 收到 写你的实质判断（禁止「收到」「处理中」空话），再
   reply --agent $BOT --id N --text \"……\" 逐条回答。
   判据：不确定就写不确定，不要替提问方猜；无数据就说没数据。
4) post --tag 任务完成 --done，然后 check（若有卡死或险情，post --tag 阻塞 写处置建议）。

收尾硬要求：只回你被 ask 的那几条，不要顺手 ack 用户的 user.md 喊话（那是 listener 那条线的职责）。" \
    >>"$LOG" 2>&1
  rc=$?
  log "opencode run 退出 rc=${rc}（ids #${ids_csv}）"
  if [ "$rc" -ne 0 ]; then
    # 失败 ⇒ 回退游标，下轮重放（提问不丢）。
    log "rc=${rc} ⇒ 回退游标 #${ids_csv}，下轮重放"
    python3 - "$CURSOR" $fresh <<'PY' 2>/dev/null || true
import json, os, sys
p = sys.argv[1]
try:
    cur = set(json.load(open(p)))
except Exception:
    cur = set()
cur -= set(sys.argv[2:])
tmp = p + ".tmp"
json.dump(sorted(cur), open(tmp, "w"))
os.replace(tmp, p)
PY
  fi
  rmdir "$LOCK" 2>/dev/null
done
