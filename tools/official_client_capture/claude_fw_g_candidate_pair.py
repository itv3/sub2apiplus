#!/usr/bin/env python3
"""复算 Claude FW-G 实现覆盖并生成 106 条画像原子 PAIR 结果。

本工具在固定源码提交上直接执行 Go 门禁，只保存测试终态、摘要和源码锚点，
不保存测试输出、请求正文、账号身份或 OAuth 凭据。官方侧输入必须是独立
FW-G Campaign 的脱敏派生制品；本阶段不会签发 production_replacement 批准。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)


EXPECTED_VERSION = "2.1.226"
EXPECTED_REQUIRED_RULES = 40
EXPECTED_PROFILE_ASSERTIONS = 106
EXPECTED_SCENARIO_ASSERTIONS = 4
EXPECTED_ATOMIC_ASSERTIONS = 110
EXPECTED_COVERAGE_SHA256 = (
    "378a880d7293b7e9f3916031ee3b0e45884456371784ba6cd48ff15432cda6cb"
)
EXPECTED_REQUIRED_RULES_SHA256 = (
    "50261962778b8a7cf85f2dd01a8057f8004e92c0978456e88d9457d4ef8030b3"
)
EXPECTED_PROFILE_SHA256 = (
    "4da60bc238694a06a0dc80d68117abddd2de98c7c924c4db4c5dd929ea411e17"
)
EXPECTED_WIRE_SHA256 = (
    "c1c3c8c83710c9afc7005f71fa45d0837484a6bd042f75c08e5cde5451822a3e"
)
GO_MODULE = "github.com/Wei-Shaw/sub2api"

NEGATIVE_GATES = {
    "compatibility_and_official_claim_fail_close": [
        ("internal/officialegress", "TestClaudeFWGRejectsMalformedOfficialClaimAndLossyThirdParty"),
    ],
    "egress_disposition_and_unknown_route": [
        ("internal/officialegress", "TestClaudeFWGCandidateCatalogSeparatesStrictManagedAndDenied"),
        ("internal/officialegress", "TestClaudeFWGStrictAuxiliaryEndpointsUseExecutor"),
        ("internal/officialegress", "TestClaudeFWGCountTokensOfficialIngressIsClosed"),
    ],
    "tool_and_feature_fail_close": [
        ("internal/officialegress", "TestClaudeFWGToolPolicyIsClosed"),
        ("internal/officialegress", "TestClaudeFWGToolUseResultRelationsAreClosed"),
    ],
    "session_agent_and_order_fail_close": [
        ("internal/officialegress", "TestClaudeFWGSessionLineagesAndConcurrencyAreIndependent"),
        ("internal/officialegress", "TestClaudeFWGAgentLineageRequiresAcceptedParentAndMaxThreeLevels"),
        ("internal/officialegress", "TestClaudeFWGForkRequiresNewSessionAndTracksRequestOwnership"),
        ("internal/officialegress", "TestClaudeFWGTUITitleMustPrecedeMainRequest"),
        ("internal/officialegress", "TestClaudeFWGWebSearchRequiresOuterServerContinuationOrder"),
    ],
    "retry_and_attempt_boundary": [
        ("internal/officialegress", "TestClaudeFWGTransportErrorIsRetryableWithoutStateReuse"),
        ("internal/officialegress", "TestClaudeFWGRetryStatusBudgetAndRetryAfterPolicyAreClosed"),
    ],
    "service_route_transport_and_refresh_boundary": [
        ("internal/service", "TestClaudeFWGServiceCandidateUsesStrictTransportContext"),
        ("internal/service", "TestClaudeFWGServiceCountTokensUsesStrictCandidateRoute"),
        ("internal/service", "TestClaudeFWGServiceIngressSnapshotAndRouteAreClosed"),
        ("internal/service", "TestClaudeFWGServiceRefreshUsesStrictEndpoint"),
    ],
}

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
BEARER_RE = re.compile(r"(?i)\bBearer\s+(?!<)[A-Za-z0-9._~+/-]{20,}")
CLAUDE_TOKEN_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
CALLBACK_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}\b")


class CandidatePairError(RuntimeError):
    """表示候选身份、覆盖、测试或脱敏门禁失败。"""


def require(condition: bool, message: str) -> None:
    """失败即停止，禁止输出部分通过的 PAIR。"""

    if not condition:
        raise CandidatePairError(message)


def load_json(path: Path) -> dict[str, Any]:
    """读取顶层为对象的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidatePairError(f"无法读取 JSON：{path}: {exc}") from exc
    require(isinstance(value, dict), f"JSON 顶层不是对象：{path}")
    return value


