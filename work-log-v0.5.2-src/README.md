<div align="center">

<img src="assets/banner.svg" alt="work-log — heartbeat board, watchdog and directed Q&A for multi-agent work" width="100%">

[![Python 3.9+](https://img.shields.io/badge/python-3.9%20%7C%203.13-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![Zero deps](https://img.shields.io/badge/dependencies-0-2ea44f)](#requirements)
[![Assertions](https://img.shields.io/badge/assertions-290%20passing-2ea44f)](docs/VALIDATION.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![English](https://img.shields.io/badge/docs-English-1f6feb)](#quick-start) [![中文](https://img.shields.io/badge/docs-中文-6e7781)](README.md)

**When several agents work in parallel, let them see each other, question each other, and notice when one has stopped.**

`work-log` is a **shared plain-text board** + a **watchdog** + a **directed Q&A channel**.
Standard library only, no third-party dependencies, no network, no changes to your codebase.

</div>

---

## Three failure modes it catches

| Failure mode | What it looks like | How work-log catches it |
|---|---|---|
| **Silent stall** | The agent process is alive but hasn't moved in 10 minutes. You only find out by asking. | Scans the board every 15s. Silent past the threshold **and** without writing "task done" → flagged, alert written for a live agent to take over. |
| **The all-green deadlock** | Two agents asked each other questions and are now **both waiting for the other's answer**. Both heartbeats are green (waiting auto-refreshes liveness), so a watchdog sees nothing — but the team is already dead. | **Wait graph**: `await` records the waiting edge into shared state, which lets it distinguish `[unreachable wait]` (peer already finished — the answer will never come) from `[mutual wait]` (a real deadlock). |
| **Budget burned on politeness** | Two agents ping-pong "ok", "got it", "sounds good" a dozen times; or one agent loops and spams heartbeats. | **Communication breaker**: warns at 6 consecutive alternating rounds, hard-refuses at 12. Heartbeat budget of 60/min — over budget is refused **and does not refresh liveness**, so the same agent also gets flagged as spinning. |

> The third one is especially nasty: **on the board these look like the two healthiest agents** —
> one is chatting the most, the other is posting the most. In reality one is burning tokens
> and the other is burning a loop.

## See it first (no install, no commands)

Open **[`docs/preview.html`](docs/preview.html)** — a single file, no dependencies, works offline.
It turns the **real output** of the four scenarios below into diagrams:

> watchdog catches a silent stall · wait graph catches the "all-green deadlock" ·
> circuit breaker stops the ping-pong · multi-way `await` resolves on the first reply

(GitHub does not render HTML files — clone the repo and open it in a browser,
or serve it via GitHub Pages yourself.)

## See it in 60 seconds

No API key, no model, no files left in your project:

```bash
git clone https://github.com/YangLiHaoLiuYing/Work-Log.git /tmp/work-log
bash /tmp/work-log/examples/demo.sh
```

(Already have the code locally? Skip the clone and just run `bash examples/demo.sh`. Takes ~20s.)

The demo reproduces all three failure modes with real CLI output (watchdog catches a stall →
wait graph catches a deadlock → breaker stops the ping-pong → multi-way `await`).
Full captured output: [`examples/demo-output.txt`](examples/demo-output.txt).

## Quick start

```bash
WL="$PWD/scripts/work_log.py"

# 1) Create a board (defaults to ~/Desktop/work-log/<cwd-name>/ — one board per project)
python3 "$WL" init --task "Add a voice toggle, touching 3 files" --agents agent1,agent2

# 2) In another terminal: run the watchdog (scans every 15s, alerts on stalls)
python3 "$WL" watch &

# 3) In another terminal: live browser view (default http://localhost:8787; `--port` to change)
python3 "$WL" serve

# 4) Each agent writes a heartbeat every ≤15s
python3 "$WL" post --agent agent1 --text "Got the request: adding a voice toggle. Editing tts.py now." --tag 收到
```

> **The address is whatever `serve` prints** — `http://localhost:<port>/`, default **8787**.
> A common gotcha: `init --no-auto-ui` disables "second agent starts → auto-launch `serve` + open the
> browser", and there is **no reverse flag** (you have to flip `auto_ui` in the board's `state.json`).
> With it, the UI will **not** appear on its own — run `serve` yourself. `init` prints the board directory.

Loop template for each agent (put it in its system prompt):

```text
1. python3 "$WL" brief --agent <me>     # delta: peers' heartbeats + user shouts + alerts
2. do a chunk of work
3. python3 "$WL" post --agent <me> --text "<one line: what I am doing right now>"
4. done:  post --agent <me> --text "All done, changed a.py/b.py" --tag 任务完成
   long run: hold --agent <me> --reason "build" --for 1800
```

## Agents that actually talk to each other

This is the difference from "just write to a log file": negotiation is a **mechanism**, not a verbal promise.

```bash
# A asks → creates an exchange (status: open), returns id #1
python3 "$WL" ask --agent agent1 --to agent2 --text "Can the audio response reuse /api/tts's schema?"

# B sees "waiting for your reply #1" in brief, then answers (only the addressee can answer, and only once)
python3 "$WL" brief --agent agent2
python3 "$WL" reply --agent agent2 --id 1 --text "Yes, but the field is text — no wrapper object"

# A blocks until the answer arrives (waiting auto-refreshes its heartbeat)
python3 "$WL" await --agent agent1 --id 1 --timeout 300

# Wait on several people at once: default is "all of them"; --any means "first one wins"
python3 "$WL" await --agent agent1 --id 1,2,3 --any --timeout 300
```

`await` has three distinct exits — **do not treat them as the same failure**:

| Code | Meaning | What to do |
|---|---|---|
| `0` | Got it | Continue |
| `1` | Timed out. The peer is alive, it just didn't answer. | Worth a nudge, or ask someone else |
| `3` | The peer **has finished** — this answer will never arrive | Decide on your own now; do not keep waiting |

## Exit code contract

Callers (shell scripts, CI, agent drivers) draw **business conclusions** from exit codes, so the codes are a public contract:

| Code | Meaning | Category |
|---|---|---|
| `0` | Success / everyone healthy | — |
| `1` | `check` found a stall **or a coordination hazard**; `await` timed out | **business** |
| `2` | Usage / precondition error (bad id, empty text, asking yourself, `--id 0`, …) | usage |
| `3` | Peer already finished / lock conflict | **business** |
| `4` | Refused by the communication breaker or heartbeat budget | **business** |
| `70` | The tool itself is broken (a software bug) | infrastructure |

**One hard rule: a usage error must never borrow business code `1`.**
Otherwise "I got the id wrong" and "the peer refused to cooperate" look identical to the caller,
and a tool usage bug gets read as a business fact. We actually shipped that bug once —
see [docs/DESIGN.md](docs/DESIGN.md) (Chinese).

## Command reference

20 subcommands, grouped by purpose:

| Group | Commands |
|---|---|
| Heartbeat | `init` `post` `hold` `release` `tail` |
| Direct Q&A | `ask` `reply` `await` `brief` `ack-user` `read-user` |
| Supervision | `check` `status` `watch` `ack` |
| Resource locks | `lock` `unlock` `locks` |
| For humans | `serve` (live browser view, with a collaboration badge) `say` (shout to everyone) |

**Collaboration is the activation condition**: with ≥2 agents working, the board lights up
(visible in `status`, on the `serve` page, and as a board event). **You can join the conversation**:
an agent runs `ask --to 用户`, you answer with `reply --agent 用户 --id N`, and its `await` receives it.

Full parameters: `python3 scripts/work_log.py --help`. Protocol details:
[`references/protocol.md`](references/protocol.md).

## Requirements

- Python **3.9+** (verified on 3.9.6 and 3.13.12; the suite is green on both)
- **Nothing else.** No pip install, no network, no database. `work_log.py` is a single self-contained file.

## Validation

Not "written and shipped" — actually run:

- **Human in the loop**: agents can `ask --to 用户` (the reserved "user" identity); you answer with
  `reply --agent 用户 --id N` and the asker's `await` receives it natively. Human–agent exchanges are
  exempt from the ping-pong breaker.
- **Multi-agent collaboration sensing**: the board lights up a "collaboration" marker the moment a
  second agent starts working (visible in `status`, the browser view, and as a board event) —
  this is the tool's activation condition.
- **290 assertions across 32 test groups, 0 failures** — green on both Python 3.9.6 and 3.13.12
- **Real-model validation**: drove 2 agents through the full protocol for 3 rounds against a live
  OpenAI-compatible endpoint. Every exchange closed, decisions quoted the peer's actual wording,
  a user request conflicting with an already-agreed decision went through "block + negotiate + explicit
  compromise", and the watchdog produced **0 false positives** across all three rounds.
- **Performance**: with a 2000-line board, one `await` poller costs **2.3% of a core**;
  6 concurrent waiters cost **24%** (linear, zero lock contention).

Details and reproduction steps: [docs/VALIDATION.md](docs/VALIDATION.md) (Chinese).

## Honest limits (read before adopting)

- **It cannot detect "still moving, but in the wrong direction."** An agent that dutifully writes a
  heartbeat every 15s while editing the wrong file / retrying forever is invisible to any heartbeat-based
  mechanism, including this one.
- **It does not model waits on external conditions.** CI queues, model downloads, port availability —
  the thing being waited on is not an agent, so the wait graph does not apply.
- **It is not a scheduler.** No task assignment, no priorities, no retry orchestration. It makes state and
  intent *visible*, and turns "waiting" into something *blocking, timeout-able and reportable*.
- **The board is a human-readable text file, not a database.** Concurrent writes are serialized with
  `flock`; there are no transactions. On Windows (no `fcntl`) it degrades to lock-free — fine for a handful
  of local processes, not for distributed use.

## How it differs

| | work-log | Plain log file | Framework trace | MCP team/agent messaging |
|---|---|---|---|---|
| Agents **block** on each other's answers | ✅ | ❌ | ❌ | partial |
| Detects **silent stalls** and alerts a live agent | ✅ | ❌ | post-mortem | ❌ |
| Detects the **all-green deadlock** | ✅ wait graph | ❌ | ❌ | ❌ |
| Prevents **polite ping-pong** burning budget | ✅ breaker | ❌ | ❌ | ❌ |
| Zero deps / offline / plain text | ✅ | ✅ | framework-bound | ❌ |

In one line: **frameworks handle getting agents running; work-log handles whether they are actually
making progress.** It layers on top of any framework.

## Install

```bash
# A. As a Skill (WorkBuddy / Claude Code — anything that reads SKILL.md)
git clone https://github.com/YangLiHaoLiuYing/Work-Log.git ~/.workbuddy/skills/work-log

# B. CLI only (the trailing `work-log` is an explicit target dir — see the note in README.md)
git clone https://github.com/YangLiHaoLiuYing/Work-Log.git work-log && python3 work-log/scripts/work_log.py --help
```

## Repository layout

```
work-log/
├── SKILL.md                 agent-facing manual (triggers / command table / pitfalls)
├── scripts/
│   ├── work_log.py         engine: single file, zero deps, 20 subcommands
│   ├── llm_agent.py         drive a real model as an agent via any OpenAI-compatible endpoint
│   └── selftest.sh          290-assertion regression suite
├── references/protocol.md   spec: state machine, exit codes, board grammar, trade-offs
├── assets/viewer.html       live view page (served by `serve`)
├── examples/demo.sh         60-second demo (no key, leaves no files)
└── docs/                    design, validation, publishing notes + visual preview (mostly Chinese)
```

## Development

```bash
bash scripts/selftest.sh   # measured 1m34s on an M1; runs only in a temp dir, never touches your project
```

Note for `.sh` edits: `bash -n` only checks syntax. A `$var` immediately followed by a non-ASCII
character (e.g. `echo "exit code $rc（business）"`) makes macOS's bundled **bash 3.2** swallow the
punctuation's first byte into the variable name and abort with `rc?: unbound variable` — and
`bash -n` considers it perfectly legal. Always actually run the script
(`bash examples/demo.sh`); CI has a static guard for this pattern too.

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Design decisions, deliberate non-goals, war stories (Chinese) |
| [`docs/VALIDATION.md`](docs/VALIDATION.md) | Validation: 290 assertions + 3 real-model rounds + performance + how to reproduce (Chinese) |
| [`docs/preview.html`](docs/preview.html) | **Visual preview**: the four scenarios as diagrams (single file, offline) |
| [`docs/PUBLISH.md`](docs/PUBLISH.md) | Publishing manual: step by step to GitHub / Gitee (Chinese) |
| [`docs/LAUNCH.md`](docs/LAUNCH.md) | Launch copy: repo blurb, topics, per-platform posts (Chinese) |
| [`references/protocol.md`](references/protocol.md) | Protocol spec: state machine, board grammar, exit-code contract (Chinese) |
| [`SKILL.md`](SKILL.md) | The agent-facing manual (Chinese) |

## License

[MIT](LICENSE).
