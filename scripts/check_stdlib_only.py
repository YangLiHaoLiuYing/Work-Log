#!/usr/bin/env python3
"""依赖检查：引擎与驱动层只允许 import 标准库。

这个项目的卖点之一是「零第三方依赖、纯标准库」。卖点要靠 CI 守住，
不能靠自觉 —— 一次 `import requests` 就能让它悄悄失效，而且没人会发现。

做法：用 importlib 解析每个 import 的模块，看它的真实来源文件是否位于
当前解释器的标准库目录下。这样不需要维护白名单，也不会被 Python 版本差异影响
（`sys.stdlib_module_names` 是 3.10 才有的，这个脚本要能在 3.9 上跑）。

退出码：0 = 干净，1 = 发现非标准库依赖。
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import sysconfig

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
    bad = []
    checked = 0
    for path in TARGETS:
        src = pathlib.Path(path).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
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
