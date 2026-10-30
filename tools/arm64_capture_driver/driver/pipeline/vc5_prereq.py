#!/usr/bin/env python3
"""VC-5 库表前置的临时改动、恢复与核对（采集主机，随驱动安装在 pipeline/vc5_prereq.py，shell 入口 vc5-prereq.sh）。

背景：候选采集（VC-5）要求三项库表前置——采集账号 ``credentials.model_mapping`` 清空、采集分组 ``allow_live`` 打开、分组内只许采集 Key 一把活跃
（``vc5-precheck.sh --dry-run`` 的 ``candidate_prerequisites`` 逐项列出）。VC-5 前临时改、VC-6 后恢复原值。上一版（0.165）这套改动是一份只在本机分析目录里的
一次性脚本，恢复后核对 7 项摘要，4 项一致、另 3 项（账号 #90 其余列、其它账号、其它 Key）不同——原因是摘要口径把 ``last_used_at``、``updated_at``、会话窗口等
**运行时列**也算进去，而 VC-6 金丝雀与生产的真实请求会更新它们；又没有改前的行级快照，只能说“疑为仅运行时列变化”，无法核实。

本模块把这件事固化成驱动的一部分，并修这两处：

* **摘要口径排除运行时列**（``RUNTIME_COLUMNS``、``RUNTIME_JSON_KEYS``，按表结构逐列写清楚，见下）。7 项摘要的名字沿用上一版的含义，但不再带轮次的账号号：
  ``account_other_cols``（采集账号除 ``credentials`` 外的列）、``account_cred_without_mapping``（采集账号凭据去掉 ``model_mapping``）、``other_accounts``、
  ``group_without_live``（采集分组除 ``allow_live`` 外的列）、``other_groups``、``key_without_status``（采集 Key 除 ``status`` 外的列）、``other_keys``。
* **改前做行级快照，恢复后逐列比较**：``apply`` 先把 ``accounts``、``groups``、``api_keys`` 三张表的**全部行、全部列**存成 ``snapshot-before.json``；
  ``restore``／``verify`` 重新取一份与它逐行逐列比较，差异分三类：``expected``（本工具故意改的那三处，仅 ``apply`` 之后出现）、``runtime``（运行时列，
  允许变化，只报告）、``unexpected``（其余任何列的任何变化，包括多出或少了的行）。有 ``unexpected`` 就退出非零。差异里**不打印**凭据、Key 密钥的值，只列表、行号、列名、凭据里的键名。

运行时列（按表结构，来自 ``backend/ent/schema``；网关真实请求或后台服务会写它们）：

* ``accounts``：``last_used_at``、``updated_at``、``session_window_start``、``session_window_end``、``session_window_status``（会话窗口）、``rate_limited_at``、
  ``rate_limit_reset_at``、``overload_until``、``temp_unschedulable_until``、``temp_unschedulable_reason``、``error_message``；JSON 列 ``extra`` 里的用量快照键
  （``codex_5h_*``、``codex_7d_*``、``codex_primary_*``、``codex_secondary_*``、``codex_usage_updated_at``、``model_rate_limits``）与 ``credentials`` 里令牌刷新会写的键
  （``access_token``、``refresh_token``、``id_token``、``expires_at``、``expires_in``、``last_refresh``、``_token_version``）。
* ``groups``：``updated_at``。
* ``api_keys``：``last_used_at``、``updated_at``、``quota_used``、``usage_5h``、``usage_1d``、``usage_7d``、``window_5h_start``、``window_1d_start``、``window_7d_start``。

``status``、``schedulable``、``deleted_at``、``credentials`` 其余键、``extra`` 其余键都不是运行时列：这些变了就是 ``unexpected``。清单之外的运行时键（日后新增的）会先以
``unexpected`` 报出键名，由人判断后补进清单，不会被悄悄放过。

命令（账号、分组、Key 号一律由参数给，不写死；备份目录 0700，文件 0600）::

    vc5_prereq.py show    --account N --group N --key N --backup-dir D     只读，打印三项前置的当前值
    vc5_prereq.py apply   --account N --group N --key N --backup-dir D     备份原值与行级快照 → 三处临时改动 → 核对只有这三处（外加运行时列）变了
    vc5_prereq.py restore --account N --group N --key N --backup-dir D     写回备份的原值 → 与改前快照逐列比较
    vc5_prereq.py verify  --account N --group N --key N --backup-dir D     只读：当前库与改前快照逐列比较

数据库经 ``docker exec -i <容器> psql`` 访问（``--container`` 缺省 sub2apiplus-postgres，``--db-user`` sub2api，``--db-name`` sub2api）。
``apply`` 后 7 项摘要一项都不该变（每项已排除故意改的那一处），``restore`` 后同样；``verify`` 另外把“故意改的三处”也算进逐列比较，所以恢复不全会直接报 ``unexpected``。
备份里没有 ``model_mapping``（apply 时本来就是空）时，``restore`` 跳过这一项，分组与 Key 照常恢复。
退出码：0 通过；1 核对不通过（有 ``unexpected`` 或摘要变了）；2 用法错误；3 状态不允许（重复 apply、没 apply 就 restore、备份缺失）。
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # 驱动目录不得出现 __pycache__（清单闭合）。

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping, Sequence  # noqa: E402

TABLES = ("accounts", "groups", "api_keys")

# 运行时列：网关真实请求与后台服务会写，不代表“有人改了配置”。
RUNTIME_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "accounts": ("last_used_at", "updated_at", "session_window_start", "session_window_end", "session_window_status", "rate_limited_at",
                 "rate_limit_reset_at", "overload_until", "temp_unschedulable_until", "temp_unschedulable_reason", "error_message"),
    "groups": ("updated_at",),
    "api_keys": ("last_used_at", "updated_at", "quota_used", "usage_5h", "usage_1d", "usage_7d", "window_5h_start", "window_1d_start",
                 "window_7d_start"),
}
# JSON 列里的运行时键（键名正则，整段匹配）。
RUNTIME_JSON_KEYS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("accounts", "extra"): (r"codex_(?:5h|7d|primary|secondary)_.+", r"codex_usage_updated_at", r"model_rate_limits"),
    ("accounts", "credentials"): (r"access_token", r"refresh_token", r"id_token", r"expires_at", r"expires_in", r"last_refresh", r"_token_version"),
}
# 差异输出里值不打印的列（凭据、Key 密钥）。
SECRET_COLUMNS = {("accounts", "credentials"), ("api_keys", "key")}

DIGEST_NAMES = ("account_other_cols", "account_cred_without_mapping", "other_accounts", "group_without_live", "other_groups", "key_without_status",
                "other_keys")
MARKER = "applied"


class PrereqError(RuntimeError):
    """用法或状态不允许（退出码 3）。"""


# ---------------------------------------------------------------- 纯逻辑：运行时列、摘要、逐列比较

def is_runtime_key(table: str, column: str, key: str) -> bool:
    return any(re.fullmatch(pattern, key) for pattern in RUNTIME_JSON_KEYS.get((table, column), ()))


def strip_runtime(table: str, row: Mapping[str, Any], *, drop: Sequence[str] = ()) -> dict[str, Any]:
    """去掉运行时列、JSON 列里的运行时键和 ``drop`` 指定的列后的行（新字典，不改入参）。"""

    result: dict[str, Any] = {}
    for column, value in row.items():
        if column in RUNTIME_COLUMNS[table] or column in drop:
            continue
        if isinstance(value, dict) and (table, column) in RUNTIME_JSON_KEYS:
            value = {key: item for key, item in value.items() if not is_runtime_key(table, column, key)}
        result[column] = value
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def compute_digests(snapshot: Mapping[str, Sequence[Mapping[str, Any]]], *, account: int, group: int, key: int) -> dict[str, str]:
    """7 项摘要（口径见模块说明）。目标行不存在时该项摘要记为 ``<absent>``，不抛错（核对时会因为与改前不同而报出来）。"""

    def row(table: str, row_id: int) -> Mapping[str, Any] | None:
        return next((item for item in snapshot[table] if item.get("id") == row_id), None)

    def others(table: str, row_id: int) -> list[dict[str, Any]]:
        return [strip_runtime(table, item) for item in sorted(snapshot[table], key=lambda item: item["id"]) if item.get("id") != row_id]

    target_account, target_group, target_key = row("accounts", account), row("groups", group), row("api_keys", key)

    def credentials_without_mapping() -> Any:
        if target_account is None:
            return "<absent>"
        stripped = strip_runtime("accounts", {"credentials": target_account.get("credentials")})["credentials"]
        return {k: v for k, v in stripped.items() if k != "model_mapping"} if isinstance(stripped, dict) else stripped

    return {
        "account_other_cols": _digest(strip_runtime("accounts", target_account, drop=("credentials",)) if target_account else "<absent>"),
        "account_cred_without_mapping": _digest(credentials_without_mapping()),
        "other_accounts": _digest(others("accounts", account)),
        "group_without_live": _digest(strip_runtime("groups", target_group, drop=("allow_live",)) if target_group else "<absent>"),
        "other_groups": _digest(others("groups", group)),
        "key_without_status": _digest(strip_runtime("api_keys", target_key, drop=("status",)) if target_key else "<absent>"),
        "other_keys": _digest(others("api_keys", key)),
    }


def _json_keys(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def diff_snapshots(before: Mapping[str, Sequence[Mapping[str, Any]]], after: Mapping[str, Sequence[Mapping[str, Any]]],
                   *, expected: Sequence[tuple[str, int, str]] = ()) -> list[dict[str, Any]]:
    """逐表逐行逐列比较两份行级快照。``expected`` 是故意改的 ``(表, 行号, 列)`` 三元组；``credentials.model_mapping`` 写作列
    ``credentials.model_mapping``。返回差异列表，每项 ``{table, id, column, kind}``（kind 为 expected／runtime／unexpected；
    行多出或少了 column 为 ``<row>``），非密钥列另带截断后的 before／after 值。"""

    diffs: list[dict[str, Any]] = []

    def add(table: str, row_id: Any, column: str, kind: str, old: Any = None, new: Any = None) -> None:
        item: dict[str, Any] = {"table": table, "id": row_id, "column": column, "kind": kind}
        secret = (table, column.split(".")[0]) in SECRET_COLUMNS
        if not secret and column != "<row>":
            item["before"], item["after"] = str(old)[:80], str(new)[:80]
        diffs.append(item)

    for table in TABLES:
        old_rows = {item["id"]: item for item in before[table]}
        new_rows = {item["id"]: item for item in after[table]}
        for row_id in sorted(set(old_rows) | set(new_rows)):
            if row_id not in old_rows or row_id not in new_rows:
                add(table, row_id, "<row>", "unexpected")
                continue
            old, new = old_rows[row_id], new_rows[row_id]
            for column in sorted(set(old) | set(new)):
                if old.get(column) == new.get(column):
                    continue
                if (table, column) in RUNTIME_JSON_KEYS and isinstance(old.get(column), dict) and isinstance(new.get(column), dict):
                    left, right = _json_keys(old.get(column)), _json_keys(new.get(column))
                    for key in sorted(set(left) | set(right)):
                        if left.get(key) == right.get(key):
                            continue
                        name = f"{column}.{key}"
                        if (table, row_id, name) in expected:
                            add(table, row_id, name, "expected", left.get(key), right.get(key))
                        elif is_runtime_key(table, column, key):
                            add(table, row_id, name, "runtime", left.get(key), right.get(key))
                        else:
                            add(table, row_id, name, "unexpected", left.get(key), right.get(key))
                    continue
                if (table, row_id, column) in expected:
                    add(table, row_id, column, "expected", old.get(column), new.get(column))
                elif column in RUNTIME_COLUMNS[table]:
                    add(table, row_id, column, "runtime", old.get(column), new.get(column))
                else:
                    add(table, row_id, column, "unexpected", old.get(column), new.get(column))
    return diffs


def summarize(diffs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {kind: sum(1 for item in diffs if item["kind"] == kind) for kind in ("expected", "runtime", "unexpected")}
    return {"counts": counts, "unexpected": [item for item in diffs if item["kind"] == "unexpected"],
            "runtime_columns_touched": sorted({f"{item['table']}.{item['column']}" for item in diffs if item["kind"] == "runtime"})}


# ---------------------------------------------------------------- 数据库访问

class PsqlDatabase:
    """经 ``docker exec -i <容器> psql`` 访问；语句与上一版（0.165）在 ARM64 上跑过的脚本一致。"""

    def __init__(self, container: str, user: str, name: str) -> None:
        self.command = ["docker", "exec", "-i", container, "psql", "-U", user, "-d", name, "-qAt", "-v", "ON_ERROR_STOP=1"]

    def _run(self, sql: str, variables: Mapping[str, str] | None = None) -> str:
        command = list(self.command)
        for key, value in (variables or {}).items():
            command += ["-v", f"{key}={value}"]
        done = subprocess.run(command, input=sql, capture_output=True, text=True, stdin=None)
        if done.returncode != 0:
            raise PrereqError(f"psql 失败（rc={done.returncode}）：{done.stderr.strip()[:300]}")
        return done.stdout.strip()

    def table(self, name: str) -> list[dict[str, Any]]:
        assert name in TABLES
        text = self._run(f"select coalesce(json_agg(to_jsonb(t) order by t.id), '[]'::json) from {name} t")
        return json.loads(text)

    def remove_model_mapping(self, account: int) -> None:
        self._run(f"update accounts set credentials = credentials - 'model_mapping' where id={account} and jsonb_typeof(credentials->'model_mapping')='object'")

    def set_allow_live(self, group: int, value: bool) -> None:
        self._run(f"update groups set allow_live={'true' if value else 'false'} where id={group}")

    def set_key_status(self, key: int, old: str, new: str) -> None:
        self._run(f"update api_keys set status='{new}' where id={key} and status='{old}'")

    def restore_model_mapping(self, account: int, mapping: Any) -> None:
        self._run(f"update accounts set credentials = jsonb_set(credentials, '{{model_mapping}}', :'m'::jsonb) where id={account}",
                  {"m": json.dumps(mapping)})

    def restore_group_fields(self, group: int, row: Mapping[str, Any]) -> None:
        def literal(value: Any) -> str:
            return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"

        self._run(f"update groups set allow_live={str(bool(row['allow_live'])).lower()}, codex_models_manifest_config={literal(row['codex_models_manifest_config'])}, "
                  f"model_allowlist={literal(row['model_allowlist'])} where id={group}")


# ---------------------------------------------------------------- 命令

def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def _snapshot(db: Any) -> dict[str, list[dict[str, Any]]]:
    return {table: db.table(table) for table in TABLES}


def _digest_lines(digests: Mapping[str, str]) -> str:
    return "".join(f"{name}={digests[name]}\n" for name in DIGEST_NAMES)


def _find(snapshot: Mapping[str, Sequence[Mapping[str, Any]]], table: str, row_id: int) -> Mapping[str, Any]:
    for item in snapshot[table]:
        if item.get("id") == row_id:
            return item
    raise PrereqError(f"{table} 里没有 id={row_id} 的行，拒绝")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_backup_dir(backup: Path) -> None:
    backup.mkdir(parents=True, exist_ok=True)
    os.chmod(backup, 0o700)


def show(db: Any, *, account: int, group: int, key: int) -> list[str]:
    snapshot = _snapshot(db)
    account_row, group_row = _find(snapshot, "accounts", account), _find(snapshot, "groups", group)
    mapping = (account_row.get("credentials") or {}).get("model_mapping")
    keys = [item for item in snapshot["api_keys"] if item.get("group_id") == group]
    return [
        f"model_mapping #{account}: " + ("无" if mapping is None else f"{type(mapping).__name__} {json.dumps(mapping, ensure_ascii=False)}"),
        f"groups#{group}: allow_live={group_row.get('allow_live')} manifest={json.dumps(group_row.get('codex_models_manifest_config'))} "
        f"allowlist={json.dumps(group_row.get('model_allowlist'))}",
        f"分组 {group} 的 Key: " + " ".join(f"{item['id']}:{item.get('status')}:{item.get('deleted_at') is not None}" for item in sorted(keys, key=lambda x: x["id"])),
        f"#{account} status/schedulable: {account_row.get('status')}/{account_row.get('schedulable')}",
    ]


def apply(db: Any, backup: Path, *, account: int, group: int, key: int) -> dict[str, Any]:
    _ensure_backup_dir(backup)
    if (backup / MARKER).exists():
        raise PrereqError(f"已应用过（{backup / MARKER}），拒绝重复备份覆盖原值；要重来先 restore")
    before = _snapshot(db)
    account_row, group_row, key_row = _find(before, "accounts", account), _find(before, "groups", group), _find(before, "api_keys", key)
    mapping = (account_row.get("credentials") or {}).get("model_mapping")
    _write(backup / "snapshot-before.json", json.dumps(before, ensure_ascii=False, sort_keys=True))
    _write(backup / "account-model_mapping.json", json.dumps(mapping, ensure_ascii=False))
    _write(backup / "group-row.json", json.dumps(group_row, ensure_ascii=False, sort_keys=True))
    _write(backup / "group-keys-status.json", json.dumps(
        [{"id": item["id"], "status": item.get("status")} for item in before["api_keys"] if item.get("group_id") == group]))
    _write(backup / "account-switch.json", json.dumps({"status": account_row.get("status"), "schedulable": account_row.get("schedulable")}))
    digests_before = compute_digests(before, account=account, group=group, key=key)
    _write(backup / "digests-before.txt", _digest_lines(digests_before))
    db.remove_model_mapping(account)
    db.set_allow_live(group, True)
    db.set_key_status(key, "active", "inactive")
    after = _snapshot(db)
    _write(backup / "snapshot-after.json", json.dumps(after, ensure_ascii=False, sort_keys=True))
    digests_after = compute_digests(after, account=account, group=group, key=key)
    _write(backup / "digests-after.txt", _digest_lines(digests_after))
    expected = [("accounts", account, "credentials.model_mapping"), ("groups", group, "allow_live"), ("api_keys", key, "status")]
    diffs = diff_snapshots(before, after, expected=expected)
    report = {"phase": "apply", "digests_unchanged": [name for name in DIGEST_NAMES if digests_before[name] == digests_after[name]],
              "digests_changed": [name for name in DIGEST_NAMES if digests_before[name] != digests_after[name]], **summarize(diffs),
              "expected_changes": [item for item in diffs if item["kind"] == "expected"],
              "model_mapping_backup_empty": mapping in (None, {}, [])}
    # 7 项摘要各自排除了“这次故意改的那一处”（凭据去掉 model_mapping、分组去掉 allow_live、Key 去掉 status）和运行时列，所以 apply 之后一项都不该变；
    # 逐列比较：只允许出现三处故意改动（备份里没有 model_mapping 时是两处）与运行时列，其余任何列变化都算 unexpected。
    report["ok"] = (not report["digests_changed"] and not report["unexpected"]
                    and len(report["expected_changes"]) == (3 if mapping else 2))
    if report["ok"]:
        _write(backup / MARKER, _now() + "\n")
    return report


def restore(db: Any, backup: Path, *, account: int, group: int, key: int) -> dict[str, Any]:
    if not (backup / MARKER).exists():
        raise PrereqError("没有应用记录，拒绝")
    try:
        mapping = json.loads((backup / "account-model_mapping.json").read_text(encoding="utf-8"))
        group_row = json.loads((backup / "group-row.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PrereqError(f"备份读不出来：{error}") from error
    if mapping:    # 备份里没有 model_mapping（apply 时本来就是空）就没有可写回的，跳过这一项，分组与 Key 照常恢复
        db.restore_model_mapping(account, mapping)
    db.restore_group_fields(group, group_row)
    db.set_key_status(key, "inactive", "active")
    report = verify(db, backup, account=account, group=group, key=key, phase="restore")
    _write(backup / "snapshot-restored.json", json.dumps(_snapshot(db), ensure_ascii=False, sort_keys=True))
    return report


def verify(db: Any, backup: Path, *, account: int, group: int, key: int, phase: str = "verify") -> dict[str, Any]:
    """当前库与改前快照逐列比较：运行时列允许变化（只报告），其余任何列变了都算 unexpected。"""

    try:
        before = json.loads((backup / "snapshot-before.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PrereqError(f"改前快照读不出来：{error}") from error
    current = _snapshot(db)
    diffs = diff_snapshots(before, current)
    digests_before = compute_digests(before, account=account, group=group, key=key)
    digests_now = compute_digests(current, account=account, group=group, key=key)
    _write(backup / "digests-restored.txt", _digest_lines(digests_now))
    report = {"phase": phase, "digests_unchanged": [name for name in DIGEST_NAMES if digests_before[name] == digests_now[name]],
              "digests_changed": [name for name in DIGEST_NAMES if digests_before[name] != digests_now[name]], **summarize(diffs)}
    report["ok"] = not report["unexpected"] and not report["digests_changed"]
    return report


def _print(report: Mapping[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))


def main(argv: list[str] | None = None, *, database: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("show", "apply", "restore", "verify"))
    parser.add_argument("--account", type=int, required=True, help="采集账号 id")
    parser.add_argument("--group", type=int, required=True, help="采集分组 id")
    parser.add_argument("--key", type=int, required=True, help="采集 Key id（分组里要被临时停用的那一把）")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--container", default="sub2apiplus-postgres")
    parser.add_argument("--db-user", default="sub2api")
    parser.add_argument("--db-name", default="sub2api")
    args = parser.parse_args(argv)
    if not args.backup_dir.is_absolute():
        print("--backup-dir 必须是绝对路径", file=sys.stderr)
        return 2
    db = database if database is not None else PsqlDatabase(args.container, args.db_user, args.db_name)
    ids = {"account": args.account, "group": args.group, "key": args.key}
    try:
        if args.command == "show":
            print("\n".join(show(db, **ids)))
            return 0
        report = {"apply": apply, "restore": restore, "verify": verify}[args.command](db, args.backup_dir, **ids)
    except PrereqError as error:
        print(f"vc5-prereq：{error}", file=sys.stderr)
        return 3
    _print(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
