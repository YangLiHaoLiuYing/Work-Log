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
    # 页面自身零外部依赖（本地绑定/离线可用）：不许引 CDN 样式或脚本
    "https://" not in html and "<link " not in html and "<script src" not in html,
]
print("PASS" if all(checks) else "FAIL " + str(checks))
PY
)
eq "HTTP 接口与页面都正常（${probe}）" "$probe" "PASS"
has "serve 打印访问地址" "$(cat "$D5/serve.out")" "http://localhost:$PORT/"
kill $BG_PIDS 2>/dev/null; BG_PIDS=""
rm -rf "$D5"

# ---- 21 默认目录在桌面 ----------------------------------------------------
echo "[21] 默认日志目录（桌面可见 + 按项目隔离）"
defdir=$(cd /tmp && "$PY" -c "
import sys; sys.path.insert(0, '$HERE')
import work_log; print(work_log.default_log_dir())")
has "默认目录落在桌面" "$defdir" "/Desktop/"
has "目录名含 work-log" "$defdir" "work-log"
has "目录名含项目名(tmp)" "$defdir" "/tmp"

# 隔离性：两个不同项目的看板必须是两个目录。
# 共用一块看板会让甲项目的看门狗去报乙项目 agent 的「卡死」——必须挡住。
# 隔离目录必须放在 $TMP 下：放在 /tmp 这种全局路径上，两个套件实例并发时
# A 的 rmdir 会删掉 B 正在 cd 的目录，Path.cwd() 直接 FileNotFoundError。
mkdir -p "$TMP/wl_iso_a" "$TMP/wl_iso_b"
iso_a=$(cd "$TMP/wl_iso_a" && "$PY" -c "
import sys; sys.path.insert(0, '$HERE')
import work_log; print(work_log.default_log_dir())")
iso_b=$(cd "$TMP/wl_iso_b" && "$PY" -c "
import sys; sys.path.insert(0, '$HERE')
import work_log; print(work_log.default_log_dir())")
if [ "$iso_a" = "$iso_b" ]; then ng "不同项目应落到不同目录  都得到[$iso_a]"; else ok "不同项目落到不同目录"; fi
hasnt "隔离目录不含对方项目名" "$iso_a" "wl_iso_b"
# 反向断言：两个目录都必须是**非空**的真实路径，
# 否则上面那句"不同"在两个都为空时也会假绿（压测里真的出现过 traceback 被吞掉）。
if [ -n "$iso_a" ] && [ -n "$iso_b" ]; then ok "两个默认目录都解析成功"; else ng "默认目录解析被吞（cwd 失效？）"; fi
rmdir "$TMP/wl_iso_a" "$TMP/wl_iso_b" 2>/dev/null

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
  "$PY" -c "
import re, json, time
s = open('$Q/board.md', encoding='utf-8').read()
s = re.sub(r'(<boss>\s+)\d\d:\d\d:\d\d', r'\g<1>00:00:07', s)
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
has "挂起窗口过期后照样判卡死" "$(QW status --json --stale-after 1 2>/dev/null)" '疑似卡死'
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
# 必须另起一个干净目录：A 阶段留下的 a1 还在(过期)等待，会把险情串进来。
HZ2="$(mktemp -d)"
Q1=$("$PY" "$WL" --dir "$HZ2" init --task "险情B" --agents b1,b2 >/dev/null; \
     "$PY" "$WL" --dir "$HZ2" post --agent b1 --text "开工" >/dev/null; \
     "$PY" "$WL" --dir "$HZ2" post --agent b2 --text "开工" >/dev/null; \
     "$PY" "$WL" --dir "$HZ2" ask --agent b1 --to b2 --text "B1 问 B2" | grep -oE '提问 #[0-9]+' | tr -dc '0-9')
Q2=$("$PY" "$WL" --dir "$HZ2" ask --agent b2 --to b1 --text "B2 问 B1" | grep -oE '提问 #[0-9]+' | tr -dc '0-9')
eq "B 两个提问各自编号" "$Q1-$Q2" "1-2"
"$PY" "$WL" --dir "$HZ2" await --agent b1 --id "$Q1" --timeout 60 --interval 1 >/dev/null 2>&1 &
B1P=$!
"$PY" "$WL" --dir "$HZ2" await --agent b2 --id "$Q2" --timeout 60 --interval 1 >/dev/null 2>&1 &
B2P=$!
sleep 3
o=$("$PY" "$WL" --dir "$HZ2" check --stale-after 90 --readonly)
has "B 认出互相等待" "$o" "互相等待"
has "B 点明这是一条真死锁" "$o" "真死锁"
hasnt "B 不把死锁误判成不可达等待" "$o" "不可达等待"
"$PY" "$WL" --dir "$HZ2" check --stale-after 90 --readonly >/dev/null; eq "B 真死锁时 check 退出码 1" $? 1
# 一方先答 → 环被打破 → await 退出必须撤掉等待标记（否则看板长期显示"它在等"）
"$PY" "$WL" --dir "$HZ2" reply --agent b2 --id "$Q1" --text "我先答你" >/dev/null
sleep 3
o=$("$PY" "$WL" --dir "$HZ2" status --stale-after 90 --json | "$PY" -c \
     'import json,sys; print({a["name"]: a["waiting_on"] for a in json.load(sys.stdin)["agents"]})')
has "B 收到回应后 b1 的等待标记被撤掉" "$o" "'b1': None"
hasnt "B 环破了之后不再报险情" "$("$PY" "$WL" --dir "$HZ2" check --stale-after 90 --readonly)" "互相等待"
# 反向：险情消失后必须回到 0，否则"退 1"就变成了永久噪声
"$PY" "$WL" --dir "$HZ2" check --stale-after 90 --readonly >/dev/null; eq "B 险情消失后 check 回到 0" $? 0
kill "$B1P" "$B2P" 2>/dev/null; wait "$B1P" "$B2P" 2>/dev/null
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
has "并按它自己的日期判卡死（时间戳没被今天覆盖）" \
    "$(printf '%s\n' "$o" | grep '^old1')" "疑似卡死"
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

# ---- 30 多人协作（启动条件：≥2 个 agent 在干活） ---------------------------
echo "[30] 多人协作感知"
D30="$TMP/g30"
C30(){ "$PY" "$WL" --dir "$D30" "$@"; }
C30 init --task "协作测试" >/dev/null
C30 post --agent solo --text "一个人先开工" >/dev/null
j30=$(C30 status --json)
eq "(a) 单人在干活 → count 1" "$(printf '%s' "$j30" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["count"])')" 1
eq "(a) 单人未达协作 → active false" "$(printf '%s' "$j30" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["active"])')" "False"
o30=$(C30 post --agent mate --text "第二个人也开工")
has "(b) 第二个 agent 开工时 post 提示协作" "$o30" "🤝 多人协作中"
j30=$(C30 status --json)
eq "(c) 双人在干活 → count 2" "$(printf '%s' "$j30" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["count"])')" 2
eq "(c) 双人达成启动条件 → active true" "$(printf '%s' "$j30" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["collab"]["active"])')" "True"
has "(d) status 人类可读行显示协作模式" "$(C30 check --readonly)" "多人协作模式"
C30 check >/dev/null                        # 非只读扫描：emit 写切换事件
C30 check >/dev/null                        # 再扫一次：事件不重复
eq "(e) 协作开启事件只写一次" "$(grep -c '协作模式开启' "$D30/board.md")" 1
C30 post --agent solo --done --text "收工" >/dev/null
C30 post --agent mate --done --text "收工" >/dev/null
C30 check >/dev/null
has "(f) 全员收工后写协作结束" "$(cat "$D30/board.md")" "多人协作模式结束"
eq "(g) 收工后可重新触发（标记已复位）" "$(grep -c '协作模式结束' "$D30/board.md")" 1

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
C32 init --task "自动UI" >/dev/null
C32 post --agent a1 --text 开工 >/dev/null
o32=$(C32 post --agent a2 --text 也开工)
has "(a) 第 2 个 agent 开工自动起协作界面" "$o32" "🖥 协作界面已自动启动"
has "(a) 提示未弹浏览器（WORK_LOG_NO_UI 生效）" "$o32" "未弹浏览器"
SPID32=$("$PY" -c "import json;print(json.load(open('$D32/state.json')).get('ui_pid',''))")
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

# ---- 汇总 -----------------------------------------------------------------
echo
echo "----------------------------------------"
echo "通过 $PASS · 失败 $FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '失败项:%s\n' "$NOTES"
  exit 1
fi
echo "全部通过"
exit 0
