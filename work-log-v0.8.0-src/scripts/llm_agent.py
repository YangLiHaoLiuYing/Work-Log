#!/usr/bin/env python3
"""用真实 LLM 驱动一个遵守 work-log 协议的 agent —— 补上 skill 最缺的那块验证。

selftest.sh 只证明「CLI 本身没坏」，证明不了「真实模型会不会照协议走」。
这个 runner 把 work-log 的动词暴露成工具，让模型自己决定 post 什么、问谁、等谁，
然后把它说的每一句话落到真实看板上。

    # 起一个 agent
    python3 llm_agent.py --agent agent1 --board /tmp/demo/log \
        --base https://host/api/v1 --key "$KEY" --model DeepSeek-V4.1-Flash \
        --role "调用方" --task "对齐 tts_speak 接口"

    # 通常两个一起跑（各自一个进程）
    python3 llm_agent.py --agent agent2 ... --role "合成端" &

零依赖（标准库）。退出码 0 = 正常收工，1 = 没走完（看日志）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WL = HERE / "work_log.py"


# --------------------------------------------------------------------------- 工具定义

def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {"type": "function", "function": {
        "name": "post",
        "description": "往共享看板写一条心跳。每完成一个动作都要写一次；正文要能自证"
                       "（遇到什么、打算做什么、卡在哪），禁止写「工作中」这类空话。",
        "parameters": _obj({
            "text": {"type": "string", "description": "想了什么/干了什么，具体一点"},
            "tag": {"type": "string", "enum": ["收到", "决定", "执行", "阻塞", "建议", "任务完成"],
                    "description": "标签，默认 执行"},
        }, ["text"])}},
    {"type": "function", "function": {
        "name": "ask",
        "description": "定向提问另一个 agent（不是广播，只有它能回答）。"
                       "返回的编号 #N 要留着给 await/reply 用。",
        "parameters": _obj({
            "to": {"type": "string", "description": "对方的 agent 名字"},
            "text": {"type": "string", "description": "问题，要具体、一次问清"},
        }, ["to", "text"])}},
    {"type": "function", "function": {
        "name": "await",
        "description": "阻塞等待提问被回答。id 可以给多个（如 \"1,2,3\"）：默认要全部，"
                       "加 any=true 则任一回应即算达成。真拿到回答=成功；超时=对方没回；"
                       "若提示「对方已收工」说明它永远不会回了。这些情况都**绝不能编造"
                       "对方的回答**——超时/对方已收工时，请依据已有信息自己拍板。",
        "parameters": _obj({
            "id": {"type": "string", "description": "提问编号；多个用逗号分隔，如 \"1,2\""},
            "any": {"type": "boolean", "description": "多路等待时：任一回应即算达成（默认全部）"},
            "timeout": {"type": "integer", "description": "最多等多少秒，建议 60~120"},
        }, ["id"])}},
    {"type": "function", "function": {
        "name": "reply",
        "description": "回答别人对你的提问。只有被问的那一方能答，且不能重复答。",
        "parameters": _obj({
            "id": {"type": "integer", "description": "提问编号"},
            "text": {"type": "string", "description": "你的答复"},
        }, ["id", "text"])}},
    {"type": "function", "function": {
        "name": "brief",
        "description": "查看「自你上次读过之后」的新动态：别人的心跳、用户喊话、待你回应的提问。"
                       "每次 post 之后就该调一次。",
        "parameters": _obj({}, [])}},
    {"type": "function", "function": {
        "name": "read_user",
        "description": "查看用户有没有喊话（会给出喊话编号）。",
        "parameters": _obj({}, [])}},
    {"type": "function", "function": {
        "name": "ack_user",
        "description": "认领一条用户喊话，告诉用户有人在管了。",
        "parameters": _obj({
            "id": {"type": "integer", "description": "read_user 给出的喊话编号"},
            "text": {"type": "string", "description": "你改了什么"},
        }, ["id", "text"])}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "确认全部工作完成时调用。它会替你写一条「任务完成」心跳。",
        "parameters": _obj({
            "summary": {"type": "string", "description": "一句话说清最终结论"},
        }, ["summary"])}},
]


def system_prompt(agent: str, role: str, task: str, peer: str, board: str) -> str:
    return f"""你是 {agent}，在和一个叫 {peer} 的 agent 通过共享心跳看板协作。
