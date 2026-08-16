#!/usr/bin/env python3
"""生成并校验变更集 6 benchmark driver 等价性与原始结果阈值证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE_METADATA = ROOT / "docs" / "egress" / "validation" / "baseline" / "benchmark-metadata.json"
POST_METADATA = ROOT / "docs" / "egress" / "validation" / "post" / "benchmark-metadata.json"
CALCULATION_PATH = ROOT / "docs" / "egress" / "validation" / "post" / "benchmark-calculation.json"

LIVE_BODY_DRIVER = ROOT / "backend" / "internal" / "service" / "official_egress_changeset6_benchmark_test.go"
LIVE_CATALOG_DRIVER = ROOT / "backend" / "internal" / "officialegress" / "release_catalog_benchmark_test.go"
LIVE_PROFILE_DRIVER = ROOT / "backend" / "internal" / "service" / "official_egress_benchmark_test.go"

PRE_DRIVER_DIR = ROOT / "docs" / "egress" / "validation" / "baseline" / "benchmark-drivers"
POST_DRIVER_DIR = ROOT / "docs" / "egress" / "validation" / "post" / "benchmark-drivers"
PRE_BODY_DRIVER = PRE_DRIVER_DIR / "body-pre_test.go"
PRE_CATALOG_DRIVER = PRE_DRIVER_DIR / "catalog-pre_test.go"
PRE_PROFILE_DRIVER = PRE_DRIVER_DIR / "profile-pre_test.go"
POST_BODY_DRIVER = POST_DRIVER_DIR / "body-post_test.go"
POST_CATALOG_DRIVER = POST_DRIVER_DIR / "catalog-post_test.go"
POST_PROFILE_DRIVER = POST_DRIVER_DIR / "profile-post_test.go"

PRE_BODY_SHA256 = "c2902c023bfddb35d6b746a103069774d7d14c4b2a300efa1efb25632c320cab"
POST_BODY_SHA256 = "bce3b5c5aace49adee353ce45d893a048b3c577d4244242fa34a911f1f847b4f"
CATALOG_DRIVER_SHA256 = "f7731b9f5a2999e94ab869f245ba74e20654c22b018af74ab7a6f430f1822aed"
PROFILE_DRIVER_SHA256 = "66e0775b72be6456d71c5527b21664c1b06b9c743fdfb83515a808812331a846"
FIXTURE_SHA256 = "7698cddeadace650567e46e7be9b66286212e26e983edb29e78da423ac713e08"

FROZEN_PROFILE_CALLEE = b"finalizeOpenAIOfficialEgressWSFrame("
LIVE_PROFILE_CALLEE = b"prepareOpenAIOfficialEgressSemanticWSFrame("
PROFILE_LIVE_CALLEE_DELTA_COUNT = 2

PRE_BODY_ADAPTER = b"\t\t\t\tBody:            officialegress.NewReplayableRequestBody(semantic.Body),"
POST_BODY_ADAPTER = b"\t\t\t\tBody:            semantic.Body,"

BODY_CASES = (
    "BenchmarkBodyCompileLargeUnchanged",
    "BenchmarkBodyCompileLargeDirty",
    "BenchmarkBodyCompileRetry",
    "BenchmarkBodyCompileOpenWS",
)
CATALOG_CASES = ("BenchmarkReleaseCatalogResolve",)
PROFILE_CASES = (
    "BenchmarkOfficialCodexVersionProfileResolve",
    "BenchmarkOfficialCodexEndpointResolve",
)

BENCHMARK_LINE = re.compile(
    r"^(Benchmark\S+?)-\d+\s+\d+\s+([0-9.]+) ns/op"
    r"(?:\s+[0-9.]+ MB/s)?\s+([0-9.]+) B/op\s+([0-9.]+) allocs/op$"
)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def relative(path: pathlib.Path) -> str:
    return path.relative_to(ROOT).as_posix()


def resolve_recorded_path(value: str) -> pathlib.Path:
    """把封存收据中的旧目录路径映射到当前语义目录，不改写历史原文。"""
    prefix = "docs/changeset6/"
    if value.startswith(prefix):
        value = "docs/egress/validation/" + value[len(prefix) :]
    return ROOT / pathlib.PurePosixPath(value)


def historical_recorded_path(path: pathlib.Path) -> str:
    """返回封存 metadata 使用的历史路径，供原文摘要链核对。"""
    value = relative(path)
    prefix = "docs/egress/validation/"
    if value.startswith(prefix):
        return "docs/changeset6/" + value[len(prefix) :]
    return value


def artifact(path: pathlib.Path) -> dict[str, str]:
    return {"path": historical_recorded_path(path), "sha256": sha256(path.read_bytes())}


def load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON 顶层必须是对象：{relative(path)}")
    return value


def write_frozen(path: pathlib.Path, raw: bytes) -> None:
    if path.exists():
        if path.read_bytes() != raw:
            raise RuntimeError(f"冻结证据已存在且字节不同，禁止覆盖：{relative(path)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def recovered_pre_body(post_body: bytes) -> bytes:
    if post_body.count(POST_BODY_ADAPTER) != 1 or PRE_BODY_ADAPTER in post_body:
        raise RuntimeError("post Body driver 的声明 API 适配点不是唯一预期形态")
    recovered = post_body.replace(POST_BODY_ADAPTER, PRE_BODY_ADAPTER, 1)
    if sha256(recovered) != PRE_BODY_SHA256:
        raise RuntimeError("恢复的 pre Body driver 摘要不等于审核冻结值 c290…")
    return recovered


def validate_body_driver_equivalence(pre_body: bytes, post_body: bytes) -> None:
    if sha256(pre_body) != PRE_BODY_SHA256 or sha256(post_body) != POST_BODY_SHA256:
        raise RuntimeError("pre/post Body driver 摘要与冻结值不一致")
    if pre_body.count(PRE_BODY_ADAPTER) != 1 or POST_BODY_ADAPTER in pre_body:
        raise RuntimeError("pre Body driver 的旧 API 适配点不是唯一预期形态")
    if post_body.count(POST_BODY_ADAPTER) != 1 or PRE_BODY_ADAPTER in post_body:
        raise RuntimeError("post Body driver 的新 API 适配点不是唯一预期形态")
    normalized = pre_body.replace(PRE_BODY_ADAPTER, POST_BODY_ADAPTER, 1)
    if normalized != post_body:
        raise RuntimeError("pre/post Body driver 存在声明 API 适配之外的任何差异")

    required_contracts = (
        b"b.SetBytes(int64(len(testCase.body) * attemptsPerIteration))",
        b"b.ResetTimer()",
        b"for range b.N {",
        b"for attempt := 0; attempt < attemptsPerIteration; attempt++ {",
        b"prepareOfficialCodexSemanticAttempt(",
        b"compiler.Compile(",
        b"changeset6BenchmarkDigestSink = compiled.CompiledDigest()",
        b"benchmarkChangeset6BodyCompile(b, newChangeset6ResponsesBenchmarkCase(b, fixtures.largeUnchanged), 1)",
        b"benchmarkChangeset6BodyCompile(b, newChangeset6ResponsesBenchmarkCase(b, fixtures.largeDirty), 1)",
        b"benchmarkChangeset6BodyCompile(b, newChangeset6ResponsesBenchmarkCase(b, fixtures.largeDirty), 2)",
        b"benchmarkChangeset6BodyCompile(b, newChangeset6OpenWSBenchmarkCase(b, fixtures.largeOpenWS), 1)",
    )
    for contract in required_contracts:
        if pre_body.count(contract) != 1 or post_body.count(contract) != 1:
            raise RuntimeError(f"benchmark driver 计时或负载契约缺失／重复：{contract!r}")


def recovered_frozen_profile_driver(live_profile: bytes) -> bytes:
    if (
        live_profile.count(LIVE_PROFILE_CALLEE) != PROFILE_LIVE_CALLEE_DELTA_COUNT
        or FROZEN_PROFILE_CALLEE in live_profile
    ):
        raise RuntimeError("当前 Profile driver 的 WS finalizer 退休适配点不是严格两处")
    recovered = live_profile.replace(LIVE_PROFILE_CALLEE, FROZEN_PROFILE_CALLEE)
    if sha256(recovered) != PROFILE_DRIVER_SHA256:
        raise RuntimeError("恢复的冻结 Profile driver 摘要不等于审核值 66e0…")
    return recovered


def validate_profile_driver_equivalence(
    profile_pre: bytes,
    profile_post: bytes,
    live_profile: bytes,
) -> None:
    if (
        profile_pre != profile_post
        or sha256(profile_pre) != PROFILE_DRIVER_SHA256
        or profile_pre.count(FROZEN_PROFILE_CALLEE) != PROFILE_LIVE_CALLEE_DELTA_COUNT
        or LIVE_PROFILE_CALLEE in profile_pre
    ):
        raise RuntimeError("Profile pre/post driver 不是逐字节相同的冻结程序")
    if recovered_frozen_profile_driver(live_profile) != profile_post:
        raise RuntimeError("当前 Profile driver 存在两处 finalizer 调用迁移之外的差异")


def validate_driver_artifacts() -> None:
    pre_body = PRE_BODY_DRIVER.read_bytes()
    post_body = POST_BODY_DRIVER.read_bytes()
    validate_body_driver_equivalence(pre_body, post_body)
    if post_body != LIVE_BODY_DRIVER.read_bytes():
        raise RuntimeError("当前 Body benchmark driver 与冻结 post driver 不一致")

    catalog_pre = PRE_CATALOG_DRIVER.read_bytes()
    catalog_post = POST_CATALOG_DRIVER.read_bytes()
    if (
        catalog_pre != catalog_post
        or catalog_post != LIVE_CATALOG_DRIVER.read_bytes()
        or sha256(catalog_pre) != CATALOG_DRIVER_SHA256
    ):
        raise RuntimeError("Catalog pre/post driver 不是逐字节相同的冻结程序")

    profile_pre = PRE_PROFILE_DRIVER.read_bytes()
    profile_post = POST_PROFILE_DRIVER.read_bytes()
    validate_profile_driver_equivalence(
        profile_pre,
        profile_post,
        LIVE_PROFILE_DRIVER.read_bytes(),
    )


def parse_benchmark(path: pathlib.Path, expected_cases: tuple[str, ...]) -> dict[str, list[tuple[Decimal, Decimal, Decimal]]]:
    samples: dict[str, list[tuple[Decimal, Decimal, Decimal]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = BENCHMARK_LINE.match(line.strip())
        if match is None:
            continue
        name = match.group(1)
        samples.setdefault(name, []).append(
            (Decimal(match.group(2)), Decimal(match.group(3)), Decimal(match.group(4)))
        )
    if set(samples) != set(expected_cases):
        raise RuntimeError(
            f"benchmark case 闭集漂移：{relative(path)} actual={sorted(samples)}"
        )
    for name, values in samples.items():
        if len(values) != 10:
            raise RuntimeError(f"benchmark 必须严格有 10 个样本：{name} actual={len(values)}")
    return samples


def median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def decimal_text(value: Decimal, places: int = 6) -> str:
    quantum = Decimal(1).scaleb(-places)
    text = format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def metrics(values: list[tuple[Decimal, Decimal, Decimal]]) -> tuple[Decimal, Decimal, Decimal]:
    return tuple(median([sample[index] for sample in values]) for index in range(3))  # type: ignore[return-value]


def body_case_result(
    name: str,
    pre_values: list[tuple[Decimal, Decimal, Decimal]],
    post_values: list[tuple[Decimal, Decimal, Decimal]],
) -> dict[str, Any]:
    pre = metrics(pre_values)
    post = metrics(post_values)
    ns_change = (post[0] - pre[0]) * Decimal(100) / pre[0]
    bytes_reduction = (pre[1] - post[1]) * Decimal(100) / pre[1]
    allocs_reduction = (pre[2] - post[2]) * Decimal(100) / pre[2]
    checks = {
        "bytes_reduction_at_least_30_percent": bytes_reduction >= Decimal(30),
        "allocs_reduction_at_least_30_percent": allocs_reduction >= Decimal(30),
        "ns_regression_not_over_5_percent": ns_change <= Decimal(5),
    }
    return {
        "name": name,
        "sample_count_pre": len(pre_values),
        "sample_count_post": len(post_values),
        "pre_median": {
            "ns_per_op": decimal_text(pre[0]),
            "bytes_per_op": decimal_text(pre[1]),
            "allocs_per_op": decimal_text(pre[2]),
        },
        "post_median": {
            "ns_per_op": decimal_text(post[0]),
            "bytes_per_op": decimal_text(post[1]),
            "allocs_per_op": decimal_text(post[2]),
        },
        "ns_change_percent": decimal_text(ns_change),
        "bytes_reduction_percent": decimal_text(bytes_reduction),
        "allocs_reduction_percent": decimal_text(allocs_reduction),
        "checks": checks,
        "result": "passed" if all(checks.values()) else "failed",
    }


def absolute_case_result(
    name: str,
    pre_values: list[tuple[Decimal, Decimal, Decimal]],
    post_values: list[tuple[Decimal, Decimal, Decimal]],
    expected_post_bytes: Decimal,
    expected_post_allocs: Decimal,
) -> dict[str, Any]:
    pre = metrics(pre_values)
    post = metrics(post_values)
    ns_change = (post[0] - pre[0]) * Decimal(100) / pre[0]
    every_sample_matches = all(
        sample[1] == expected_post_bytes and sample[2] == expected_post_allocs
        for sample in post_values
    )
    return {
        "name": name,
        "sample_count_pre": len(pre_values),
        "sample_count_post": len(post_values),
        "pre_median": {
            "ns_per_op": decimal_text(pre[0]),
            "bytes_per_op": decimal_text(pre[1]),
            "allocs_per_op": decimal_text(pre[2]),
        },
        "post_median": {
            "ns_per_op": decimal_text(post[0]),
            "bytes_per_op": decimal_text(post[1]),
            "allocs_per_op": decimal_text(post[2]),
        },
        "ns_change_percent": decimal_text(ns_change),
        "expected_post": {
            "bytes_per_op": decimal_text(expected_post_bytes),
            "allocs_per_op": decimal_text(expected_post_allocs),
        },
        "every_post_sample_matches_absolute_contract": every_sample_matches,
        "result": "passed" if every_sample_matches else "failed",
    }


def metadata_artifacts(metadata: dict[str, Any], key: str) -> list[dict[str, str]]:
    values = metadata.get(key)
    if not isinstance(values, list):
        raise RuntimeError(f"benchmark metadata 缺少数组：{key}")
    result: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise RuntimeError(f"benchmark metadata 条目非法：{item!r}")
        path = resolve_recorded_path(item["path"])
        if sha256(path.read_bytes()) != item.get("sha256"):
            raise RuntimeError(f"benchmark metadata 摘要与文件不一致：{item['path']}")
        result.append({"path": item["path"], "sha256": item["sha256"]})
    return result


def build_calculation() -> dict[str, Any]:
    validate_driver_artifacts()
    baseline_metadata = load_json(BASELINE_METADATA)
    post_metadata = load_json(POST_METADATA)
    baseline_fixture = baseline_metadata.get("fixture", {})
    post_fixture = post_metadata.get("fixture", {})
    if (
        baseline_fixture.get("generator_sha256") != PRE_BODY_SHA256
        or post_fixture.get("generator_sha256") != POST_BODY_SHA256
        or baseline_fixture.get("fixture_sha256") != FIXTURE_SHA256
        or post_fixture.get("fixture_sha256") != FIXTURE_SHA256
        or baseline_fixture.get("sizes") != post_fixture.get("sizes")
        or baseline_metadata.get("commands") != post_metadata.get("commands")
    ):
        raise RuntimeError("pre/post metadata 的 driver、fixture、sizes 或命令不等价")

    baseline_raw = metadata_artifacts(baseline_metadata, "raw_results")
    post_results = metadata_artifacts(post_metadata, "results")
    paths = {item["path"]: item for item in baseline_raw + post_results}

    body_pre_path = ROOT / "docs/egress/validation/baseline/benchmarks/body-pre.txt"
    body_post_path = ROOT / "docs/egress/validation/post/benchmarks/body-post.txt"
    catalog_pre_path = ROOT / "docs/egress/validation/baseline/benchmarks/catalog-pre.txt"
    catalog_post_path = ROOT / "docs/egress/validation/post/benchmarks/catalog-post.txt"
    profile_pre_path = ROOT / "docs/egress/validation/baseline/benchmarks/profile-pre.txt"
    profile_post_path = ROOT / "docs/egress/validation/post/benchmarks/profile-post.txt"
    for path in (
        body_pre_path,
        body_post_path,
        catalog_pre_path,
        catalog_post_path,
        profile_pre_path,
        profile_post_path,
    ):
        if historical_recorded_path(path) not in paths:
            raise RuntimeError(f"原始 benchmark 未进入 metadata 摘要链：{relative(path)}")

    body_pre = parse_benchmark(body_pre_path, BODY_CASES)
    body_post = parse_benchmark(body_post_path, BODY_CASES)
    catalog_pre = parse_benchmark(catalog_pre_path, CATALOG_CASES)
    catalog_post = parse_benchmark(catalog_post_path, CATALOG_CASES)
    profile_pre = parse_benchmark(profile_pre_path, PROFILE_CASES)
    profile_post = parse_benchmark(profile_post_path, PROFILE_CASES)

    body_results = [
        body_case_result(name, body_pre[name], body_post[name]) for name in BODY_CASES
    ]
    absolute_results = [
        absolute_case_result(
            CATALOG_CASES[0],
            catalog_pre[CATALOG_CASES[0]],
            catalog_post[CATALOG_CASES[0]],
            Decimal(0),
            Decimal(0),
        ),
        absolute_case_result(
            PROFILE_CASES[0],
            profile_pre[PROFILE_CASES[0]],
            profile_post[PROFILE_CASES[0]],
            Decimal(0),
            Decimal(0),
        ),
        absolute_case_result(
            PROFILE_CASES[1],
            profile_pre[PROFILE_CASES[1]],
            profile_post[PROFILE_CASES[1]],
            Decimal(3968),
            Decimal(2),
        ),
    ]
    passed = all(item["result"] == "passed" for item in body_results + absolute_results)
    return {
        "schema_version": "changeset6-benchmark-calculation/v1",
        "changeset": "6",
        "baseline_metadata": artifact(BASELINE_METADATA),
        "post_metadata": artifact(POST_METADATA),
        "drivers": {
            "body_pre": artifact(PRE_BODY_DRIVER),
            "body_post": artifact(POST_BODY_DRIVER),
            "catalog_pre": artifact(PRE_CATALOG_DRIVER),
            "catalog_post": artifact(POST_CATALOG_DRIVER),
            "profile_pre": artifact(PRE_PROFILE_DRIVER),
            "profile_post": artifact(POST_PROFILE_DRIVER),
        },
        "driver_equivalence": {
            "body_allowed_delta_count": 1,
            "body_allowed_delta": "CodexEgressPlan.Body 的旧 []byte constructor 适配为 post RequestBody",
            "catalog_byte_equal": True,
            "profile_byte_equal": True,
            "fixture_sha256": FIXTURE_SHA256,
            "commands_byte_equal": True,
        },
        "raw_results": baseline_raw
        + [item for item in post_results if item["path"].endswith("-post.txt")],
        "benchstat_results": [
            item for item in post_results if item["path"].endswith("-benchstat.txt")
        ],
        "body_cases": body_results,
        "catalog_and_profile_cases": absolute_results,
        "thresholds": {
            "body_minimum_bytes_reduction_percent": "30",
            "body_minimum_allocs_reduction_percent": "30",
            "maximum_ns_regression_percent": "5",
            "sample_count_each_side": 10,
        },
        "result": "passed" if passed else "failed",
    }


def write_evidence() -> None:
    post_body = LIVE_BODY_DRIVER.read_bytes()
    if sha256(post_body) != POST_BODY_SHA256:
        raise RuntimeError("当前 post Body driver 摘要不是已复审的 bce3…")
    pre_body = recovered_pre_body(post_body)
    catalog = LIVE_CATALOG_DRIVER.read_bytes()
    profile = recovered_frozen_profile_driver(LIVE_PROFILE_DRIVER.read_bytes())
    if sha256(catalog) != CATALOG_DRIVER_SHA256:
        raise RuntimeError("Catalog benchmark driver 已漂移")
    for path, raw in (
        (PRE_BODY_DRIVER, pre_body),
        (POST_BODY_DRIVER, post_body),
        (PRE_CATALOG_DRIVER, catalog),
        (POST_CATALOG_DRIVER, catalog),
        (PRE_PROFILE_DRIVER, profile),
        (POST_PROFILE_DRIVER, profile),
    ):
        write_frozen(path, raw)
    write_frozen(CALCULATION_PATH, canonical_json(build_calculation()))


def validate() -> None:
    expected = build_calculation()
    actual_raw = CALCULATION_PATH.read_bytes()
    actual = json.loads(actual_raw)
    if actual != expected:
        raise RuntimeError("benchmark calculation 与原始结果的确定性复算不一致")
    if actual.get("result") != "passed":
        raise RuntimeError("benchmark 原始结果阈值复算未通过")
    print(
        "变更集 6 benchmark 证据链有效：Body driver 仅 1 处声明适配，"
        "当前 Profile driver 仅 2 处 WS finalizer 退休适配，"
        f"7 个 case 原始结果复算通过，calculation SHA-256={sha256(actual_raw)}"
    )


def self_test() -> None:
    post = LIVE_BODY_DRIVER.read_bytes()
    pre = recovered_pre_body(post)
    validate_body_driver_equivalence(pre, post)
    mutations = (
        post.replace(b"fixtures.largeDirty), 2)", b"fixtures.largeDirty), 1)", 1),
        post.replace(b"b.ResetTimer()", b"b.StopTimer()", 1),
        post.replace(
            b"changeset6BenchmarkDigestSink = compiled.CompiledDigest()",
            b"changeset6BenchmarkDigestSink = testCase.endpointID",
            1,
        ),
        post.replace(FIXTURE_SHA256.encode("ascii"), b"0" * 64, 1),
    )
    for mutation in mutations:
        try:
            validate_body_driver_equivalence(pre, mutation)
        except RuntimeError:
            continue
        raise RuntimeError("benchmark driver 非法 mutation 未被等价门禁拒绝")

    live_profile = LIVE_PROFILE_DRIVER.read_bytes()
    frozen_profile = recovered_frozen_profile_driver(live_profile)
    validate_profile_driver_equivalence(frozen_profile, frozen_profile, live_profile)
    profile_mutations = (
        live_profile.replace(LIVE_PROFILE_CALLEE, FROZEN_PROFILE_CALLEE, 1),
        live_profile.replace(b"payload := []byte", b"sample := []byte", 1),
        live_profile + b"\n" + LIVE_PROFILE_CALLEE,
    )
    for mutation in profile_mutations:
        try:
            validate_profile_driver_equivalence(frozen_profile, frozen_profile, mutation)
        except RuntimeError:
            continue
        raise RuntimeError("Profile benchmark driver 非法 mutation 未被退休等价门禁拒绝")

    good = [(Decimal(100), Decimal(100), Decimal(100))] * 10
    insufficient = [(Decimal(96), Decimal(71), Decimal(71))] * 10
    regression = [(Decimal(106), Decimal(60), Decimal(60))] * 10
    if body_case_result("mutation", good, insufficient)["result"] != "failed":
        raise RuntimeError("不足 30% 的 bytes/allocs 降幅 mutation 未被拒绝")
    if body_case_result("mutation", good, regression)["result"] != "failed":
        raise RuntimeError("超过 5% 的 ns 回退 mutation 未被拒绝")
    print("变更集 6 benchmark driver 等价性与阈值判据 mutation 自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="首次生成冻结 driver 与计算结果")
    parser.add_argument("--self-test", action="store_true", help="运行等价性和阈值 mutation")
    args = parser.parse_args()
    if args.write:
        write_evidence()
    if args.self_test:
        self_test()
        return 0
    validate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
