#!/usr/bin/env bash
# work-log 自测 —— 可反复跑，只在临时目录里折腾，不碰项目文件。
#   用法: bash selftest.sh
#   退出码: 0 全通过 / 1 有失败
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WL="$HERE/work_log.py"
PY="${PYTHON:-python3}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/work-selftest.XXXXXX")"
BG_PIDS=""
# 自动协作 UI 会让 post 偷偷起一个带看门狗的 serve —— 除 [32] 外所有组都假设
# "没有背景写手"，所以全套件先整体关掉；[32] 用 env -u 单独把它打开来测。
export WORK_LOG_NO_AUTO_UI=1
cleanup() { for p in $BG_PIDS; do kill "$p" 2>/dev/null; done; rm -rf "$TMP"; }
trap cleanup EXIT

PASS=0; FAIL=0; NOTES=""

# 源文件指纹。**这个护栏是被自己坑出来的**（2026-09-29 一天内犯了两次）：改引擎的同时
# 让自测在跑 ⇒ 前面的组读旧文件、后面的组读新文件，而那次的"红/绿"两边都不可信，
# 最坏的情况是"绿了但其实半路换过文件"。靠"记得别同时改"是防不住的，所以让它自证：
src_fp() { for f in "$HERE/work_log.py" "$HERE/selftest.sh" "$HERE/waker.sh" "$HERE/ask_listener.sh" "$HERE/check_stdlib_only.py"; do
             [ -f "$f" ] && cksum "$f"; done 2>/dev/null | cksum; }
FP0="$(src_fp)"
ok()  { PASS=$((PASS+1)); printf '  [ok] %s\n' "$1"; }
ng()  { FAIL=$((FAIL+1)); NOTES="${NOTES}
    - $1"; printf '  [!!] %s\n' "$1"; }
eq()  { if [ "$2" = "$3" ]; then ok "$1"; else ng "$1  期望[$3] 实际[$2]"; fi; }
# 前置条件/用法错误必须是 2（EXIT_USAGE），**更不能是 1** ——
# 1 在这套协议里是「超时/对方没回」的业务结论。用法错误借用 1，
# 调用方就会把「编号写错了」读成「对方不配合」并据此继续往下做。
usage(){ if   [ "$2" = 2 ]; then ok "$1（→2，不是业务码 1）"
         elif [ "$2" = 1 ]; then ng "$1 —— 用法错误退成了 1，会伪装成「对方没回」"
         else ng "$1  期望[2] 实际[$2]"; fi; }
has() { case "$2" in *"$3"*) ok "$1";; *) ng "$1  未找到[$3]";; esac; }
hasnt(){ case "$2" in *"$3"*) ng "$1  不该出现[$3]";; *) ok "$1";; esac; }
W()   { "$PY" "$WL" --dir "$TMP" "$@"; }
ln_() { printf '%s\n' "$2" | grep -E -- "$1" | head -1; }

echo "work-log selftest"
echo "脚本: $WL"
echo "沙箱: $TMP"
echo

# ---- 1 初始化 -------------------------------------------------------------
echo "[1] 初始化"
W init --task "自测" --agents agent1,agent2 >/dev/null; eq "init 退出码 0" $? 0
for f in board.md user.md alerts.md state.json; do
  if [ -f "$TMP/$f" ]; then ok "生成 $f"; else ng "缺文件 $f"; fi
done
has "看板含任务名" "$(cat "$TMP/board.md")" "自测"
W init --task "自测2" >/dev/null; eq "重复 init 幂等" $? 0

# ---- 2 心跳写入格式 -------------------------------------------------------
echo "[2] 心跳写入"
out=$(W post --agent agent1 --text "收到用户要求：加语音开关。我现在要改 tts.py。" --tag 收到)
case "$out" in
  '<agent1> '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]' [收到] 收到用户要求'*)
    ok "post 行格式符合协议";;
  *) ng "post 行格式异常: $out";;
esac
out=$(W post --agent agent2 --text "$(printf '多行\n文本\t带制表符 反斜杠\\ 引号"x"')")
first=$(printf '%s\n' "$out" | head -1)
eq "换行被压成单行" "$(printf '%s\n' "$first" | wc -l | tr -d ' ')" "1"
has "反斜杠保留" "$first" '\'
has "引号保留" "$first" '引号"x"'
W post --agent agent1 --text "" >/dev/null 2>&1; usage "空文本被拒" $?
long=$(python3 -c "print('长'*5000)")
W post --agent agent1 --text "$long" >/dev/null; eq "5000 字长文本可写" $? 0
out=$(W post --agent agent1 --text "- 我先停一下，等锁")
has "以 - 开头的文本可用" "$out" "- 我先停一下"
W post --agent agent1 --text --done >/dev/null 2>&1; eq "漏传 --text 值仍报错" $? 2

# ---- 3 参数校验 -----------------------------------------------------------
echo "[3] 参数校验"
W post --agent "a b" --text x   >/dev/null 2>&1; usage "含空格的 agent 名被拒" $?
W post --agent "a>b" --text x   >/dev/null 2>&1; usage "含 > 的 agent 名被拒" $?
W post --agent watchdog --text x >/dev/null 2>&1; usage "保留名 watchdog 被拒" $?
W post --agent "中文名" --text ok >/dev/null 2>&1; eq "中文 agent 名可用" $? 0
W check --stale-after 0  >/dev/null 2>&1; usage "--stale-after 0 被拒" $?
# 注意：阈值 1s 时前面的 agent 都算过期，退出码 1 是**正确**行为，
# 这里只验证参数被接受（不是 argparse 的 2），别把两件事混成一条断言。
W check --stale-after 1 --readonly >/dev/null; rc=$?
if [ "$rc" -le 1 ]; then ok "--stale-after 1 被接受（rc=${rc}）"; else ng "--stale-after 1 被当成参数错误 rc=$rc"; fi
echo x > "$TMP/afile"
"$PY" "$WL" --dir "$TMP/afile" status >/dev/null 2>&1; usage "--dir 指向文件被拒" $?

# ---- 4 只写错名字的 agent 不该被误报 --------------------------------------
echo "[4] 待启动保护（typo 不误报）"
W read-user --agent agnet1 >/dev/null 2>&1
sleep 2
r=$(W check --stale-after 1 --readonly)
has "拼错的 agent 记为待启动" "$r" "待启动"
line=$(ln_ '^agnet1' "$r")
case "$line" in *疑似卡死*) ng "拼错的 agent 被误报卡死";; *) ok "拼错的 agent 未被告警";; esac

# ---- 5 并发写 -------------------------------------------------------------
echo "[5] 并发写（20 进程）"
before=$(grep -c '^<' "$TMP/board.md")
i=1; while [ $i -le 20 ]; do W post --agent "cc$i" --text "并发 $i" >/dev/null & i=$((i+1)); done
wait
after=$(grep -c '^<' "$TMP/board.md")
eq "20 条并发心跳全部落盘" "$((after-before))" "20"
n=$(python3 -c "
import json;d=json.load(open('$TMP/state.json'))
print(sum(1 for k in d['agents'] if k.startswith('cc')))")
eq "20 个并发 agent 全部注册" "$n" "20"
n=$(python3 -c "
import json;d=json.load(open('$TMP/state.json'))
print(sum(v['entries'] for k,v in d['agents'].items() if k.startswith('cc')))")
eq "并发下 entries 计数无丢失" "$n" "20"

# ---- 6 卡死判定与告警 -----------------------------------------------------
echo "[6] 卡死判定 / 告警 / 冷却"
W post --agent st1 --text "我还活着" >/dev/null
sleep 2
out=$(W check --stale-after 1); eq "有卡死时退出码 1" $? 1
has "输出标记疑似卡死" "$out" "疑似卡死"
has "告警写入 alerts.md" "$(cat "$TMP/alerts.md")" "疑似卡死"
# 告警必须自带补救动作：只说"疑似卡死"会让收到的人只会补一条心跳，过 90s 又被报一次
#（2026-09-28 实测刷到 1800+ 条）。文案里直接给出可复制的 hold 命令。
has "告警文案给出补救命令（指名补 hold）" "$(cat "$TMP/alerts.md")" "hold --agent st1"
n1=$(grep -c '\[告警\]' "$TMP/alerts.md")
W check --stale-after 1 --cooldown 60 >/dev/null
n2=$(grep -c '\[告警\]' "$TMP/alerts.md")
eq "冷却期内不重复告警" "$n2" "$n1"
W check --stale-after 1 --cooldown 0 >/dev/null
n3=$(grep -c '\[告警\]' "$TMP/alerts.md")
if [ "$n3" -gt "$n2" ]; then ok "冷却过后升级重复告警"; else ng "升级告警未触发"; fi
has "升级文案含「重复告警 #」" "$(cat "$TMP/alerts.md")" "重复告警 #"

# ---- 7 恢复自动闭环 -------------------------------------------------------
echo "[7] 恢复自动闭环"
W post --agent st1 --text "回来了" >/dev/null
W check --stale-after 1 >/dev/null
has "看门狗写 [恢复]" "$(cat "$TMP/board.md")" "[恢复]"
has "恢复文案含自动关闭" "$(cat "$TMP/board.md")" "自动关闭"
n=$(python3 -c "
import json;d=json.load(open('$TMP/state.json'))
print(sum(1 for a in d['alerts'] if a['agent']=='st1' and not a.get('acked_by')))")
eq "st1 告警已自动确认" "$n" "0"

# ---- 8 任务完成 -----------------------------------------------------------
echo "[8] 「任务完成」退出监督"
W post --agent done1 --text "干完了" --done >/dev/null
has "完成文案提示" "$(W post --agent done1 --text x --done 2>&1)" "不再对它报卡死"
sleep 2
line=$(ln_ '^done1' "$(W check --stale-after 1 --readonly)")
has "完成态显示为完成" "$line" "完成"
case "$line" in *疑似卡死*) ng "已完成仍被告警";; *) ok "已完成不被告警";; esac
W post --agent done1 --text "又有新活" >/dev/null
line=$(ln_ '^done1' "$(W check --stale-after 1 --readonly)")
case "$line" in *疑似卡死*) ok "重新开工后回到监督（可判卡死）";; *) ok "重新开工后状态已刷新";; esac

# ---- 9 挂起 ---------------------------------------------------------------
echo "[9] hold 长任务挂起"
# 窗口给足余量（30s）：慢机器/高负载下 check 滑出 5s 窗口会把"挂起中"误判成"过期"
# —— 这个组曾因此偶发失败。过期判定单独用 1s 短窗口的 hold2 测。
W post --agent hold1 --text 开始 >/dev/null
W hold --agent hold1 --seconds 30 --text "跑 vite build" >/dev/null
line=$(ln_ '^hold1' "$(W check --stale-after 1 --readonly)")
has "挂起中" "$line" "挂起中"
case "$line" in *疑似卡死*) ng "挂起期间被误报";; *) ok "挂起期间不误报";; esac
W post --agent hold2 --text 开始 >/dev/null
W hold --agent hold2 --seconds 1 --text "短任务" >/dev/null
sleep 2.5
line=$(ln_ '^hold2' "$(W check --stale-after 1 --readonly)")
has "挂起过期后判卡死" "$line" "疑似卡死"
W release --agent hold1 >/dev/null; eq "release 退出码 0" $? 0

# --seconds 可省略（默认 10 分钟）：长任务前声明不该有"还得先估个时长"的摩擦。
# （post 与 hold 已解耦：post 不会解除挂起，窗口只随时间失效或被 release 清除。）
W post --agent hold3 --text 开始 >/dev/null
outh=$(W hold --agent hold3 --text "不估时长直接挂起")
eq "hold 省略 --seconds 仍退 0" $? 0
line=$(ln_ '^hold3' "$(W check --stale-after 1 --readonly)")
has "省略 --seconds 时按默认 600s 挂起" "$line" "挂起中"
n=$(python3 -c "
import json,time;d=json.load(open('$TMP/state.json'))
print(int(d['agents']['hold3']['expected_silence_until']-time.time()))")
if [ "$n" -ge 590 ] && [ "$n" -le 601 ]; then ok "默认挂起窗口 ≈600s（实测 ${n}s）"
else ng "默认挂起窗口应是 600s，实测 ${n}s"; fi

# ---- 10 资源锁 ------------------------------------------------------------
echo "[10] 资源锁"
W lock --agent agent1 --resource gpu --note "在跑训练" >/dev/null; eq "首次加锁成功" $? 0
W lock --agent agent2 --resource gpu >/dev/null 2>&1; eq "抢锁冲突退出码 3" $? 3
has "冲突在看板留痕" "$(cat "$TMP/board.md")" "正持有"
W lock --agent agent2 --resource gpu --force >/dev/null; eq "--force 抢锁成功" $? 0
has "locks 显示持有者" "$(W locks)" "agent2"
W unlock --agent agent1 --resource gpu >/dev/null 2>&1; eq "非持有者解不了锁" $? 3
W unlock --agent agent2 --resource gpu >/dev/null; eq "持有者可解锁" $? 0
W lock --agent agent1 --resource "" >/dev/null 2>&1; usage "空资源名被拒" $?

# ---- 11 用户喊话通道 ------------------------------------------------------
echo "[11] 用户喊话"
W say --text "记得默认关" >/dev/null
W say --text "第二条" >/dev/null
o=$(W read-user --agent agent1)
has "首读拿到第 1 条" "$o" "记得默认关"
has "首读同时拿到第 2 条" "$o" "第二条"
has "取完后无新消息" "$(W read-user --agent agent1)" "无新消息"
has "另一 agent 游标独立" "$(W read-user --agent agent2)" "记得默认关"
W read-user --agent agent3 --peek >/dev/null
has "peek 不推进游标" "$(W read-user --agent agent3)" "记得默认关"
# 「谁认领的」必须**按人**算。用全局集合去标「你已回执」，会让别人认领过的喊话
# 在我这边显示成「我已处理过」—— 我就把一条从没看过的用户话跳过去了（实测 2026-09-29）。
W ack-user --agent agent2 --id 1 --text "别人先认领" >/dev/null
o=$(W read-user --agent agent4 --peek)
has "★ 别人认领会如实说是谁（重复回执从此看得见）" "$o" "<agent2> 已认领"
hasnt "★ 但绝不显示成「你已回执过」（否则我会跳过没看过的喊话）" "$o" "（你已回执过）"
hasnt "标记放在行尾，不再插在名字与冒号之间" "$o" "用户你已回执"
W ack-user --agent agent4 --id 2 --text "我认领" >/dev/null
o=$(W read-user --agent agent4 --peek)
has "自己认领才标「你已回执过」" "$o" "（你已回执过）"
hasnt "且不把自己算进「别人已认领」" "$o" "<agent4> 已认领"
# 认领去重：先到先得，后到退 3 —— **不是 1**（1 的样本全是故障；"别人认领了"是期望结局，
# 塞进 1 会让 `check || 告警` 把良性结论当异常）
o=$(W ack-user --agent agent4 --id 2 --text "再认领一次" 2>&1); eq "自己重复认领退 0（幂等）" $? 0
o=$(W ack-user --agent agent1 --id 2 --text "抢别人的" 2>&1); rc=$?
eq "★ 已被别人认领则退 3（不是故障码 1）" "$rc" 3
has "并点名是谁先认领的（附它写的处理说明）" "$o" "已被 <agent4> 认领"
o=$(W ack-user --agent agent1 --id 2 --text "接管" --force 2>&1); eq "--force 仍可覆盖（多应答方的逃生口）" $? 0
has "抢不到也进看板（谁让给谁看得见）" "$(cat "$TMP/board.md")" "让给它"
usage "越界编号仍退 2（不因为新增去重就被改写成别的码）" \
      "$(W ack-user --agent agent1 --id 9999 --text x >/dev/null 2>&1; echo $?)"
W say --text "第三条" >/dev/null
has "post 顺带提醒新动态" "$(W post --agent agent1 --text 继续干活)" "新动态"

# ---- 11b 探针 brief（增量投喂） -------------------------------------------
echo "[11b] 探针 brief"
o=$(W brief --agent agent1)
has "brief 拿到用户的喊话" "$o" "第三条"
has "brief 带状态行" "$o" "· 当前："
eq "brief 不投喂自己的日志" "$(printf '%s\n' "$o" | grep -c '^<agent1>')" "0"
has "读完即清空" "$(W brief --agent agent1)" "无新动态"
W post --agent agent2 --text "我改完了前端" >/dev/null
o=$(W brief --agent agent1)
has "brief 能看到别人的发言" "$o" "我改完了前端"
W post --agent agent2 --text "再看一眼" >/dev/null
has "peek 不推进游标" "$(W brief --agent agent1 --peek)" "再看一眼"
has "peek 之后仍能读到" "$(W brief --agent agent1)" "再看一眼"
# 这条断言曾偶发失败过一次（248 通过 / 1 失败），而且当时只有一句"未找到"，
# 完全没法判断是锁没加上、brief 丢了字段、还是 brief 自己崩了。
# 现在把 stderr 一起收进来（内部错误会打印 traceback 而不是走进 stdout），
# 并且把三个退出码/现场一起写进失败信息 —— 下次再抖就能直接定位。
W lock --agent agent2 --resource probe/r1 >/dev/null; LRC=$?
o=$(W brief --agent agent1 2>&1); BRC=$?
case "$o" in
  *被占用*) ok "brief 报告别人的锁" ;;
  *)
    LOCKS=$(grep -o '"locks":[^}]*}' "$TMP/state.json" 2>/dev/null | head -c 200)
    ng "brief 报告别人的锁  未找到[被占用] | lock 退出码=$LRC | brief 退出码=$BRC$([ "$BRC" = 70 ] && echo '（70=引擎内部错误，去看 traceback）') | state.locks=$LOCKS | brief=($o)"
    ;;
esac
o=$(W brief --agent agent2)
has "brief 报告自己持有的锁" "$o" "你持有"

# ---- 11c 定向交流：问 / 答 / 等 / 回执 ------------------------------------
echo "[11c] 定向交流（问 → 答 → 等 → 回执）"
TALK="$(mktemp -d)"
K() { "$PY" "$WL" --dir "$TALK" "$@"; }
K init --task "交流测试" >/dev/null
K post --agent c1 --text "开工" >/dev/null
K post --agent c2 --text "开工" >/dev/null
o=$(K ask --agent c1 --to c2 --text "能不能复用 /api/tts？")
has "ask 返回编号" "$o" "提问 #1"
has "ask 给出等待命令" "$o" "await --agent c1 --id 1"
o=$(K post --agent c2 --text "顺手写一条")
has "有提问在等时 post 会催" "$o" "在等你回应 #1"
o=$(K brief --agent c2)
has "被问方 brief 置顶提醒" "$o" "待你回应 #1"
has "被问方给出 reply 命令" "$o" "reply --agent c2 --id 1"
o=$(K brief --agent c1)
has "提问方 brief 显示在等" "$o" "回答 #1"
K reply --agent c9 --id 1 --text "我插嘴" >/dev/null 2>&1
usage "非被问方不能代答" $?
K reply --agent c2 --id 1 --text "验证过，可行，直接复用" >/dev/null; eq "被问方能回应" $? 0
K reply --agent c2 --id 1 --text "再答一次" >/dev/null 2>&1; usage "同一问题不能重复回应" $?
o=$(K brief --agent c1)
has "提问方看到答复" "$o" "验证过，可行"
eq "回应后不再是待回应" "$(printf '%s\n' "$o" | grep -c '待你回应')" "0"

# await：真等到（另一个进程 3s 后回应）
K ask --agent c1 --to c2 --text "第二个问题" >/dev/null
( sleep 3; K reply --agent c2 --id 2 --text "答第二个" >/dev/null ) &
t0=$(date +%s)
o=$(K await --agent c1 --id 2 --timeout 20 --report 2); rc=$?
eq "await 等到回应后返回 0" "$rc" "0"
has "await 打出答复内容" "$o" "答第二个"
if [ $(( $(date +%s) - t0 )) -ge 3 ]; then ok "await 确实阻塞等了 3s"; else ng "await 没有真的阻塞"; fi
wait

# await：对方永不回应必须失败（曾因"期间有别的心跳"误报成功）
# 注意"永不回应"有两种，语义不同、返回码也必须不同：
#   (a) 对方已写「任务完成」退出 → 这个答案永远不会来，立刻收手（3），别把超时干耗完
#   (b) 对方还活着、就是不答   → 老实等到超时（1），不能谎报成功
K post --agent c3 --text "我先收工了" --done >/dev/null
K ask --agent c1 --to c3 --text "你那边结果呢？" >/dev/null
t0=$(date +%s)
o=$(K await --agent c1 --id 3 --timeout 30 --report 1); rc=$?
el=$(( $(date +%s) - t0 ))
eq "(a) 对方已收工 → await 返回 3" "$rc" "3"
has "(a) 说明对方已收工" "$o" "对方已收工"
has "(a) 点明答案不会来了" "$o" "不会来了"
if [ "$el" -lt 10 ]; then ok "(a) 没白耗完 30s 超时（实际 ${el}s）"; else ng "(a) 白等满了超时（${el}s）"; fi
eq "(a) 收手后撤掉等待标记（看板不再显示它在等）" \
   "$(K status --stale-after 999 --json | "$PY" -c 'import json,sys; print([a["waiting_on"] for a in json.load(sys.stdin)["agents"] if a["name"]=="c1"][0])')" \
   "None"

o=$(K ask --agent c1 --to c2 --text "第三个问题（对方在线，但我不打算回应）")
nid=$(printf '%s\n' "$o" | grep -oE '提问 #[0-9]+' | grep -oE '[0-9]+$')
o=$(K await --agent c1 --id "$nid" --timeout 3 --report 1); rc=$?
eq "(b) 在线的沉默方 → await 超时返回 1" "$rc" "1"
has "(b) await 明确说没等到" "$o" "始终没等到回应"

o=$(K ask --agent c1 --to c3 --text "再问一次")
has "问已收工的 agent 会警告" "$o" "大概率等不到回应"

# 用户喊话回执闭环
K say --text "默认关掉" >/dev/null
K say --text "别动数据库" >/dev/null
o=$(K read-user --agent c1)
has "喊话带编号" "$o" "[#2]"
K ack-user --agent c1 --id 2 --text "已改成默认 false" >/dev/null; eq "回执成功" $? 0
K ack-user --agent c1 --id 99 --text "x" >/dev/null 2>&1; usage "越界编号被拒" $?
o=$(K check --stale-after 999 --readonly)
has "认领后从待认领消失" "$o" "还没人认领 1 条"
o=$(K brief --agent c2)
has "brief 提醒认领用户喊话" "$o" "还没人认领"
# 看门狗存活自检
has "状态里报告上次扫描时间" "$o" "· 当前："
o=$(K check --stale-after 999 --readonly)
has "非只读 check 写下扫描时间" "$(K check --stale-after 999 >/dev/null 2>&1; K status --stale-after 999)" "上次扫描："

# 问一个"从未出现过"的名字：必须成功（A 常比 B 先启动，实测第二轮 01:53:02 问、对方 01:53:03 才发声）
# 但必须警告——"从未出现过"比"待启动"更可能是拼错名字，否则 await 会白等到超时
o=$(K ask --agent c1 --to ghost --text "你在吗")
has "问未出现过的 agent 仍允许" "$o" "提问 #"
has "但警告名字可能拼错" "$o" "从未出现过"
# 被预注册但从未发言 → 允许 + 提示当前状态
K init --task "交流测试" --agents c1,c9 >/dev/null
o=$(K ask --agent c1 --to c9 --text "你在吗")
has "问待启动的 agent 仍允许" "$o" "提问 #"
has "并提示其当前状态" "$o" "待启动"
rm -rf "$TALK"

# ---- 12 手写看板也要被监督 ------------------------------------------------
echo "[12] 手写看板反解"
printf '<manual> %s [执行] 手写一行\n' "$(date +%H:%M:%S)" >> "$TMP/board.md"
has "手写行被识别为 agent" "$(W check --stale-after 1 --readonly)" "manual"
printf '<manual2> %s [任务完成] 手写完成\n' "$(date +%H:%M:%S)" >> "$TMP/board.md"
line=$(ln_ '^manual2' "$(W check --stale-after 1 --readonly)")
has "手写完成态被识别" "$line" "完成"

# ---- 13 脏输入不崩 --------------------------------------------------------
echo "[13] 脏输入健壮性"
printf 'random garbage\n<<>>\n<>\n<weird> not-a-time 内容\n<中文名> 00:00:00 中文\n' >> "$TMP/board.md"
W check --stale-after 999 --readonly >/dev/null; eq "脏看板行不崩" $? 0
printf 'garbage in user.md\n' >> "$TMP/user.md"
W read-user --agent agent1 >/dev/null; eq "脏 user.md 不崩" $? 0
echo '{ broken json' > "$TMP/state.json"
# 注意必须带 --stale-after 999：这条断言只验证"损坏不崩"，不验证"没有卡死"。
# 漏了它就会用默认 45s —— 并发/慢机器下，到这一组时看板里最后一条心跳
# 早已超过 45s，board 反解出来的 agent 全被判「疑似卡死」→ 退 1 → 假失败。
# （这条在 6 路并发压测下 6/6 复现，是整套件里唯一漏掉 999 的 check。）
W check --stale-after 999 --readonly >/dev/null 2>&1; eq "损坏 state.json 不崩" $? 0
W post --agent fix1 --text "损坏后重建" >/dev/null; eq "损坏后仍能写" $? 0

# ---- 14 json 输出 ---------------------------------------------------------
echo "[14] 机器可读输出"
W check --stale-after 999 --json --readonly > "$TMP/j.json" || true
python3 -c "import json;d=json.load(open('$TMP/j.json'));assert 'agents' in d and 'stale' in d"
eq "--json 输出合法 JSON" $? 0

# ---- 15 跨天分节与告警裁剪 ------------------------------------------------
echo "[15] 跨天分节 / 状态裁剪"
python3 -c "
import json;p='$TMP/state.json';d=json.load(open(p));d['board_date']='2000-01-01';json.dump(d,open(p,'w'),ensure_ascii=False)"
W post --agent agent1 --text 跨天 >/dev/null
has "跨天插入日期分节" "$(cat "$TMP/board.md")" "## $(date +%Y-%m-%d)"
python3 -c "
import json;p='$TMP/state.json';d=json.load(open(p));d['board_date']='2000-01-01'
d['alerts']=[{'id':i,'agent':'x','ts':0,'silence':1,'text':'t','acked_by':'z'} for i in range(250)]
json.dump(d,open(p,'w'),ensure_ascii=False)"
W check --stale-after 999 >/dev/null
eq "alerts 裁剪到上限 200" "$(python3 -c "
import json;print(len(json.load(open('$TMP/state.json'))['alerts']))")" "200"

# ---- 16 看门狗短跑 --------------------------------------------------------
echo "[16] 看门狗"
has "watch 打印 tick" "$(W watch --interval 1 --stale-after 999 --ticks 2)" "tick"
hasnt "quiet 模式无变化不打印 tick" "$(W watch --interval 1 --stale-after 999 --ticks 2 --quiet)" "tick"
has "watch 空目录提示" "$(D2=$(mktemp -d); "$PY" "$WL" --dir "$D2" watch --interval 1 --ticks 1; rm -rf "$D2")" "还没有任何 agent 记录"

# ---- 17 未初始化直接操作 --------------------------------------------------
echo "[17] 边界：未初始化"
D3="$(mktemp -d)"
"$PY" "$WL" --dir "$D3" check --readonly >/dev/null; eq "空目录 check 不崩" $? 0
"$PY" "$WL" --dir "$D3" tail >/dev/null;      eq "空目录 tail 不崩" $? 0
"$PY" "$WL" --dir "$D3" locks >/dev/null;     eq "空目录 locks 不崩" $? 0
"$PY" "$WL" --dir "$D3" post --agent a --text x >/dev/null; eq "空目录 post 自动建表" $? 0
rm -rf "$D3"

# ---- 18 零依赖 ------------------------------------------------------------
echo "[18] 依赖检查"
python3 - "$WL" <<'PY'
import ast, sys
allow = {"argparse", "contextlib", "json", "os", "re", "sys", "time",
         "datetime", "pathlib", "fcntl", "__future__", "traceback",
         "http", "threading", "socketserver", "urllib", "subprocess",
         "webbrowser"}
mods = set()
for n in ast.walk(ast.parse(open(sys.argv[1], encoding="utf-8").read())):
    if isinstance(n, ast.Import):
        mods |= {a.name.split(".")[0] for a in n.names}
    elif isinstance(n, ast.ImportFrom) and n.module:
        mods.add(n.module.split(".")[0])
extra = mods - allow
print("外部依赖:", ", ".join(sorted(extra)) if extra else "无（纯标准库）")
sys.exit(1 if extra else 0)
PY
eq "只依赖标准库" $? 0