你的角色：{role}。看板目录：{board}
总任务：{task}

# 铁律
1. **每做完一个动作，立刻用 post 写一条心跳。** 写清你遇到什么、打算做什么、卡在哪。
   禁止写「工作中」「处理中」这类无信息量的内容——那是骗看门狗。
2. **要确认对方的事情，必须用 ask 问，再用 await 等回答。**
   await 返回「超时」或「对方已收工」就如实说，**绝对不许编造对方的回答**。
   你没用工具读到的内容，就不存在。对方已收工时，自己拍板往下做。
3. **每次 post 之后调一次 brief**，看看别人说了什么、用户有没有喊话、有没有人问你问题。
   有「待你回应 #N」就用 reply 正面回答。
4. 用户可能随时插话。read_user 看到喊话就用 ack_user 认领，并在后续动作里遵守。
5. **如果用户的要求和你已经定下的结论冲突**：不许沉默忽略，也不许嘴上答应却什么都不改。
   用 post 打「阻塞」标签，写清「用户要求 X / 当前约束 Y / 冲突点 / 理由」，
   然后 ask 对方协商折中，再定最终版。最终结论要显式说明：哪部分满足了、哪部分做不到、替代方案是什么。
6. 全部做完，用 finish 收工（它会替你写「任务完成」）。

# 动作顺序（照这个走，别自己发挥顺序）
1. 用 post(tag="收到") 说明你是谁、负责什么。
2. 用 ask 向 {peer} 提一个你**真的不知道答案**的具体问题
   （比如接口字段、格式、取值范围）。问题要具体，一次问清。
3. 用 await 等它的回答。返回超时就如实记「等 {peer} 超时，未收到回应」，**不许编造它说了什么**。
4. 拿到真实回答后，用 post(tag="决定") 定下结论——结论里要能看出你是**读了它的回答**才定的。
5. 用 brief 看 {peer} 有没有反过来问你（会出现「待你回应 #N」）。
   有就用 reply 正面回答；如果暂时没有，就 post 一条进展再 brief 一次。
6. 用 read_user 看用户有没有喊话；有就用 ack_user 认领，并按用户要求调整。
7. 如果用户的要求和你已定的结论冲突：用 post(tag="阻塞") 写清
   「用户要求 X / 当前约束 Y / 冲突点 / 理由」，然后 ask {peer} 协商折中，再定最终版。
   最终结论要显式说明：哪部分满足了、哪部分做不到、替代方案是什么。
8. 全部做完，用 finish 收工。