def git_output(repository_root: Path, *args: str) -> str:
    """读取 Git 身份，不接受失败或多余输出。"""

    result = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    require(result.returncode == 0, f"Git 命令失败：{' '.join(args)}")
    return result.stdout.strip()


def freeze_source(repository_root: Path, expected_commit: str) -> dict[str, Any]:
    """要求源码处于指定的干净提交，并冻结 tree 身份。"""

    require(re.fullmatch(r"[0-9a-f]{40}", expected_commit) is not None, "源码提交格式非法")
    actual_commit = git_output(repository_root, "rev-parse", "HEAD")
    require(actual_commit == expected_commit, "当前 HEAD 不是候选指定提交")
    require(git_output(repository_root, "status", "--porcelain=v1") == "", "候选源码树不干净")
    tree = git_output(repository_root, "rev-parse", "HEAD^{tree}")
    require(re.fullmatch(r"[0-9a-f]{40}", tree) is not None, "Git tree 身份非法")
    return {"commit": actual_commit, "tree": tree, "clean": True}


def go_package_for_path(path: str) -> str:
    """从覆盖矩阵中的测试路径推导 Go test package。"""

    parts = Path(path).parts
    require(len(parts) >= 4 and parts[0:2] == ("backend", "internal"), f"测试路径不在 backend/internal：{path}")
    return f"{GO_MODULE}/internal/{parts[2]}"