# ---- 19 后台实时输出（缓冲回归） ------------------------------------------
echo "[19] 后台实时输出不得被缓冲憋住"
D4="$(mktemp -d)"
"$PY" "$WL" --dir "$D4" init --task bg >/dev/null
"$PY" "$WL" --dir "$D4" watch --interval 1 --stale-after 999 > "$D4/w.out" 2>&1 &
BG_PIDS="$BG_PIDS $!"
disown 2>/dev/null || true
sleep 3
sz=$(wc -c < "$D4/w.out" | tr -d ' ')
if [ "$sz" -gt 0 ]; then ok "进程存活时即有输出（$sz 字节）"; else ng "看门狗输出被缓冲憋住（0 字节，重定向场景下等于瞎跑）"; fi
has "存活时就能读到 tick" "$(cat "$D4/w.out")" "tick"
kill $BG_PIDS 2>/dev/null; BG_PIDS=""
rm -rf "$D4"

# ---- 20 实时视图 serve ----------------------------------------------------
echo "[20] 实时视图 serve（真起服务真发请求）"
D5="$(mktemp -d)"
PORT=$((8800 + RANDOM % 400))
"$PY" "$WL" --dir "$D5" init --task "视图测试" >/dev/null
"$PY" "$WL" --dir "$D5" post --agent vue1 --text "在改前端" >/dev/null
"$PY" "$WL" --dir "$D5" say --text "别动后端" >/dev/null
"$PY" "$WL" --dir "$D5" serve --port "$PORT" --interval 1 --stale-after 999 \
    > "$D5/serve.out" 2>&1 &
BG_PIDS="$BG_PIDS $!"
disown 2>/dev/null || true
sleep 3
probe=$(python3 - "$PORT" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
try:
    api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board", timeout=5))
    html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
except Exception as e:
    print("ERR", e); raise SystemExit(0)
ent = api.get("entries", [])
kinds = {e["kind"] for e in ent}
checks = [
    api.get("task") == "视图测试",
    any(e["agent"] == "vue1" for e in ent),
    "user" in kinds,                     # 用户喊话被合并进同一条时间线
    isinstance(api.get("agents"), list) and api["agents"] and "state" in api["agents"][0],
    "<!DOCTYPE html>" in html and "work-log" in html,
    # ── 页面的"能力钩子"必须还在（2026-09-22 补）────────────────────────────
    # 等待图是本工具相对其它多 agent 方案唯一独有的东西，但页面第一版**完全没渲染它**：
    # 后端算出了 hazards，前端连一个渲染位都没有，等于这个能力对外不可见。
    # 这几条不是"测 HTML 长什么样"，是钉住"后端算出来的东西有没有人在显示"：
    # 谁把 hazards 块删了、把等待列删了，这里当场红。
    "hazards" in api,                    # 后端永远给这个字段（前端 data.hazards || []）
    'id="hazards"' in html,              # 有渲染位
    "hazardsHtml" in html,               # 且真的接了渲染函数
    "waiting_ids" in html,               # agent 表用了"在等谁"
    'id="jump"' in html,                 # 停止跟随后有明确出口（原来只是默默不跟随）
    "measureStick" in html,              # 吸顶高度是量出来的，不写死像素
    # ── 告警必须"可处置"（2026-09-28 补）────────────────────────────────────
    # 「疑似卡死」是给人看的，人看到后得有下一步动作：①页面上能一键确认；
    # ②旁边能一键复制补救命令（hold）。光显示一个红字状态，收到的人只会补心跳，
    # 过 90s 又被报一次 —— 实测刷到 1800+ 条。
    'id="unacked"' in html,              # 「未确认告警」是可点的，不是死文本
    "ackAll" in html,                    # 且真的接了确认函数
    "copyHold" in html,                  # 「疑似卡死」旁能一键复制 hold 补救命令
    # 输入法（IME）防护：不判 isComposing 会把拼音回车上屏当发送（实测踩到）
    "isComposing" in html,
    # ── 协作人数是「不定量」，不许写成分数式（2026-09-28 补）──────────────────
    # 实际同时在干活的 agent 常常 3~6 个且随时增减，写成 "N/2" 会被读成
    # "上限两人 / 还有个空位"（用户实测质疑："怎么可能只有两个 agent 干活"）。
    # 所以：①只报当前人数；②分母一个字都不许剩；③必须说明人数不固定、不设上限。
    # ⚠ 分母那条**刻意写成字符串拼接**（"/" + "$" + "{"），不要在 heredoc 里
    #   直接写"美元符+花括号且不闭合"——bash 3.2 解析器会从这里开始错乱，
    #   报错位置飘到几百行之外的 case 分支上（`;;` unexpected token），
    #   本轮实测排查了半天才二分定位到这一行。
    "`协作：${cb.count} 人在干活`" in html,   # 未达标记线：只报人数
    "`🤝 协作中：${cb.count} 人在干活`" in html,  # 已达成：也只报人数
    ("/" + "$" + "{") not in html,            # 不许再出现 N/分母的分数式
    "不设上限" in html,                        # 且说明人数不固定、随时增减
    # 页面自身零外部依赖（本地绑定/离线可用）：不许引 CDN 样式或脚本
    "https://" not in html and "<link " not in html and "<script src" not in html,
]
print("PASS" if all(checks) else "FAIL " + str(checks))
PY
)
eq "HTTP 接口与页面都正常（${probe}）" "$probe" "PASS"
# 一键确认告警：按钮背后的接口必须真能把待确认数清零（不是个摆设按钮）
"$PY" "$WL" --dir "$D5" post --agent vue2 --text "开工" >/dev/null
sleep 2
"$PY" "$WL" --dir "$D5" check --stale-after 1 >/dev/null 2>&1 || true
ackres=$(python3 - "$PORT" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
req = urllib.request.Request(
    f"http://127.0.0.1:{port}/api/ack",
    data=json.dumps({"by": "用户"}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    r = json.load(urllib.request.urlopen(req, timeout=5))
    print("PASS" if r.get("ok") and int(r.get("hit", 0)) >= 1 else "FAIL " + str(r))
except Exception as e:                                   # noqa: BLE001
    print("FAIL", e)
PY
)
eq "看板一键确认告警接口可用（${ackres}）" "$ackres" "PASS"
has "serve 打印访问地址" "$(cat "$D5/serve.out")" "http://localhost:$PORT/"
kill $BG_PIDS 2>/dev/null; BG_PIDS=""
rm -rf "$D5"

# ---- 21 无默认落点（--dir 必填 / $WORK_LOG_DIR 兜底） ----------------------
echo "[21] 缺 --dir 不再猜默认落点（退 2 + 指路；\$WORK_LOG_DIR 仍生效）"
# 旧版默认落 ~/Desktop/work-log/<当前目录名>，两个坑都实测踩过：
# ① agent 不传 --dir 时静默落到桌面，触发 macOS 权限弹窗；
# ② 各 agent 的"默认"互相对不上，两块板各说各话。
# 一个不能信任的默认值比没有默认值更费神 ⇒ 2026-09-28 起缺 --dir 一律退 2 并指路。
o21=$(cd /tmp && env -u WORK_LOG_DIR "$PY" "$WL" post --agent a1 --text hi 2>&1); rc21=$?
usage "缺 --dir 且无 \$WORK_LOG_DIR ⇒ 退 2" "${rc21}"
has "报错指出三种修法里有 --dir" "$o21" "--dir"
has "报错提到 \$WORK_LOG_DIR" "$o21" "WORK_LOG_DIR"
has "报错说明已不再猜默认落点" "$o21" "不再猜默认落点"
o21e=$(cd /tmp && env WORK_LOG_DIR="$TMP" "$PY" "$WL" post --agent a1 --text "env 生效" 2>&1)
eq "\$WORK_LOG_DIR 顶上后正常写入" "$?" 0
# doctor（诊断命令）与所有命令走**同一个** resolve_dir：不许自己另养一套默认值
o21d=$(cd /tmp && env -u WORK_LOG_DIR "$PY" "$WL" doctor 2>&1); rc21d=$?
usage "doctor 缺 --dir 同样退 2（同一解析路径）" "${rc21d}"
has "doctor 的报错也是同一条指路" "$o21d" "WORK_LOG_DIR"

echo "[22] 串项目检测（记录项目路径 + 串项目告警）"
W init --task "自测" >/dev/null
same=$("$PY" -c "
import json
from pathlib import Path
d = json.load(open('$TMP/state.json'))
print('OK' if d.get('cwd') == str(Path.cwd().resolve()) else 'MISMATCH:' + str(d.get('cwd')))")
has "state 记录本项目路径" "$same" "OK"
# 伪造"这块看板属于别的项目"，init 必须当场说破，而不是让它悄悄混用
"$PY" -c "
import json
p = '$TMP/state.json'
d = json.load(open(p)); d['cwd'] = '/some/other/project'
json.dump(d, open(p, 'w'), ensure_ascii=False)"
out=$(W init --task "自测" 2>&1)
has "串项目时给出告警" "$out" "原本属于"
W init --task "自测" >/dev/null

# ---- 23 静默挂起（hold --quiet）------------------------------------------
# 驱动层（LLM agent）每步都要等模型 30~120s，期间发不出心跳。
# 它需要一个"只声明别判我卡死、但别往看板刷垃圾"的动作。
echo "[23] 静默挂起 hold --quiet"
Q="$TMP/q"; mkdir -p "$Q"
QW() { "$PY" "$WL" --dir "$Q" "$@"; }
QW init --task "静默挂起自测" --agents boss >/dev/null
QW post --agent boss --text "我先说一句，好让看板里有它" >/dev/null 2>&1
before="$(grep -c '' "$Q/board.md")"
QW hold --agent boss --quiet --seconds 60 >/dev/null 2>&1
eq "quiet 挂起返回 0" $? 0
after="$(grep -c '' "$Q/board.md")"
eq "quiet 挂起不往看板写条目" "$((after-before))" "0"
has "quiet 挂起后状态为挂起中" "$(QW status --json 2>/dev/null)" '挂起中'
QW hold --agent boss --seconds 60 >/dev/null 2>&1
after2="$(grep -c '' "$Q/board.md")"
if [ "$after2" -gt "$after" ]; then ok "普通 hold 仍会留痕（跑长任务可见）"
else ng "普通 hold 未写心跳，长任务就不可见了"; fi
# 挂起窗口内，就算很久没吭声也不算卡死（这就是它存在的意义）。
# 关键语义：静默 = max(state.last_seen, 看板最后一条心跳) 距今多久 —— 两处都要改老，
# 才是真的"很久没吭声"；只改一处会测出一个假的健康场景。
backdate() {  # 把 boss 伪装成"最后心跳在很久以前"
  # ★ 日期段也要一并改老（2026-09-30）：静默口径是 max(state.last_seen, 看板时间戳)，
  #   看板那行只锚 00:00:07 的话，静默会随"今天几点跑"在 7s~24h 之间漂 ——
  #   「离线」档按 3600s 分界，断言就会在每天午夜后一小时左右翻车。
  #   日期段改成 2000-01-01 ⇒ max 恒取 last_seen，静默恒等于 9999s，与挂钟脱钩。
  "$PY" -c "
import re, json, time
s = open('$Q/board.md', encoding='utf-8').read()
s = re.sub(r'(<boss>\s+)\d\d:\d\d:\d\d', r'\g<1>00:00:07', s)
s = re.sub(r'^## \d{4}-\d\d-\d\d\s*\$', '## 2000-01-01', s, flags=re.M)
open('$Q/board.md', 'w', encoding='utf-8').write(s)
d = json.load(open('$Q/state.json'))
d['agents']['boss']['last_seen'] = time.time() - 9999
json.dump(d, open('$Q/state.json', 'w'), ensure_ascii=False)"
}
set_window() {  # 把挂起窗口设成 +3600s（未来）或 -1s（已过期）
  "$PY" -c "
import json, time
d = json.load(open('$Q/state.json'))
d['agents']['boss']['expected_silence_until'] = time.time() + $1
json.dump(d, open('$Q/state.json', 'w'), ensure_ascii=False)"
}
backdate; set_window 3600
out="$(QW status --json 2>/dev/null)"
has   "status --json 真的吐 JSON"     "$out" '"stale_after"'
has   "挂起窗口内久不吭声仍判挂起中"   "$out" '挂起中'
hasnt "挂起窗口内不产生卡死告警"       "$out" '疑似卡死'
# 窗口过期后必须恢复"会被判卡死"——挂起不是永久免死金牌。
# --stale-after 1：阈值压到 1s，不依赖"现在离 00:00:07 很远"——
# 硬编码时刻在午夜后 45s 内是"未来/刚发生"，默认阈值下这条会在每天零点后抖红
set_window -1
backdate
# 静默恒 9999s ≈ 2.8h，已过「离线」分界（≥3600s）⇒ 档位是离线，不是疑似卡死。
# 监督语义不变：只要 stale 名单还收它、还被告警，挂起过期就没有免死金牌。
has "挂起窗口过期后照样被监督（判离线，不因挂起豁免）" "$(QW status --json --stale-after 1 2>/dev/null)" '离线'
# 驱动层退出前要能静默清掉自己最后一次 hold，否则已退出的 agent 会以"挂起中"继续装活
before="$(grep -c '' "$Q/board.md")"
QW release --agent boss --quiet >/dev/null 2>&1
eq "release --quiet 返回 0" $? 0
eq "release --quiet 不写看板条目" "$(( $(grep -c '' "$Q/board.md") - before ))" "0"
hasnt "release --quiet 后不再显示挂起中" "$(QW status --json 2>/dev/null)" '挂起中'

# ---- 24 驱动层参数解析（llm_agent.py）-------------------------------------
# 这些用例全部来自真机跑真实模型时**实际收到**的坏参数。
# 其中最危险的一条：模型把参数包成对象壳 {"arguments":{"id":1,...}} 时若丢掉 id，
# await 会退化成 --id 0（= "等任何动静"）并返回成功 —— 提问者以为自己拿到了答案。
echo "[24] 驱动层参数解析（llm_agent.py）"
PLINES="$("$PY" - "$HERE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from llm_agent import _parse_args

cases = [
    ("post",   {"tag": "收到", "text": "hi"},                       {"tag": "收到", "text": "hi"},  True),
    ("ask",    {"to": "agent2", "text": "q"},                       {"to": "agent2", "text": "q"},  True),
    # 值是**对象**的壳（真机踩到：id 丢了会让 await 静默退化成 --id 0 并返回成功）
    ("await",  {"arguments": {"id": 1, "timeout": 120}},            {"id": 1, "timeout": 120},      True),
    ("reply",  {"arguments": {"id": 1, "text": "定义"}},             {"id": 1, "text": "定义"},       True),
    # 值是**字符串**的壳
    ("reply",  {"arguments": '{"arguments": {"id": 1, "text": "x"}}'},
               {"id": 1, "text": "x"},                              True),
    ("finish", {"input": '{"summary": "done"}'},                    {"summary": "done"},            True),
    ("brief",  {},                                                  {},                             True),
    # 缺必需字段 → 必须报错，交回模型重试
    ("await",  {"arguments": {"timeout": 100}},                     {"timeout": 100},               False),
    ("post",   {"arguments": {"tag": "执行"}},                       {"tag": "执行"},                 False),
    ("reply",  "not json",                                          {},                             False),
]
for tool, raw, want, should_ok in cases:
    got, err = _parse_args(raw, tool)
    good = (got == want) and (bool(err) != should_ok)
    print(f"{'ok' if good else 'ng'}|{tool}({str(raw)[:44]}) -> {str(got)[:60]}"
          + ("" if good else f"  err={err[:50]!r}"))
PY
)"
while IFS='|' read -r st desc; do
  [ -z "$st" ] && continue
  if [ "$st" = ok ]; then ok "参数解析 $desc"; else ng "参数解析 $desc"; fi
done < <(printf '%s\n' "$PLINES")

# 驱动层必须把 await 的三种出口翻译成人话交给模型 —— 把 1 和 3 混成一个
# "失败了" 会让模型只知道"再等等"，而那正是白等超时的来源。
DL="$("$PY" - "$HERE" <<'PY'
import os, shutil, subprocess, sys, tempfile
sys.path.insert(0, sys.argv[1])
from llm_agent import run_tool

wl = os.path.join(sys.argv[1], "work_log.py")
d = tempfile.mkdtemp(prefix="work-drv.")
def W(*a):
    return subprocess.run([sys.executable, wl, "--dir", d, *a],
                          capture_output=True, text=True).stdout

W("init", "--task", "驱动层", "--agents", "d1,d2")
W("post", "--agent", "d1", "--text", "开工")
W("post", "--agent", "d2", "--text", "开工")

def call(tool, args):
    return run_tool(d, "d1", tool, args, lambda *_: None)

# (a) 对端已收工 → await 必须是"别等了"，不是"再等等"
W("post", "--agent", "d2", "--text", "我先收工", "--done")
W("ask", "--agent", "d1", "--to", "d2", "--text", "结果呢")
out, done = call("await", {"id": 1, "timeout": 5})
okay = ("已经收工" in out) and ("不会来" in out) and not done
print(("ok" if okay else "ng") + "|对方已收工 -> 驱动层告知模型别等了")
print(("ok" if ("拍板" in out or "换一个" in out) else "ng") + "|驱动层给出下一步动作")

# (b) 对端活着但不答 → 必须说清是"超时"，不能暗示拿到了回答
W("post", "--agent", "d2", "--text", "我复活了")
W("ask", "--agent", "d1", "--to", "d2", "--text", "第二个问题")
out, done = call("await", {"id": 2, "timeout": 3})
print(("ok" if ("超时" in out and not done) else "ng") + "|超时 -> 驱动层明确说是超时")

# (c) 缺 id 必须被拦住，绝不能掉进 --id 0 的合法语义
out, done = call("await", {"timeout": 30})
blocked = ("编号" in out) and ("不要用 0" in out) and not done
print(("ok" if blocked else "ng") + "|缺 id 被硬拦（不退化成 --id 0）  " + out[:60])
shutil.rmtree(d, ignore_errors=True)
PY
)"
while IFS='|' read -r st desc; do
  [ -z "$st" ] && continue
  if [ "$st" = ok ]; then ok "$desc"; else ng "$desc"; fi
done < <(printf '%s\n' "$DL")

# ---- 25 等待险情：心跳全绿，团队其实已经卡死 -------------------------------
# 心跳只能证明"某个 agent 最近动过"，证明不了"它等的那个答案还会不会来"。
# 这一组就是把那两类「看板一片健康、实际全员卡住」的故障钉死。
echo "[25] 等待险情（等待图：不可达等待 / 互相等待）"
HZ="$(mktemp -d)"

# --- A 不可达等待：a1 在等 a2，a2 却已经写「任务完成」收工了 ---
"$PY" "$WL" --dir "$HZ" init --task "险情A" --agents a1,a2 >/dev/null
"$PY" "$WL" --dir "$HZ" post --agent a1 --text "开工" >/dev/null
"$PY" "$WL" --dir "$HZ" post --agent a2 --text "开工" >/dev/null
"$PY" "$WL" --dir "$HZ" ask --agent a1 --to a2 --text "你那边结果呢？" >/dev/null
# 起一个真的 await（interval 拉长，好让 waiting 关系稳定地留在共享状态里）
"$PY" "$WL" --dir "$HZ" await --agent a1 --id 1 --timeout 60 --interval 30 >/dev/null 2>&1 &
APID=$!
sleep 1.5
"$PY" "$WL" --dir "$HZ" post --agent a2 --text "我先收工" --done >/dev/null
o=$("$PY" "$WL" --dir "$HZ" check --stale-after 90 --readonly)
has "A 认出不可达等待" "$o" "不可达等待"
has "A 点明这个答案不会来了" "$o" "这个答案不会来了"
has "A 险情计数正确" "$o" "协作险情 1 条"
eq "A 不该同时误报卡死（心跳全是绿的）" "$(printf '%s\n' "$o" | grep -c '<= 疑似卡死')" "0"
# 有险情时不能退 0 —— 否则脚本里 `check || 告警` 会把这类故障整个漏掉，
# 而它偏偏是这套东西最想抓的那类。
"$PY" "$WL" --dir "$HZ" check --stale-after 90 --readonly >/dev/null; eq "A 有险情时 check 退出码 1" $? 1
o=$("$PY" "$WL" --dir "$HZ" status --stale-after 90 --json)
has "A 机器可读输出带 hazards" "$o" '"hazards"'
has "A hazards 标出类别" "$o" "不可达等待"
has "A 每行状态能看到它在等谁" "$o" '"waiting_on": 1'

# 落盘告警 + 冷却期去重（否则每 2s 扫一次会刷爆告警台账）
"$PY" "$WL" --dir "$HZ" check --stale-after 90 >/dev/null
n1=$(grep -c '不可达等待' "$HZ/alerts.md")
if [ "$n1" -ge 1 ]; then ok "A 险情被写进告警台账"; else ng "A 险情没进告警台账"; fi
"$PY" "$WL" --dir "$HZ" check --stale-after 90 >/dev/null
eq "A 冷却期内不重复刷屏" "$(grep -c '不可达等待' "$HZ/alerts.md")" "$n1"

# 过期标记不能被采信：等待者被杀后静默超过阈值，就不再算"还在等"
kill "$APID" 2>/dev/null; wait "$APID" 2>/dev/null
sleep 4
o=$("$PY" "$WL" --dir "$HZ" check --stale-after 3 --readonly)
hasnt "A 过期的等待标记不再触发险情" "$o" "不可达等待"

# --- B 互相等待（真死锁）：双方各卡在自己的 await 里，谁都不会先答 ---
# ⚠ 2026-09-29 语义变了：await **自己**会认出环并立刻退 5（[43] 组锁着），
#   所以旧版「两个后台 await 干等 + sleep 3 再造险情」的写法已经造不出场景了。
#   更要紧的是**两边都得退出来**：先认出环的一方如果在退出时顺手清掉自己的等待标记，
#   另一侧下一轮扫过来时环已经没了 ⇒ 它只会白等到 --timeout 才超时退出。
#   实测（2026-09-29）：b2 1s 内退 5，b1 却干等满 60s 才退 1，
#   全程没有任何人告诉它"你们已经死锁了" —— 死锁是两个人的事，只救一边等于没救。
#   所以现在的契约是：两边都退 5 且都远早于 --timeout；标记**故意留着**，
#   好让看门狗也看得见这是一条环；一方 reply 后环破、标记摘掉、check 回到 0。
# 必须另起一个干净目录：A 阶段留下的 a1 还在(过期)等待，会把险情串进来。
HZ2="$(mktemp -d)"
Q1=$("$PY" "$WL" --dir "$HZ2" init --task "险情B" --agents b1,b2 >/dev/null; \
     "$PY" "$WL" --dir "$HZ2" post --agent b1 --text "开工" >/dev/null; \
     "$PY" "$WL" --dir "$HZ2" post --agent b2 --text "开工" >/dev/null; \
     "$PY" "$WL" --dir "$HZ2" ask --agent b1 --to b2 --text "B1 问 B2" | grep -oE '提问 #[0-9]+' | tr -dc '0-9')
Q2=$("$PY" "$WL" --dir "$HZ2" ask --agent b2 --to b1 --text "B2 问 B1" | grep -oE '提问 #[0-9]+' | tr -dc '0-9')
eq "B 两个提问各自编号" "$Q1-$Q2" "1-2"
B1L="$HZ2/b1.log"; B2L="$HZ2/b2.log"
T0=$(date +%s)
"$PY" "$WL" --dir "$HZ2" await --agent b1 --id "$Q1" --timeout 45 --interval 1 >"$B1L" 2>&1 &
B1P=$!
"$PY" "$WL" --dir "$HZ2" await --agent b2 --id "$Q2" --timeout 45 --interval 1 >"$B2L" 2>&1 &
B2P=$!
wait "$B1P"; B1R=$?
wait "$B2P"; B2R=$?
BEL=$(( $(date +%s) - T0 ))
eq "B1 先扫到的一侧认出互相等待（退 5）" "$B1R" 5
eq "B1 后扫到的一侧也认出互相等待（退 5）" "$B2R" 5
# ★ 关键断言：不是「一边退 5、另一边干等满 45s 才超时」—— 两者都必须远早于 --timeout 收手
if [ "$BEL" -le 20 ]; then ok "B1 两边 ${BEL}s 内都收手（没人干等满 45s）"
else ng "B1 两边花了 ${BEL}s 才停 —— 有一边干等满了（对端的标记没留住？）"; fi
has "B1 先退的一侧点名对方在等哪一条" "$(cat "$B2L")" "回应 #"
has "B1 后退的一侧也点名对方在等哪一条" "$(cat "$B1L")" "回应 #"
# 两边都退了之后，等待标记是**故意**留着的 ⇒ 看门狗必须仍看得见这条环
o=$("$PY" "$WL" --dir "$HZ2" check --stale-after 90 --readonly)
has "B2 两边都退了之后看门狗仍认出互相等待" "$o" "互相等待"
has "B2 点明这是一条真死锁" "$o" "真死锁"
has "B2 措辞改成「都已收手」（不再说各卡在 await 里）" "$o" "收手"
hasnt "B2 不把死锁误判成不可达等待" "$o" "不可达等待"
"$PY" "$WL" --dir "$HZ2" check --stale-after 90 --readonly >/dev/null; eq "B2 两边退了之后 check 退出码 1" $? 1
# 一方先答 → 环被打破：「回应即撤掉等待」，无论那个 await 进程还在不在
"$PY" "$WL" --dir "$HZ2" reply --agent b2 --id "$Q1" --text "我先答你" >/dev/null
o=$("$PY" "$WL" --dir "$HZ2" status --stale-after 90 --json | "$PY" -c \
     'import json,sys; print({a["name"]: a["waiting_on"] for a in json.load(sys.stdin)["agents"]})')
has "B3 收到回应后 b1 的等待标记被撤掉" "$o" "'b1': None"
has "B3 没人答的那条 b2 仍在等（不误清）" "$o" "'b2': 2"
hasnt "B3 环破了之后不再报险情" "$("$PY" "$WL" --dir "$HZ2" check --stale-after 90 --readonly)" "互相等待"
# 反向：险情消失后必须回到 0，否则"退 1"就变成了永久噪声
"$PY" "$WL" --dir "$HZ2" check --stale-after 90 --readonly >/dev/null; eq "B3 险情消失后 check 回到 0" $? 0
rm -rf "$HZ" "$HZ2"

# ---- 26 通信熔断：把预算从"互相确认"和"循环刷心跳"里救回来 -----------------
# 这两类故障的心跳都是绿的 —— 一个聊得最起劲、一个刷得最勤，
# 从看板看是"最健康的两个人"，实际一个在烧 token、一个在烧循环。
echo "[26] 通信熔断（乒乓 / 心跳预算）"
CH="$(mktemp -d)"
C() { "$PY" "$WL" --dir "$CH" "$@"; }
C init --task "熔断测试" --agents p1,p2 --rate-cap 0 >/dev/null   # 先关心跳预算，专测乒乓
C post --agent p1 --text "开工" >/dev/null
C post --agent p2 --text "开工" >/dev/null

# 前 3 个来回 = 6 条点对点记录，正好压到预警线
for i in 1 2 3; do
  C ask --agent p1 --to p2 --text "第 $i 问" >/dev/null
  C reply --agent p2 --id "$i" --text "第 $i 答" >/dev/null
done
o=$(C ask --agent p1 --to p2 --text "第 4 问"); rc=$?
eq "预警线内仍然放行（不误伤）" "$rc" "0"
has "到预警线时给出提醒" "$o" "连续来回"
has "提醒里预告会被熔断" "$o" "会被强制熔断"

# 继续推到硬阈值：第 6 问时 11 条、第 7 问时 12 条
C reply --agent p2 --id 4 --text "第 4 答" >/dev/null
C ask --agent p1 --to p2 --text "第 5 问" >/dev/null
C reply --agent p2 --id 5 --text "第 5 答" >/dev/null
C ask --agent p1 --to p2 --text "第 6 问" >/dev/null
C ask --agent p1 --to p2 --text "第 7 问" >/dev/null
# 此刻尾部的连续来回 = 12 条 → 两侧都被拦
o=$(C ask --agent p1 --to p2 --text "第 8 问"); rc=$?
eq "ask 侧到硬阈值被熔断（退出码 4）" "$rc" "4"
has "熔断文案点名「通信熔断」" "$o" "通信熔断"
has "熔断文案给出收敛动作" "$o" "收敛"
has "熔断文案给出 --force 出口" "$o" "--force"
o=$(C reply --agent p2 --id 6 --text "答第 6 问"); rc=$?
eq "reply 侧同样被熔断" "$rc" "4"
eq "被熔断的 ask 没有落盘（编号没被消耗）" \
   "$(python3 -c "import json;print(len(json.load(open('$CH/state.json'))['exchanges']))")" "7"
o=$(C ask --agent p1 --to p2 --text "第 8 问（确认要深挖）" --force); rc=$?
eq "--force 可以放行" "$rc" "0"

# 看板上要看得见 —— 否则熔断只是个暗箱
o=$(C check --stale-after 999 --readonly)
has "险情里报出「通信过热」" "$o" "通信过热"
has "险情标题是「协作险情」" "$o" "协作险情"
has "过热文案给出轮次" "$o" "连续来回"
has "机器可读输出里也有" "$(C status --stale-after 999 --json)" "通信过热"

# 换人对话后计数必须归零（正常的话题切换不能被冤枉）
C post --agent p3 --text "我插一句" >/dev/null
C ask --agent p3 --to p1 --text "帮我看看" >/dev/null
o=$(C ask --agent p1 --to p2 --text "话题换了，重新问"); rc=$?
eq "被第三方打断后不再熔断" "$rc" "0"
hasnt "旧的对子不再报过热" "$(C check --stale-after 999 --readonly)" "通信过热"

# 心跳预算：循环刷心跳的 agent 会被拦住
CB="$(mktemp -d)"
B() { "$PY" "$WL" --dir "$CB" "$@"; }
B init --task "预算测试" --agents q1 --rate-cap 5 >/dev/null
for i in 1 2 3 4 5; do B post --agent q1 --text "心跳 $i" >/dev/null; done
o=$(B post --agent q1 --text "心跳 6"); rc=$?
eq "超过心跳预算被拦（退出码 4）" "$rc" "4"
has "预算文案说明这是循环" "$o" "心跳预算熔断"
has "预算文案给出调整办法" "$o" "rate-cap"
o=$(B post --agent q1 --text "心跳 7" --tag 决定 2>&1); rc=$?
eq "预算不看 tag（写了结论也一样拦）" "$rc" "4"
has "看板标出心跳偏密" "$(B check --stale-after 999 --readonly)" "心跳偏密"
eq "status --json 暴露 post_cap" \
   "$(B status --stale-after 999 --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["post_cap"])')" "5"
B init --task "预算测试" --agents q1 --rate-cap 0 >/dev/null
o=$(B post --agent q1 --text "关了熔断就能发"); rc=$?
eq "rate-cap 0 可以关掉预算" "$rc" "0"
has "关掉之后照常落盘" "$o" "关了熔断就能发"
rm -rf "$CH" "$CB"

# ---- 27 多路 await（--id 1,2,3 / --any）------------------------------------
echo "[27] 多路 await（--id 1,2,3 / --any）"
MW="$(mktemp -d)"
M() { "$PY" "$WL" --dir "$MW" "$@"; }
M init --task "多路等待" --agents w1,w2,w3 --rate-cap 0 >/dev/null
for n in w1 w2 w3; do M post --agent "$n" --text "开工" >/dev/null; done
M ask --agent w1 --to w2 --text "问 w2" >/dev/null      # #1
M ask --agent w1 --to w3 --text "问 w3" >/dev/null      # #2

# --any：谁先答都能往下走
( sleep 2; M reply --agent w3 --id 2 --text "w3 先答" >/dev/null ) &
o=$(M await --agent w1 --id 1,2 --any --timeout 20 --interval 1); rc=$?
wait
eq "--any 有回应即返回 0" "$rc" "0"
has "--any 打印拿到的那条答复" "$o" "w3 先答"
has "--any 说明是任一即达成" "$o" "任一回应即达成"
eq "--any 结束后不留等待标记" \
   "$(M status --stale-after 999 --json | "$PY" -c 'import json,sys;print([a["waiting_ids"] for a in json.load(sys.stdin)["agents"] if a["name"]=="w1"][0])')" \
   "[]"

# 默认「全部」：只答一条必须继续等，超时返回 1 并列出还缺哪条
M ask --agent w1 --to w2 --text "再问 w2" >/dev/null    # #3
M ask --agent w1 --to w3 --text "再问 w3" >/dev/null    # #4
( sleep 2; M reply --agent w2 --id 3 --text "w2 答了" >/dev/null ) &
o=$(M await --agent w1 --id 3,4 --timeout 6 --interval 1); rc=$?
wait
eq "全部模式：只答一条仍超时返回 1" "$rc" "1"
has "超时文案列出还缺哪条" "$o" "#4"
has "超时文案带上已拿到的那条" "$o" "w2 答了"

# 全部模式 + 目标收工 → 结构性等不齐，返回 3 且不空耗超时
M post --agent w3 --text "我先收工" --done >/dev/null
t0=$(date +%s)
o=$(M await --agent w1 --id 4 --timeout 30 --interval 1); rc=$?
el=$(( $(date +%s) - t0 ))
eq "多路里目标已收工 → 返回 3" "$rc" "3"
has "并说明对方已收工" "$o" "对方已收工"
if [ "$el" -lt 8 ]; then ok "没白耗完 30s 超时（实际 ${el}s）"; else ng "白等满了超时（${el}s）"; fi

# 混合：一条已答、一条已收工 → 3，且两条都要交代清楚
M ask --agent w1 --to w3 --text "第三种问法" >/dev/null  # #5
o=$(M await --agent w1 --id 3,5 --timeout 20 --interval 1); rc=$?
eq "混合（已答 + 已收工）→ 返回 3" "$rc" "3"
has "混合时仍打印已拿到的答复" "$o" "w2 答了"
has "混合时说明等不齐" "$o" "等不齐"

# 等待图必须为每个未回应的目标各建一条边
M post --agent w3 --text "我又回来了" >/dev/null
A1=$(M ask --agent w1 --to w2 --text "并行等 w2" | grep -oE '提问 #[0-9]+' | tr -dc '0-9')
A2=$(M ask --agent w1 --to w3 --text "并行等 w3" | grep -oE '提问 #[0-9]+' | tr -dc '0-9')
"$PY" "$WL" --dir "$MW" await --agent w1 --id "$A1,$A2" --timeout 60 --interval 30 >/dev/null 2>&1 &
MP=$!
sleep 2
has "多路等待时状态里是多个编号" \
    "$(M status --stale-after 999 --json | "$PY" -c 'import json,sys;print([a["waiting_ids"] for a in json.load(sys.stdin)["agents"] if a["name"]=="w1"][0])')" \
    "[$A1, $A2]"
M post --agent w3 --text "这次真收工" --done >/dev/null
o=$(M check --stale-after 90 --readonly)
has "多路等待里单独一条不可达也会被发现" "$o" "不可达等待"
has "而且点的是收工那一个" "$o" "<w3>"
hasnt "不把还活着的那个也报成不可达" "$o" "等 <w2> 回答"
kill "$MP" 2>/dev/null; wait "$MP" 2>/dev/null

# 参数校验：坏编号必须在"开始等"之前就被拦下
o=$(M await --agent w1 --id 999 --timeout 5 2>&1); rc=$?
usage "等不存在的编号 → 提前报错" "$rc"
has "并说明没有这个编号" "$o" "没有编号 #999"
o=$(M await --agent w2 --id 1 --timeout 5 2>&1); rc=$?
usage "等不是自己问的编号 → 报错" "$rc"
has "并说明不是你问的" "$o" "不是你问的"
o=$(M await --agent w1 --id 0 --timeout 5 2>&1); rc=$?
eq "--id 0 被直接拒绝（彻底拆掉那个陷阱）" "$rc" "2"
has "并解释想要那个语义就别写 --id" "$o" "就不要写 --id"
o=$(M await --agent w1 --any --timeout 5 2>&1); rc=$?
usage "--any 却不给编号 → 报错" "$rc"
has "并说明 --any 的适用范围" "$o" "只在同时等多条"
rm -rf "$MW"

# ---- 28 看板解析：时间戳要对，且不许退回"逐行 strptime" ---------------------
# 为什么要专门盯这个：`await` 每轮轮询都要扫一遍看板，实测 2000 行时
# `datetime.strptime` 一个人吃掉 34ms（占单轮 97%）。
# 换成"日期段算一次基准 + 整数加法"后降到 11ms。
# 这里不比较秒数（机器负载会让它抖），而是**直接数 strptime 被调了几次** ——
# 语义断言，跟机器快慢无关。
echo "[28] 看板解析（历史日期正确 + 不许逐行 strptime）"
PB="$(mktemp -d)"
# 注意：old1 **不能**预注册。预注册会把 last_seen 设成"当下"，而判定取的是
# max(state.last_seen, 看板那行的时间戳) —— 2020 年那行反而更旧，被盖掉就测不到日期解析了。
# 只让它出现在看板里，判定才会直接采信看板时间戳（"仅出现在看板（手写）"这条路径）。
"$PY" "$WL" --dir "$PB" init --task "解析" --agents new1 >/dev/null
{
  printf '## 2000-01-01\n<old1> 12:00:00 [执行] 上个世纪的日志\n'
  printf '## %s\n' "$(date +%Y-%m-%d)"
  printf '<new1> %s [执行] 刚刚\n' "$(date +%H:%M:%S)"
  i=1; while [ $i -le 3000 ]; do
    printf '<filler%s> 00:00:%02d [执行] 填充 %d\n' "$((i%7))" "$((i%60))" "$i"
    i=$((i+1))
  done
} > "$PB/board.md"
o=$("$PY" "$WL" --dir "$PB" check --stale-after 60 --readonly)
has "历史日期段的 agent 被识别出来" "$o" "old1"
has "并按它自己的日期判离线（静默 26 年，久到不可能是「卡在某一步」）" \
    "$(printf '%s\n' "$o" | grep '^old1')" "离线"
has "刚写过心跳的 agent 判为心跳中" "$(printf '%s\n' "$o" | grep '^new1')" "心跳中"

o="$("$PY" - "$HERE" "$PB" <<'PY'
import datetime as dt, importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("wl", sys.argv[1] + "/work_log.py")
wl = importlib.util.module_from_spec(spec); spec.loader.exec_module(wl)
n = {"calls": 0}
Base = dt.datetime
class Counting(Base):
    @classmethod
    def strptime(cls, *a, **k):
        n["calls"] += 1
        return Base.strptime(*a, **k)
wl.datetime = Counting
d = pathlib.Path(sys.argv[2])
ents = wl.board_entries(d)
lines = len((d / "board.md").read_text(encoding="utf-8").splitlines())
# 允许的调用次数只跟"日期段个数"有关（1 个起始 + 每个 ## 段一次），与行数无关
okay = n["calls"] <= 8 and len(ents) == lines - 2
print(("ok" if okay else "ng") + f"|解析 {lines} 行只用了 {n['calls']} 次 strptime，"
      f"认出 {len(ents)} 条心跳")
PY
)"
while IFS='|' read -r st desc; do
  [ -z "$st" ] && continue
  if [ "$st" = ok ]; then ok "$desc"; else ng "$desc"; fi
