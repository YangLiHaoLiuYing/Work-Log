#!/usr/bin/env bash
# work-log 60 秒演示 —— 不需要任何 API key、不需要模型、不碰你的项目文件。
#
# 跑法：
#   bash examples/demo.sh
#
# 它会依次演示三件"别的看板做不到"的事：
#   1. 看门狗抓静默卡死
#   2. 等待图抓「心跳全绿的死锁」（互相等待）—— 看门狗抓不到的那种
#   3. 通信熔断抓「两个 agent 互相客套停不下来」
# 外加多路等待（一次等三个人，任一人回答就能往下走）。
#
# 全程在 mktemp 出来的临时目录里，退出时自动删除。

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WL="$HERE/../scripts/work_log.py"
PY="${PYTHON:-python3}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}""/work-demo.XXXXXX")"
BG=""
cleanup() {
  for p in $BG; do kill "$p" 2>/dev/null; done
  wait 2>/dev/null          # 收割子进程，否则 shell 会在末尾打印一堆 "Terminated: 15"
  rm -rf "$TMP"
}
trap cleanup EXIT

if [ ! -f "$WL" ]; then
  echo "找不到 $WL —— 请在仓库根目录下运行: bash examples/demo.sh" >&2
  exit 1
fi

hr()  { printf '\n\033[1;36m────────────────────────────────────────────────────────────\033[0m\n'; }
say() { printf '\033[1;37m%s\033[0m\n' "$*"; }
dim() { printf '\033[2m%s\033[0m\n' "$*"; }

# 每个场景用独立目录，免得互相干扰
A="$TMP/stuck"; B="$TMP/deadlock"; C="$TMP/pingpong"
run() { local d="$1"; shift; "$PY" "$WL" --dir "$d" "$@"; }

echo
echo "  work-log 演示 · 临时目录 $TMP"
dim "  （所有输出都是这套 CLI 的真实输出，没有美化过）"

# ═══════════════════════════════════════════════════════════════════
hr
say "① 看门狗：抓到静默卡死"
dim "   三个 agent 都在写心跳，agent3 写到一半不吭声了。"
dim "   为了让演示不用等 45 秒，这里把卡死阈值压到 3 秒（--stale-after 3）。"

run "$A" init --agents agent1,agent2,agent3 --task "演示：谁卡死了" >/dev/null
run "$A" post --agent agent1 --text "改 tts.py 的音色参数" --tag 收到 >/dev/null
run "$A" post --agent agent2 --text "跑前端构建" --tag 收到 >/dev/null
run "$A" post --agent agent3 --text "开始下载模型" --tag 收到 >/dev/null
dim "   ...agent3 之后再也没有写过心跳，等 4 秒..."
sleep 4
dim "   （agent1 / agent2 继续在写，只有 agent3 停了）"
run "$A" post --agent agent1 --text "音色参数改完了，接着改映射表" >/dev/null
run "$A" post --agent agent2 --text "构建 60%，继续" >/dev/null

echo
run "$A" check --stale-after 3; rc=$?
echo
dim "   ↑ 只有 agent3 被标红，agent1/agent2 静默 0s 属于正常。"
dim "     check 有卡死时退出码是 1（业务码），可以直接写进 CI / 脚本判断。"

# ═══════════════════════════════════════════════════════════════════
hr
say "② 等待图：抓「心跳全绿的死锁」"
dim "   这是 work-log 独有的能力。场景：agent1 和 agent2 互相问了对方一个问题，"
dim "   然后两边都阻塞等对方回答 —— 双方心跳都很勤（await 会持续续心跳），"
dim "   看门狗看它们是「最健康的两个人」，但整个团队其实已经死了。"

run "$B" init --agents agent1,agent2 --task "演示：互相等待" >/dev/null
run "$B" ask --agent agent1 --to agent2 --text "你的 schema 定了吗" >/dev/null
run "$B" ask --agent agent2 --to agent1 --text "你的字段名定了吗" >/dev/null

# 两边同时进入 await，各自等自己问出去的那条 → 形成环
run "$B" await --agent agent1 --id 1 --timeout 30 --interval 0.2 --report 100 >/dev/null 2>&1 &
BG="$BG $!"
run "$B" await --agent agent2 --id 2 --timeout 30 --interval 0.2 --report 100 >/dev/null 2>&1 &
BG="$BG $!"
sleep 3

echo
run "$B" check --stale-after 45; rc=$?
echo
dim "   ↑ 两个 agent 的心跳都是「心跳中、静默 0s」，一切正常 —— 但协作险情那行指出了真死锁。"
dim "     这正是「心跳全绿」类故障：没有进程崩溃、没有超时，就是谁也不动了。"
dim "     所以 check 在这里也必须是非零（${rc}），否则这类故障在脚本里会被整个漏掉。"

# ═══════════════════════════════════════════════════════════════════
hr
say "③ 通信熔断：两个 agent 互相客套停不下来"
dim "   同一对 agent 连续交替来回，到 6 轮开始提醒，到 12 轮直接拒绝写入。"
dim "   目的是把预算花在推进上，而不是「好的」「收到」「那就这样」的无限循环。"

run "$C" init --agents agent1,agent2 --task "演示：乒乓" >/dev/null
for i in $(seq 1 12); do
  run "$C" ask   --agent agent1 --to agent2 --text "第 $i 轮：再确认一下" >/dev/null 2>&1
  run "$C" reply --agent agent2 --to agent1 --id "$i" --text "第 $i 轮：好的没问题" >/dev/null 2>&1
done
echo
say "   第 13 轮 ask："
run "$C" ask --agent agent1 --to agent2 --text "第 13 轮"; rc=$?
echo
dim "   退出码 = ${rc}（4 = 被机制叫停，不是参数错）。"
dim "   确认必须继续深挖时，加 --force 可以放行；但熔断想让你先写清结论。"

# ═══════════════════════════════════════════════════════════════════
hr
say "④ 多路等待：一次等多人，谁先答都能往下走"
dim "   默认「全部都要」（--id 1,2）；加 --any 变成「任一即可」。"

D="$TMP/multi"
run "$D" init --agents asker,alice,bob --task "演示：多路等待" >/dev/null
run "$D" ask --agent asker --to alice --text "缓存用 Redis 还是本地文件" >/dev/null
run "$D" ask --agent asker --to bob   --text "同一件事问第二个人" >/dev/null
echo
say "   bob 回了一条，然后用 --any 等："
run "$D" reply --agent bob --id 2 --text "本地文件就行，量不大" >/dev/null
run "$D" await --agent asker --id 1,2 --any --timeout 5 --interval 0.2
echo
dim "   退出码 = $?（0 = 达成）。--any 拿到一条就返回，不会为了等齐所有人干耗。"

echo
hr
printf '  \033[1;32m演示结束\033[0m —— 全部在 %s，已自动清理。\n' "$TMP"
echo "  真实用法：让每个 agent 每 15s 写一条心跳，另开一个 watch 当看门狗，"
echo "  具体命令见 README 的「30 秒上手」与 SKILL.md。"
echo
