#!/usr/bin/env python3
"""依赖检查：引擎与驱动层只允许 import 标准库。

这个项目的卖点之一是「零第三方依赖、纯标准库」。卖点要靠 CI 守住，
不能靠自觉 —— 一次 `import requests` 就能让它悄悄失效，而且没人会发现。

做法：用 importlib 解析每个 import 的模块，看它的真实来源文件是否位于
当前解释器的标准库目录下。这样不需要维护白名单，也不会被 Python 版本差异影响
（`sys.stdlib_module_names` 是 3.10 才有的，这个脚本要能在 3.9 上跑）。

退出码：`0` = 干净 · `1` = **发现非标准库依赖** · `2` = **检查器自己没跑起来**。

区分 1 与 2 不是洁癖：本脚本的调用方一律是 `python3 check_stdlib_only.py || 报警`
这种形式。若「我没找到要检查的文件」也退 1，它会与「真的引入了 requests」
长得一模一样，两边都会骗人 —— 一边是假违规，一边是真违规被淹没在噪音里。
工具跑不起来，绝不该伪装成业务结论。
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import sysconfig

# 相对**本脚本自身**定位，不相对 cwd。
# 实测踩到：`TARGETS` 写成相对路径时，换个目录调用就抛 FileNotFoundError，
# 而解释器此时恰好退 1 —— 与「发现违规」撞车（2026-09-24）。
ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = ("scripts/work_log.py", "scripts/llm_agent.py")

STDLIB = pathlib.Path(sysconfig.get_paths()["stdlib"]).resolve()
# 有些模块是内建/冻结的，没有真实文件路径
INTRINSIC_ORIGINS = {None, "built-in", "frozen", "namespace"}


def is_stdlib(mod: str) -> bool:
    if mod == "__future__":
        return True
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ModuleNotFoundError, ValueError):
        return False
    if spec is None:
        return False
    if spec.origin in INTRINSIC_ORIGINS:
        return True
    try:
        origin = pathlib.Path(spec.origin).resolve()
    except (TypeError, OSError):
        return False
    return STDLIB in origin.parents


def main() -> int:
    missing = [p for p in TARGETS if not (ROOT / p).is_file()]
    if missing:
        print("✗ 找不到要检查的文件 —— 这是检查器自己没跑起来，不是发现了依赖问题：")
        for p in missing:
            print(f"   {ROOT / p}")
        print(f"  （包根按脚本自身位置推断为 {ROOT}；本脚本应在 <包根>/scripts/ 下）")
        return 2

    bad = []
    checked = 0
    for path in TARGETS:
        try:
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as e:
            print(f"✗ 读不了 / 解析不了 {ROOT / path}：{type(e).__name__}: {e}")
            print("  这是检查器自己的问题，不是依赖违规。")
            return 2
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for full in mods:
                top = full.split(".")[0]
                if not top:
                    continue
                checked += 1
                if not is_stdlib(top):
                    bad.append(f"{path}:{node.lineno}  import {full}")

    print(f"扫描 {len(TARGETS)} 个文件，共 {checked} 处 import")
    if bad:
        print("\n!! 发现非标准库依赖（这个项目要求零第三方依赖）：")
        for line in bad:
            print("   " + line)
        return 1
    print("ok: 全部来自标准库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