done < <(printf '%s\n' "$o")
rm -rf "$PB"

# ---- 29 退出码契约：用法错误必须是 2，绝不能是 1 -----------------------------
# 这一组是给一个**已经发生过的真事故**上的锁：
#   以前所有前置条件错误都写 `sys.exit("✗ …")`，Python 对字符串参数退 **1**，
#   而 1 在这套协议里是「超时 / 对方没回」的业务结论。
#   于是 "我把编号写错了" 和 "对方不配合" 在调用方看来一模一样 ——
#   一个工具用法错误被当成了业务事实，agent 据此继续往下做。
# 断言方式：不仅要求等于 2，还专门把「等于 1」单独报出来（否则又只测了个数字）。
echo "[29] 退出码契约（用法错误 → 2，且绝不许退成业务码 1）"
D29="$TMP/g29"
"$PY" "$WL" --dir "$D29" init --agents q1,q2 >/dev/null 2>&1
"$PY" "$WL" --dir "$D29" ask --agent q1 --to q2 --text "先造一条提问" >/dev/null 2>&1
"$PY" "$WL" --dir "$D29" ask --agent q2 --to q1 --text "再造一条反方向的" >/dev/null 2>&1

# 常量表本身就是对外契约，顺手锁住
consts=$("$PY" - "$HERE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import work_log as w
print(w.EXIT_OK, w.EXIT_TIMEOUT, w.EXIT_USAGE, w.EXIT_PEER_DONE, w.EXIT_BREAKER, w.EXIT_INTERNAL)
PY
)
eq "退出码常量 = 0/1/2/3/4/70" "$consts" "0 1 2 3 4 70"
eq "前置条件错误统一走 die()" "$(grep -c 'def die(' "$WL" | tr -d ' ')" 1
# 静态护栏：源码里不能再出现"语句位置上的字符串型 sys.exit"（那个会退 1）。
# 只认语句位置（行首缩进 / `;` `:` `(` `[` `{` 之后），
# 这样注释和文档里引述这个坑（带反引号的写法）不会被误伤。
STREXIT=$(grep -cE '(^[[:space:]]*|[;:(\[{][[:space:]]*)sys\.exit\([[:space:]]*f?"' "$WL" 2>/dev/null || true)
[ -z "$STREXIT" ] && STREXIT=0
eq "源码里不再有语句位置的字符串型 sys.exit（会退 1）" "$STREXIT" 0
# 结构性护栏（2026-09-28 补）：`KNOWN_OPTS` 是**手工维护**的"选项名单"，
# 漏登记一个就等于给 `--text` 开了个"吞掉下一个选项"的后门
# （`--text --timeout` 会被静默当成正文，用法错误伪装成内容，退 0 一路往下走）。
# 实测漏了 5 个（--any --as --collab-min --report --timeout）。
# 手工名单迟早会漏 —— 所以直接从 argparse 把所有选项拉出来对账：
# 以后谁再加选项忘了登记，这里当场红，不靠人记得。
KNOWN_AUDIT=$("$PY" - "$WL" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("wl_mod", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
opts = set()
def grab(parser):
    for act in parser._actions:
        for s in act.option_strings:
            if s.startswith("--"):
                opts.add(s)
        ch = getattr(act, "choices", None)
        if isinstance(ch, dict):
            for sub in ch.values():
                if hasattr(sub, "_actions"):
                    grab(sub)
grab(m.build_parser())
missing = sorted(opts - set(m.KNOWN_OPTS) - {"--help"})
print("OK" if not missing else "MISSING " + ",".join(missing))
PY
)
eq "所有 CLI 选项都登记进 KNOWN_OPTS（漏了会吞掉下一个参数）" "$KNOWN_AUDIT" "OK"

u29() {  # u29 描述 参数...
  local desc="$1"; shift
  "$PY" "$WL" --dir "$D29" "$@" >/dev/null 2>&1; local rc=$?
  if   [ "$rc" = 2 ]; then ok "「${desc}」→ 2"
  elif [ "$rc" = 1 ]; then ng "「${desc}」退成了 1 —— 用法错误伪装成「对方没回」"
  else ng "「${desc}」期望 2 实际 $rc"; fi
}
u29 "空心跳文本"        post --agent q1 --text ""
u29 "agent 名含空格"    post --agent "a b" --text x
u29 "agent 名含 >"      post --agent "a>b" --text x
u29 "保留名 watchdog"   post --agent watchdog --text x
u29 "stale-after 设 0"  check --stale-after 0
u29 "空资源名"          lock --agent q1 --resource ""
u29 "问自己"            ask --agent q1 --to q1 --text "自言自语"
u29 "问别人但文本空"    ask --agent q1 --to q2 --text ""
u29 "答不存在的编号"    reply --agent q2 --id 9999 --text "嗯"
u29 "代答别人的提问"    reply --agent q2 --id 2 --text "越俎代庖"
u29 "重复回应同一问"    reply --agent q1 --id 1 --text "补一句"
u29 "回到不存在的编号"  ack-user --agent q1 --id 9999 --text x
u29 "等不存在的编号"    await --agent q1 --id 9999 --timeout 1
u29 "等别人的提问"      await --agent q2 --id 1 --timeout 1
u29 "--any 却不给编号"  await --agent q1 --any --timeout 1
u29 "--id 0"            await --agent q1 --id 0 --timeout 1
u29 "不认识的子命令"    frobnicate
u29 "缺必填参数"        post --agent q1

# 反向：确认 1 **仍然可达** —— 否则这组测试等于在验证"把所有码都改成 2"
"$PY" "$WL" --dir "$D29" await --agent q1 --id 1 --timeout 1 --interval 0.2 >/dev/null 2>&1
eq "对端在线但不答，超时仍是 1" "$?" 1

# 驱动层必须把 2 和 70 翻译成人话，否则模型还是会把它们读成"对方不配合"
eq "驱动层翻译了 rc=2（用法错误）" "$(grep -c 'rc == 2' "$HERE/llm_agent.py" | tr -d ' ')" 1
eq "驱动层翻译了 rc=70（工具坏了）" "$(grep -c 'rc == 70' "$HERE/llm_agent.py" | tr -d ' ')" 1

# ---- 30 多人协作（标记线：默认 ≥2 个 agent 在干活） -------------------------
echo "[30] 多人协作感知"
D30="$TMP/g30"
C30(){ "$PY" "$WL" --dir "$D30" "$@"; }
C30 init --task "协作测试" >/dev/null
C30 post --agent solo --text "一个人先开工" >/dev/null
j30=$(C30 status --json)
eq "(a) 单人在干活 → count 1" "$(printf '%s' "$j30" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["count"])')" 1
eq "(a) 单人未达协作 → active false" "$(printf '%s' "$j30" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["active"])')" "False"
o30s=$(C30 check --readonly)
has "(a2) 单人时也只报人数（协作：1 个 agent 在干活）" "$o30s" "协作：1 个 agent 在干活"
eq "(a3) 呈现里没有「N/M」分数式" "$(printf '%s' "$o30s" | grep '协作' | grep -cE '[0-9]+/[0-9]+')" 0
o30=$(C30 post --agent mate --text "第二个人也开工")
has "(b) 第二个 agent 开工时 post 提示协作" "$o30" "🤝 协作中"
j30=$(C30 status --json)
eq "(c) 双人在干活 → count 2" "$(printf '%s' "$j30" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["count"])')" 2
eq "(c) 双人达成启动条件 → active true" "$(printf '%s' "$j30" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["active"])')" "True"
o30s=$(C30 check --readonly)
has "(d) status 达成后显示「协作中：2 个 agent 在干活」" "$o30s" "🤝 协作中：2 个 agent 在干活"
eq "(d2) 达成后同样不写分母" "$(printf '%s' "$o30s" | grep '协作' | grep -cE '[0-9]+/[0-9]+')" 0
C30 check >/dev/null                        # 非只读扫描：emit 写切换事件
C30 check >/dev/null                        # 再扫一次：事件不重复
eq "(e) 协作开启事件只写一次" "$(grep -c '协作模式开启' "$D30/board.md")" 1
C30 post --agent solo --done --text "收工" >/dev/null
C30 post --agent mate --done --text "收工" >/dev/null
C30 check >/dev/null
has "(f) 全员收工后写协作结束" "$(cat "$D30/board.md")" "多人协作模式结束"
eq "(g) 收工后可重新触发（标记已复位）" "$(grep -c '协作模式结束' "$D30/board.md")" 1

# ---- 30b 标记线可配置（`init --collab-min N`） ------------------------------
# 为什么要有这一条：同时干活的 agent 数**不固定**（常见 3~6 个），
# 把标记线焊死在 2 上会让"我们这条线平时就 4 个人"的团队没法表达自己的常态；
# 而写死一个数呈现出来又像是"上限两人"。所以门槛可调、且呈现只报人数。
echo "[30b] 协作标记线可配置"
D30B="$TMP/g30b"
C30B(){ "$PY" "$WL" --dir "$D30B" "$@"; }
C30B init --task "标记线可调" --collab-min 3 >/dev/null
eq "(a) 标记线写进 state（required=3）" "$(C30B status --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["required"])')" 3
C30B post --agent a --text "开工" >/dev/null
C30B post --agent b --text "开工" >/dev/null
j30b=$(C30B status --json)
eq "(b) 抬到 3 后 2 人 → count 仍为 2" "$(printf '%s' "$j30b" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["count"])')" 2
eq "(b) 抬到 3 后 2 人 → active false" "$(printf '%s' "$j30b" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["active"])')" "False"
o30b=$(C30B check --readonly)
has "(c) 未达标记线时点明「标记线」且说明人数不设上限" "$o30b" "不设上限"
has "(c2) 文案带出可调开关" "$o30b" "init --collab-min"
C30B post --agent c --text "开工" >/dev/null
eq "(d) 3 人在干活 → active true" "$(C30B status --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["active"])')" "True"
has "(d2) 3 人时就报 3（不是「到顶」的 2/2）" "$(C30B check --readonly)" "🤝 协作中：3 个 agent 在干活"
# 非法门槛必须在**动手前**拒绝，且不动已落盘的值（die() → 退 2）
usage30b(){ if [ "$2" = 2 ]; then ok "$1（→2）"; else ng "$1  期望[2] 实际[$2]"; fi; }
C30B init --task x --collab-min 0 >/dev/null 2>&1
usage30b "(e) --collab-min 0 被拒（退 2）" "$?"
C30B init --task x --collab-min -3 >/dev/null 2>&1
usage30b "(e2) --collab-min 负数被拒（退 2）" "$?"
eq "(f) 被拒后已落盘的门槛未被改动" "$(C30B status --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["required"])')" 3

# ---- 30c 卡死阈值可声明：隐藏看门狗不再用写死的 45s -------------------------
# 事故形状：用户 `serve --stale-after 90` 起了一个看门狗，可 `post` 触发的**自动界面**
# 又偷偷起了一只（提示行里只有"协作界面已自动启动"，看不到阈值），那只写死用内置 45s。
# 于是"我明明设了 90s"是句空话 —— 真 LLM agent 单轮 30~120s，必然被误报。
# 修法：阈值能被**声明一次**（state 或 env），且自动起的那只必须继承它。
echo "[30c] 卡死阈值声明一次、两个看门狗都听"
D30C="$TMP/g30c"
C30C(){ "$PY" "$WL" --dir "$D30C" "$@"; }
C30C init --task "阈值" >/dev/null
eq "(a) 未声明时退回内置默认" "$(env -u WORK_LOG_STALE_AFTER "$PY" - "$WL" "$D30C" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("wl_mod", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.board_stale_after(json.load(open(sys.argv[2] + "/state.json"))))
PY
)" 45.0
C30C init --task "阈值" --stale-after 90 >/dev/null
eq "(b) unset env 时取看板声明的 90" "$(env -u WORK_LOG_STALE_AFTER "$PY" - "$WL" "$D30C" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("wl_mod", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.board_stale_after(json.load(open(sys.argv[2] + "/state.json"))))
PY
)" 90.0
eq "(c) env 优先于 state（会话级一键改）" "$(WORK_LOG_STALE_AFTER=200 "$PY" - "$WL" "$D30C" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("wl_mod", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.board_stale_after(json.load(open(sys.argv[2] + "/state.json"))))
PY
)" 200.0
eq "(d) env 写坏值时不炸、退回 state" "$(WORK_LOG_STALE_AFTER=abc "$PY" - "$WL" "$D30C" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("wl_mod", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.board_stale_after(json.load(open(sys.argv[2] + "/state.json"))))
PY
)" 90.0
# 自动起的那只 serve 命令行里必须真的带上阈值（不能只是内存里算对）
eq "(e) _spawn_serve 把阈值传给子进程" "$(grep -c 'cmd += \["--stale-after"' "$WL" | tr -d ' ')" 1
eq "(e2) auto_ui 起服务时确实算了阈值" "$(grep -c 'sa = board_stale_after(st)' "$WL" | tr -d ' ')" 1
usage30c(){ if [ "$2" = 2 ]; then ok "$1（→2）"; else ng "$1  期望[2] 实际[$2]"; fi; }
C30C init --task x --stale-after 0 >/dev/null 2>&1
usage30c "(f) --stale-after 0 被拒（→2）" "$?"
C30C init --task x --stale-after -5 >/dev/null 2>&1
usage30c "(f2) --stale-after 负数被拒（→2）" "$?"
eq "(g) 被拒后已落盘的阈值未被改动" "$(env -u WORK_LOG_STALE_AFTER "$PY" - "$WL" "$D30C" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("wl_mod", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.board_stale_after(json.load(open(sys.argv[2] + "/state.json"))))
PY
)" 90.0
# 光有 helper 算对不算数：**各子命令不传参时也必须取到板里声明的值**。
# （这一条是 2026-09-28 补的：原先只有 auto_ui 继承，`serve`/`watch`/`check` 手起时
#   仍用写死的 45s —— 于是「板里声明过 90」对手起的那只看门狗完全无效。）
# ⚠ 别写成 `env -u VAR C30C status` —— `env` 只认外部命令，**不能调用 shell 函数**
#   （实测：输出直接为空，三条断言全变成"期望 90 实际空"）。
#   要么展开成完整命令，要么用 `VAR=值 函数名` 这种前缀赋值（那个对函数有效）。
eq "(h) status 不传参时取板里声明的 90" "$(env -u WORK_LOG_STALE_AFTER "$PY" "$WL" --dir "$D30C" status --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["stale_after"])')" 90.0
eq "(h2) 显式传参仍然优先（7）" "$(env -u WORK_LOG_STALE_AFTER "$PY" "$WL" --dir "$D30C" status --json --stale-after 7 | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["stale_after"])')" 7.0
eq "(h3) env 优先于 state（33）" "$(WORK_LOG_STALE_AFTER=33 "$PY" "$WL" --dir "$D30C" status --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["stale_after"])')" 33.0
eq "(h4) 未声明的板退回内置默认 45" "$(env -u WORK_LOG_STALE_AFTER "$PY" "$WL" --dir "$D30B" status --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["stale_after"])')" 45.0
# 行为级验证：板里声明 1s，那么**不传 --stale-after** 的 check 也必须按 1s 判卡死。
# 若实现退回写死的 45s，这一条会红（agent 才静默 2s，根本够不到 45）。
D30D="$TMP/g30d"
C30D(){ env -u WORK_LOG_STALE_AFTER "$PY" "$WL" --dir "$D30D" "$@"; }
C30D init --task "阈值真被用上" --stale-after 1 >/dev/null
C30D post --agent d1 --text "开工" >/dev/null
sleep 2
C30D check --cooldown 0 >/dev/null 2>&1 || true
eq "(i) 声明的 1s 真的被 check 用上（不传参也判卡死）" "$(grep -c '疑似卡死' "$D30D/alerts.md")" 1
has "(j) 板头写的是本板实际阈值（90），不是写死的 45" "$(grep '看门狗' "$D30C/board.md")" "静默超过 90s"

# ---- 30d 同名多实例识别 -----------------------------------------------------
# 现实：一台机器上常开着**两个 opencode**，它们都会报 `opencode` ⇒ `_get_agent`
# 取到同一个 dict：last_seen / 用户喊话游标 / 心跳预算全被合并 ——
# 一个死了另一个替它刷新（看门狗测不到），用户喊话被先读到的那个取走（另一个永远看不到）。
# 它们的 cwd 往往完全相同，所以 `identity set` 的目录冲突**也**挡不住。
echo "[30d] 同名多实例识别"
D30E="$TMP/g30e"
I30(){ WORK_LOG_INSTANCE="$1" "$PY" "$WL" --dir "$D30E" "${@:2}"; }
I30 A init --task "多实例" >/dev/null
o30e=$(I30 A post --agent oc --text "实例 A 开工")
hasnt "(a) 第一个实例不啰嗦（首见不算多实例）" "$o30e" "同名多实例"
o30e=$(I30 A post --agent oc --text "实例 A 继续")
hasnt "(a2) 同一个实例再来也不啰嗦" "$o30e" "同名多实例"
o30e=$(I30 B post --agent oc --text "实例 B 也开工")
has "(b) 第二个实例出现时当场提醒" "$o30e" "同名多实例"
has "(b2) 提醒里点明会共用哪些东西" "$o30e" "用户喊话游标"
has "(b3) 提醒里给出改法（逐实例钉名）" "$o30e" "WORK_LOG_AGENT=oc-"
eq "(c) 板上留痕（[协作] 条目）" "$(grep -c '这个名字下现在有 2 个实例在写' "$D30E/board.md")" 1
eq "(c2) 留痕只写一次（不是每次 post 都刷）" "$(I30 B post --agent oc --text "再一条" >/dev/null; grep -c '这个名字下现在有' "$D30E/board.md")" 1
has "(d) status 标出同名多实例" "$("$PY" "$WL" --dir "$D30E" status)" "同名 2 实例"
has "(e) doctor ⑦ 报出来并列清是哪两个" "$("$PY" "$WL" --dir "$D30E" doctor 2>&1)" "下有 2 个实例在写"
eq "(f) 没有幽灵 agent（watchdog 不参与心跳判定）" "$("$PY" "$WL" --dir "$D30E" status --json | "$PY" -c 'import json,sys;print([a["name"] for a in json.load(sys.stdin)["agents"]])')" "['oc']"
# 反面：单实例的板绝不能被报成多实例（否则这个功能本身就是噪音源）
D30F="$TMP/g30f"
WORK_LOG_INSTANCE=only "$PY" "$WL" --dir "$D30F" init --task 单实例 >/dev/null
WORK_LOG_INSTANCE=only "$PY" "$WL" --dir "$D30F" post --agent solo --text 开工 >/dev/null
has "(g) 单实例不误报" "$("$PY" "$WL" --dir "$D30F" doctor 2>&1)" "没有同名多实例"
# 可关：不想被探测（或宿主分不出会话）时可以整体关掉
WORK_LOG_NO_INSTANCE=1 WORK_LOG_INSTANCE=A "$PY" "$WL" --dir "$D30E" post --agent oc2 --text "关掉探测" >/dev/null
eq "(h) WORK_LOG_NO_INSTANCE=1 关掉留痕" "$("$PY" -c "import json;print(json.load(open('$D30E/state.json'))['agents']['oc2'].get('instances'))")" "{}"
# 自动探测：不给 WORK_LOG_INSTANCE 也必须能跑（认不出就是空串，绝不用猜的当判据、绝不报错）
eq "(i) 不设实例变量时自动探测不报错（退 0）" "$(env -u WORK_LOG_INSTANCE "$PY" "$WL" --dir "$D30F" post --agent solo --text "自动探测" >/dev/null 2>&1; echo $?)" "0"
# state 不许无限长：实例多了只留最近 INSTANCE_KEEP 个
for i in 1 2 3 4 5 6 7 8 9 10 11; do WORK_LOG_INSTANCE="i$i" "$PY" "$WL" --dir "$D30F" post --agent solo --text "实例 $i" >/dev/null; done
eq "(j) 实例标识有上限（只留最近 8 个）" "$("$PY" -c "import json;print(len(json.load(open('$D30F/state.json'))['agents']['solo']['instances']))")" 8
eq "(j2) 留的是**最近**的（i11 在、i1 不在）" "$("$PY" -c "import json
d=json.load(open('$D30F/state.json'))['agents']['solo']['instances']
print(('i11' in d) and ('i1' not in d))")" "True"
# 幽灵实例会被清掉：换了锚点口径、或某个实例再也不出现时，标识不能永远挂在 state 里
# （否则 doctor ⑦ 会永远报一个早就不存在的实例 —— 刚踩过）
"$PY" - <<PY
import json, time
p = "$D30F/state.json"
d = json.load(open(p))
inst = d["agents"]["solo"]["instances"]
inst.clear()
inst["ancient"] = 0.0            # 1970 年，早过期
inst["recent"] = time.time()     # 刚刚还在
json.dump(d, open(p, "w"))
PY
WORK_LOG_INSTANCE=fresh "$PY" "$WL" --dir "$D30F" post --agent solo --text "触发清理" >/dev/null
eq "(k) 过期很久的幽灵实例被清掉" "$("$PY" -c "import json;print('ancient' in json.load(open('$D30F/state.json'))['agents']['solo']['instances'])")" "False"
eq "(k2) 没过期的实例不受影响" "$("$PY" -c "import json;print('recent' in json.load(open('$D30F/state.json'))['agents']['solo']['instances'])")" "True"


