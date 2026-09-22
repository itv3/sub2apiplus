#!/usr/bin/env python3
"""每轮参数文件（$ARM64_VC_ENV）的安全解析器：绝不 source、绝不执行。

2026-09-22 审核 P1：lib.sh 原本用正则放行 ``KEY=.*`` 后直接 ``source``，``KEY=$(cmd)``／反引号／``;``
都会被 bash 执行。本解析器只接受：

* 注释行（``#`` 开头）与空行；
* ``KEY=VALUE`` 或 ``KEY="VALUE"``（整值一对双引号），KEY 必须属于精确键集合 ``REQUIRED_KEYS``（不多不少）；
* VALUE 里只允许引用**本文件前面已定义**的键（``$NAME`` 或 ``${NAME}``），由本解析器展开；任何其他 ``$``
  形态、反引号、``;``、``&``、``|``、``<``、``>``、``(``、``)``、``\\``、换行、回车一律拒绝。

输出：每键一行 ``export KEY='value'``（``shlex.quote``），lib.sh 只 ``eval`` 这些赋值。任何问题以退出码 2
失败并把原因写到 stderr（lib.sh 先捕获输出再 eval，失败即退出）。
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

REQUIRED_KEYS = (
    "ROUND", "STAMP", "D", "RUNROOT", "NEW", "IN", "UP", "CAND", "B", "PREV_CANDIDATE", "HISTORY_TEST_TREE",
    "C", "DC", "RECEIPT", "BUNDLE", "BUNDLE_BRANCH", "OFFICIAL_CAMPAIGN", "OFFICIAL_STOP_LEDGER", "OFFICIAL_STOP_RECEIPT",
    "INPUT_RULE_MIGRATION", "INPUT_TARGET_SNAPSHOT", "PROJECT_DEADLINE_UTC", "STAGE_BUDGETS", "MIN_FREE_GIB",
    "FRONTEND_DEVIATION_APPROVED_BY", "PROFILE_ID", "PROFILE_DIGEST", "KILO_BIN", "KILO_VERSION", "KILO_SHA256",
    "COMPOSE_DIR", "COMPOSE_BACKUP", "PRODUCTION_IMAGE",
)
ASSIGNMENT = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
REFERENCE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)")
FORBIDDEN = set("`;&|<>()\\\r\n")


class EnvFileError(ValueError):
    pass


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match is None:
            raise EnvFileError(f"第 {number} 行不是 KEY=VALUE 赋值")
        key, value = match.group(1), match.group(2)
        if key not in REQUIRED_KEYS:
            raise EnvFileError(f"第 {number} 行的键不在允许集合内：{key}")
        if key in values:
            raise EnvFileError(f"第 {number} 行重复定义：{key}")
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        elif '"' in value or "'" in value:
            raise EnvFileError(f"第 {number} 行的值含未闭合或内嵌引号：{key}")
        bad = sorted(ch for ch in set(value) if ch in FORBIDDEN)
        if bad:
            raise EnvFileError(f"第 {number} 行的值含禁止字符 {bad!r}：{key}")
        if "$" in value:
            stripped = REFERENCE.sub("", value)
            if "$" in stripped:
                raise EnvFileError(f"第 {number} 行的值含不允许的 $ 形态（只允许 $NAME／${{NAME}} 引用已定义键）：{key}")

            def lookup(found: re.Match[str]) -> str:
                name = found.group(1) or found.group(2)
                if name not in values:
                    raise EnvFileError(f"第 {number} 行引用了未定义（或后定义）的键：{name}")
                return values[name]

            value = REFERENCE.sub(lookup, value)
        if not value:
            raise EnvFileError(f"第 {number} 行的值为空：{key}")
        values[key] = value
    missing = [key for key in REQUIRED_KEYS if key not in values]
    if missing:
        raise EnvFileError(f"参数文件缺少键：{missing}")
    for key in ("C", "DC"):
        if not re.fullmatch(r"[0-9a-f]{40}", values[key]):
            raise EnvFileError(f"{key} 必须是完整 40 位小写 sha1")
    return values


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：parse_env.py <参数文件>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        if path.is_symlink() or not path.is_file():
            raise EnvFileError(f"参数文件不存在或不可信：{path}")
        values = parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, EnvFileError) as error:
        print(f"参数文件拒绝加载：{error}", file=sys.stderr)
        return 2
    for key in REQUIRED_KEYS:
        sys.stdout.write(f"export {key}={shlex.quote(values[key])}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