# 现在开始
先 post 你的「收到」，然后按上面 1~8 走。不要等别人先动，也不要跳过 ask/await 直接下结论。"""


# --------------------------------------------------------------------------- HTTP

def chat(base: str, key: str, payload: dict, timeout: int = 300, retries: int = 4):
    """返回 (message_dict, usage_dict) 或抛异常。自带重试——自建网关经常冷启动/无可用 worker。"""
    last = None
    for i in range(retries):
        req = urllib.request.Request(
            f"{base.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            return d["choices"][0]["message"], d.get("usage", {}) or {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {e.code}: {body}"
            # 503 no_available_workers / 429 值得重试，别的直接失败
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(last) from None
        except Exception as e:                    # noqa: BLE001 - 超时/连接都可能
            last = f"{type(e).__name__}: {e}"
        if i < retries - 1:
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"重试 {retries} 次仍失败 -> {last}")


# --------------------------------------------------------------------------- 工具执行

def run_wl(board: str, args: list[str], timeout: float = 900) -> tuple[int, str]:
    """跑一条 work-log 命令。

    timeout 必须**大于模型自己请求的等待时间**：模型调 `await --timeout 120` 时，
    如果我们这里也只给 120s，子进程会在 await 即将返回的那一刻被我们亲手杀掉，
    抛出没人接的 TimeoutExpired，直接把 agent 干死（实测踩到，agent1 挂在第 3 步）。
    """
    try:
        p = subprocess.run([sys.executable, str(WL), "--dir", board, *args],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, (f"（驱动层强制中断：这条命令跑了超过 {timeout:g}s 还没结束。"
                     f"注意这不是协议层的超时判断，不要据此断言对方没回应。）")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


WRAPPER_KEYS = ("arguments", "input", "params", "args")


def _required_of(tool_name: str) -> list[str]:
    """该工具声明的必需字段。用来在"参数被套壳"时把真参数找回来。"""
    for t in TOOLS:
        if t["function"]["name"] == tool_name:
            return list(t["function"]["parameters"].get("required", []) or [])
    return []


def _parse_args(raw, tool_name: str = "") -> tuple[dict, str]:
    """把模型给的 tool_calls.arguments 解析成 dict。

    模型（尤其小模型）经常把参数包成多余的壳，且**壳的值可能是字符串也可能是对象**：
    值是 JSON 字符串（"{\"arguments\": \"{...}\"}"）和值是对象（{"arguments": {...}}）都见过。
    只剥字符串那种、不剥对象那种，就会得到一个 id 丢失的 dict，
    于是 `await` 退化成 `--id 0`（= "等任何动静"）并**返回成功**——
    提问者会以为自己拿到了答案，其实只看到一条无关心跳。这是最危险的一种静默失败。
    所以这里三管齐下：① 剥 str/dict 两种壳；② 再按该工具声明的 required 字段，
    在包裹键里把真参数找回来；③ 最后校验必需字段，缺了就明确报错、交回模型重试。
    """
    if isinstance(raw, dict):
        d = raw
    else:
        try:
            d = json.loads(raw or "{}")
        except Exception:                          # noqa: BLE001
            return {}, f"参数不是合法 JSON：{str(raw)[:120]!r}"

    req = _required_of(tool_name)
    saw_wrapper = False
    for _ in range(4):
        if not isinstance(d, dict):
            break
        moved = False
        # ① 唯一的键就是包裹名：值是 str 或**对象**都要剥。
        #    只剥 str 那种是踩过的坑：模型给 {"arguments": {"id": 1, ...}} 时
        #    id 会丢，await 静默退化成 --id 0（= "等任何动静"）并返回成功。
        if len(d) == 1:
            k, v = next(iter(d.items()))
            if k in WRAPPER_KEYS:
                saw_wrapper = True
                inner = v
                if isinstance(v, str):
                    try:
                        inner = json.loads(v)
                    except Exception:              # noqa: BLE001
                        inner = None
                if isinstance(inner, dict) and inner:
                    d, moved = inner, True
        # ② 该工具必需的字段不在顶层、却整包待在某个包裹键里 → 取出来
        if not moved and req and not all(k in d for k in req):
            for k in WRAPPER_KEYS:
                v = d.get(k)
                if isinstance(v, dict) and all(x in v for x in req):
                    d, moved = v, True
                    break
        if not moved:
            break

    if not isinstance(d, dict):
        return {}, f"参数解析后不是对象：{str(d)[:120]!r}"
    if req:
        missing = [k for k in req if k not in d]
        if missing:
            hint = ("你疑似把参数又套了一层 JSON 字符串（形如 "
                    '"{\\"arguments\\": \\"{...}\\"}"），而且那一层的 JSON 不合法'
                    "（常见原因：文本太长被 max_tokens 截断、或引号/换行没转义）。"
                    if saw_wrapper else "")
            return d, (f"缺少必需参数 {missing}"
                       f"（收到的参数是 {json.dumps(d, ensure_ascii=False)[:140]}）。"
                       f"{hint}请把参数**直接放在顶层**重新调用 {tool_name}，"
                       f"不要再包一层。")
    return d, ""


def _num(v, default: float) -> float:
    """模型给的数字常带壳：可能是 "60"、"60s"、None 或 ''。
    这里绝不能抛异常——驱动层为了一个参数的格式问题把 agent 弄死，
    正是上面那条 TimeoutExpired 事故的同类错误。"""
    try:
        return float(str(v).strip().rstrip("sS秒"))
    except (TypeError, ValueError):
        return default


def _ids_of(v) -> list:
    """把模型给的编号参数解析成列表：1 / "1" / "1,2,3" / [1,2] 都要认。

    真机里模型会写 "1,2" 也会写 [1,2]，两种都得进得去；而"编号丢了"
    （None / "" / 无法解析）必须留下**空列表**，让 _need_id 拦住 ——
    绝不能兜底成某个永远存在的默认值。
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        parts = [str(x) for x in v]
    else:
        parts = str(v).split(",")
    out = []
    for p in parts:
        p = p.strip().lstrip("#").rstrip("号")
        if not p:
            continue
        try:
            out.append(int(float(p)))
        except ValueError:
            return []
    return out