# ---- 30e 实例锚点必须稳（同一个会话不能被算成两个实例） ---------------------
# 2026-09-28 真实误报的两个根因，都要焊死：
#   ① 旧实现用「关键词 in 整条命令行」做子串匹配 —— WorkBuddy 自己的 Electron 辅助进程
#      把**整块环境变量 JSON** 当参数传（里面就有 CLAUDE_PLUGIN_ROOT）⇒ 宿主自己被认成 claude；
#   ② `pid = ppid` 之后才用**上一行**的「启动时刻」拼字符串 ⇒ 返回的是
#      「父进程的 pid + 子进程的启动时刻」这个根本不存在的组合，锚点挂在随时重启的
#      辅助进程上 ⇒ 同一个会话每探测一次就换一个标识，看板报出幽灵实例。
# 这两条都是**纯函数级**的判据，所以直接拿真样本喂 host_hint，比跑进程可靠。
echo "[30e] 实例锚点稳定性"
r30g=$("$PY" - "$HERE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import os
import work_log as W

# 真·误报样本（照抄自 ps 输出：Electron 辅助进程 + 整块环境 JSON）
FALSE_POS = ('/Applications/WorkBuddy.app/Contents/MacOS/Electron '
             '/Applications/WorkBuddy.app/Contents/Resources/app.asar/main/sidecar-entry.js '
             '--env {"CHATPAY_PAYSIGN_SERVICE_ID":"agentpay",'
             '"CLAUDE_PLUGIN_ROOT":"/Users/x/.workbuddy/plugins"}')
# 一次性 shell：argv 里就带着关键词，认它等于每次换锚点
FALSE_SHELL = "bash -c 'python3 work_log.py post --text \"opencode 卡住了\"'"

pos = [("opencode", "opencode"),
       ("/opt/homebrew/bin/opencode", "opencode"),
       ("node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js", "claude"),
       ("npx opencode", "opencode"),
       # ★ shebang 脚本当宿主：macOS 上 `ps` 显示的是 `/bin/sh /路径/opencode …`，
       #   真身在 argv[1]。只认 argv[0] 会**漏掉真实宿主**（实测栽在这：假 opencode
       #   宿主探测返回空串）。但 `-c` 开关必须跳过，见下面负例。
       ("/bin/sh /Users/x/.local/bin/opencode /tmp/w.sh A", "opencode"),
       ("env opencode --dir /x", "opencode"),
       # ★ 2026-09-28 本机实测的真形态：hermes 是 bash 脚本、kimi/claude 是 Mach-O 二进制
       ("/bin/bash /Users/x/.local/bin/hermes", "hermes"),
       ("/bin/bash /Users/x/.local/bin/hermes-agent --acp", "hermes"),
       ("/Users/x/.kimi-code/bin/kimi", "kimi"),
       ("/Users/x/.local/bin/claude", "claude")]
neg = [FALSE_POS, FALSE_SHELL,
       "/bin/zsh -c . '/Users/x/snap.sh' && eval 'echo opencode'",
       "/bin/sh -c 'grep -r opencode /src'",
       "grep -r opencode /src", "vim /tmp/opencode-notes.md", "-zsh", ""]

miss = sum(1 for c, want in pos if W.host_hint(c) != want)
bad = sum(1 for c in neg if W.host_hint(c))
print("POS_MISS=" + str(miss))
print("NEG_HIT=" + str(bad))
print("DIS=%s" % (W.detect_instance() or "EMPTY"))
# GUI App 包里的进程：**结构性排除**（一个进程承载多个会话，分不出会话）
print("APP=" + str(W._is_app_bundle(
    '/Applications/WorkBuddy.app/Contents/MacOS/Electron /x/y.js '
    '--env {"CLAUDE_PLUGIN_ROOT":"/z"}')))
print("NOT_APP=" + str(W._is_app_bundle("/opt/homebrew/bin/opencode")))
# 名单外的工具：默认认不出，但用 $WORK_LOG_HOST_HINTS 追加即可 —— 不用改代码
print("EXT_BEFORE=" + str(W.host_hint("/tmp/x/bin/myagent")))
os.environ[W.INSTANCE_HINTS_ENV] = "myagent,另一家"
print("EXT_AFTER=" + str(W.host_hint("/tmp/x/bin/myagent")))
print("EXT_AFTER2=" + str(W.host_hint("/tmp/x/bin/另一家-cli")))
del os.environ[W.INSTANCE_HINTS_ENV]
PY
)
eq "(a) 该认的宿主都认得出（opencode / claude-code / hermes / kimi / npx 启法）" "$(printf '%s\n' "$r30g" | sed -n 's/^POS_MISS=//p')" 0
eq "(b) 该不认的一个都不认（Electron 环境块 / 一次性 shell / 文件名带词）" "$(printf '%s\n' "$r30g" | sed -n 's/^NEG_HIT=//p')" 0
eq "(b2) GUI App 包里的进程一律不算宿主（一个进程承载多个会话）" "$(printf '%s\n' "$r30g" | sed -n 's/^APP=//p')" True
eq "(b3) 普通路径不被 App 规则误伤" "$(printf '%s\n' "$r30g" | sed -n 's/^NOT_APP=//p')" False
eq "(b4) 名单外的工具默认认不出（空）" "$(printf '%s\n' "$r30g" | sed -n 's/^EXT_BEFORE=//p')" None
eq "(b5) 用 WORK_LOG_HOST_HINTS 追加就能认出 —— 不用改代码" "$(printf '%s\n' "$r30g" | sed -n 's/^EXT_AFTER=//p')" myagent
eq "(b6) 追加的名字同样支持「关键词-」前缀形态" "$(printf '%s\n' "$r30g" | sed -n 's/^EXT_AFTER2=//p')" "另一家"
# 两个**独立进程**各探一次：结果必须一致。旧实现会在辅助进程重启后给出不同标识。
s30g1=$("$PY" -c "import sys;sys.path.insert(0,'$HERE');import work_log as W;print(W.detect_instance())")
sleep 1
s30g2=$("$PY" -c "import sys;sys.path.insert(0,'$HERE');import work_log as W;print(W.detect_instance())")
eq "(c) 两次独立探测结果一致（旧实现会漂）" "$s30g1" "$s30g2"


# ---- 30f 锚点死活 / 自动探测端到端 -----------------------------------------
# 为什么要有「死活」这条：锚点是 pid@启动时刻，而宿主会重启（关掉再开、
# 或用 `someagent run "…"` 这种"一次一个进程"的用法）。不看死活，旧锚点会一直留在
# state 里，下一次 post 就被读成"又来一个新实例" ⇒ **满屏假的多实例告警**。
echo "[30f] 锚点死活与自动探测端到端"
r30f=$("$PY" - "$HERE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import os, work_log as W
# 不存在的 pid ⇒ 死
print("DEAD=" + str(W.instance_alive("999999@Sun Sep  1 00:00:00 2026")))
# 显式钉的名字（没有 pid）⇒ 查不了死活 ⇒ 一律当活着（不许因为查不到就清掉）
print("EXPLICIT=" + str(W.instance_alive("oc-eval")))
# pid 还在、但启动时刻对不上 ⇒ 那个 pid 已被系统复用给别人了 ⇒ 也算死
print("REUSED=" + str(W.instance_alive(str(os.getpid()) + "@Sun Sep  1 00:00:00 2026")))
PY
)
eq "(a) 宿主进程已不在 ⇒ 判为死" "$(printf '%s\n' "$r30f" | sed -n 's/^DEAD=//p')" False
eq "(b) 显式钉的名字查不了死活 ⇒ 一律当活着（不许误清）" "$(printf '%s\n' "$r30f" | sed -n 's/^EXPLICIT=//p')" True
eq "(c) pid 被系统复用（启动时刻对不上）⇒ 也算死" "$(printf '%s\n' "$r30f" | sed -n 's/^REUSED=//p')" False
# 宿主重启 ⇒ 静默替换，**不许**假报「又多了一个实例」
D30G="$TMP/g30g"
"$PY" "$WL" --dir "$D30G" init --task 重启 >/dev/null
"$PY" "$WL" --dir "$D30G" post --agent oc --text 开工 >/dev/null
"$PY" - <<PY
import json, time
p = "$D30G/state.json"
d = json.load(open(p))
d["agents"]["oc"]["instances"] = {"999999@Sun Sep  1 00:00:00 2026": time.time()}
json.dump(d, open(p, "w"))
PY
o30g=$(WORK_LOG_INSTANCE=新宿主 "$PY" "$WL" --dir "$D30G" post --agent oc --text "重启后")
hasnt "(d) 宿主重启不假报多实例" "$o30g" "同名多实例"
eq "(d2) 死掉的旧锚点已被清掉" "$("$PY" -c "import json;print(list(json.load(open('$D30G/state.json'))['agents']['oc']['instances']))")" "['新宿主']"
eq "(e) 板上没有多实例留痕" "$(grep -c '这个名字下现在有' "$D30G/board.md")" 0
# doctor --explain：把「我是哪个实例」的父链逐层判定打出来（用户自助查"我的工具认不认得出"）
has "(f) doctor --explain 打出父链逐层判定" "$("$PY" "$WL" --dir "$D30G" doctor --agent oc --explain 2>&1)" "逐层判定"
# 自动探测端到端（在**真实父链**上，不传 WORK_LOG_INSTANCE）：
# 造一个名叫 hermes 的宿主脚本，形态照本机真实 hermes（bash 脚本 + 把活交给子进程）
B30H="$TMP/bin30h"; mkdir -p "$B30H"
printf '#!/bin/bash\n"$@"\n' > "$B30H/hermes"; chmod +x "$B30H/hermes"
PROBE30H="\"$PY\" -c \"import sys;sys.path.insert(0,'$HERE');import work_log as W;print(W.detect_instance())\""
P30H="$TMP/probe30h.sh"
cat > "$P30H" <<EOF
#!/bin/sh
$PROBE30H
EOF
# ⚠ 等宿主长大的 sleep 必须写在**宿主自己的进程里**，不能写在宿主外面 ——
# 写在外面的话第二次调用是**又新起一个宿主**，年龄还是 0，永远测不到"活够了的宿主"。
# （这个坑把自己绊了一次：断言红了才发现是测试写法错，不是实现错。）
S30H="$TMP/slow30h.sh"
cat > "$S30H" <<EOF
#!/bin/sh
sleep 21
$PROBE30H
sleep 1
$PROBE30H
EOF
chmod +x "$P30H" "$S30H"
eq "(g) 宿主刚起（< 20s）时拒认 —— 锚在短命进程上会乱跳" "$("$B30H/hermes" "$P30H")" ""
out30h=$("$B30H/hermes" "$S30H")
g30h1=$(printf '%s\n' "$out30h" | sed -n '1p')
g30h2=$(printf '%s\n' "$out30h" | sed -n '2p')
# ⚠ `$g30h1` 后面紧跟中文括号必须写成 `${g30h1}`
#   —— bash 3.2 会把中文标点的首字节吞进变量名，`set -u` 下当场 unbound variable。
#   （这个坑写进了项目记忆，我还是踩了一次；所以下面 [30g] 拿脚本自己做了结构性自查。）
[ -n "$g30h1" ] && ok "(h) 宿主活够 20s 后认得出（${g30h1}）" || ng "(h) 宿主活够 20s 后仍认不出"
eq "(i) 同一个宿主两次探测结果一致" "$g30h1" "$g30h2"


# ---- 30g 结构性自查：脚本里不许出现「$变量紧跟中文标点」 ---------------------
# bash 3.2 会把紧随变量的**非 ASCII 标点的首字节**吞进变量名 ⇒ `set -u` 下当场
# `unbound variable`，而且是**整个脚本以 rc=127 死掉**（不是「变量取空」）。
# rc=127 还伪装成「命令找不到」，最误导 —— 实测（2026-09-29）：
#   裸 $BOT 后接中文逗号   → /bin/bash: BOT?x: unbound variable，rc=127
#   加上花括号 ${BOT}       → 正常，rc=0
# `bash -n` **查不出来**。这个坑已经踩了四次
# （2026-09-23 演示脚本、2026-09-24 复现脚本、2026-09-28 本文件自己、
#  2026-09-29 ask_listener.sh 五处）。前三处靠人记得，第四处是**这组扩面之后
#  它自己抓出来的** —— 这就是为什么扫描面必须是通配而不是枚举。
echo "[30g] 结构性自查：\$变量 不许紧跟中文标点（扫 scripts/*.sh 与 examples/*.sh）"
# 扫描面**不再逐个写死**：原来只列 selftest.sh 与 demo.sh 两个文件，于是
# scripts/ 自己的驱动脚本全在护栏外面 —— 「护栏扫哪些文件」本身就是一条判据，
# 它一样可以「写在文档里但没长对」。改成通配：新增脚本自动进护栏。
r30i=$("$PY" - "$HERE/selftest.sh" "$HERE"/*.sh "$HERE"/../examples/*.sh <<'PY'
import os, re, sys
files = sorted({f for f in sys.argv[1:] if os.path.isfile(f)})
hits = []
for f in files:
    src = open(f, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r'\$[A-Za-z_][A-Za-z0-9_]*', src):
        nxt = src[m.end():m.end() + 1]
        if nxt and ord(nxt) > 127:               # 变量后面紧跟非 ASCII（中文标点等）
            hits.append(f"{f}:{src[:m.start()].count(chr(10)) + 1}  {m.group(0)}")
# 第二类「写法级」问题：**反引号会在被当命令替换的地方真去执行**。两个面各自扫：
#   ① 断言文案（一般写成双引号）：2026-09-30 自己踩到 —— 把 waker 用反引号包进 ok 的
#      文案里，真去跑了一次 waker，而那行文案**悄悄少一个词**（不报错，只是变了样）。
#   ② 引号 heredoc 的**正文**。这条是当天实测出来的解析陷阱：heredoc 写在 $( ) 里时，
#      **bash 3.2 并不把正文当纯字面量** —— 最小复现是「x=$(cat <<引号PY / if 双引号
#      反引号 双引号 in line: / PY / )」⇒ 直接语法错。正文里反引号**偶数个**时能侥幸跑通
#      （正好凑成一对命令替换），**奇数个就让整段错乱、报错飘到一千行之外**（本项目在
#      未闭合的美元花括号上吃过同形态的亏：报错位置 ≠ 出错位置）。
#      ⇒ 判据是「总数必须是偶数」：奇数 = 必然崩，偶数 = 侥幸但仍是雷。
#      本段自己**一个反引号都不写**（要判反引号就写 chr 96），否则这条判据自己就是雷。
msgre = re.compile(r'^\s*(ok|ng|has|hasnt|eq|usage)\s+"')
hdre = re.compile(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?\s*$")
bt, msg_lines, odd, bodies = [], 0, [], 0
BT = chr(96)
for f in files:
    if os.path.basename(f) == "selftest.sh":
        for i, line in enumerate(open(f, encoding="utf-8", errors="replace"), 1):
            if msgre.match(line):
                msg_lines += 1
                if BT in line:
                    bt.append(f"{f}:{i}")
    if not f.endswith(".sh"):
        continue
    lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
    k = 0
    while k < len(lines):
        m = hdre.search(lines[k])
        if m:
            term, j, buf = m.group(1), k + 1, []
            while j < len(lines) and lines[j].strip() != term:
                buf.append(lines[j]); j += 1
            if j < len(lines):
                bodies += 1
                n = sum(l.count(BT) for l in buf)
                if n % 2:
                    odd.append(f"{f}:{k + 1}  正文里反引号 {n} 个（奇数）")
                k = j
        k += 1
print("BACKTICK=" + str(len(bt)))
print("MSGLINES=" + str(msg_lines))
print("ODDBT=" + str(len(odd)))
print("BODIES=" + str(bodies))
for b in bt + odd:
    print("  " + b)
print("COUNT=" + str(len(hits)))
print("FILES=" + str(len(files)))
for h in hits:
    print("  " + h)
for f in files:
    print("SCANNED " + f)
PY
)
n30i=$(printf '%s\n' "$r30i" | sed -n 's/^COUNT=//p')
n30f=$(printf '%s\n' "$r30i" | sed -n 's/^FILES=//p')
[ "$n30i" = 0 ] || printf '%s\n' "$r30i"             # 有就把它打出来，否则只有条数没法改
eq "(a) 没有「\$变量紧跟非 ASCII」的写法（一律写 \${变量}）" "$n30i" 0
# 覆盖面也要断言：只断言「没命中」时，若扫描面被写空/写窄，空集合恒真照样绿。
# 所以既把实际扫到的文件打出来（肉眼可核），也点名要求驱动脚本必须在里面。
printf '    实际扫到 %s 个脚本：%s\n' "${n30f:-?}" \
       "$(printf '%s\n' "$r30i" | sed -n 's|^SCANNED .*/||p' | tr '\n' ' ')"
if [ "${n30f:-0}" -ge 3 ]; then
  ok "(b) 扫描面不是空集也不是单文件（${n30f} 个）"
else
  ng "(b) 扫描面只剩 ${n30f:-0} 个脚本 —— 护栏已退化成恒真"
fi
eq "(c) 驱动脚本 ask_listener.sh / waker.sh 都在扫描面内" \
   "$(printf '%s\n' "$r30i" | sed -n 's|^SCANNED .*/||p' | grep -cE '^(ask_listener|waker)\.sh$')" 2
# 第二类写法级自查（同一趟扫描里算出来的）：**断言文案里不许出现反引号**。
# 双引号里的反引号会被 shell 当命令替换**真去执行**（2026-09-30 自己踩到），
# 后果是文案悄悄少一个词 —— 不报错、只是变了样，正是最难发现的一类。
n30b=$(printf '%s\n' "$r30i" | sed -n 's/^BACKTICK=//p')
n30m=$(printf '%s\n' "$r30i" | sed -n 's/^MSGLINES=//p')
[ "${n30b:-0}" = 0 ] || printf '%s\n' "$r30i"
eq "(d) 断言文案里没有反引号（会被当命令执行，不是字面量）" "${n30b:-?}" 0
if [ "${n30m:-0}" -ge 100 ]; then
  ok "(e) 这条扫描的落点不是空集（扫了 ${n30m} 行断言文案）"
else
  ng "(e) 只扫到 ${n30m:-0} 行断言文案 —— 这条护栏已退化成恒真"
fi
# 第二面：引号 heredoc 的正文。奇数个别说必崩 —— 当天就是这么把整个套件搞崩的
# （报错还飘到一千行之外，见上面那段注释里的解析陷阱）。
n30o=$(printf '%s\n' "$r30i" | sed -n 's/^ODDBT=//p')
n30d=$(printf '%s\n' "$r30i" | sed -n 's/^BODIES=//p')
[ "${n30o:-0}" = 0 ] || printf '%s\n' "$r30i"
eq "(f) 引号 heredoc 正文里反引号是偶数个（奇数 ⇒ 整段解析错乱）" "${n30o:-?}" 0
if [ "${n30d:-0}" -ge 1 ]; then
  ok "(g) 真的扫到了 heredoc 正文（${n30d} 段）—— 判据不落在空集上"
else
  ng "(g) 一段 heredoc 正文都没扫到 —— 这条护栏已退化成恒真"
fi


# ---- 31 用户对话（人类是一等参与者） ---------------------------------------
echo "[31] 用户对话通道"
D31="$TMP/g31"
U31(){ "$PY" "$WL" --dir "$D31" "$@"; }
U31 init --task "用户对话" >/dev/null
U31 post --agent alice --text "开工" >/dev/null
U31 post --agent bob --text "开工" >/dev/null
usage31(){ if   [ "$2" = 2 ]; then ok "$1（→2）"; else ng "$1  期望[2] 实际[$2]"; fi; }
U31 post --agent 用户 --text "冒充" >/dev/null 2>&1
usage31 "(a) agent 不能冒充「用户」" "$?"
o31=$(U31 ask --agent alice --to 用户 --text "要加语音开关吗？")
eq "(b) 向用户提问返回 0" "$?" 0
has "(b) 提示用户回答方式" "$o31" "reply --agent 用户"
U31 ask --agent bob --to 用户 --text "第二个问题" >/dev/null
has "(c) status 显示问用户的待答提问" "$(U31 status)" "等你回答"
o31=$(U31 reply --agent 用户 --id 1 --text "要，默认开")
eq "(d) 用户回答返回 0" "$?" 0
o31=$(U31 await --agent alice --id 1 --timeout 3)
eq "(e) await 原生等到用户答复" "$?" 0
has "(e) 答复内容原样回到提问者" "$o31" "要，默认开"
has "(f) 答复者显示为用户" "$o31" "已由 <用户> 回应"
j31=$(U31 status --json)
eq "(f2) 用户不计入协作数（仍为 2）" "$(printf '%s' "$j31" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["count"])')" 2
eq "(f3) 用户不进 agent 状态表" "$(U31 status | grep -cE '^用户 +心跳')" 0
# 用户对话豁免乒乓熔断：人机来回不算「两个模型互相确认」
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  U31 ask --agent alice --to 用户 --text "第 $i 轮" >/dev/null
  U31 reply --agent 用户 --id $((i+2)) --text "答 $i" >/dev/null
done
U31 ask --agent alice --to 用户 --text "第 13 轮" >/dev/null 2>&1
eq "(g) 与用户来回 13 轮也不熔断" "$?" 0
hz31=$(U31 check --readonly --json | "$PY" -c 'import json,sys;h=json.load(sys.stdin)["hazards"];print(sum(1 for x in h if x["kind"]=="通信过热"))')
eq "(h) 通信过热险情不覆盖用户对" "$hz31" 0

# ---- 32 自动协作 UI + 页面写通道 -------------------------------------------
echo "[32] 自动协作 UI / UI 内交流"
D32="$TMP/g32"
UIPORT=$((20000 + $$ % 20000))          # 用测试进程 pid 选基口，并行实例互不撞口
C32(){ env -u WORK_LOG_NO_AUTO_UI WORK_LOG_NO_UI=1 WORK_LOG_UI_PORT=$UIPORT \
       "$PY" "$WL" --dir "$D32" "$@"; }
C32 init --task "自动UI" --stale-after 90 >/dev/null
C32 post --agent a1 --text 开工 >/dev/null
o32=$(C32 post --agent a2 --text 也开工)
has "(a) 第 2 个 agent 开工自动起协作界面" "$o32" "🖥 协作界面已自动启动"
has "(a) 提示未弹浏览器（WORK_LOG_NO_UI 生效）" "$o32" "未弹浏览器"
# 自动起的那只看门狗是**用户看不见的**，它原来写死用内置默认 45s ——
# 于是"我明明用 --stale-after 90 起的 serve"照样被它按 45s 判卡死。
# 两条一起钉：①提示行里把阈值打出来；②子进程命令行里真的收到了这个值。
has "(a2) 提示行打出这只隐藏看门狗的阈值" "$o32" "卡死阈值 90s"
SPID32=$("$PY" -c "import json;print(json.load(open('$D32/state.json')).get('ui_pid',''))")
eq "(a3) 子进程命令行真的带上了 --stale-after 90" \
   "$(ps -o command= -p "$SPID32" 2>/dev/null | grep -c -- '--stale-after 90')" 1
BG_PIDS="$BG_PIDS $SPID32"
eq "(b) serve 子进程已脱离父进程存活" "$([ -n "$SPID32" ] && kill -0 "$SPID32" 2>/dev/null && echo yes)" "yes"
eq "(c) 健康检查通过（自动选的口 ${UIPORT}）" \
  "$("$PY" -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:$UIPORT/api/health',timeout=2).status)")" 200
r32=$("$PY" - <<PYEOF
import json, urllib.request
def jpost(path, obj):
    req = urllib.request.Request("http://127.0.0.1:$UIPORT" + path,
        data=json.dumps(obj).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=2))
print(jpost("/api/say", {"text": "UI 里说的话"})["ok"])
PYEOF
)
eq "(d) UI 发言落盘成功" "$r32" "True"
has "(e) agent 能取走 UI 里说的话" "$(C32 read-user --agent a1)" "UI 里说的话"
C32 ask --agent a1 --to 用户 --text "页面上的问题" >/dev/null
Q32=$("$PY" -c "import json;s=json.load(open('$D32/state.json'));print([e['id'] for e in s['exchanges'] if e['status']=='open'][0])")
r32=$("$PY" - <<PYEOF
import json, urllib.request
req = urllib.request.Request("http://127.0.0.1:$UIPORT/api/reply",
    data=json.dumps({"id": $Q32, "text": "页面上点的回答"}).encode(), method="POST",
    headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=2))["ok"])
