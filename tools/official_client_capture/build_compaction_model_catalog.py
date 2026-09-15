#!/usr/bin/env python3
"""为官方压缩场景生成只含两个模型的受控目录。"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


MAX_CATALOG_BYTES = 16 * 1024 * 1024
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class CatalogError(ValueError):
    """模型目录不满足压缩场景的冻结条件。"""


def _read_catalog(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CatalogError("模型目录必须是可信绝对普通文件。")
    size = path.stat().st_size
    if size <= 0 or size > MAX_CATALOG_BYTES:
        raise CatalogError("模型目录大小超出允许范围。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogError(f"模型目录无法解析：{error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise CatalogError("模型目录缺少 models 数组。")
    return payload


def build_catalog(
    payload: dict[str, Any],
    *,
    first_model: str,
    second_model: str,
    second_track: str,
    reason: str,
) -> dict[str, Any]:
    """校验两个模型的真实轨道，并只改写压缩触发字段。"""

    if (
        not MODEL_RE.fullmatch(first_model)
        or not MODEL_RE.fullmatch(second_model)
        or first_model == second_model
    ):
        raise CatalogError("压缩场景必须提供两个不同且格式合法的模型。")
    if second_track not in {"main", "lite"}:
        raise CatalogError("第二模型轨道只能是 main 或 lite。")
    models = payload.get("models")
    if not isinstance(models, list):
        raise CatalogError("模型目录缺少 models 数组。")
    matches: dict[str, dict[str, Any]] = {}
    for item in models:
        if not isinstance(item, dict):
            raise CatalogError("模型目录条目必须是对象。")
        slug = item.get("slug")
        if slug not in {first_model, second_model}:
            continue
        if slug in matches:
            raise CatalogError(f"模型目录包含重复模型：{slug}")
        matches[str(slug)] = dict(item)
    if set(matches) != {first_model, second_model}:
        missing = sorted({first_model, second_model} - set(matches))
        raise CatalogError("压缩场景模型目录缺少唯一模型：" + "、".join(missing))
    if matches[first_model].get("use_responses_lite") is not False:
        raise CatalogError("压缩场景首模型必须是 use_responses_lite=false 的 Main 模型。")
    expected_second_lite = second_track == "lite"
    if matches[second_model].get("use_responses_lite") is not expected_second_lite:
        raise CatalogError(
            "压缩场景第二模型的 use_responses_lite 与冻结轨道不一致："
            f"expected={str(expected_second_lite).lower()}"
        )

    selected = [matches[first_model], matches[second_model]]
    if reason == "comp_hash_changed":
        selected[0]["comp_hash"] = "comp-hash-probe-first"
        selected[1]["comp_hash"] = "comp-hash-probe-second"
    elif reason == "model_downshift":
        for item in selected:
            item["comp_hash"] = "downshift-probe"
        selected[0]["context_window"] = 272000
        selected[0]["auto_compact_token_limit"] = 16000
        selected[1]["context_window"] = 128000
        selected[1]["auto_compact_token_limit"] = 8000
    else:
        raise CatalogError("压缩原因只能是 comp_hash_changed 或 model_downshift。")
    return {"models": selected}


def _write_catalog(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise CatalogError("输出目录必须是尚不存在的绝对普通文件。")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise CatalogError("输出目录的父目录不存在或不可信。")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        os.chmod(path, 0o600)
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--first-model", required=True)
    parser.add_argument("--second-model", required=True)
    parser.add_argument("--second-track", choices=("main", "lite"), required=True)
    parser.add_argument(
        "--reason",
        choices=("comp_hash_changed", "model_downshift"),
        required=True,
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        payload = build_catalog(
            _read_catalog(arguments.models_cache),
            first_model=arguments.first_model,
            second_model=arguments.second_model,
            second_track=arguments.second_track,
            reason=arguments.reason,
        )
        _write_catalog(arguments.output, payload)
    except (CatalogError, OSError) as error:
        print(str(error), file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