def _one_id(args: dict) -> int:
    """只处理一个编号的命令（reply / ack_user）用它取编号。"""
    ids = _ids_of(args.get("id"))
    return ids[0] if ids else 0


def _need_id(name: str, args: dict) -> str:
    """await / reply / ack_user 都必须在**具体的编号**上操作。

    为什么这条要单独拦：`await --id 0` 曾经是合法语义（等任何新动态）且**返回成功**。
    所以一旦编号在参数解析中丢掉、我们照传 0，提问者会收到一个"成功"，
    以为自己拿到了答案 —— 而它其实只看到一条路过的无关心跳，然后基于这个假象继续
    往下做。这个静默失败比报错危险得多。（现在 work_log.py 自己也拒绝 `--id 0` 了，
    这是双保险：驱动层拦住，CLI 层也不接受。）
    """
    ids = _ids_of(args.get("id"))
    if not ids or any(n < 1 for n in ids):
        return (f"✗ {name} 需要一个**具体的编号**（正整数；多个用逗号分隔），"
                f"你这次传的是 {args.get('id')!r}。"
                f"编号来自 ask 的输出或 brief 里的「待你回应 #N」；"
                f"不要用 0。请重新调用 {name} 并给出正确的编号。")
    if name != "await" and len(ids) != 1:
        return (f"✗ {name} 一次只能处理一个编号，你传了 {args.get('id')!r}。"
                f"请只给一个编号（只有 await 支持 1,2,3 这种多路等待）。")
    return ""