PYEOF
)
eq "(f) UI 回答提问成功" "$r32" "True"
o32=$(C32 await --agent a1 --id $Q32 --timeout 3)
eq "(g) await 原生拿到 UI 里的回答" "$?" 0
has "(g) 回答内容原样送达" "$o32" "页面上点的回答"
c32=$("$PY" - <<PYEOF
import json, urllib.request, urllib.error
try:
    req = urllib.request.Request("http://127.0.0.1:$UIPORT/api/say", data=b"notjson",
        method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2)
    print("200")
except urllib.error.HTTPError as e:
    print(e.code)
PYEOF
)
eq "(h) 坏请求体返回 400" "$c32" 400
"$PY" "$WL" --dir "$D32" ask --agent a1 --to a2 --text "agent 之间的问题" >/dev/null
QX32=$("$PY" -c "import json;s=json.load(open('$D32/state.json'));print([e['id'] for e in s['exchanges'] if e['status']=='open'][0])")
c32=$("$PY" - <<PYEOF
import json, urllib.request, urllib.error
try:
    req = urllib.request.Request("http://127.0.0.1:$UIPORT/api/reply",
        data=json.dumps({"id": $QX32, "text": "用户不能代答"}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2)
    print("200")
except urllib.error.HTTPError as e:
    print(e.code)
PYEOF
)
eq "(i) 用户不能替 agent 回答问向 agent 的问题（409）" "$c32" 409
o32=$(C32 post --agent a1 --text 继续)
eq "(j) 后续 post 不重复弹 UI" "$(printf '%s\n' "$o32" | grep -c '🖥' )" 0
big32=$("$PY" - <<PYEOF
import json, urllib.request, urllib.error
try:
    req = urllib.request.Request("http://127.0.0.1:$UIPORT/api/say",
        data=json.dumps({"text": "x" * 70000}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=3)
    print("200")
except urllib.error.HTTPError as e:
    print(e.code)
PYEOF
)
eq "(k) 超大请求体被拒（413）" "$big32" 413
# 浏览器把 text/plain 视为"简单请求"直接放行（无预检）——不校验 Content-Type，
# 你浏览的任何网页都能用 text/plain 夹带 JSON 冒充「用户」给 agent 下指令
cs32=$("$PY" - <<PYEOF
import json, urllib.request, urllib.error
try:
    req = urllib.request.Request("http://127.0.0.1:$UIPORT/api/say",
        data=json.dumps({"text": "跨站伪造"}).encode(), method="POST")   # urllib 默认 text/plain
    urllib.request.urlopen(req, timeout=3)
    print("200")
except urllib.error.HTTPError as e:
    print(e.code)
PYEOF
)
eq "(n) 非 JSON 的 Content-Type 被拒（415，防跨站写）" "$cs32" 415
# DNS rebinding：恶意域名把 A 记录指到 127.0.0.1，浏览器同源策略就被绕掉了；
# 只认 Host 是 127.0.0.1/localhost
h32=$("$PY" - <<PYEOF
import urllib.request, urllib.error
try:
    req = urllib.request.Request("http://127.0.0.1:$UIPORT/api/board",
        headers={"Host": "evil.example.com"})
    urllib.request.urlopen(req, timeout=3)
    print("200")
except urllib.error.HTTPError as e:
    print(e.code)
PYEOF
)
eq "(o) 伪造 Host 被拒（403，防 DNS rebinding）" "$h32" 403
# auto_ui 复用端口前必须核对"这是本项目的 serve"，否则多项目同时跑会弹别人的看板
hd32=$("$PY" - <<PYEOF
import json, urllib.request, sys
sys.path.insert(0, '$HERE')
from work_log import _ui_running
from pathlib import Path
ok_right = _ui_running($UIPORT, Path("$D32").resolve())
ok_wrong = _ui_running($UIPORT, Path("/tmp/另一个项目").resolve())
print(f"{ok_right}-{ok_wrong}")
PYEOF
)
eq "(p) 端口复用核对项目目录（对得上才复用）" "$hd32" "True-False"
u32_before=$(grep -c '' "$D32/user.md")
"$PY" - <<PYEOF
import json, urllib.request
req = urllib.request.Request("http://127.0.0.1:$UIPORT/api/say",
    data=json.dumps({"text": "一行", "who": "坏\n名字：冒充"}).encode(),
    method="POST", headers={"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=3)
PYEOF
u32_after=$(grep -c '' "$D32/user.md")
eq "(m) who 含换行仍只写一行（格式不被伪造）" "$((u32_after - u32_before))" 1
hasnt "(m) 清洗后不含冒号伪装" "$(tail -1 "$D32/user.md")" "名字：冒充"
D33="$TMP/g33"
env -u WORK_LOG_NO_AUTO_UI "$PY" "$WL" --dir "$D33" init --task "关掉自动UI" --no-auto-ui >/dev/null
env -u WORK_LOG_NO_AUTO_UI "$PY" "$WL" --dir "$D33" post --agent a1 --text 开工 >/dev/null
o33=$(env -u WORK_LOG_NO_AUTO_UI WORK_LOG_NO_UI=1 WORK_LOG_UI_PORT=$((UIPORT + 20)) \
      "$PY" "$WL" --dir "$D33" post --agent a2 --text 也开工)
eq "(k) init --no-auto-ui 后不再自动起界面" "$(printf '%s\n' "$o33" | grep -c '🖥')" 0

# ---- 33 告警退避：离线 agent 不该无限刷屏（噪音会把真信号埋掉） ---------------
# 实测事故：某板 42 小时攒出 994 行 watchdog vs 110 行实质发言，"另一个 agent
# 真的停了"这件事被淹没。所以重复到阈值后必须退避。（告警措辞的「离线」翻档
# 如今按**静默时长**走 agent_offline()，另由 [33b] 组专门锁 —— 两件事刻意解耦。）
echo "[33] 告警退避（离线判定）"
BD="$TMP/backoff"
Q33(){ "$PY" "$WL" --dir "$BD" "$@"; }
Q33 init --task 退避 >/dev/null
Q33 post --agent b1 --text "活着" >/dev/null
sleep 2
al33(){ grep -c '\[告警\]' "$BD/alerts.md"; }
for _ in 1 2 3; do Q33 check --stale-after 1 --cooldown 0 >/dev/null 2>&1; done
eq "前三次照常重复告警" "$(al33)" 3
has "第 2 次起用「重复告警 #」措辞" "$(cat "$BD/alerts.md")" "重复告警 #"
for _ in 4 5 6 7; do Q33 check --stale-after 1 --cooldown 0 >/dev/null 2>&1; done
eq "达阈值后进入退避（cooldown=0 也不再刷）" "$(al33)" 3
eq "退避计数记进 state" "$(python3 -c "
import json;print(json.load(open('$BD/state.json'))['agents']['b1'].get('alert_repeat'))")" 3
Q33 post --agent b1 --text "回来了" >/dev/null
eq "恢复心跳后退避计数归零" "$(python3 -c "
import json;print(json.load(open('$BD/state.json'))['agents']['b1'].get('alert_repeat'))")" 0
sleep 2
Q33 check --stale-after 1 --cooldown 0 >/dev/null 2>&1
case "$(tail -1 "$BD/alerts.md")" in
  *疑似卡死*) ok "恢复后措辞复位（短静默仍是疑似卡死，不因退避历史翻档）";;
  *) ng "恢复后措辞未复位";;
esac

# ---- 33b 「离线」档：判据唯一（agent_offline），三处（状态表/告警/徽章）一致 ----
# 规格说按**时长**分档：静默几分钟说「疑似卡死」（值得去救），静默到小时量级说
# 「离线」（人已经不在了）—— 两者的处置动作**相反**。旧实现按 alert_repeat>=3
# （报了几次）在告警正文里翻档：几分钟就升格，会把可能真卡死的 agent 过早说成
# "人不在了"；而状态表/网页徽章根本不知道这一档 ⇒ 同一个 agent 一屏两种说法。
# 这组锁三件事：①判据就是模块级 agent_offline()（下限 3600s 与 20×阈值取大）；
# ②状态表与告警用**同一个**判据；③翻到「离线」后不再出现「疑似卡死」措辞。
echo "[33b] 离线档（判据唯一 + 三处一致）"
OD="$TMP/offline"
QO(){ "$PY" "$WL" --dir "$OD" "$@"; }
QO init --task 离线 >/dev/null
QO post --agent o1 --text "开工" >/dev/null
# 伪造长静默：state 与看板**两处都要改老**（静默口径 = max(两者)），
# 只改 state 一处会被看板那行新鲜时间戳压回几秒，测出假的健康场景。
# 日期段也一并改到 2000-01-01：否则静默随"今天几点跑"漂移，断言会在午夜后翻车。
"$PY" -c "
import re, json, time
s = open('$OD/board.md', encoding='utf-8').read()
s = re.sub(r'(<o1>\s+)\d\d:\d\d:\d\d', r'\g<1>00:00:07', s)
s = re.sub(r'^## \d{4}-\d\d-\d\d\s*\$', '## 2000-01-01', s, flags=re.M)
open('$OD/board.md', 'w', encoding='utf-8').write(s)
d = json.load(open('$OD/state.json'))
d['agents']['o1']['last_seen'] = time.time() - 9999
json.dump(d, open('$OD/state.json', 'w'), ensure_ascii=False)"
# (a) 纯函数边界：下限 3600s 与 20×stale_after 谁大取谁，两侧各验
bo="$("$PY" - "$HERE" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("wl", sys.argv[1] + "/work_log.py")
wl = importlib.util.module_from_spec(spec); spec.loader.exec_module(wl)
f = wl.agent_offline
print(f(3599, 1), f(3600, 1), f(3601, 1),
      f(3599, 45), f(3600, 45),
      f(5999, 300), f(6000, 300), f(86400, 1))
PY
)"
eq "agent_offline 边界（1h 下限 / 20×阈值，两侧各验）" \
   "$bo" "False True True False True False True True"
eq "状态机与告警措辞两个调用点都走 agent_offline（结构性，防有人绕开判据）" \
   "$(grep -cE 'if agent_offline\(' "$HERE/work_log.py")" "2"
# (b) 行为级「三处一致」：状态表、check、告警都说离线，且不再说疑似卡死
oO="$(QO status --stale-after 1 2>/dev/null)"
has   "状态表判为离线（<= 已离线 旗标）"      "$oO" "已离线"
hasnt "同一份状态表不再同时说疑似卡死"        "$oO" "疑似卡死"
oOc="$(QO check --stale-after 1 --readonly 2>/dev/null)"; rcO=$?
eq   "check 退出码仍是 1（离线不豁免心跳轴 —— 工作可能真丢了）" "$rcO" 1
has  "check 输出也判离线（两处一致）"         "$oOc" "离线"
QO check --stale-after 1 --cooldown 0 >/dev/null 2>&1
has  "告警措辞翻成「判定为**离线**，不是卡死」" "$(cat "$OD/alerts.md")" "判定为**离线**"
hasnt "同一份告警不再说「疑似卡死」（一屏两种说法就是缺陷）" "$(cat "$OD/alerts.md")" "疑似卡死"

# ---- 33c 已离场（retire）：给「确定永远不会回来」的 agent 一个生命周期出口 ------
# 「离线」是引擎按时长**猜**的（工作可能真丢了，照样告警 —— 每只按 1h 退避上限
# 永远重复下去，板上永远挂着行）；「已离场」是人**拍板**的：不再判卡死、不再发告警、
# 名下未确认告警一并确认。复位必须便宜：它 post 一条即自动撤销，或 retire --undo。
echo "[33c] 已离场（retire：宣告出口 + post 自动复位）"
OG="$TMP/gone"
QG(){ "$PY" "$WL" --dir "$OG" "$@"; }
QG init --task 离场 --agents g1,g2 >/dev/null
QG post --agent g1 --text 开工 >/dev/null
sleep 2
has "(正向前置) retire 前它在被监督" "$(QG check --stale-after 1 --readonly 2>/dev/null)" "疑似卡死"
QG check --stale-after 1 --cooldown 0 >/dev/null 2>&1
eq "(正向前置) 它名下真的有过告警" "$(grep -c '\[告警\]' "$OG/alerts.md")" 1
oG="$(QG retire --agent g1 --note 进程已关，不会再回来)"; eq "retire 退 0" $? 0
has "输出说已宣告离场" "$oG" "已宣告离场"
has "顺手确认了它名下的未确认告警" "$oG" "已一并确认"
oGj="$(QG status --stale-after 1 --json 2>/dev/null)"
has  "status --json 仍含离场者（数据层保留：undo/驱动层要用）" "$oGj" '"已离场"'
hasnt "不再判疑似卡死"           "$oGj" '疑似卡死'
hasnt "不再判离线"               "$oGj" '"离线"'
eq "stale 名单不收离场者" \
   "$(printf '%s' "$oGj" | "$PY" -c "import json,sys; print(len(json.load(sys.stdin).get('stale') or []))")" 0
# 人看的输出里**直接不出现**（2026-09-30 用户拍板：都说了不会再启动，别再占板面）
hasnt "人看的状态表不再显示离场者" "$(QG status --stale-after 1 2>/dev/null)" '已离场'
eq   "状态表行整个消失（连名字都不在）" "$(QG status --stale-after 1 2>/dev/null | grep -cE '^g1 ')" 0
eq   "viewer 过滤离场行（结构性）" "$(grep -c "state !== '已离场'" "$HERE/../assets/viewer.html")" 1
QG check --stale-after 1 --readonly >/dev/null 2>&1; eq "check 退 0（不再因它退 1）" $? 0
aG1="$(grep -c '\[告警\]' "$OG/alerts.md")"
QG check --stale-after 1 --cooldown 0 >/dev/null 2>&1
eq "离场后不再产生新告警" "$(grep -c '\[告警\]' "$OG/alerts.md")" "$aG1"
oG="$(QG post --agent g1 --text 回来了 2>&1)"
has "post 当场提示离场标记已自动复位" "$oG" "离场标记已自动复位"
# ⚠ 这两条用 --stale-after 30 而不是 1：复位断言认的是「心跳中」，post 与 status 之间
# 只要滑过 1s（高负载下真实发生过，2026-09-30 运行副本那遍 9m29s），1s 窗口就会把它
# 判成疑似卡死 —— 断言红的是负载不是 bug。30s 窗口下 silence ~2s 稳在心跳中，
# 离线分界（max(3600, 600)）更是远够不着。
hasnt "复位后不再是已离场" "$(QG status --stale-after 30 --json 2>/dev/null)" '"已离场"'
has "复位后回到监督（心跳中）" "$(QG status --stale-after 30 --json 2>/dev/null)" '"心跳中"'
QG retire --agent ghost >/dev/null 2>&1; usage "retire 不存在的名字退 2（不静默创建）" $?
QG post --agent g2 --text 注册 >/dev/null
QG retire --agent g2 >/dev/null
has "(正向前置) g2 已在离场名单" "$(QG status --stale-after 1 --json 2>/dev/null)" '"已离场"'
QG retire --agent g2 --undo >/dev/null; eq "undo 退 0" $? 0
hasnt "undo 后离场标记撤销" "$(QG status --stale-after 1 --json 2>/dev/null)" '"已离场"'
eq "「已离场」全引擎只有一个推导点（结构性：状态机唯一出处）" \
   "$(grep -c 'state = "已离场"' "$HERE/work_log.py")" "1"

# ---- 33d claim 自己认领：多实例起名不靠人指派 --------------------------------
# 「同名多实例静默合并」的根治：两个会话共用一个名字时 last_seen/游标/预算全混在
# 一行，板上看不出来。claim 在锁内挑空号并当场占座 —— 并发启动也拿不到同一个号。
# 三条铁律：①同目录幂等（重跑启动脚本不冒新号）；②号码永不复用（回收 retired 号
# = 让带陈年 last_seen 的名字复活、当场被判离线）；③看板/identity 表出现过的名字都算占用。
echo "[33d] claim 自己认领（多实例起名，号码永不复用）"
KL="$TMP/claim"; mkdir -p "$KL/d1" "$KL/d2" "$KL/d3"
QK(){ "$PY" "$WL" --dir "$KL" "$@"; }
QK init --task 认领 >/dev/null
oK="$( (cd "$KL/d1" && "$PY" "$WL" --dir "$KL" claim --base kk) )"
has  "认领到 1 号" "$oK" "kk1"
oK="$( (cd "$KL/d1" && "$PY" "$WL" --dir "$KL" claim --base kk) )"
has  "同目录幂等（不重复占号）" "$oK" "不重复占号"
oK="$( (cd "$KL/d2" && "$PY" "$WL" --dir "$KL" claim --base kk) )"
has  "第二目录递增到 kk2（锁内挑空号，不撞名）" "$oK" "kk2"
has  "(正向前置) 认领的号都进了 state" "$(QK status --json 2>/dev/null)" '"待启动"'
eq   "待启动永不告警（占座不等于心跳，第一条 post 才点亮）" \
     "$(QK status --stale-after 1 2>/dev/null | grep -cE '^kk[12] +[^ ]*(疑似卡死|离线)')" 0
oK="$( (cd "$KL/d3" && "$PY" "$WL" --dir "$KL" claim --base kk --json) )"
eq   "--json 机器可读（驱动层取名字不用扒人话输出）" "$oK" '{"name": "kk3"}'
oK="$( (cd "$KL/d3" && env WORK_LOG_INSTANCE=claudecode "$PY" "$WL" --dir "$KL" claim) )"
has  "不写 --base 自己认宿主（框架名+序号：claudecode1，不写死 opencode）" "$oK" "claudecode1"
( cd "$KL/d3" && env -u WORK_LOG_INSTANCE WORK_LOG_NO_INSTANCE=1 "$PY" "$WL" --dir "$KL" claim >/dev/null 2>&1 )
usage "认不出宿主（探测已关）退 2 —— 名字宁可显式给，也不瞎猜" $?
printf '<kx1> 12:00:00 [执行] 手写占号\n' >> "$KL/board.md"
oK="$( (cd "$KL/d3" && "$PY" "$WL" --dir "$KL" claim --base kx) )"
has  "看板手写过的名字也算占用（跳到 kx2，不收编别人的历史）" "$oK" "kx2"
eq   "结构性：挑空号的循环在（防有人改成写死 1 号）" \
     "$(grep -c 'while f"{base}{n}" in taken:' "$HERE/work_log.py")" 1

# ---- 34 doctor：把「我这侧的拉取点」变成可执行检查 ---------------------------
echo "[34] doctor 接入自查"
DD="$TMP/doctor"; PJ="$TMP/proj"
mkdir -p "$DD" "$PJ/.workbuddy/memory"
# macOS 的 $TMPDIR 结尾带 "/"，于是 `$TMPDIR/x` 会变成 `…/T//x` —— 双斜杠。
# 而 doctor 打印的是 resolve() 之后的路径。两边必须统一成同一形态再比，
# 否则测试会因为一个斜杠而红，看起来像功能坏了（实测踩到）。
DDR="$(cd "$DD" && pwd -P)"
# ⚠️ 义务句必须和坐标**一起**写：doctor ③ 现在要求「坐标 + 报到义务」齐备。
#    只写坐标会被判成"我知道有这个工具、但没被告知必须去报到"——那正是本命令要抓的病之一。
printf '# 记忆\n看板：%s\n每个动作后 post；收工 --done\n' "$DDR" \
  > "$PJ/.workbuddy/memory/MEMORY.md"
: > "$PJ/AGENTS.md"
( cd "$PJ" && "$PY" "$WL" --dir "$DD" init --task 自查 ) >/dev/null
( cd "$PJ" && "$PY" "$WL" --dir "$DD" post --agent d1 --text "报到了" ) >/dev/null
o34=$( cd "$PJ" && "$PY" "$WL" --dir "$DD" doctor --agent d1 2>&1 ); rc34=$?
has "doctor 打印看板目录" "$o34" "$DDR"
has "doctor 列出板上的人" "$o34" "<d1>"
has "认出 cwd 里的 AGENTS.md" "$o34" "② 当前 cwd 的指令文件：✓ AGENTS.md"
has "认出记忆里的「坐标 + 义务」" "$o34" "③ 通道坐标 + 报到义务：✓"
# 测试环境里，用户级记忆不可能含这个临时板路径 ⇒ 必然走到"仅本工作区"，
# 那行警示必须出现（否则它就成了没人看的装饰）。
has "义务只落在项目级时，警示「换目录会断」" "$o34" "跨工作区"
eq "通道落好且我刚写过心跳 ⇒ doctor 退 0" "$rc34" 0

# 反向：**只写坐标、不写义务** ⇒ 必须被认出来。这正是 2026-09-24 实测到的
# 「半吊子接入」——旧判据只查坐标，会把它判成 ✓，完全看不见。
printf '# 记忆\n看板：%s\n' "$DDR" > "$PJ/.workbuddy/memory/MEMORY.md"
o34n=$( cd "$PJ" && "$PY" "$WL" --dir "$DD" doctor --agent d1 2>&1 ); rc34n=$?
has "只写坐标不写义务 ⇒ 被认出" "$o34n" "⚠ 有坐标、**没有报到义务**"
eq "并因此退 1（半吊子接入也算有病）" "$rc34n" 1
# 还原，后面还要用这个项目
printf '# 记忆\n看板：%s\n每个动作后 post；收工 --done\n' "$DDR" \
  > "$PJ/.workbuddy/memory/MEMORY.md"

eq "板不存在 ⇒ 退 2（用法错，不伪装成业务码 1）" \
   "$("$PY" "$WL" --dir "$TMP/no-such-board" doctor >/dev/null 2>&1; echo $?)" 2
sleep 2
o34b=$( cd "$PJ" && "$PY" "$WL" --dir "$DD" doctor --agent d1 --stale-after 1 2>&1 ); rc34b=$?
has "自己静默过阈值时点名「我」" "$o34b" "← 我"
eq "并因此退 1（有病是业务结论）" "$rc34b" 1

# ---- 35 onboard：把「接入片段」交到第二个 agent 手里 ------------------------
# 「第二个 agent 自动接入」的另一半：工具侧本来就自动（post 对未知名字当场注册），
# 真正会失效的是「它根本不知道有这块板」。本组要证明的就一句话：
# **onboard 吐出来的东西，必须能让 doctor ③ 判绿** —— 否则它只是又一段没人读的文档。
echo "[35] onboard 接入片段"
OB="$TMP/onboard"; OJ="$TMP/onboard-proj"; OH="$TMP/onboard-home"
mkdir -p "$OB" "$OJ" "$OH/.workbuddy" "$OH/Desktop"
# ⚠️ 必须把 HOME 换到沙箱：`onboard --to user` 写的是 **用户级记忆**，
#    不隔离就等于让自测去改本机 ~/.workbuddy/MEMORY.md。
OBR="$(cd "$OB" && pwd -P)"
"$PY" "$WL" --dir "$OB" init --no-auto-ui --agents a1,b2 >/dev/null

# 默认必须**只打印**：既不落盘，也不能顺手 mkdir（否则"dry-run"是假的）。
o35p=$( "$PY" "$WL" --dir "$OB" onboard --agent b2 2>&1 ); rc35p=$?
eq "默认只打印 ⇒ 退 0" "$rc35p" 0
has "打印里有标记块起点" "$o35p" "work-log:begin"
has "打印里有看板绝对路径" "$o35p" "$OBR"
eq "默认不落盘：cwd 里没多出文件" "$(ls -A "$OJ" | wc -l | tr -d ' ')" 0
"$PY" "$WL" --dir "$TMP/onboard-nodir" onboard >/dev/null 2>&1
if [ -d "$TMP/onboard-nodir" ]; then ng "默认打印却把看板目录建了出来（dry-run 是假的）"
else ok "默认打印不建目录（dry-run 是真的）"; fi

# 落盘 + 幂等：跑两次，字节必须完全一致（标记块的意义就在这里）。
"$PY" "$WL" --dir "$OB" onboard --agent b2 --to "$OJ/AGENTS.md" >/dev/null 2>&1
eq "落盘到 AGENTS.md ⇒ 退 0" $? 0
has "文件里有标记块终点" "$(cat "$OJ/AGENTS.md")" "work-log:end"
cp "$OJ/AGENTS.md" "$TMP/onboard-before"
"$PY" "$WL" --dir "$OB" onboard --agent b2 --to "$OJ/AGENTS.md" >/dev/null 2>&1
if cmp -s "$TMP/onboard-before" "$OJ/AGENTS.md"; then ok "重复 onboard 幂等（字节不变）"
else ng "重复 onboard 改动了文件 —— 标记块失效"; fi

# 已有**手写**协议、又没有标记块 ⇒ 必须拒绝，且不许动原文件。
printf '# 手写协议\n看板：%s\n' "$OBR" > "$TMP/onboard-hand.md"
cp "$TMP/onboard-hand.md" "$TMP/onboard-hand.before"
o35d=$( "$PY" "$WL" --dir "$OB" onboard --to "$TMP/onboard-hand.md" 2>&1 ); rc35d=$?
usage "已有手写 work-log 内容但无标记块 ⇒ 拒绝" "$rc35d"
has "并说明为什么（两份协议会打架）" "$o35d" "两份互相打架"
if cmp -s "$TMP/onboard-hand.before" "$TMP/onboard-hand.md"; then ok "拒绝时原文件一字未动"
else ng "拒绝时却改了文件"; fi
"$PY" "$WL" --dir "$OB" onboard --to "$TMP/onboard-hand.md" --force >/dev/null 2>&1
eq "--force 是明确的逃生口 ⇒ 退 0" $? 0

# 用法错误一律 2：不许借用业务码 1。
usage "--to 指到目录 ⇒ 用法错" \
  "$("$PY" "$WL" --dir "$OB" onboard --to "$TMP" >/dev/null 2>&1; echo $?)"
usage "--to 指到工具自己 ⇒ 用法错" \
  "$("$PY" "$WL" --dir "$OB" onboard --to "$WL" >/dev/null 2>&1; echo $?)"

# ★ 本组的存在理由：onboard 与 doctor 必须能**对上**。
#   onboard 是"开药"、doctor 是"验药"，两头用的是同一套判据。
#   写进用户级记忆（跨工作区那份），换个目录也必须认。
( cd "$OJ" && HOME="$OH" "$PY" "$WL" --dir "$OB" onboard --agent b2 --to user ) >/dev/null 2>&1
o35k=$( cd "$OJ" && HOME="$OH" "$PY" "$WL" --dir "$OB" doctor --agent b2 2>&1 ); rc35k=$?
has "★ onboard 写的片段让 doctor ③ 判绿" "$o35k" "③ 通道坐标 + 报到义务：✓"
has "★ 并且认出它的落点是跨工作区那份" "$o35k" "跨工作区"
eq "★ 于是 doctor 整体退 0（新开会话也能自己找到）" "$rc35k" 0
# 反向对照：把那份记忆**换成只剩坐标、没有任何义务句**，同一个 ✓ 必须立刻掉下来。
# 只改一句话是不够的 —— 片段里「收工」「--done」也都是 DUTY_MARKERS 的成员，
# 改一处仍然为 ✓，那样的反向测试是假绿的。
printf '# 记忆\n看板：%s\n' "$OBR" > "$OH/.workbuddy/MEMORY.md"
o35n=$( cd "$OJ" && HOME="$OH" "$PY" "$WL" --dir "$OB" doctor --agent b2 2>&1 )
has "★ 反向对照：只剩坐标时不再判绿" "$o35n" "⚠ 有坐标、**没有报到义务**"

# ---- 36 固定名字 + 可移植（身份 / 任何 agent / 换台电脑） ----------------------
# 这一组盯的是三件**互相独立**的失效，它们都能让"接入"在看起来正常的情况下断掉：
#   ① 名字：两个会话叫同一个名字 → _get_agent 取到**同一个 dict**，心跳/游标混在一起，
#      而板上完全看不出来（这是"看起来在协作、其实是一个人"）
#   ② 中立：接入片段若假定对方是某个特定工具，换一个 agent 就用不上
#   ③ 可移植：片段里写死引擎的**本机绝对路径**，换台电脑就是一条死链，
#      而且失效是静默的（对方照抄、报文件不存在、然后不再报到）
echo "[36] 固定名字 + 可移植"
SB="$TMP/ident"; S1="$TMP/ident-a"; S2="$TMP/ident-b"
mkdir -p "$SB" "$S1" "$S2"
SBW="$(cd "$SB" && pwd -P)"

# ① init 之后就自带引导脚本，**可执行**、且真能跑（不是个摆设文件）。
( cd "$S1" && "$PY" "$WL" --dir "$SB" init --task 身份 --agents a1 --no-auto-ui ) >/dev/null
if [ -x "$SB/worklog" ]; then ok "init 在看板目录里落了可执行的引导脚本"
else ng "init 没落引导脚本（或没有可执行位）"; fi
has "引导脚本自己找引擎（写出查找顺序）" "$(cat "$SB/worklog")" "WORK_LOG_ENGINE"

# ② 固定名字：登记「名字 ← 当前目录」之后，**不传 --agent** 也能认出自己。
( cd "$S1" && "$SB/worklog" whoami ) > "$TMP/w36-whoami" 2>&1
has "whoami 按目录自动认出名字" "$(cat "$TMP/w36-whoami")" "我在这块板上叫：a1"
has "并说明**凭什么**（依据可查验，不是口头约定）" "$(cat "$TMP/w36-whoami")" "依据："
o36p=$( cd "$S1" && "$SB/worklog" post --text "不传 --agent 也要能报到" 2>&1 )
case "$o36p" in
  '<a1>'*) ok "不传 --agent 也能 post（名字来自板上登记）";;
  *) ng "不传 --agent 的 post 没有落到 a1：$o36p";;
esac

# ③ 环境变量给**会话**钉死名字：换到没登记过的目录，照样能报到。
o36e=$( cd "$S2" && WORK_LOG_AGENT=a2 "$SB/worklog" post --text "靠环境变量钉死" 2>&1 )
case "$o36e" in
  '<a2>'*) ok "\$WORK_LOG_AGENT 能给会话钉死名字（换目录也认）";;
  *) ng "WORK_LOG_AGENT 没生效：$o36e";;
esac

# ④ 抢名字必须被**拒绝**：这正是"两个会话共用一个身份"的入口。
usage "别的目录抢已登记的名字 ⇒ 拒绝" \
  "$( cd "$S2" && "$SB/worklog" identity set --agent a1 >/dev/null 2>&1; echo $?)"
has "并告诉它被谁占着" \
  "$( cd "$S2" && "$SB/worklog" identity set --agent a1 2>&1 )" "已经被"
eq "确实是你自己换了目录时，--force 是逃生口" \
  "$( cd "$S2" && "$SB/worklog" identity set --agent a1 --force >/dev/null 2>&1; echo $?)" 0