def run_go_tests(repository_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """执行固定的两个 Go package，只保留顶层测试 pass 终态。"""

    command = [
        "go",
        "test",
        "-json",
        "-count=1",
        "./internal/officialegress",
        "./internal/service",
    ]
    result = subprocess.run(
        command,
        cwd=repository_root / "backend",
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    require(result.returncode == 0, "Claude FW-G Go 门禁失败")
    require(result.stderr.strip() == "", "Claude FW-G Go 门禁产生 stderr")
    tests: dict[tuple[str, str], dict[str, Any]] = {}
    package_pass: set[str] = set()
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CandidatePairError("Go test -json 输出含非法 JSON") from exc
        require(isinstance(event, dict), "Go test event 不是对象")
        package = event.get("Package")
        action = event.get("Action")
        test = event.get("Test")
        if action == "pass" and isinstance(package, str) and not test:
            package_pass.add(package)
        if (
            action == "pass"
            and isinstance(package, str)
            and isinstance(test, str)
            and "/" not in test
        ):
            key = (package, test)
            require(key not in tests, f"Go 顶层测试重复通过：{package} {test}")
            tests[key] = {
                "package": package,
                "test": test,
                "elapsed_seconds": event.get("Elapsed", 0),
                "result": "passed",
            }
    expected_packages = {
        f"{GO_MODULE}/internal/officialegress",
        f"{GO_MODULE}/internal/service",
    }
    require(expected_packages.issubset(package_pass), "Go package 终态未全部通过")
    return tests


def validate_rule_and_coverage(
    required_manifest: dict[str, Any],
    coverage: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """对齐 40 条规则、106 条画像断言和实现覆盖集合。"""

    require(required_manifest.get("target_version") == EXPECTED_VERSION, "RequiredRules 版本不匹配")
    require(required_manifest.get("required_rule_count") == EXPECTED_REQUIRED_RULES, "RequiredRules 不是 40 条")
    require(required_manifest.get("profile_atomic_assertion_count") == EXPECTED_PROFILE_ASSERTIONS, "画像断言不是 106 条")
    require(required_manifest.get("scenario_only_assertion_count") == EXPECTED_SCENARIO_ASSERTIONS, "场景断言不是 4 条")
    require(required_manifest.get("atomic_assertion_count") == EXPECTED_ATOMIC_ASSERTIONS, "原子断言不是 110 条")
    rules = required_manifest.get("required_rules")
    require(isinstance(rules, list) and len(rules) == EXPECTED_REQUIRED_RULES, "RequiredRules 数组不闭合")
    require(coverage.get("schema_version") == "claude-code-fw-g-implementation-coverage/v1", "实现覆盖 schema 不匹配")
    require(coverage.get("target_version") == EXPECTED_VERSION, "实现覆盖版本不匹配")
    require(coverage.get("required_rules_manifest_sha256") == EXPECTED_REQUIRED_RULES_SHA256, "实现覆盖未绑定 RequiredRules")
    entries = coverage.get("entries")
    require(isinstance(entries, list) and len(entries) == EXPECTED_REQUIRED_RULES, "实现覆盖不是 40 条")

    rule_map: dict[str, dict[str, Any]] = {}
    atomic_owner: dict[str, str] = {}
    for rule in rules:
        require(isinstance(rule, dict), "RequiredRule 不是对象")
        spec_id = rule.get("spec_id")
        atomic_ids = rule.get("atomic_assertion_ids")
        require(isinstance(spec_id, str) and isinstance(atomic_ids, list) and atomic_ids, "RequiredRule 身份非法")
        require(spec_id not in rule_map, f"RequiredRule 重复：{spec_id}")
        rule_map[spec_id] = rule
        for atomic_id in atomic_ids:
            require(isinstance(atomic_id, str) and atomic_id not in atomic_owner, f"画像断言重复归属：{atomic_id}")
            atomic_owner[atomic_id] = spec_id
    require(len(atomic_owner) == EXPECTED_PROFILE_ASSERTIONS, "画像原子断言归属不是 106 条")

    coverage_map: dict[str, dict[str, Any]] = {}
    for entry in entries:
        require(isinstance(entry, dict), "实现覆盖条目不是对象")
        spec_id = entry.get("spec_id")
        require(isinstance(spec_id, str) and spec_id not in coverage_map, f"实现覆盖重复：{spec_id}")
        require(entry.get("implementation_anchors") and entry.get("test_anchors"), f"实现覆盖锚点为空：{spec_id}")
        coverage_map[spec_id] = entry
    require(set(rule_map) == set(coverage_map), "实现覆盖集合不等于 RequiredRules")
    return rules, coverage_map, atomic_owner


def validate_source_anchors(
    repository_root: Path,
    coverage_map: dict[str, dict[str, Any]],
    go_tests: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """冻结全部实现／测试文件，并要求引用测试真实通过。"""

    files: dict[str, dict[str, Any]] = {}
    test_results: dict[str, dict[str, Any]] = {}
    for spec_id, entry in sorted(coverage_map.items()):
        for field, is_test in (("implementation_anchors", False), ("test_anchors", True)):
            for anchor in entry[field]:
                require(isinstance(anchor, dict), f"{spec_id} 的锚点不是对象")
                path = anchor.get("path")
                symbol = anchor.get("symbol")
                require(isinstance(path, str) and isinstance(symbol, str), f"{spec_id} 的锚点非法")
                source_path = (repository_root / path).resolve()
                require(source_path.is_relative_to(repository_root) and source_path.is_file(), f"{spec_id} 的源码文件不存在：{path}")
                files[path] = {
                    "path": path,
                    "sha256": sha256_file(source_path),
                    "bytes": source_path.stat().st_size,
                }
                if is_test:
                    package = go_package_for_path(path)
                    result = go_tests.get((package, symbol))
                    require(result is not None, f"{spec_id} 引用的测试没有通过：{package} {symbol}")
                    test_results[f"{package}#{symbol}"] = result
    for tests in NEGATIVE_GATES.values():
        for package_suffix, test in tests:
            package = f"{GO_MODULE}/{package_suffix}"
            result = go_tests.get((package, test))
            require(result is not None, f"负例门禁测试没有通过：{package} {test}")
            test_results[f"{package}#{test}"] = result
    return files, test_results


def build_pair_documents(
    official_atomic: dict[str, Any],
    official_rules: dict[str, Any],
    rules: list[dict[str, Any]],
    coverage_map: dict[str, dict[str, Any]],
    atomic_owner: dict[str, str],
    test_results: dict[str, dict[str, Any]],
    source: dict[str, Any],
    source_files: dict[str, dict[str, Any]],
    profile_binding: dict[str, Any],
    wire_binding: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """生成 106 条画像原子 PAIR、40 条规则 PAIR 和全局负例门禁。"""

    official_entries = official_atomic.get("entries")
    require(
        official_atomic.get("result") == "passed"
        and official_atomic.get("atomic_assertion_count") == EXPECTED_ATOMIC_ASSERTIONS
        and official_atomic.get("profile_atomic_assertion_count") == EXPECTED_PROFILE_ASSERTIONS
        and isinstance(official_entries, list),
        "官方原子复测制品不闭合",
    )
    require(
        official_rules.get("result") == "passed"
        and official_rules.get("required_rule_count") == EXPECTED_REQUIRED_RULES,
        "官方 RequiredRule 复测制品不闭合",
    )

    atomic_pairs: list[dict[str, Any]] = []
    official_atomic_ids: set[str] = set()
    for official in official_entries:
        require(isinstance(official, dict), "官方原子复测条目不是对象")
        atomic_id = official.get("spec_id")
        owner = official.get("profile_required_rule_id")
        scenario_group = official.get("scenario_only_group_id")
        require(isinstance(atomic_id, str) and atomic_id not in official_atomic_ids, f"官方原子断言重复：{atomic_id}")
        official_atomic_ids.add(atomic_id)
        if owner is None:
            require(isinstance(scenario_group, str), f"官方原子断言没有归属：{atomic_id}")
            continue
        require(atomic_owner.get(atomic_id) == owner, f"原子断言归属漂移：{atomic_id}")
        coverage = coverage_map[owner]
        test_refs = []
        for anchor in coverage["test_anchors"]:
            package = go_package_for_path(anchor["path"])
            key = f"{package}#{anchor['symbol']}"
            require(key in test_results, f"PAIR 缺少通过测试：{atomic_id} {key}")
            test_refs.append(key)
        atomic_pairs.append(
            {
                "pair_id": f"PAIR-{atomic_id}",
                "atomic_assertion_id": atomic_id,
                "required_rule_id": owner,
                "official_verification_sha256": canonical_sha256(official),
                "implementation_coverage_sha256": canonical_sha256(coverage),
                "candidate_profile_sha256": profile_binding["sha256"],
                "candidate_wire_sha256": wire_binding["sha256"],
                "test_refs": sorted(test_refs),
                "official_result": "passed",
                "candidate_result": "passed",
                "result": "passed",
            }
        )
    require(len(official_atomic_ids) == EXPECTED_ATOMIC_ASSERTIONS, "官方原子断言总集合不是 110 条")
    require(len(atomic_pairs) == EXPECTED_PROFILE_ASSERTIONS, "候选画像 PAIR 不是 106 条")
    require({item["atomic_assertion_id"] for item in atomic_pairs} == set(atomic_owner), "候选画像 PAIR 集合不闭合")

    atomics_by_rule: dict[str, list[dict[str, Any]]] = {}
    for pair in atomic_pairs:
        atomics_by_rule.setdefault(pair["required_rule_id"], []).append(pair)
    rule_pairs: list[dict[str, Any]] = []
    for rule in rules:
        spec_id = rule["spec_id"]
        pairs = sorted(atomics_by_rule.get(spec_id, []), key=lambda value: value["atomic_assertion_id"])
        require([value["atomic_assertion_id"] for value in pairs] == rule["atomic_assertion_ids"], f"RequiredRule 原子 PAIR 顺序或集合不一致：{spec_id}")
        coverage = coverage_map[spec_id]
        rule_pairs.append(
            {
                "pair_id": f"PAIR-{spec_id}",
                "spec_id": spec_id,
                "atomic_assertion_ids": rule["atomic_assertion_ids"],
                "atomic_pair_sha256": canonical_sha256(pairs),
                "implementation_anchors": coverage["implementation_anchors"],
                "test_anchors": coverage["test_anchors"],
                "result": "passed",
            }
        )

    negative_entries = []
    for gate_id, tests in sorted(NEGATIVE_GATES.items()):
        refs = []
        for package_suffix, test in tests:
            key = f"{GO_MODULE}/{package_suffix}#{test}"
            require(key in test_results, f"负例门禁测试未通过：{key}")
            refs.append(key)
        negative_entries.append({"gate_id": gate_id, "test_refs": refs, "result": "passed"})

    atomic_document = {
        "schema_version": "claude-code-fw-g-candidate-atomic-pair/v1",
        "target_version": EXPECTED_VERSION,
        "source": source,
        "candidate_profile": profile_binding,
        "candidate_wire": wire_binding,
        "profile_atomic_pair_count": EXPECTED_PROFILE_ASSERTIONS,
        "scenario_only_assertion_count": EXPECTED_SCENARIO_ASSERTIONS,
        "entries": sorted(atomic_pairs, key=lambda value: value["atomic_assertion_id"]),
        "unresolved_count": 0,
        "result": "passed",
    }
    rule_document = {
        "schema_version": "claude-code-fw-g-required-rule-candidate-pair/v1",
        "target_version": EXPECTED_VERSION,
        "required_rule_count": EXPECTED_REQUIRED_RULES,
        "entries": sorted(rule_pairs, key=lambda value: value["spec_id"]),
        "unresolved_count": 0,
        "promotion_eligibility": "blocked_until_dmit_acceptance_and_rollback",
        "result": "passed",
    }
    negative_document = {
        "schema_version": "claude-code-fw-g-candidate-negative-gates/v1",
        "target_version": EXPECTED_VERSION,
        "entries": negative_entries,
        "gate_count": len(negative_entries),
        "unresolved_count": 0,
        "result": "passed",
    }
    require(
        all(binding["path"] in source_files for binding in source_files.values()),
        "源码绑定内部不一致",
    )
    return atomic_document, rule_document, negative_document


def scan_documents(documents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """阻断 OAuth secret、回调码、邮箱和未脱敏 Bearer。"""

    patterns = {
        "email": EMAIL_RE,
        "bearer": BEARER_RE,
        "claude_token": CLAUDE_TOKEN_RE,
        "jwt": JWT_RE,
        "oauth_callback": CALLBACK_RE,
    }
    findings: list[dict[str, str]] = []
    scanned = []
    for name, document in sorted(documents.items()):
        raw = canonical_json_bytes(document)
        text = raw.decode("utf-8")
        scanned.append({"path": name, "sha256": canonical_sha256(document), "bytes": len(raw)})
        for pattern_name, pattern in patterns.items():
            if pattern.search(text):
                findings.append({"path": name, "pattern": pattern_name})
    require(not findings, f"候选 PAIR 制品命中敏感模式：{findings}")
    return {
        "schema_version": "claude-code-fw-g-candidate-secret-scan/v1",
        "scanned_files": scanned,
        "finding_count": 0,
        "result": "passed",
    }


def write_once(output_dir: Path, documents: dict[str, dict[str, Any]]) -> None:
    """以全新目录写入规范 JSON，禁止覆盖既有候选事实。"""

    require(not output_dir.exists(), f"输出目录已存在，禁止覆盖：{output_dir}")
    output_dir.mkdir(parents=True, mode=0o700)
    for name, document in sorted(documents.items()):
        path = output_dir / name
        path.write_bytes(canonical_json_bytes(document))
        path.chmod(0o600)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析候选源码、官方复测制品和输出位置。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--official-dir", required=True, type=Path)
    parser.add_argument("--required-rules", required=True, type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--wire", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行 Go 门禁并生成实现后逐规则 PAIR 制品。"""

    args = parse_args(argv)
    try:
        repository_root = args.repository_root.resolve()
        require(repository_root.is_dir(), "仓库根不存在")
        source = freeze_source(repository_root, args.expected_commit)
        required_path = args.required_rules.resolve()
        coverage_path = args.coverage.resolve()
        profile_path = args.profile.resolve()
        wire_path = args.wire.resolve()
        for path in (required_path, coverage_path, profile_path, wire_path):
            require(path.is_relative_to(repository_root) and path.is_file(), f"候选输入不在仓库内：{path}")
        require(sha256_file(required_path) == EXPECTED_REQUIRED_RULES_SHA256, "RequiredRules 摘要漂移")
        require(sha256_file(coverage_path) == EXPECTED_COVERAGE_SHA256, "实现覆盖矩阵摘要漂移")
        require(sha256_file(profile_path) == EXPECTED_PROFILE_SHA256, "候选 Profile 摘要漂移")
        require(sha256_file(wire_path) == EXPECTED_WIRE_SHA256, "候选 Wire 摘要漂移")

        required_manifest = load_json(required_path)
        coverage = load_json(coverage_path)
        rules, coverage_map, atomic_owner = validate_rule_and_coverage(required_manifest, coverage)
        go_tests = run_go_tests(repository_root)
        source_files, test_results = validate_source_anchors(
            repository_root, coverage_map, go_tests
        )
        official_dir = args.official_dir.resolve()
        require(official_dir.is_dir(), "官方复测派生目录不存在")
        official_atomic_path = official_dir / "official-atomic-verification.json"
        official_rules_path = official_dir / "required-rule-official-verification.json"
        official_manifest_path = official_dir / "portable-manifest.json"
        official_campaign_path = official_dir / "campaign-verification.json"
        official_atomic = load_json(official_atomic_path)
        official_rules = load_json(official_rules_path)
        official_manifest = load_json(official_manifest_path)
        official_campaign = load_json(official_campaign_path)
        require(official_manifest.get("approval_issued") is False and official_manifest.get("result") == "passed", "官方复测 portable manifest 非法")
        require(
            official_campaign.get("result") == "passed"
            and official_campaign.get("traffic_presence_policy", {}).get(
                "comparison_dimension"
            )
            is False
            and official_campaign.get("traffic_presence_policy", {}).get(
                "occurrence_counts_are_campaign_integrity_only"
            )
            is True,
            "官方复测没有冻结流量出现次数的非规则边界",
        )

        relative = lambda path: path.relative_to(repository_root).as_posix()
        profile_binding = {
            "path": relative(profile_path),
            "sha256": sha256_file(profile_path),
            "bytes": profile_path.stat().st_size,
        }
        wire_binding = {
            "path": relative(wire_path),
            "sha256": sha256_file(wire_path),
            "bytes": wire_path.stat().st_size,
        }
        atomic, rule_pairs, negatives = build_pair_documents(
            official_atomic,
            official_rules,
            rules,
            coverage_map,
            atomic_owner,
            test_results,
            source,
            source_files,
            profile_binding,
            wire_binding,
        )
        verification = {
            "schema_version": "claude-code-fw-g-candidate-verification/v1",
            "target_version": EXPECTED_VERSION,
            "source": source,
            "required_rules": {
                "path": relative(required_path),
                "sha256": sha256_file(required_path),
            },
            "implementation_coverage": {
                "path": relative(coverage_path),
                "sha256": sha256_file(coverage_path),
            },
            "candidate_profile": profile_binding,
            "candidate_wire": wire_binding,
            "official_inputs": {
                "portable_manifest_sha256": sha256_file(official_manifest_path),
                "campaign_verification_sha256": sha256_file(official_campaign_path),
                "official_atomic_verification_sha256": sha256_file(official_atomic_path),
                "required_rule_official_verification_sha256": sha256_file(official_rules_path),
            },
            "source_files": [source_files[path] for path in sorted(source_files)],
            "go_test": {
                "command": [
                    "go", "test", "-json", "-count=1",
                    "./internal/officialegress", "./internal/service",
                ],
                "working_directory": "backend",
                "referenced_test_count": len(test_results),
                "referenced_tests": [test_results[key] for key in sorted(test_results)],
                "result": "passed",
            },
            "production_selector_modified": False,
            "approval_issued": False,
            "result": "passed",
        }
        documents = {
            "candidate-atomic-pair.json": atomic,
            "required-rule-candidate-pair.json": rule_pairs,
            "candidate-negative-gates.json": negatives,
            "candidate-verification.json": verification,
        }
        manifest = {
            "schema_version": "claude-code-fw-g-candidate-portable-manifest/v1",
            "target_version": EXPECTED_VERSION,
            "source": source,
            "artifacts": [
                {
                    "path": name,
                    "sha256": canonical_sha256(document),
                    "bytes": len(canonical_json_bytes(document)),
                }
                for name, document in sorted(documents.items())
            ],
            "profile_atomic_pair_count": EXPECTED_PROFILE_ASSERTIONS,
            "required_rule_pair_count": EXPECTED_REQUIRED_RULES,
            "approval_issued": False,
            "promotion_eligibility": "blocked_until_dmit_acceptance_and_rollback",
            "result": "passed",
        }
        documents["portable-manifest.json"] = manifest
        documents["portable-secret-scan.json"] = scan_documents(documents)
        write_once(args.output_dir.resolve(), documents)
    except CandidatePairError as exc:
        print(f"Claude FW-G 候选 PAIR 失败：{exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "result": "passed",
                "profile_atomic_pair_count": EXPECTED_PROFILE_ASSERTIONS,
                "required_rule_pair_count": EXPECTED_REQUIRED_RULES,
                "approval_issued": False,
                "promotion_eligibility": "blocked_until_dmit_acceptance_and_rollback",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