def run_tool(board: str, agent: str, name: str, args: dict, log) -> tuple[str, bool]:
    """执行一个工具调用，返回 (给模型看的文本, 是否结束)。"""
    if name == "post":
        text = str(args.get("text", "") or "").strip()
        if not text:
            return ("✗ post 需要非空 text。你这次没给出 text（可能是参数写错了）。"
                    "请重新调用 post，并把要写的内容放进 text 字段。"), False
        rc, out = run_wl(board, ["post", "--agent", agent, "--text", text,
                                 "--tag", str(args.get("tag", "执行"))])
    elif name == "ask":
        if not str(args.get("to", "") or "").strip():
            return "✗ ask 需要 to（对方的 agent 名字）。请重新调用。", False
        rc, out = run_wl(board, ["ask", "--agent", agent,
                                 "--to", str(args.get("to", "")),
                                 "--text", str(args.get("text", ""))])
    elif name == "await":
        bad = _need_id(name, args)
        if bad:
            return bad, False
        wish = _ids_of(args.get("id"))
        any_mode = bool(args.get("any") or args.get("any_mode"))
        want = max(5.0, min(_num(args.get("timeout"), 90), 600.0))
        cmd = ["await", "--agent", agent, "--id", ",".join(str(i) for i in wish),
               "--timeout", str(int(want))]
        if any_mode and len(wish) > 1:
            cmd.append("--any")
        # 永远给自己留足余量，别把 await 本身超时当成"对方没回"
        rc, out = run_wl(board, cmd, timeout=want + 90)
        if rc == 1:
            out += "\n【重要】这是**超时**：对方还没回答，你什么都没收到。不要假设它说了什么。"
        elif rc == 3:
            out += ("\n【重要】对方**已经收工**，这个答案不会来了 —— 你没有白等。"
                    "不要假设它同意了什么；请依据已有信息自己拍板，或换一个还在线的 agent 问。")
        elif rc == 124:
            out += "\n【重要】这次连等待都没跑完（驱动层中断）。不能据此说对方没回应。"
    elif name == "reply":
        bad = _need_id(name, args)
        if bad:
            return bad, False
        text = str(args.get("text", "") or "").strip()
        if not text:
            return ("✗ reply 需要非空 text。请重新调用 reply，把答复放进 text 字段。"), False
        rc, out = run_wl(board, ["reply", "--agent", agent,
                                 "--id", str(_one_id(args)), "--text", text])
    elif name == "brief":
        rc, out = run_wl(board, ["brief", "--agent", agent])
    elif name == "read_user":
        rc, out = run_wl(board, ["read-user", "--agent", agent])
    elif name == "ack_user":
        bad = _need_id(name, args)
        if bad:
            return bad, False
        text = str(args.get("text", "") or "").strip()
        if not text:
            return "✗ ack_user 需要非空 text。请重新调用。", False
        rc, out = run_wl(board, ["ack-user", "--agent", agent,
                                 "--id", str(_one_id(args)), "--text", text])
    elif name == "finish":
        rc, out = run_wl(board, ["post", "--agent", agent,
                                 "--text", str(args.get("summary", "完成") or "完成"),
                                 "--tag", "任务完成"])
        log(f"[finish] {args.get('summary', '')}")
        return f"已收工：{out.strip()}", True
    else:
        return f"未知工具 {name}", False
    log(f"[{name}] rc={rc} {out.strip()[:150]}")
    if rc == 4:
        out += ("\n【重要】这是**通信熔断**：机制判定你和同一个对端已经陷入无进展的"
                "互相确认（或心跳刷得太密），已经替你停下来。请立刻收敛："
                "post --tag 决定 写清「结论是什么、接下来谁做什么」，然后往下做。"
                "只有你确认必须继续深挖时才加 --force。")
    elif rc == 2:
        # 这一档以前和 1 混在一起，结果"我把编号写错了"被读成"对方不配合"。
        # 现在它是纯粹的用法错误：改参数重试即可，**不能**据此推断对方的态度。
        out += ("\n【重要】这是**参数/前置条件错误**（退出码 2），不是对方不配合。"
                "常见原因：编号不存在或不是你问的、文本为空、问了多条却漏了编号。"
                "请读上面的提示改正后重试；不要把它当成「对方没回应」。")
    elif rc == 70:
        out += ("\n【重要】这是**工具内部错误**（退出码 70），机制本身坏了。"
                "不要把它当成对方没回、也不要假装没发生：请把这条错误如实上报，"
                "或换一种方式继续（例如先 post 一条心跳说明自己卡在工具上）。")
    return out.strip()[:3000] or "(无输出)", False