# --force 的语义是「把这条登记改到我现在这个目录」，所以它**确实**会把 a1 挪走 ——
# 测试里必须还回去，否则后面的用例会因为 fixture 被自己改掉而红（这一版就踩到了）。
( cd "$S1" && "$SB/worklog" identity set --agent a1 --force ) >/dev/null 2>&1
has "identity list 能看见登记" "$( "$SB/worklog" identity list )" "a1"
( cd "$S2" && "$SB/worklog" identity set --agent tmp9 ) >/dev/null 2>&1
( cd "$S2" && "$SB/worklog" identity rm --agent tmp9 ) >/dev/null 2>&1
hasnt "identity rm 之后名单里没有它" "$( "$SB/worklog" identity list )" "tmp9"

# ⑤ doctor ⑥：定不下名字 = 病（退 1）；定下来了 = 绿。
o36d=$( cd "$S2" && "$PY" "$WL" --dir "$SB" doctor --stale-after 9999 2>&1 ); rc36d=$?
has "医生能说出「我定不下名字」" "$o36d" "✗ 定不下我在板上叫什么"
eq "并因此退 1（连自己叫什么都定不下来，是真病）" "$rc36d" 1
o36d2=$( cd "$S1" && "$PY" "$WL" --dir "$SB" doctor --stale-after 9999 2>&1 )
has "登记过的目录里，⑥ 判绿" "$o36d2" "✓ a1 —— 板上登记的就是这个目录"

# ⑥ ★ 本组的回归杀手锏：**接入片段里不许出现引擎的本机路径**。
#   片段是要被抄进对方的记忆、被拷到别的电脑的 —— 写死绝对路径的那一刻，
#   这段协议在第二台机器上就是死链。判据必须是"整段文本里没有它"，不是"我记得没写"。
"$PY" "$WL" --dir "$SB" onboard --agent a1 --to "$TMP/ident-proto.md" >/dev/null 2>&1
proto="$(cat "$TMP/ident-proto.md")"
hasnt "★ 片段里没有引擎的本机绝对路径" "$proto" "$WL"
hasnt "★ 片段里没有 skills 安装位" "$proto" ".workbuddy/skills/work-log"
hasnt "★ 片段里没有仓库目录名" "$proto" "Desktop/work-log-"
has "★ 它改为调「板自带的引导脚本」" "$proto" "/worklog"
has "★ 看板坐标仍然在（doctor ③ 靠它）" "$proto" "$SBW"
# ★ 2026-09-30：片段必须教「怎么被别人问到」。只教 `ask` 是**单向**的 ——
#   用户实测的「两个 agent 都装上了却交流不通」就是缺了这一层：
#   照旧片段装上的响应式会话永远不会被叫醒，而这件事**不报错**。
has "★ 片段给了唤醒器入口（会话能被叫醒）" "$proto" "waker --dir"
has "★ 片段要求「醒来先 brief --peek」" "$proto" "--peek"
has "★ 片段写明那条教训：只刷心跳不算在线" "$proto" "只刷心跳不算在线"
# ★ 2026-09-30 重审：片段还必须教「离线 / 离场」处置 —— 告警措辞翻成「离线」后
#   处置与卡死**相反**（去救没意义，进程多半早没了）；这一层片段不教，
#   照片段接管的人就会去救一个不存在的进程。与 waker 那三条同一性质：漏讲不报错。
has "★ 片段教了「离线不是卡死、别去救」" "$proto" "判定为**离线**，不是卡死"
has "★ 片段教了确定不回来就 retire 宣告离场" "$proto" "retire --agent"
# 前置：片段让人跑 `waker`，那这个子命令就必须真的存在 ——
# 否则片段在教一条不存在的命令（"文档里写了" ≠ "工具里长出来了"）。
"$PY" "$WL" --dir "$SB" waker --help >/dev/null 2>&1; rc36w=$?
eq "★ 片段里那个 waker 子命令真的存在（--help 退 0）" "$rc36w" 0

# ★ 但 `--help` 退 0 **只证明 argparse 注册了它**，不证明「exec 进 waker.sh」那层是通的 ——
#   而那层正是修「两个 agent 都装上了却交流不通」的那层：它要是断了，片段照样印得出来、
#   人照样装得上，**就是叫不醒**，且不报错。（"注册了"和"跑起来"是两件事，本项目
#   在 `onboard` 片段上刚吃过一次同形态的亏。）所以按片段自己的变量约定（D / WL）
#   真起一次唤醒器，再核验它**真的落到了 waker.sh**。
rm -f "$SB/waker-stop"
w36log="$TMP/w36-waker.log"
( "$SB/worklog" waker --dir "$SB" --agent a1 --window-min 45 --interval 2 \
    >"$w36log" 2>&1 & echo $! >"$TMP/w36-waker.pid" )
p36="$(cat "$TMP/w36-waker.pid")"
BG_PIDS="$BG_PIDS $p36"          # 登记进 trap：命令替换/子 shell 里的后台进程是孤儿
sleep 2
if kill -0 "$p36" 2>/dev/null; then
  ok "(f) 片段里那条 waker 命令**真能起来**（2s 后进程仍在）"
else
  ng "(f) 片段里那条 waker 命令没起来或当场退了：$(cat "$w36log")"
fi
has "(g) 而且真的 exec 进了 waker.sh（中间那层包装没断）" \
  "$(ps -o command= -p "$p36" 2>/dev/null)" "waker.sh --dir"
has "(h) 上线横幅打出来了（不是静默空跑）" "$(cat "$w36log")" "waker 上线"
# 收尾要**确定性**：唤醒器是每 --interval 秒看一眼，`touch` 完立刻 `rm` 会正好错开它的
# 轮询窗口（那样它会一直跑到 45 分钟窗口结束）。所以等它真的退出再删标记。
touch "$SB/waker-stop"
for _ in 1 2 3 4 5 6; do kill -0 "$p36" 2>/dev/null || break; sleep 1; done
rm -f "$SB/waker-stop"

# ⑦ 换台电脑：引擎路径全失效时，引导脚本必须**当场说清楚怎么修**，而不是抛个报错。
#    ⚠ PATH 收窄到 /usr/bin:/bin 是必须的：脚本最后还有一档 `command -v work_log.py`，
#      如果测试机 PATH 上恰好有一个，这一组就会变成假绿 —— 而假绿比红更贵。
#      （/usr/bin/python3 是 macOS 自带的，收窄后 python3 依然可用。）
cp "$SB/.worklogrc" "$TMP/ident-rc.bak"
sed -i.bak 's|^export WORK_LOG_ENGINE=.*|export WORK_LOG_ENGINE="${WORK_LOG_ENGINE:-/nonexistent/work_log.py}"|' "$SB/.worklogrc"
o36n=$( cd "$S1" && env -u WORK_LOG_BIN -u WORK_LOG_ENGINE HOME="$TMP/ident-nohome" \
        PATH=/usr/bin:/bin "$SB/worklog" post --text x 2>&1 ); rc36n=$?
eq "引擎找不到 ⇒ 退 70（工具坏了，不是业务码）" "$rc36n" 70
has "并给出三条可操作的修法" "$o36n" "WORK_LOG_BIN=/path/to/work_log.py"
o36f=$( cd "$S1" && env -u WORK_LOG_BIN HOME="$TMP/ident-nohome" PATH=/usr/bin:/bin \
        WORK_LOG_BIN="$WL" "$SB/worklog" post --text "指回来就能跑" 2>&1 )
case "$o36f" in
  '<a1>'*) ok "按提示设 WORK_LOG_BIN ⇒ 立刻恢复";;
  *) ng "WORK_LOG_BIN 没救回来：$o36f";;
esac
cp "$TMP/ident-rc.bak" "$SB/.worklogrc"

# ---- 37 落点自动探测 ------------------------------------------------------
echo "[37] 落点自动探测（--to auto / --detect）"
# 这一组钉的是「换台电脑也能用」里最后一处人工步骤：到底该落哪个文件。
# 判据是**观测**（配置目录 / PATH 上的可执行文件），所以测试必须能造出
# 「装了」和「没装」两种机器 —— 靠 HOME 沙箱 **加** 收窄 PATH 两件事一起做。
# ⚠ 只改 HOME 不够：PATH 上真实存在的 opencode / claude 会被照常命中，
#   于是"空机器"根本模拟不出来（第一次写这组就栽在这儿，测出来的空机器其实是本机）。
DB="$TMP/det-b"; DH="$TMP/det-home"; mkdir -p "$DB" "$DH"
DBW="$(cd "$DB" && pwd -P)"
# ⚠ macOS 的 $TMPDIR 以 `/` 结尾，mktemp 模板会拼出 `.../T//work-selftest.X`。
#   引擎打印的是 Python `Path` 的字符串形式，而它会把重复斜杠归一成单个 ——
#   拿带 `//` 的 $DH 去比对必然对不上。这条断言要的是"证据行里打出了真实路径"，
#   所以先把期望串按同样的方式归一，否则测的就不是引擎而是斜杠个数。
DHN="$(printf '%s' "$DH" | tr -s /)"
# 收窄 PATH 后连 python3 都找不到了 —— 先把解释器解析成绝对路径再用
PYABS="$(command -v "$PY" 2>/dev/null || echo "$PY")"
D37() { env HOME="$DH" PATH=/usr/bin:/bin "$PYABS" "$WL" --dir "$DBW" "$@"; }

o37=$(D37 onboard --to auto 2>&1); rc37=$?
eq "空机器上 --to auto 退 1（探测不到是业务结果，不是用法错误）" "${rc37}" 1
has "并指出先去看探测结果这一步" "$o37" "onboard --detect"
if [ -e "$DH/.config/opencode/AGENTS.md" ] || [ -e "$DH/.claude/CLAUDE.md" ]; then
  ng "空机器时不该写任何文件"
else ok "空机器时一个文件都没写"; fi

# 造出两台"装了工具链"的机器（只造配置目录，不碰真机）
mkdir -p "$DH/.config/opencode" "$DH/.claude"
o37d=$(D37 onboard --detect 2>&1)
has "探测到 opencode（正面命中工具名，而不是「没报错」）" "$o37d" "✓ opencode"
has "探测到 Claude Code" "$o37d" "✓ Claude Code"
has "证据可查验（把它命中的目录打出来）" "$o37d" "$DHN/.config/opencode"
has "并算出可自动落的处数" "$o37d" "可自动落 2 处"
hasnt "没装的工具链不硬报成已装" "$o37d" "✓ Gemini"
if [ -e "$DH/.config/opencode/AGENTS.md" ] || [ -e "$DH/.claude/CLAUDE.md" ]; then
  ng "--detect 必须只读（它却写了盘）"
else ok "--detect 只读：一个文件都没写"; fi

o37a=$(D37 onboard --to auto 2>&1)
has "auto 落了 opencode 那一处" "$o37a" "✓ opencode ⇒"
has "auto 落了 Claude Code 那一处" "$o37a" "✓ Claude Code ⇒"
eq "落进去的片段带标记块（AGENTS.md）" \
  "$(grep -c 'work-log:begin' "$DH/.config/opencode/AGENTS.md")" 1
eq "落进去的片段带标记块（CLAUDE.md）" \
  "$(grep -c 'work-log:begin' "$DH/.claude/CLAUDE.md")" 1
has "片段里带的是看板坐标（doctor ③ 靠它）" "$(cat "$DH/.claude/CLAUDE.md")" "$DBW"

o37i=$(D37 onboard --to auto 2>&1)
has "重复落盘是「替换原标记块」而不是再追加一份" "$o37i" "替换原标记块"
eq "重复落盘后标记块仍只有一对" "$(grep -c 'work-log:begin' "$DH/.claude/CLAUDE.md")" 1

if [ -e "$DH/.workbuddy/MEMORY.md" ]; then
  ng "user 档默认不该被落 —— 它会盖到**所有**工作区"
else ok "user 档默认被跳过"; fi
mkdir -p "$DH/.workbuddy"
o37u=$(D37 onboard --to auto --include-user 2>&1)
has "--include-user 才把 user 档算进去" "$o37u" "✓ WorkBuddy ⇒"
if [ -e "$DH/.workbuddy/MEMORY.md" ]; then ok "--include-user 真的落了 user 档"
else ng "--include-user 没生效"; fi

# ★ 本组的杀手锏：批量落盘绝不许留「落了一半」。
#   半落状态比全不落难收拾得多 —— 没人知道哪几处生效了。
PB="$TMP/det-b2"; PH="$TMP/det-home2"; mkdir -p "$PB" "$PH/.config/opencode" "$PH/.claude"
PBW="$(cd "$PB" && pwd -P)"
printf '# 手写的协作说明\n\n看板：%s\n' "$PBW" > "$PH/.claude/CLAUDE.md"
D37b() { env HOME="$PH" PATH=/usr/bin:/bin "$PYABS" "$WL" --dir "$PBW" "$@"; }
o37p=$(D37b onboard --to auto 2>&1); rc37p=$?
usage "某个落点里已有手写协议 ⇒ 拒绝（不硬追加出两份互相打架的协议）" "${rc37p}"
if [ -e "$PH/.config/opencode/AGENTS.md" ]; then
  ng "★ 预检失效：一处被拒却把另一处落了（留下半落状态）"
else ok "★ 预检生效：有一处会被拒 ⇒ 另一处也一个字节都没写"; fi
o37f=$(D37b onboard --to claude-global --force 2>&1)
has "--force 是逃生口：确认要追加就放行" "$o37f" "新增"
has "且手写原文一字未动" "$(cat "$PH/.claude/CLAUDE.md")" "手写的协作说明"

# 新选项必须登记进 KNOWN_OPTS，否则 `--text --detect` 这种"漏传值"会被
# 当成正文悄悄吞掉 —— 那等于给 --text 开了一个后门（本仓老坑）。
o37k=$(D37 post --agent a1 --text --detect 2>&1); rc37k=$?
usage "新选项已登记：--text --detect 按「漏传值」报错而不是被吞成正文" "${rc37k}"

# ---- 38 同义路径判据 ------------------------------------------------------
echo "[38] 同义路径判据（同一个目录的多种写法）"
# 这一组钉的是 `onboard` 拒绝判据与 `doctor ③` 认坐标判据**共用**的一个洞：
# 两者过去都拿「引擎那份绝对路径」去文本里做**字面子串**匹配。而 `resolve_dir()`
# 总是先 `resolve()`，手写协议里却常是未解析拼法（macOS 上 `/tmp` → `/private/tmp`、
# `$TMPDIR` 的 `/var` → `/private/var`）⇒ **同一个目录、两种写法，永远匹配不上**。
# 代价不对称而且**两边都不报错**：`onboard` 静默追加出第二份互相打架的协议，
# `doctor ③` 看不见已经写好的坐标。（2026-09-25 实测复现，A/B 只改拼法即两种结论。）
PYABS="$(command -v "$PY" 2>/dev/null || echo "$PY")"
HB="$TMP/syn-b"; HH="$TMP/syn-home"; mkdir -p "$HB" "$HH/.claude" "$HH/.workbuddy"
HBW="$(cd "$HB" && pwd -P)"
HBP="$(dirname "$HBW")"
D38() { env HOME="$HH" "$PYABS" "$WL" --dir "$HBW" "$@"; }
# 前置条件必须先断言：若这台机器上两种写法本来就相同，整组什么都测不到 ——
# 而"什么都没测到"会以全绿的样子出现。假绿比红更贵。
if [ "$HB" != "$HBW" ]; then
  ok "前置条件成立：同一目录确有两种写法（${HB} ≠ ${HBW}）"
else
  ng "前置条件不成立：$HB 与解析后相同 —— 这一组测不出任何东西（假绿）"
fi

D38 init --task 同义路径 >/dev/null 2>&1
printf '# 手写的协作说明\n\n看板：%s\n自己看着办。\n' "$HB" > "$HH/.claude/CLAUDE.md"
o38=$(D38 onboard --to claude-global 2>&1); rc38=$?
usage "手写协议用了未解析拼法时，onboard 也必须拒绝（不追加出第二份）" "${rc38}"
eq "且原文件一个字没动（标记块数仍为 0）" \
  "$(grep -c 'work-log:begin' "$HH/.claude/CLAUDE.md")" 0

printf '看板：%s\n义务：每个动作后都写一条心跳；收工必须写 --done。\n' "$HB" \
  > "$HH/.workbuddy/MEMORY.md"
has "doctor ③ 在未解析拼法下也认出坐标（正面命中，不是「没报错」）" \
  "$(D38 doctor 2>&1)" "✓ 1 处齐备"

# 反面对照：只提**父目录**不算提到这块板。否则判据宽到会把无关文本当成"已有协议"，
# 于是正常的 onboard 会被自己的护栏挡住 —— 那是另一种失败。
printf '# 手写的协作说明\n\n看板目录的上一级：%s\n自己看着办。\n' "$HBP" \
  > "$HH/.claude/CLAUDE2.md"
o38p=$(D38 onboard --to "$HH/.claude/CLAUDE2.md" 2>&1); rc38p=$?
eq "只提父目录 ⇒ 不误拒（退 0）" "${rc38p}" 0
eq "并且确实写进去了（证明上一条不是因为别的原因退 0）" \
  "$(grep -c 'work-log:begin' "$HH/.claude/CLAUDE2.md")" 1

# ---- 39 判据不许猜：展不开的 `~` 不是路径 ----------------------------------
echo "[39] 判据不许猜（约数波浪号 / 展不开的波浪号写法）"
# 这一组钉的是一个**真机事故**：真机 `~/.workbuddy/MEMORY.md` 里写着
# `~90%`、`~2.4s`、`~0.9-3s`（中文行文的**约数**波浪号）。`_PATH_TOKEN_RE`
# 当时是裸 `[~|/]` 起头，于是这些被当成路径 token，而它们展不开 ——
#   ① `Path("~90%").expanduser()` 抛 `RuntimeError`（不是 `OSError`，没被接住）
#      ⇒ doctor 整条退 **70**（内部错误），一个约数把体检打成"工具坏了"；
#   ② 若把异常吞掉继续跑则更坏：展不开会**原样保留**，`resolve()` 把它当相对路径
#      接到 cwd 上变成 `<cwd>/~90%`，于是"板恰好建在当前目录"时与板前缀匹配
#      ⇒ 一句「延迟 ~2.4s」就把这份记忆判成"提到了本板"（**静默假绿**）。
# 修法不是加特例，而是把"这看起来确实是路径"变成硬前提：`~` 后必须跟分隔符，
# 且必须展开得开、展开后是绝对的。方向刻意选**宁可漏、不可猜**。
PR9="$TMP/piers"; PH9="$TMP/piers-home"; PJ9="$TMP/piers-proj"
mkdir -p "$PR9" "$PH9" "$PJ9/.workbuddy/memory"
PR9W="$(cd "$PR9" && pwd -P)"
D39() { env HOME="$PH9" "$PYABS" "$WL" --dir "$PR9W" "$@"; }

# 前置条件：本机的 pathlib 必须真的会对 `~90%` 抛错。否则"70 那个洞"根本不存在，
# 整组会以**全绿**的样子什么都没测到 —— 假绿比红更贵。
if "$PYABS" -c 'import pathlib,sys
try:
    pathlib.Path("~90%").expanduser()
except RuntimeError:
    sys.exit(0)
sys.exit(1)' 2>/dev/null; then
  ok "前置条件成立：pathlib 对 ~90% 确实抛 RuntimeError（70 的来源就在这）"
else
  ng "前置条件不成立：本机 pathlib 不抛 —— 这一组测不出任何东西（假绿）"
fi

# 判据层直接下断言（比经 CLI 更不容易被"别的原因"掩盖）。
# ⚠️ 必须让 **cwd == 板**：假阳性只在这个条件下显形 —— 换了别的目录，
#    `~90%` 接到 cwd 上也撞不上板，测试会以全绿的样子漏掉这个洞。
P39=$( cd "$PR9W" && "$PYABS" - "$PR9" <<PY39
import sys
sys.path.insert(0, r"$HERE")
import work_log as W
from pathlib import Path
d = Path.cwd()                      # 板**就是** cwd —— 假阳性的触发条件
print("板上目录=%s" % d)
for tok in ("~90%", "~2.4s", "~0.9-3s", "~nosuchuser/x"):
    print("不算路径 %s:%s" % (tok, W._is_same_dir(tok, d)))
print("只有约数的文本:%s" % W._mentions_board("# 记忆\n延迟 ~2.4s，额度 ~90%\n", d))
print("真坐标文本:%s" % W._mentions_board("# 记忆\n看板：%s\n" % d, d))
print("未解析拼法:%s" % W._mentions_board("# 记忆\n看板：%s\n" % sys.argv[1], d))
PY39
)
has "约数 ~90% 不算路径" "$P39" "不算路径 ~90%:False"
has "约数 ~2.4s 不算路径" "$P39" "不算路径 ~2.4s:False"
has "约数 ~0.9-3s 不算路径" "$P39" "不算路径 ~0.9-3s:False"
has "打错的用户名 ~nosuchuser/x 同样不算" "$P39" "不算路径 ~nosuchuser/x:False"
has "★ 板=cwd 时，只有约数的记忆仍判「没提到本板」（假阳性陷阱）" \
  "$P39" "只有约数的文本:False"
# 反面对照：证明上面几条不是因为"判据永远返回 False"而全绿。
has "★ 反面对照：真坐标仍然判 True" "$P39" "真坐标文本:True"
has "第 38 组的正向仍在：未解析拼法仍判 True" "$P39" "未解析拼法:True"

# CLI 层：真机那样"记忆里混着约数 + 义务句"时，doctor 不许再退 70。
# 刻意把工具名写进去：这样它会落进「只提到工具、没有坐标」那一支。
# 而在修复前，`~90%` 会被当成路径 ⇒ has_path=True ⇒ 判「✓ 1 处齐备」并**退 0** ——
# 也就是说这条断言在旧代码下是**假绿**，正是它要抓的东西。
# （不写工具名的话那份文件根本进不了 rows，测到的是另一条分支，白测。）
D39 init --task 判据不许猜 >/dev/null 2>&1
printf '# 记忆\nwork-log 是心跳看板\n延迟 ~2.4s，额度 ~90%%，覆盖 ~0.9-3s\n每个动作后 post；收工 --done\n' \
  > "$PJ9/.workbuddy/memory/MEMORY.md"
o39=$( cd "$PJ9" && D39 doctor --agent z1 --stale-after 9999 2>&1 ); rc39=$?
hasnt "记忆里有 ~90% 时 doctor 不再报「内部错误」" "$o39" "内部错误"
has "并且判成「只提到工具、没有坐标」" "$o39" "⚠ 只提到工具"
eq "退 1（有病是业务结论），不是 70" "${rc39}" 1
# 还原：真坐标写回去，判据必须立刻变绿（证明上一条不是"环境坏了"）。
printf '# 记忆\nwork-log 是心跳看板\n看板：%s\n延迟 ~2.4s，额度 ~90%%\n每个动作后 post；收工 --done\n' \
  "$PR9W" > "$PJ9/.workbuddy/memory/MEMORY.md"
o39b=$( cd "$PJ9" && D39 doctor --agent z1 --stale-after 9999 2>&1 )
has "混着约数的真坐标仍然判绿（判据没有被约数带偏）" "$o39b" "③ 通道坐标 + 报到义务：✓"

# 两个命令行入口：展不开的 `~` 是**用法**问题，不许借用 70。
usage "--dir 写着展不开的 ~ ⇒ 用法错（不是 70）" \
  "$("$PYABS" "$WL" --dir '~90%' status >/dev/null 2>&1; echo $?)"
usage "--to 写着展不开的 ~ ⇒ 用法错（不是 70）" \
  "$("$PYABS" "$WL" --dir "$PR9W" onboard --to '~90%/AGENTS.md' >/dev/null 2>&1; echo $?)"
usage "--dir 写着不存在的用户名 ⇒ 同样是用法错（不是只给 ~90% 打补丁）" \
  "$("$PYABS" "$WL" --dir '~nosuchuser/x' status >/dev/null 2>&1; echo $?)"
# 反面对照：正常的 `~/…` 必须照常展开 —— 别把判据做得宽到把所有 `~` 都拒了。
o39h=$( env HOME="$PH9" "$PYABS" "$WL" --dir '~/board39' init --no-auto-ui 2>&1 ); rc39h=$?
eq "正常的 ~/… 仍然照常展开（没被一刀切拒掉）" "${rc39h}" 0
if [ -d "$PH9/board39" ]; then ok "并且确实落在家目录下（证明上一条不是因为别的原因退 0）"
else ng "~/ 没有正确展开"; fi

# ---- 40 prune 归档（"搬家不是删除"） ---------------------------------------
# 为什么这一组必须有：board.md / alerts.md / state.alerts 全是**只增不减**的，
# 真看板实测 607KB / 400KB / 47KB。补了 prune 之后，最容易被忽略的**不是**
# "文件变小了没有"，而是两件事：(1) 试算必须一个字节都不改；(2) 搬走历史后
# 每个 agent 的读取游标必须跟着前移 —— 不然后续 brief 会**静默收不到新动态**。
echo "[40] prune 归档（搬家不是删除）"
D40="$TMP/g40"
P40(){ "$PY" "$WL" --dir "$D40" "$@"; }
P40 init --task prune自测 --agents a1,a2 >/dev/null 2>&1
OLD40="$TMP/old40"; mkdir -p "$OLD40"
D40="$D40" "$PY" - <<'PY'
import json, os, sys, time
from datetime import datetime, timedelta
d = os.environ["D40"]
today = datetime.now().strftime("%Y-%m-%d")
old = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
hdr = ["# work-log 看板", "> 任务：prune自测",
       "> 行格式：`<agent> HH:MM:SS [标签] 内容`", ""]
b = hdr + ["## " + old,
           "<a2> 10:00:00 [执行] 五天前的第一条",
           "<a2> 10:00:01 [执行] 五天前的第二条",
           "<a2> 10:00:02 [执行] 五天前的第三条",
           "",
           "## " + today,
           # ⚠ 这条必须写**当前时刻**，不能写死 09:00：自测凌晨跑时"今天 09:00"
           #   在未来 ⇒ (k2) 的时间戳校验会假红（DRIFTED）。
           "<a2> %s [执行] 今天的一条" % datetime.now().strftime("%H:%M:%S"),
           ""]
open(os.path.join(d, "board.md"), "w", encoding="utf-8").write("\n".join(b) + "\n")
ent = ["<watchdog> %02d:%02d:00 [告警] 流水第 %d 条" % (h, m, i)
       for i, (h, m) in enumerate([(h, m) for h in range(12) for m in range(25)])][:300]
open(os.path.join(d, "alerts.md"), "w", encoding="utf-8").write(
    "# work-log 告警流水\n\n> 由 watch 自动追加\n" + "\n".join(ent) + "\n")
open(os.path.join(d, "user.md"), "w", encoding="utf-8").write("[09:00:00] 用户：别动我\n")
p = os.path.join(d, "state.json")
st = json.load(open(p))
nowt = time.time()
st["alerts"] = [
    {"id": 1, "agent": "a1", "ts": nowt - 30 * 86400, "silence": 90,
     "text": "未确认的老告警"},
    {"id": 2, "agent": "a1", "ts": nowt - 10 * 86400, "silence": 90,
     "text": "已确认很老", "acked_by": "a1"},
    {"id": 3, "agent": "a1", "ts": nowt - 2 * 86400, "silence": 90,
     "text": "长正文" * 60, "acked_by": "a1"},
    {"id": 4, "agent": "a1", "ts": nowt - 2 * 86400, "silence": 91,
     "text": "已静默 91s（阈值 90s）且未写「任务完成」，请在线 agent 检查该 agent "
             "是否卡死并修复→ 这一段是模板话，确认之后就没有用了，直接丢掉即可",
     "acked_by": "a1"},
]
ag = st["agents"].setdefault("a1", {})
ag["board_cursor"] = 4          # 板上 4 条心跳，全读过
ag["user_cursor"] = 1
json.dump(st, open(p, "w"), ensure_ascii=False)
PY
# 留一份原件，用来证明"试算真的没改东西"
cp "$D40/board.md" "$OLD40/board.md"; cp "$D40/alerts.md" "$OLD40/alerts.md"
cp "$D40/state.json" "$OLD40/state.json"; cp "$D40/user.md" "$OLD40/user.md"

o40a=$(P40 prune --days 3 2>&1)
has "(a) 默认只试算并明说改动为零" "$o40a" "未改动任何文件"
has "(a2) 报告预告会前移游标（试算也要数）" "$o40a" "同步前移 1 个 agent"
for f in board.md alerts.md state.json user.md; do
  if cmp -s "$D40/$f" "$OLD40/$f"; then ok "(b) 试算未改 $f"
  else ng "(b) 试算改了 ${f}（试算绝对不能落盘）"; fi
done

P40 prune --days 3 --apply >/dev/null 2>&1; eq "(--apply 退出码 0" $? 0
hasnt "(c) 老日期块的内容已不在板里" "$(cat "$D40/board.md")" "五天前的第一条"
has "(c2) 板里只剩今天那一条" "$(cat "$D40/board.md")" "今天的一条"
eq "(c3) 只剩一个日期段" "$(grep -c '^## ' "$D40/board.md")" 1
eq "(c4) 板头（任务/协议那几行）留着" "$(grep -c '^> 任务：prune自测' "$D40/board.md")" 1
has "(d) 搬走的内容确实进了 archive/（搬家不是删除）" \
  "$(cat "$D40/archive/board-rotated-$(date +%Y-%m-%d).md")" "五天前的第三条"
eq "(e) alerts.md 只留最近 200 条" "$(grep -c '^<watchdog>' "$D40/alerts.md")" 200
eq "(e2) 另外 100 条进了 archive/" \
  "$(grep -c '^<watchdog>' "$D40/archive/alerts-rotated-$(date +%Y-%m-%d).md")" 100
has "(e3) alerts.md 的表头也留着" "$(cat "$D40/alerts.md")" "work-log 告警流水"
# 游标：搬走 3 条 ⇒ a1 从 4 前移到 1。这是本组最关键的一条断言。
eq "(f) 搬走 3 条心跳 ⇒ a1 的 board_cursor 从 4 前移到 1" \
  "$("$PY" -c "import json;print(json.load(open('$D40/state.json'))['agents']['a1']['board_cursor'])")" 1
eq "(g) 未确认的老告警**永不触碰**（那是待办、不是噪音）" \
  "$("$PY" -c "import json;s=json.load(open('$D40/state.json'));print(sum(1 for x in s['alerts'] if not x.get('acked_by')))")" 1
eq "(h) 已确认且超过 3 天的整条清掉" \
  "$("$PY" -c "import json;print(len(json.load(open('$D40/state.json'))['alerts']))")" 3
eq "(i) 已确认 2 天的超长正文被截短（没有 → 时按长度保底截）" \
  "$("$PY" -c "import json;s=json.load(open('$D40/state.json'));t=[x['text'] for x in s['alerts'] if x.get('id')==3][0];print('SHORT' if len(t)<100 else 'LONG')")" SHORT
# 有 → 时**只留 → 之前那半句**：→ 之后是「该怎么办」的模板话，确认过就没用了。
# 这是结构性的切法（不是拍一个长度），实测真看板 200/200 条都带 →，平均 188→69 字节。
eq "(i2) 带 → 的正文只留前半句（后半句模板话丢掉）" \
  "$("$PY" -c "import json;s=json.load(open('$D40/state.json'));t=[x['text'] for x in s['alerts'] if x.get('id')==4][0];print('CUT' if ('模板话' not in t and '已静默' in t) else 'KEPT')")" CUT
# user.md 与 user_cursor 都不许被碰（prune 只处置 board/alerts）
if cmp -s "$D40/user.md" "$OLD40/user.md"; then ok "(j) user.md 一个字节都没动"
else ng "(j) prune 动了 user.md"; fi
eq "(j2) user_cursor 不受影响（它是 user.md 的消息索引，与板行号无关）" \
  "$("$PY" -c "import json;print(json.load(open('$D40/state.json'))['agents']['a1']['user_cursor'])")" 1
# 解析层：剩下的条目时间必须还算得对（整段搬的核心依据）
r40=$(D40="$D40" "$PY" - "$HERE" <<'PY'
import os, sys, time
sys.path.insert(0, sys.argv[1])
import work_log as W
from pathlib import Path
es = W.board_entries(Path(os.environ["D40"]))
print("N=%d" % len(es))
print("D=%s" % time.strftime("%Y-%m-%d", time.localtime(es[0]["ts"])))
print("T=%.0f" % es[0]["ts"])
PY
)
eq "(k) 搬走整段后剩下 1 条心跳" "$(printf '%s\n' "$r40" | sed -n 's/^N=//p')" 1
# ⚠ 这里**不**再跟 `date +%Y-%m-%d` 比：自测跑在午夜前后时"今天"会跳变，
#   实测拿到 期望[2026-09-29] 实际[2026-09-28] 的假红（条目是 23:5x 建的）。
#   要验的本意是"整段搬没把时间带偏"，那就直接验时间戳本身仍是本次运行内的。
eq "(k2) 剩下那条的时间戳仍是本次运行内的（整段搬没把时间带偏）" \
  "$("$PY" -c "import time;print('FRESH' if abs($(printf '%s\n' "$r40" | sed -n 's/^T=//p') - time.time()) < 7200 else 'DRIFTED')")" FRESH
# ★ 端到端回归：归档之后别人发的动态**必须**还收得到。
#   没有 (f) 那条游标修正，这里 a1 的游标还停在 4，而板只剩 1 条 ⇒
#   `entries[4:]` 永远是空 ⇒ brief 一个"新动态"都看不到，且不报任何错。
#   这条**不是空转** —— 已单独验过反面：在临时板上把游标手工设成 99（= 超前），
#   再让 a2 post 一条，`brief --agent a1` 的整段输出就是「（无新动态）」。
#   也就是说"游标超前"这种故障**看起来完全正常**，只有这条断言拦得住。
P40 post --agent a2 --text "归档之后的一条新动态" >/dev/null 2>&1
has "(l) ★ 归档后 brief 仍能收到别人的新动态（游标没错位）" \
  "$(P40 brief --agent a1 2>&1)" "归档之后的一条新动态"
# 用法错误一律 2（不能借用业务码 1）
usage "--days 负数" "$(P40 prune --days -1 >/dev/null 2>&1; echo $?)"
usage "--keep-alerts-md 负数" "$(P40 prune --keep-alerts-md -1 >/dev/null 2>&1; echo $?)"
# --json：给脚本用的输出必须是合法 JSON
o40j=$(P40 prune --days 3 --json 2>&1)
eq "(--json 输出可解析且有三项" \
  "$("$PY" -c "import json,sys;d=json.loads(sys.argv[1]);print('OK' if len(d['items'])==3 else 'BAD')" "$o40j")" OK