# --------------------------------------------------------------------------- 主循环

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--board", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--peer", default="agent2")
    ap.add_argument("--role", default="协作者")
    ap.add_argument("--task", default="")
    ap.add_argument("--max-steps", type=int, default=18)
    ap.add_argument("--max-tokens", type=int, default=4000,
                    help="推理模型要把思考也算进去，给小了会被截断成空 content")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--log", default="")
    a = ap.parse_args()

    logf = open(a.log, "a", encoding="utf-8") if a.log else sys.stderr

    def log(msg: str) -> None:
        line = f"[{a.agent} {time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if a.log:
            logf.write(line + "\n")
            logf.flush()

    try:
        return _run(a, log)
    finally:
        # 退出前清掉自己最后一次 hold 留下的静默窗口。
        # 不清的话，一个**已经退出**的 agent 会以「挂起中」的健康面貌继续挂着
        # 最多一个窗口那么久 —— 看板上"看上去健康"和"真的活着"就分不开了。
        run_wl(a.board, ["release", "--agent", a.agent, "--quiet"])


def _run(a, log) -> int:
    messages = [
        {"role": "system", "content": system_prompt(a.agent, a.role, a.task, a.peer, a.board)},
        {"role": "user", "content": "开始。先 post 你的「收到」。"},
    ]
    nudges, usage_total, t_start = 0, {}, time.time()

    for step in range(1, a.max_steps + 1):
        # 每次调模型前先"静默挂起"：模型思考期间我们发不出任何心跳，
        # 而真 LLM 单轮 30~120s，默认 45s 的看门狗会把正常思考判成卡死。
        # 用 --quiet 是为了不往看板塞「进入长任务」这种垃圾条目。
        run_wl(a.board, ["hold", "--agent", a.agent, "--quiet",
                         "--seconds", str(int(a.timeout) + 60)])
        try:
            msg, usage = chat(a.base, a.key, {
                "model": a.model, "messages": messages,
                "tools": TOOLS, "tool_choice": "auto",
                # 推理模型（GLM-5.3 这类）会先把 token 烧在思考上：
                # max_tokens 给小了会 finish_reason=length 且 content 为空，
                # 看起来像"模型不听话"，其实是被截断了。
                "max_tokens": a.max_tokens,
            }, timeout=a.timeout)
        except Exception as e:                    # noqa: BLE001
            log(f"✗ 第 {step} 步调用失败: {e}")
            return 1
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                usage_total[k] = usage_total.get(k, 0) + v

        tcs = msg.get("tool_calls") or []
        clean = {"role": "assistant", "content": msg.get("content") or ""}
        if tcs:
            clean["tool_calls"] = tcs
        messages.append(clean)

        if not tcs:
            nudges += 1
            log(f"step {step}: 模型没调工具 -> {repr((msg.get('content') or '')[:120])}")
            if nudges >= 3:
                log("连续 3 次不调工具，判定卡住，退出")
                return 1
            messages.append({"role": "user",
                             "content": "你没有调用任何工具。请继续：用 post 记录进展，"
                                        "或调用 finish 结束。必须调工具。"})
            continue
        nudges = 0

        for tc in tcs:
            fn = tc.get("function", {})
            targs, err = _parse_args(fn.get("arguments"), fn.get("name", ""))
            if err:
                log(f"step {step}: {fn.get('name')} 参数解析失败 -> {err}")
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": f"✗ {err}\n请用正确的 JSON 对象重新调用 "
                                            f"{fn.get('name')}，参数直接放在顶层（不要再套一层 "
                                            f'"arguments"）。'})
                continue
            log(f"step {step}: {fn.get('name')}({json.dumps(targs, ensure_ascii=False)[:110]})")
            out, done = run_tool(a.board, a.agent, fn.get("name", ""), targs, log)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out})
            if done:
                log(f"✓ 收工 (共 {step} 步, {time.time()-t_start:.0f}s, tokens={usage_total})")
                return 0

    log(f"△ 达到 max-steps={a.max_steps} 仍未收工")
    return 1


if __name__ == "__main__":
    sys.exit(main())