# ---- 41 hold 与 post 解耦 + main() 兜底把内部异常映射成 70 ------------------
echo "[41] hold/post 解耦（post 不清窗口）+ main 兜底 70"
# 旧版 cmd_post 里有一行 expected_silence_until = 0.0：任何一条 post 都当场把
# hold 窗口拆掉 ⇒「每步都要 post」和「长任务先 hold」互相打架，得靠一段文档
# 绕口令（"hold 别紧接着 post"）去解释掉一个坑。该修状态机，不该写文档。
H41="$TMP/h41"; mkdir -p "$H41"
W41() { "$PY" "$WL" --dir "$H41" "$@"; }
W41 init --task "解耦自测" --agents a1 >/dev/null
W41 post --agent a1 --text 开始 >/dev/null
W41 hold --agent a1 --seconds 600 --text 长任务 >/dev/null
W41 post --agent a1 --text 中途心跳 >/dev/null
sil41=$("$PY" -c "
import json, time
st = json.load(open('$H41/state.json'))
u = st['agents']['a1'].get('expected_silence_until', 0.0)
print('OK' if u > time.time() + 300 else 'GONE:' + str(u))")
eq "★ post 之后 hold 窗口仍在（不再被 post 清零）" "${sil41}" "OK"
has "中途心跳时状态仍判挂起中" "$(W41 check --readonly 2>&1)" "挂起中"
W41 hold --agent a1 --seconds 0 >/dev/null
z41=$("$PY" -c "
import json
st = json.load(open('$H41/state.json'))
print(st['agents']['a1'].get('expected_silence_until', 0.0))")
eq "hold --seconds 0 = 显式解除（清零）" "${z41}" "0.0"
u41=$(W41 hold --agent a1 --seconds -5 2>&1); rc41=$?
usage "hold --seconds 负数退 2" "${rc41}"
# main() 兜底：build_parser() 阶段的 NameError（并发编辑吞函数头的真实事故，
# 2026-09-28 opencode 撞上：退出码 1，看起来像它自己参数写错）也必须映射成 70。
# 旧版 try 只包 args.func(args)，解析器构建按名字引用 cmd_* 函数，在 try 外炸。
g41=$("$PY" -c "
import sys
sys.path.insert(0, '$HERE')
import work_log as w
del w.cmd_tail
sys.exit(w.main(['tail', '--dir', '$H41', '--limit', '1']))" 2>/dev/null; echo $?)
eq "★ build_parser 阶段的 NameError → 70（不再伪装成业务码）" "${g41}" "70"

# ---- 42 同名多线（一个名字被两个不相干的目录用过）-------------------------
echo "[42] 同名多线：判据用 cwd，不用进程探测"
# 这一组钉的是今天那个现场：同一个名字从两个**不相干**的目录发帖 ⇒
# 两条线共用 last_seen / entries / 用户喊话游标 / 心跳预算，**板上完全看不出来**。
# 唯一的痕迹 `instances` 靠进程探测，对「一进程多会话」的形态（WorkBuddy /
# Electron 桌面版）探测不出、按设计返回空串不猜 ⇒ 那种情况下合并**零痕迹**。
# cwd 是工具本来就握着、且不需要猜的判据 —— 所以用它补上这一格。
X42="$TMP/split"; mkdir -p "$X42/a" "$X42/b/sub" "$X42/c"
XB="$TMP/split-board"
P42() { (cd "$1" && shift && "$PY" "$WL" --dir "$XB" "$@"); }
P42 "$X42/a" init --task "同名多线" >/dev/null

o42a=$(P42 "$X42/a" post --agent a1 --text 第一条 2>&1)
hasnt "(a) 第一次用这个名字：不喊（喊了就是每个新 agent 都刷屏）" "$o42a" "同名多线"
o42b=$(P42 "$X42/b" post --agent a1 --text 第二条 2>&1)
has "(b) ★ 换个不相干的目录用同一个名字 ⇒ 当场喊出来" "$o42b" "同名多线"
has "并给出修法（identity set / 环境变量）" "$o42b" "identity set"
has "板上也留了一条（给别人看）" "$(cat "$XB/board.md")" "两个不同的目录"
o42s=$(P42 "$X42/b/sub" post --agent a1 --text 第三条 2>&1)
hasnt "(c) 嵌套目录不算两条线（换到子目录不该喊）" "$o42s" "同名多线"
o42d=$(P42 "$X42/b" post --agent a1 --text 第四条 2>&1)
hasnt "(d) 同一个目录不重复喊（只记住「上一个」是不够的）" "$o42d" "同名多线"
o42e=$(P42 "$X42/c" post --agent a1 --text 第五条 2>&1)
has "(e) 第三个目录仍能喊一次" "$o42e" "同名多线"
o42f=$(P42 "$X42/c" post --agent a1 --text 第六条 2>&1)
hasnt "(f) 它也不重复" "$o42f" "同名多线"
eq "(g) 板上留痕恰好 2 条（b 一次 + c 一次，不是 6 条）" \
   "$(grep -c '两个不同的目录' "$XB/board.md")" "2"
has "(h) status 标出「同名多线」" "$(P42 "$X42/a" status 2>&1)" "同名多线"
has "(i) doctor ⑦a 列清是哪两个目录" "$(P42 "$X42/a" doctor 2>&1)" "⑦a 同名多线"
has "并把它算进体检不合格" "$(P42 "$X42/a" doctor >/dev/null 2>&1; echo $?)" "1"
# 反向对照：不同名字互不干扰 —— 否则判据就是"只要换目录就喊"，等于又一种噪音
P42 "$X42/c" post --agent a2 --text 另一个人 >/dev/null
o42g=$(P42 "$X42/a" status 2>&1)
hasnt "(j) 换名字就不会被算成多线（证明判据盯的是名字+目录，不是目录）" \
  "$(printf '%s\n' "$o42g" | grep '^a2' )" "同名多线"

# ---- 43 互相等待：await 当场发现「我要等的人正在等我」-----------------------
echo "[43] 互相等待：await 不再干等到超时（退出码 5）"
# 现场（2026-09-28 真模型验收首次撞到）：两个真模型 agent 各问各的、同时阻塞等对方
# ⇒ 双方稳定刷心跳、看起来最健康，实际谁都等不到答案，各干等满 5 分钟超时。
# 险情判定原本只活在 evaluate() 里 = 只有看门狗扫到才有人知道，而 await 自己不知道。
# 现在 await 会看等待图里的**环**（不猜）：对方 awaiting 的提问里有没有一条是问我的。
Y43="$TMP/dl43"; mkdir -p "$Y43"
W43() { "$PY" "$WL" --dir "$Y43" "$@"; }
W43 init --agents a1,a2 --task "互相等待" --stale-after 600 >/dev/null
q1=$(W43 ask --agent a1 --to a2 --text "音频格式？" | grep -o '#[0-9]*' | head -1 | tr -d '#')
q2=$(W43 ask --agent a2 --to a1 --text "采样率？"   | grep -o '#[0-9]*' | head -1 | tr -d '#')
eq "(a) 前置条件：两边各问了一条" "${q1}${q2}" "12"
W43 await --agent a2 --id "$q2" --timeout 30 --interval 0.5 >"$Y43/out2.txt" 2>&1 &
P43=$!
sleep 2
o43=$(W43 await --agent a1 --id "$q1" --timeout 30 --interval 0.5 2>&1); rc43=$?
sleep 1                        # 让另一侧也过一轮（它不一定能自己发现，下面只断言留痕）
wait $P43 2>/dev/null
eq "(b) ★ 一方立刻退 5（互相等待），不是干等满 30s 超时" "${rc43}" "5"
has "(c) 点名对方在等哪一条" "$o43" "正在等你回应 #${q2}"
has "并给出破局动作（先 reply 那条）" "$o43" "reply --agent a1 --id ${q2}"
has "(d) 板上留了痕（不依赖看门狗也看得见）" "$(cat "$Y43/board.md")" "[互相等待]"
# 反例：只有单向等待时不许误报 —— 否则"互相等待"会变成又一种噪音
q3=$(W43 ask --agent a1 --to a2 --text "再来一条" | grep -o '#[0-9]*' | head -1 | tr -d '#')
o43b=$(W43 await --agent a1 --id "$q3" --timeout 2 --interval 0.5 2>&1); rc43b=$?
eq "(e) 单向等待仍按超时退 1（不误报成死锁）" "${rc43b}" "1"
hasnt "且不说「互相等待」" "$o43b" "互相等待"

echo "[44] brief --from-now：新 agent 干净起步（不被历史淹没，但不吞用户喊话）"
# 现场（2026-09-29）：一块常驻板有 260KB / 数千条历史。新接入的 agent 首次 brief
# 拿到的是**全量历史**（post 不推进读游标，工具也没有"我只要从现在开始"的正门），
# 上下文当场被淹没 —— 用户叫几个 agent 进来的那一刻，"能不能交流"就已经输了。
Y44="$TMP/dl44"; mkdir -p "$Y44"
W44() { "$PY" "$WL" --dir "$Y44" "$@"; }
W44 init --agents a1 --task "from-now 测试" >/dev/null
for i in 1 2 3; do W44 post --agent a1 --text "PROBE-OLD-$i" >/dev/null; done
W44 say --text "PROBE-USER-SAY" >/dev/null
has "(a) 前置：新 agent 首次 brief 就是全量历史（这正是要解决的问题）" \
    "$(W44 brief --agent fresh0)" "PROBE-OLD-1"
o44=$(W44 brief --agent fresh1 --from-now)
has "(b) --from-now 明说跳过了多少条历史" "$o44" "已对齐到当前：跳过 3 条历史心跳"
hasnt "(c) 跳过之后不再投喂历史心跳" "$o44" "PROBE-OLD"
has "(d) ★ 但用户喊话不被吞（人类的指令宁多看一条）" "$o44" "PROBE-USER-SAY"
hasnt "(e) 对齐后连别人的心跳都不投喂（只剩喊话与待办）" "$o44" "<a1> "
eq "(f) 幂等：再跑一次跳过 0 条" \
   "$(W44 brief --agent fresh1 --from-now | sed -n 's/.*跳过 \([0-9]*\) 条历史心跳.*/\1/p')" 0
W44 post --agent a1 --text "PROBE-NEW-ONE" >/dev/null
has "(g) ★ 对齐之后别人的新动态照收（游标没推过头）" \
    "$(W44 brief --agent fresh1)" "PROBE-NEW-ONE"
# 空板边界：cursor 与总数都是 0，不该除零/报错，也不该说"跳过 N 条"
Y44b="$TMP/dl44b"; mkdir -p "$Y44b"
W44b() { "$PY" "$WL" --dir "$Y44b" "$@"; }
W44b init --agents z1 --task "空板" >/dev/null
o44c=$(W44b brief --agent z1 --from-now); rc44c=$?
eq "(h) 空板上也能用（退 0，不是拿 0 条去算差值崩掉）" "$rc44c" 0
has "并如实说跳过 0 条" "$o44c" "跳过 0 条历史心跳"
# 新 agent 刚报到就被问：对齐只跳到"现在"，**必办事项从不跳过**
W44 post --agent fresh2 --text "我是 fresh2，刚接入" >/dev/null
W44 ask --agent a1 --to fresh2 --text "PROBE-QUESTION" >/dev/null
o44b=$(W44 brief --agent fresh2 --from-now)
has "(i) ★ 「待你回应 #N」不受对齐影响（新人不该漏掉真正的待办）" "$o44b" "待你回应"
hasnt "(j) 但它自己的历史心跳仍被跳过" "$o44b" "PROBE-OLD"
usage "--peek 与 --from-now 同给（语义相反）" \
      "$(W44 brief --agent fresh1 --peek --from-now >/dev/null 2>&1; echo $?)"
usage "--from-now 后面跟了一个裸词（它是开关，不接值）" \
      "$(W44 brief --agent fresh1 --from-now PROBE-OLD-1 >/dev/null 2>&1; echo $?)"

# ---- 45 waker.sh（外部唤醒器：让"响应式会话"真的能被叫醒） -----------------
# 为什么值得一组断言：这个脚本是**唯一**能把事件驱动会话叫醒的东西，而它自己的
# 判据错了不会报任何错 —— 只会静默地"再也不醒"。实测过一次（见 (b)）。
echo "[45] waker.sh 唤醒器"
WK="$HERE/waker.sh"
Y45="$TMP/dl45"; mkdir -p "$Y45"
W45() { "$PY" "$WL" --dir "$Y45" "$@"; }
W45 init --agents a1,a2 --task "waker 自测" >/dev/null

# 用法错误一律 2 —— 前置条件不满足也不许借用业务码 1（1 在这套协议里是"对方没回"）
usage "缺 --dir（本工具没有默认落点，不猜）" \
      "$(bash "$WK" --agent a1 >/dev/null 2>&1; echo $?)"
usage "缺 --agent" \
      "$(bash "$WK" --dir "$Y45" >/dev/null 2>&1; echo $?)"
usage "--interval 0" \
      "$(bash "$WK" --dir "$Y45" --agent a1 --interval 0 >/dev/null 2>&1; echo $?)"
usage "--interval 非数字" \
      "$(bash "$WK" --dir "$Y45" --agent a1 --interval abc >/dev/null 2>&1; echo $?)"
usage "--window-min 非数字" \
      "$(bash "$WK" --dir "$Y45" --agent a1 --window-min abc >/dev/null 2>&1; echo $?)"
usage "未知参数" \
      "$(bash "$WK" --dir "$Y45" --agent a1 --bogus >/dev/null 2>&1; echo $?)"
# 注意：这里**不能**用 $TMP 本体 —— 整套跑时 [1] 组已经在 $TMP 里 init 过，
# 那块目录**有** user.md，于是唤醒器会真的启动、并在 `$( )` 里把整套测试挂死
# 直到默认 45 分钟窗口结束（2026-09-29 实测卡在 53 组不动；抽单组跑时 $TMP 没被
# init 过，所以**掩盖了**它）。任何"这条命令应当立刻失败"的断言，参数必须构造得
# 让它必然失败 —— 不能依赖"别的组没在这块目录里干过什么"。
mkdir -p "$TMP/nouser45"
usage "目录里没有 user.md（不像一块板）" \
      "$(bash "$WK" --dir "$TMP/nouser45" --agent a1 >/dev/null 2>&1; echo $?)"
eq "--help 退 0" "$(bash "$WK" --help >/dev/null 2>&1; echo $?)" 0

W45PID=""
# 必须登记进 BG_PIDS：**命令替换里的后台进程是父脚本的孤儿** —— 2026-09-29 实测
# 留下过一个跑了 9 分半的泄漏唤醒器（父脚本被杀，命令替换子 shell 里的它活着，
# 而 trap cleanup 对 SIGKILL 不生效）。登记后至少 SIGINT/SIGTERM 那两条路能清干净。
wstart() { bash "$WK" --dir "$Y45" --agent a1 --window-min 1 --interval 1 >"$1" 2>&1 & W45PID=$!; BG_PIDS="$BG_PIDS $W45PID"; }
# 等它把基线算完（横幅打出来）再动手，否则我追加的消息会被算进基线，测的就不是唤醒
wup() { local n=24; while [ "$n" -gt 0 ]; do
          grep -q '盯两件事' "$1" 2>/dev/null && return 0; sleep 0.25; n=$((n-1)); done; return 1; }
# 等它退出，最多 $2 秒；超时就杀掉并判失败 —— 一套自测不许挂在"它没醒"上
wawait() { local n=$(( $2 * 2 )); while [ "$n" -gt 0 ]; do
             kill -0 "$1" 2>/dev/null || return 0; sleep 0.5; n=$((n-1)); done
           kill "$1" 2>/dev/null; return 1; }
wstop() { kill "$1" 2>/dev/null; wait "$1" 2>/dev/null; }

slog="$TMP/w45a.log"; wstart "$slog"; wup "$slog"; W45 say --text "PROBE-A" >/dev/null
wawait "$W45PID" 12; eq "(a) 用户新喊话 → 唤醒并退 0（不是静默地等窗口到）" "$?" 0
has "(a) 而且写明是「用户新消息」并给出条数" "$(cat "$slog")" "WAKE：用户新消息"

# ★ 回归：计数口径必须问引擎，不能自己写正则复刻
# 曾经用 '^[ts] 用户：' 数，而 user.md 允许**不带前缀的纯文本**
# （引擎 parse_user 把每个非空、非 #/> 的行都算一条）⇒ 用户写纯文本就永远叫不醒我。
slog="$TMP/w45b.log"; wstart "$slog"; wup "$slog"
printf '我是手写的纯文本，没有前缀\n' >> "$Y45/user.md"
wawait "$W45PID" 12; eq "(b) ★纯文本（不带前缀）也唤醒 —— 引擎就是这么算一条消息的" "$?" 0
has "(b) 输出含 WAKE" "$(cat "$slog")" "WAKE：用户新消息"

# 反例：别人的回执/心跳绝不唤醒 —— 唤醒器一旦变成噪音源，人就会把它关掉。
# （夹具里此时已有 #1（(a) 的 say）与 #2（(b) 的纯文本），所以下面 ack #1 是有效的。）
slog="$TMP/w45c.log"; wstart "$slog"; wup "$slog"
W45 ack-user --agent a2 --id 1 --text "别人先认领了" >/dev/null
W45 post --agent a2 --text "别人在干活" >/dev/null
# ★ 回归：别人的正文里出现「待你回应」四个字也不许唤醒 ——
# 曾经用 `brief | grep '待你回应'` 判"有没有人问我"，而 brief 是**给人看的散文**，
# 于是别人在讨论这条判据时就把我吵醒了（实测误唤醒，板上当时没有任何待我回应的提问）。
W45 post --agent a2 --text "我们来讨论『待你回应』这条判据的措辞" >/dev/null
sleep 2
if kill -0 "$W45PID" 2>/dev/null; then
  ok "★ 别人的回执、心跳、以及正文里的「待你回应」都不唤醒"
else
  ng "被别人的话误唤醒了：$(cat "$slog")"
fi
wstop "$W45PID"

slog="$TMP/w45d.log"; wstart "$slog"; wup "$slog"
W45 ask --agent a2 --to a1 --text "PROBE-Q" >/dev/null
wawait "$W45PID" 12; eq "(d) 有人 ask 到我 → 也要唤醒（定向问答不能只靠人盯着）" "$?" 0
has "(d) 且说明是被提问唤醒" "$(cat "$slog")" "WAKE：有人 ask 到你"

slog="$TMP/w45e.log"; wstart "$slog"; wup "$slog"; touch "$Y45/waker-stop"
wawait "$W45PID" 12
has "(e) waker-stop 能停（且退 0，不当故障）" "$(cat "$slog")" "WAKER-STOPPED"
rm -f "$Y45/waker-stop"

# 边界：运行中 user.md 被删短。若不重设基线，从此恒有 U1<U0 ⇒ 静默失聪
slog="$TMP/w45f.log"; wstart "$slog"; wup "$slog"
"$PY" - "$Y45" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]) / "user.md"
keep = [l for l in p.read_text(encoding="utf-8").splitlines() if not l.startswith("[")][:5]
p.write_text("\n".join(keep) + "\n", encoding="utf-8")
PY
sleep 2
has "(f) ★ user.md 被删短时重设基线（否则从此静默失聪）" "$(cat "$slog")" "基线已重设"
W45 say --text "删短之后的新消息" >/dev/null
wawait "$W45PID" 12; eq "(f) 重设基线之后仍能被新消息唤醒" "$?" 0

# ---- 46 空文本的判据 + --text-file ----------------------------------------
echo "[46] 空文本的判据（载荷型 vs 可选标签型）+ --text-file"
# 判据不是"空文本一律是用法错误"，而是**空文本在这个命令里有没有默认含义**：
#   · post/ask/reply/say/ack-user：文本**就是载荷**，空 = 没有内容 ⇒ 退 2；
#   · hold/release：文本是**可选标签**，空 = 用内置默认标签 ⇒ 合法。
# 而且 argparse 里 --text 的 default 就是 ""，`--text ''` 与"根本没给"**逐字节相同**、
# 工具分不出来 —— 对 hold 退 2 等于把"用默认标签"这条路堵死。
# 这条判据是从一个**错误猜想**里改出来的：我原以为"所有命令都该拒空"，实测发现
# hold/release 是 0 之后，反过来问"为什么这几个不同"，才得到上面那句判据。
# 先跑出实测差异、再问差异从哪来 —— 别拿通则去套个案（当天全板栽过两次）。
H46="$TMP/g46"; mkdir -p "$H46"
W46() { "$PY" "$WL" --dir "$H46" "$@"; }
W46 init --agents a1,a2 --task "空文本自测" >/dev/null
usage "post 不给 --text"      "$(W46 post --agent a1 >/dev/null 2>&1; echo $?)"
usage "post --text ''"        "$(W46 post --agent a1 --text '' >/dev/null 2>&1; echo $?)"
usage "ask 不给 --text"       "$(W46 ask --agent a1 --to a2 >/dev/null 2>&1; echo $?)"
usage "reply 不给 --text"     "$(W46 reply --agent a2 --id 1 >/dev/null 2>&1; echo $?)"
usage "say 不给 --text（空喊话会把所有 agent 叫起来看一行空的）" \
      "$(W46 say >/dev/null 2>&1; echo $?)"
usage "say --text 全空白"     "$(W46 say --text '   ' >/dev/null 2>&1; echo $?)"
usage "ack-user 不给 --text（空认领让用户以为有人管了）" \
      "$(W46 ack-user --agent a1 --id 1 >/dev/null 2>&1; echo $?)"
eq "hold 不给 --text（可选标签，用内置默认）→ 0" \
   "$(W46 hold --agent a1 --seconds 60 >/dev/null 2>&1; echo $?)" 0
eq "release 不给 --text → 0" \
   "$(W46 release --agent a1 >/dev/null 2>&1; echo $?)" 0
usage "--text 与 --text-file 同时给（谁覆盖谁不该由工具猜）" \
      "$(W46 post --agent a1 --text x --text-file "$H46/no.txt" >/dev/null 2>&1; echo $?)"
usage "--text-file 指向不存在的文件" \
      "$(W46 post --agent a1 --text-file "$H46/does-not-exist.txt" >/dev/null 2>&1; echo $?)"
: > "$H46/empty.txt"
printf '   \n\n' > "$H46/blank.txt"
usage "--text-file 指向空文件" \
      "$(W46 post --agent a1 --text-file "$H46/empty.txt" >/dev/null 2>&1; echo $?)"
usage "--text-file 只有空白（空文件不是"省略"的替身）" \
      "$(W46 post --agent a1 --text-file "$H46/blank.txt" >/dev/null 2>&1; echo $?)"
usage "hold --text-file 空文件（省略能表达的语义不必用空文件表达）" \
      "$(W46 hold --agent a1 --seconds 60 --text-file "$H46/empty.txt" >/dev/null 2>&1; echo $?)"
# ★ 这一条才是 --text-file 存在的全部理由：长中文文本经 shell argv 会撞上整整一类
#   **不报错**的问题 —— 反引号被当命令执行后静默替成空格、$变量被展开、
#   ${var} 后接中文标点把标点首字节吞进变量名致整个脚本 rc=127。
#   让文本走文件 = 让判据只面对字节，整类问题不存在。
#   刻意**不**在这里模拟"调用方自己写命令行会怎样"：模拟它等于在自测里执行任意
#   命令替换，那是拿测试脚手架去踩正在被测的那个坑。这里只断言文件路径逐字节可信。
printf '%s' '正文 `id -u` 与 $HOME 与 ${var} 与「引号」' > "$H46/nasty.txt"
W46 post --agent a1 --text-file "$H46/nasty.txt" >/dev/null
has "★ --text-file：反引号/\$变量/中文标点逐字节落板" \
    "$(cat "$H46/board.md")" '正文 `id -u` 与 $HOME 与 ${var} 与「引号」'
printf '%s' '从标准输入来的正文' | W46 post --agent a1 --text-file - >/dev/null
has "--text-file - 从 stdin 读取" "$(cat "$H46/board.md")" '从标准输入来的正文'
# 反向：合法调用必须真的写进去。少这一半，上面那串"退 2"可以用"把命令整个堵死"骗过去。
W46 post --agent a1 --text '正常心跳' >/dev/null
has "合法 post 真的落板（防我把命令整个堵死）" "$(cat "$H46/board.md")" '正常心跳'
W46 say --text '正常喊话' >/dev/null
has "合法 say 真的进 user.md" "$(cat "$H46/user.md")" '正常喊话'
eq "合法 ack-user → 0" \
   "$(W46 ack-user --agent a1 --id 1 --text '我来修' >/dev/null 2>&1; echo $?)" 0

# ---- 47 办事轴（第二根轴）-------------------------------------------------
echo "[47] 办事轴：心跳全绿也要能发现「事情丢了」"
# 存在理由是一组对偶：2026-09-29 同一块板，心跳轴误报 33 条 / 真卡死 0；
# 而唯一一次真事故（提问被静默丢弃 547s）**告警 0 条**。
# 调阈值救不了 —— 错的是观测口径，不是数字。
# 三层判据：① 开够久（stale_open_after）② 对方**还答得出来** ⇒ 事故（退 1）
#          ③ 对方已收工 ⇒ 该结案（**不**退 1；报出来只是给提问者收尾用）。
# ②③ 处置相反，所以分开列 —— 混在一起报就会变成噪音，噪音会让整根轴被无视。
H47="$TMP/g47"; mkdir -p "$H47"
W47() { "$PY" "$WL" --dir "$H47" "$@"; }
# 每块板只取两件事：事故几条、该结案几条。断言写成可比对的串，别去 grep 排版。
J47() { "$PY" -c "
import json, sys
d = json.load(sys.stdin)
so, uo = d.get('stale_opens') or [], d.get('unreachable_opens') or []
print('%d %d | %s | %s' % (len(so), len(uo),
      ','.join(x['to'] for x in so), ','.join(x['to'] for x in uo)))"; }
# 每个用例一块独立的板：用例之间不该互相喂状态（那样红绿就说不清是谁造成的）
b47() { H47="$TMP/g47-$1"; mkdir -p "$H47"; }

b47 accident
W47 init --agents a --stale-open-after 1 --task "办事轴·事故" >/dev/null
W47 post --agent asker --text 开始 >/dev/null
W47 post --agent peer --text 我在 >/dev/null
W47 ask --agent asker --to peer --text 字段有没有 >/dev/null
sleep 2
W47 post --agent peer --text 还在弄别的 >/dev/null
eq "(a) ★心跳全绿 + 提问没人答（对方还答得出来）⇒ 事故 1 / 该结案 0" \
   "$(W47 check --readonly --json | J47)" "1 0 | peer | "
eq "(a) ★此时 check 退 1（#24 被静默 547s 的那一刻就是这个形态）" \
   "$(W47 check --readonly >/dev/null 2>&1; echo $?)" 1
has "(a) 视图把它标成「事故」并给出处置（催它 / 自己答掉）" \
    "$(W47 status 2>&1)" "[事故]"

b47 closed
W47 init --agents a --stale-open-after 1 --task "办事轴·该结案" >/dev/null
W47 post --agent asker --text 开始 >/dev/null
W47 post --agent peer --text 我在 >/dev/null
W47 ask --agent asker --to peer --text 问完就走 >/dev/null
sleep 2
W47 post --agent peer --tag 任务完成 --text 我收工了 >/dev/null
eq "(b) 对端真收工（只有 1 条完成标签）⇒ 事故 0 / 该结案 1" \
   "$(W47 check --readonly --json | J47)" "0 1 |  | peer"
eq "(b) ★此时 check 退 0（"该结案"不是事故，报出来是噪音）" \
   "$(W47 check --readonly >/dev/null 2>&1; echo $?)" 0
has "(b) 视图把它标成「该结案」并指路（await 会立刻退 3）" \
    "$(W47 status 2>&1)" "[该结案]"

# ★ 这三个用例锁的是**同一件事**：一个循环进程（守护/监听）把心跳打成「任务完成」，
#   会一次性拿到两份豁免（心跳轴"别报我卡死" + 办事轴"我不会再答了"），
#   而板上完全看不出来 —— 当天 ask-listener-guard 真跑了半小时。
#   三条覆盖路径不同：声明的（会忘）、结构性的（忘不了）、以及不许误伤合法二次收工。
b47 loopdecl
W47 init --agents a --stale-open-after 1 --task "办事轴·循环型声明" >/dev/null
W47 identity set --agent guard --purpose "盯着板" --loop >/dev/null
W47 post --agent asker --text 开始 >/dev/null
W47 post --agent guard --tag 任务完成 --text 守护在线 >/dev/null
W47 ask --agent asker --to guard --text 你在吗 >/dev/null
sleep 2
W47 post --agent guard --tag 任务完成 --text 守护在线 >/dev/null
eq "(c) ★声明循环型 + 完成标签 ⇒ 仍算事故 1 / 该结案 0（完成标签对它不生效）" \
   "$(W47 check --readonly --json | J47)" "1 0 | guard | "
has "(c) 视图点名「循环型身份写了任务完成」" "$(W47 status 2>&1)" "循环型身份写了「任务完成」"
has "(c) 状态表标注它是循环型（下一个人不必再去猜）" "$(W47 status 2>&1)" "循环型（守护/监听"

b47 repeat
W47 init --agents a --stale-open-after 1 --task "办事轴·反复收工" >/dev/null
W47 post --agent asker --text 开始 >/dev/null
W47 post --agent guard2 --tag 任务完成 --text 守护在线 >/dev/null
W47 ask --agent asker --to guard2 --text 你在吗 >/dev/null
sleep 2
W47 post --agent guard2 --tag 任务完成 --text 守护在线 >/dev/null
eq "(d0) 两条完成 ⇒ 仍是收工、该结案（不许误伤重试）" \
   "$(W47 check --readonly --json | J47)" "0 1 |  | guard2"
eq "(d0) 两条完成 ⇒ repeat_done 也不点名（阈值是 3，驱动层重试不误伤）" \
   "$(W47 check --readonly --json | "$PY" -c "
import json, sys
print(','.join(json.load(sys.stdin).get('repeat_done') or []) or '-')")" "-"
W47 post --agent guard2 --tag 任务完成 --text 守护在线 >/dev/null
# ★ 这一条锁的是**一个已经被推翻的设计**：曾经把"连续多条完成"直接当成循环、
#   作废其完成状态。真板实测把它撤了 —— "一个回合收工一次"的交互会话尾部本来就会
#   累积好几条完成（工作期间不发中间心跳），被误判成循环后当场开始报「疑似卡死 2h8m」，
#   而那种形态恰恰是本项目最主要的用法。**会惩罚主要使用形态的规则比它堵的洞更糟。**
eq "(d) ★三条完成 ⇒ 完成状态**不作废**，仍算该结案（引擎不替人猜）" \
   "$(W47 check --readonly --json | J47)" "0 1 |  | guard2"
eq "(d) 但要报出来提醒：json 里 repeat_done 点名它" \
   "$(W47 check --readonly --json | "$PY" -c "
import json, sys
print(','.join(json.load(sys.stdin).get('repeat_done') or []))")" "guard2"
has "(d) 提醒里写明「完成状态未被改动」（否则读者以为它被静默改判了）" \
    "$(W47 status 2>&1)" "完成状态**未被改动**"
has "(d) 提醒里给出正确做法（identity set --loop）" \
    "$(W47 status 2>&1)" "identity set --agent <名字> --loop"
# ★ 密度那一维必须真的在起作用：把时间窗压到 1s，同一块板（最新两条完成间隔 2s）
#   就从"像心跳"变成"按回合"⇒ 不该再提醒。这正是真板 workbuddy-sft 的形态
#   （它三条完成的间隔是 29~83 分钟）。
#   ⚠ 两个脚手架坑，两条都踩过：
#     ① `VAR=x func` 这种环境变量前缀**对 shell 函数不生效**（只对外部命令生效），
#        必须 `export…; func…; unset…`。W47 是函数。
#     ② 断言必须在**同一次**带 env 的调用里取 json。之前是先带 env 跑一次（输出丢弃）、
#        再 unset 后重跑一次去取值 ⇒ 窗口已回到 600，断言恒假（是我脚手架错、不是代码错）。
sleep 2
W47 post --agent guard2 --tag 任务完成 --text 守护在线 >/dev/null
export WORK_LOG_REPEAT_DONE_WINDOW=1
eq "(d) ★时间窗压到 1s（最新两条间隔 2s ⇒ 按回合收工）⇒ 不再提醒" \
   "$(W47 check --readonly --json | "$PY" -c "
import json, sys
print(','.join(json.load(sys.stdin).get('repeat_done') or []) or '-')")" "-"
# ★ 阈值本身要如实报出来 —— 否则驱动层看到 repeat_done 非空/为空，却不知道是按哪个窗口判的。
#   这条同时是我刚修的 `--json` 出口 bug（res 里有 repeat_done_window、json 里漏了）的护栏。
eq "(d) ★json 里必须给出 repeat_done_window（驱动层能复核判据）" \
   "$(W47 check --readonly --json | "$PY" -c "
import json, sys
v = json.load(sys.stdin).get('repeat_done_window')
print('missing' if v is None else int(v))")" "1"
eq "(d) 但完成状态仍是「完成」（提醒的有无从不改判状态）" \
   "$(W47 check --readonly --json | J47)" "0 1 |  | guard2"
unset WORK_LOG_REPEAT_DONE_WINDOW
# 反向：窗口恢复默认后，同一块板又要被点名（否则上一条可能是"窗口坏了"而不是"密度对了"）
eq "(d) 撤掉窗口覆盖 ⇒ 又按默认 600s 判，重新点名（证明上一条是密度判的，不是坏了）" \
   "$(W47 check --readonly --json | "$PY" -c "
import json, sys
print(','.join(json.load(sys.stdin).get('repeat_done') or []) or '-')")" "guard2"

b47 relife
W47 init --agents a --stale-open-after 1 --task "办事轴·合法二次收工" >/dev/null
W47 post --agent asker --text 开始 >/dev/null
W47 post --agent peer --tag 任务完成 --text 第一轮收工 >/dev/null
W47 post --agent peer --text 又有人叫我，回来看看 >/dev/null
W47 ask --agent asker --to peer --text 顺手问一句 >/dev/null
sleep 2
W47 post --agent peer --text 答：这样那样 >/dev/null
W47 reply --agent peer --id 1 --text 答：这样那样 >/dev/null
W47 post --agent peer --tag 任务完成 --text 真收工了 >/dev/null
eq "(e) ★反向：收工→被叫回→干完→再收工 ⇒ 仍是「完成」、退 0（不许误伤）" \
   "$(W47 check --readonly >/dev/null 2>&1; echo $?)" 0
eq "(e) 中间有真实活动 ⇒ 不算循环（repeat_done 不该命中）" \
   "$(W47 check --readonly --json | "$PY" -c "
import json, sys
print(','.join(json.load(sys.stdin).get('repeat_done') or []) or '-')")" "-"

b47 declare
W47 init --agents a --stale-open-after 600 --task "办事轴·阈值声明一次" >/dev/null
eq "阈值跟板走（init --stale-open-after 600 写进 state，命令不必各配一份）" \
   "$(W47 check --readonly --json | "$PY" -c "
import json, sys
print(int(json.load(sys.stdin).get('stale_open_after')))")" 600

# ---- 48 存活形态声明 + 收工即放手 -----------------------------------------
echo "[48] 存活形态声明（spawn / 循环型）+ 收工即放手（两种写法都必须生效）"
H48="$TMP/g48"; mkdir -p "$H48"
W48() { "$PY" "$WL" --dir "$H48" "$@"; }
W48 init --agents a --stale-open-after 600 --task "存活形态自测" >/dev/null
usage "identity set --spawn 不给 --purpose（不带理由的静音就是藏身处）" \
      "$(W48 identity set --agent sp --spawn >/dev/null 2>&1; echo $?)"
eq "identity set --spawn 给了 --purpose → 0" \
   "$(W48 identity set --agent sp --spawn --purpose '被 ask_listener 唤醒' >/dev/null 2>&1; echo $?)" 0
W48 post --agent sp --text 起来干一次 >/dev/null
sleep 2
eq "★spawn 型静默超过阈值仍退 0（判活交给它的驱动层）" \
   "$(W48 check --stale-after 1 --readonly >/dev/null 2>&1; echo $?)" 0
has "视图说清它为什么安静（下一个人不会读成卡死）" \
    "$(W48 status --stale-after 1 2>&1)" "spawn 型（判活交给它的驱动层"
eq "撤销声明（--no-spawn）→ 0" \
   "$(W48 identity set --agent sp --no-spawn >/dev/null 2>&1; echo $?)" 0
eq "★撤销后同样的静默 ⇒ 退 1（回到按心跳判，声明是唯一开关）" \
   "$(W48 check --stale-after 1 --readonly >/dev/null 2>&1; echo $?)" 1
eq "循环型声明 → 0" \
   "$(W48 identity set --agent lp --loop --purpose '守护' >/dev/null 2>&1; echo $?)" 0
has "identity list 里看得见存活形态声明（不必翻文档）" \
    "$(W48 identity list 2>&1)" "循环型（永不收工）"
eq "撤销循环型（--no-loop）→ 0" \
   "$(W48 identity set --agent lp --no-loop >/dev/null 2>&1; echo $?)" 0
eq "--loop 与 --no-loop 属于已知选项（不会被当成 --text 的值吞掉）" \
   "$(W48 identity set --agent lp --loop --no-loop >/dev/null 2>&1; echo $?)" 0

# B3：收工 = 我不再占任何东西。「我已下线」语义上本来就包含「我不再持有资源」，
# 旧版却要手工 unlock + release 两步 —— 忘了任何一步，别人就被一个**已经不在了**的
# agent 挡住，而板上看不出是它挡的。
H48b="$TMP/g48b"; mkdir -p "$H48b"
W48b() { "$PY" "$WL" --dir "$H48b" "$@"; }
W48b init --agents a --task "收工即放手" >/dev/null
W48b post --agent x --text 占两个坑 >/dev/null
W48b lock --agent x --resource A >/dev/null 2>&1
W48b lock --agent x --resource B >/dev/null 2>&1
W48b hold --agent x --seconds 600 --quiet >/dev/null 2>&1
d48=$("$PY" -c "
import json
st = json.load(open('$H48b/state.json'))
print(len(st.get('locks') or {}))")
eq "前置：x 确实占着 2 把锁（否则下面那条断言是恒真的假绿）" "${d48}" 2
out48=$(W48b post --agent x --done --text 我收工了 2>&1)
eq "post --done → 0" "$?" 0
has "收工当面报出「放掉了哪几把锁」（机制要可见，别让人以为还得手工两步）" \
    "${out48}" "顺手放掉了 2 把锁"
has "收工当面报出「清了挂起窗口」" "${out48}" "顺手清掉了挂起窗口"
z48=$("$PY" -c "
import json
st = json.load(open('$H48b/state.json'))
print(len(st.get('locks') or {}), st['agents']['x'].get('expected_silence_until', -1))")
eq "★收工后：锁台账为空 且 挂起窗口归零（判据读 state.json，不 grep 输出）" "${z48}" "0 0.0"
# 反向：普通 post 不许动锁、不许清窗口 —— 否则"长任务里照常发心跳"会把 hold 拆掉
H48c="$TMP/g48c"; mkdir -p "$H48c"
W48c() { "$PY" "$WL" --dir "$H48c" "$@"; }
W48c init --agents a --task "普通 post 不动别人东西" >/dev/null
W48c lock --agent y --resource C >/dev/null 2>&1
W48c hold --agent y --seconds 600 --quiet >/dev/null 2>&1
W48c post --agent y --text 普通心跳 >/dev/null
z48b=$("$PY" -c "
import json, time
st = json.load(open('$H48c/state.json'))
print(len(st.get('locks') or {}),
      st['agents']['y']['expected_silence_until'] > time.time() + 500)")
eq "对照：普通 post 不动锁、不清挂起窗口（两者职责不混）" "${z48b}" "1 True"

# ★ 同一个「收工」，两个入口必须同一套判据（2026-09-29 真板实测，我自己撞的）。
#   看门狗认 `tag ∈ DONE_TAGS`（也就是板上人人都在写的 `--tag 任务完成`），
#   而"收工即放手"当初只认 `--done` 这个 flag ⇒ 按惯例写法的人收工后**照旧占着锁**，
#   而且连提示语都看不到（提示只在 `--done` 时打）。那不是两个 bug，
#   是**同一件事被写了两遍** —— 所以修法是抽一个 `agent_is_done()` 让两处共用。
H48d="$TMP/g48d"; mkdir -p "$H48d"
W48d() { "$PY" "$WL" --dir "$H48d" "$@"; }
W48d init --agents a --task "惯例写法也要放手" >/dev/null
W48d post --agent q --text 开工 >/dev/null
W48d lock --agent q --resource db >/dev/null 2>&1
W48d hold --agent q --seconds 600 --quiet >/dev/null 2>&1
eq "前置：q 占着 1 把锁（否则下一条是恒真的假绿）" \
   "$("$PY" -c "import json;print(len(json.load(open('$H48d/state.json')).get('locks') or {}))")" 1
out48d=$(W48d post --agent q --tag 任务完成 --text 我收工了 2>&1)
eq "★板上惯例「--tag 任务完成」（不是 --done）⇒ 0" "$?" 0
has "★惯例写法也必须当面报出放锁（旧版这里一片沉默，人根本不知道还欠手工一步）" \
    "${out48d}" "顺手放掉了 1 把锁"
z48d=$("$PY" -c "
import json
st = json.load(open('$H48d/state.json'))
print(len(st.get('locks') or {}), st['agents']['q'].get('expected_silence_until', -1))")
eq "★收工后：锁空 + 挂起窗口归零（两个入口共用同一判据）" "${z48d}" "0 0.0"
# 反向：声明过循环型的人写完成标签**不许**放锁 —— 声明优先于标签。
# 否则会出现"锁放了、状态却还是 active"的错配（等于两根轴各用一套"完成"定义）。
H48e="$TMP/g48e"; mkdir -p "$H48e"
W48e() { "$PY" "$WL" --dir "$H48e" "$@"; }
W48e init --agents a --task "循环型不许被标签放锁" >/dev/null
W48e identity set --agent gd --purpose "守板" --loop >/dev/null
W48e lock --agent gd --resource x >/dev/null 2>&1
eq "前置：gd 占着 1 把锁" \
   "$("$PY" -c "import json;print(len(json.load(open('$H48e/state.json')).get('locks') or {}))")" 1
W48e post --agent gd --tag 任务完成 --text 守护在线 >/dev/null
z48e=$("$PY" -c "
import json
st = json.load(open('$H48e/state.json'))
print(len(st.get('locks') or {}), st['agents']['gd']['status'])")
eq "★声明 --loop 的人写「任务完成」⇒ 锁仍在、状态不是 done（声明优先于标签）" "${z48e}" "1 active"

echo "[49] 喊话的时间戳：写坏格式不许被伪装成「刚刚说的」"
# 现场（2026-09-29 截图里肉眼可见）：user.md 里两条手写消息的时间戳多打了一段秒
# （`[21:37:20:20]`），USER_RE 匹配不上 ⇒ parse_user 把整行当纯文本、time 为空 ⇒
# snapshot 旧版 `ts = now()` ⇒ **这两条 21:37 的老消息在页面上显示成"刚刚说的"**，
# 而且每次刷新时间都变、永远贴在时间线末尾。另有跨零点的同一类病：只写 HH:MM:SS，
# 所以昨天 22:55 的喊话会被算成"今天 22:55"（未来 22 小时），排序整个错乱。
H49="$TMP/g49"; mkdir -p "$H49"
W49() { "$PY" "$WL" --dir "$H49" "$@"; }
W49 init --agents a --task "喊话时间戳边界" >/dev/null
# 四条手写喊话，覆盖：正常 / 多打一段秒 / 真的没带前缀 / 正文里的方括号
{
  printf '%s\n' "# 用户喊话通道"
  printf '[%s] 用户：%s\n' "09:15:00" "正常一条"
  printf '[%s] 用户：%s\n' "21:37:20:20" "多打了一段秒"
  printf '%s\n' "手写没带前缀的纯文本"
  printf '%s\n' "[重要] 这是正文里的方括号，不是时间"
} > "$H49/user.md"

eq "(a) ★坏格式被认出来（不再静默降级）" \
   "$(W49 check --readonly 2>&1 | grep -c '时间前缀解析不出来')" 1
eq "(a) json 出口也点名（驱动层能看见降级，不必去数人话输出）" \
   "$(W49 check --readonly --json | "$PY" -c "
import json,sys
print(len(json.load(sys.stdin).get('user_malformed') or []))")" 1
eq "(a) ★不多报：正文里的方括号（「[重要] …」）不许被当成格式异常" \
   "$("$PY" -c "
import sys; sys.path.insert(0,'$HERE')
import work_log as w
from pathlib import Path
print(len(w.snapshot(Path('$H49'), 90)['user_malformed']))")" 1
# ★ 核心断言：坏格式那两条的 time 必须是**空**（页面渲染成 --:--:--）。
#   旧版这里是 now() ⇒ 一条老消息伪装成"刚说的"。用「无时间的条数」比对，
#   而不是去 grep 时间字符串（后者会随当前时刻变，是条会自己变绿的假断言）。
eq "(b) ★坏格式那两条的 time 为空（页面显示「没标时间」，不是伪造的现在）" \
   "$("$PY" -c "
import sys; sys.path.insert(0,'$HERE')
import work_log as w
from pathlib import Path
us = [e for e in w.snapshot(Path('$H49'), 90)['entries'] if e.get('kind')=='user']
print(sum(1 for e in us if not e['time']), sum(1 for e in us if e['time']))")" "3 1"
eq "(c) ★坏格式的条目按**文件顺序**落位（继承上一条的时间，不跳到时间线末尾）" \
   "$("$PY" -c "
import sys; sys.path.insert(0,'$HERE')
import work_log as w
from pathlib import Path
us = [e['text'] for e in w.snapshot(Path('$H49'), 90)['entries'] if e.get('kind')=='user']
i = next(k for k, t in enumerate(us) if t.startswith('[21:37'))
print(i, len(us))")" "1 4"
eq "(d) ★user_ts 永不返回未来时刻（跨零点后不把昨天的喊话算成今天）" \
   "$("$PY" -c "
import sys, time; sys.path.insert(0,'$HERE')
import work_log as w
probes = ('00:00:01','06:30:00','12:00:00','18:45:30','23:59:59')
print([h for h in probes if w.user_ts(h)[0] > time.time() + 1])")" "[]"
eq "(d) 坏格式 ⇒ (None, False)，不猜" \
   "$("$PY" -c "
import sys; sys.path.insert(0,'$HERE')
import work_log as w
print(w.user_ts('21:37:20:20'))")" "(None, False)"
eq "(d) 正常格式 ⇒ (时间戳, True)" \
   "$("$PY" -c "
import sys; sys.path.insert(0,'$HERE')
import work_log as w
ts, ok = w.user_ts('09:15:00')
print(ok, ts > 0)")" "True True"
eq "(e) 对照：干净看板不该报任何格式异常（否则提示会变成噪音被无视）" \
   "$(W49 check --readonly --json | "$PY" -c "
import json,sys
d = json.load(sys.stdin)
print(len([m for m in (d.get('user_malformed') or []) if '方括号' in m or '没带前缀' in m]))")" 0

# ---- 50 驱动脚本不许给用户弹浏览器 ------------------------------------------
# 2026-09-30 实测发现：`examples/demo.sh` 一个 `WORK_LOG_*` 都没设，而它让 3 个 agent
# 同时写心跳 ⇒ 每跑一次演示就触发 `post` 的 auto_ui，后果三件：
#   ① 弹一个浏览器窗口；② 留下一只**脱离父进程**的 serve，指着演示随后 `rm -rf`
#   掉的临时板；③ 占住 8788 起的端口。本机实测积了 2 只（占 8789/8790）。
# 严重性在于**演示脚本是给外人跑的** —— 对方机器上会凭空多出进程和端口占用。
# 判据做成**结构性**的（不写文件名）：扫到的脚本里，凡出现 `post --agent` 的，
# 必须同时出现 `WORK_LOG_NO_AUTO_UI` —— 新增驱动脚本自动进护栏。
echo "[50] 驱动脚本不许触发自动协作 UI（会弹浏览器 + 留脱离父进程的 serve）"
r50=$("$PY" - "$HERE"/*.sh "$HERE"/../examples/*.sh <<'PY'
import os, sys
files = sorted({f for f in sys.argv[1:] if os.path.isfile(f)})
posters = []
for f in files:
    src = open(f, encoding="utf-8", errors="replace").read()
    # ★ 只看**没被注释掉**的行。这条判据是反向验证打出来的：第一版直接搜全文，
    # 于是把 `# export WORK_LOG_NO_AUTO_UI=1`（开关被注释掉了）也读成"已经关了"，
    # 反向测试当场**没红** —— 正是「字面命中 ≠ 语义残留」。
    live = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    if "post --agent" in live:                # 会自己 post = 可能触发 auto_ui
        posters.append((f, "WORK_LOG_NO_AUTO_UI=" in live))
print("FILES=" + str(len(files)))
print("POSTERS=" + str(len(posters)))
for f, guarded in posters:
    print(("OK   " if guarded else "LEAK ") + f)
PY
)
n50f=$(printf '%s\n' "$r50" | sed -n 's/^FILES=//p')
n50p=$(printf '%s\n' "$r50" | sed -n 's/^POSTERS=//p')
n50l=$(printf '%s\n' "$r50" | sed -n 's/^LEAK //p' | grep -c . || true)
printf '    会自己 post 的脚本 %s 个（扫描面 %s 个）：\n' "${n50p:-?}" "${n50f:-?}"
printf '%s\n' "$r50" | sed -n -e 's|^OK .*/|    OK   |p' -e 's|^LEAK .*/|    LEAK |p'
# ⚠ 上面别改回 `\(OK\|LEAK\)` 那种交替：macOS 自带的是 BSD sed，BRE 不支持 `\|`，
# 会**静默不匹配** —— 实测三条文件一个都没打出来，而断言全绿（信息缺失不报错，最难发现）。
printf '    （OK = 已关掉自动 UI；LEAK = 会在用户机器上弹浏览器）\n'
# (a) 前置：扫描面里必须真的存在"会 post 的脚本" —— 否则下面那条断言是空集恒真（[30g] 的教训）。
if [ "${n50p:-0}" -ge 1 ]; then
  ok "(a) 判据不落在空集上（扫到 ${n50p} 个会自己 post 的脚本）"
else
  ng "(a) 没扫到任何会 post 的脚本 —— 这条护栏已退化成恒真"
fi
# (b) 结构性：每个会 post 的脚本都必须显式关掉自动 UI。
eq "(b) 会 post 的脚本都显式关掉了自动 UI（缺了会点名）" "${n50l:-0}" 0
[ "${n50l:-0}" -eq 0 ] || printf '    ↑ 缺 WORK_LOG_NO_AUTO_UI：%s\n' "$(printf '%s\n' "$r50" | sed -n 's/^LEAK //p' | tr '\n' ' ')"

# ---- 汇总 -----------------------------------------------------------------
echo
echo "----------------------------------------"
# 半路换文件必须自曝，否则"绿"是个假象（见头部 src_fp 的注释）
if [ "$(src_fp)" != "$FP0" ]; then
  echo "⚠ 测试期间 scripts/ 里的源文件指纹变了 —— 本次结果不可信（前段读旧文件、后段读新文件）。"
  echo "  重跑一次；要改代码就等这轮结束。"
  FAIL=$((FAIL+1))
  NOTES="${NOTES}
    - 测试期间源文件被改动，本次结果作废"
fi
echo "通过 $PASS · 失败 $FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '失败项:%s\n' "$NOTES"
  exit 1
fi
echo "全部通过"
exit 0
