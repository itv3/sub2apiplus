#!/usr/bin/env python3
"""Codex CLI 0.145.0 候选侧 42 条规则的严格离线验收器。

本工具只验证已经归档的源码、原始抓包、断言结果和环境恢复证据，
不会执行提交文件中声明的命令，避免验收阶段意外修改生产环境。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


ACCEPTANCE_SCHEMA_VERSION = "codex-candidate-42-acceptance/v1"
ASSERTION_SCHEMA_VERSION = "codex-candidate-rule-assertion/v1"
REPORT_SCHEMA_VERSION = "codex-candidate-42-acceptance-report/v1"
RULE_MANIFEST_SCHEMA_VERSION = "codex-egress-rule-manifest/v1"
CODEX_VERSION = "0.145.0"
REQUIRED_RULE_COUNT = 42

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

CANDIDATE_RAW_KINDS = frozenset(
    {
        "application_log",
        "database_trace",
        "filesystem_snapshot",
        "http_trace",
        "mitm_jsonl",
        "pcap",
        "pcapng",
        "process_trace",
        "relay_binary",
        "stderr_log",
        "stdout_log",
        "tls_keylog",
        "websocket_trace",
        "wire_dump",
    }
)
OFFICIAL_EVIDENCE_KINDS = CANDIDATE_RAW_KINDS | frozenset(
    {
        "official_analysis",
        "official_index",
        "official_report",
        "source_excerpt",
    }
)

PLACEHOLDER_VALUES = frozenset(
    {
        "placeholder",
        "todo",
        "tbd",
        "n/a",
        "待填写",
        "待补充",
        "待采集",
        "占位",
        "示例",
    }
)
TRIVIAL_COMMANDS = frozenset({":", "true", "/bin/true", "exit 0"})


@dataclass(frozen=True)
class Finding:
    """单个验收失败项。"""

    code: str
    message: str
    field: str
    rule_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "code": self.code,
            "message": self.message,
            "field": self.field,
        }
        if self.rule_id is not None:
            result["rule_id"] = self.rule_id
        return result


class AcceptanceValidator:
    """对候选侧验收提交执行封闭、可重复的离线校验。"""

    def __init__(
        self,
        *,
        manifest_path: Path,
        submission_path: Path,
        source_root: Path,
        evidence_root: Path,
    ) -> None:
        self.manifest_path = manifest_path
        self.submission_path = submission_path
        self.source_root = source_root
        self.evidence_root = evidence_root
        self.findings: list[Finding] = []
        self.required_rules: tuple[str, ...] = ()
        self.submitted_rule_count = 0

    def validate(self) -> dict[str, Any]:
        manifest = self._load_json_file(self.manifest_path, "rule_manifest")
        submission = self._load_json_file(self.submission_path, "submission")
        self._validate_roots()
        self._validate_manifest(manifest)
        self._validate_submission(submission)
        errors = [finding.as_dict() for finding in self.findings]
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "codex_version": CODEX_VERSION,
            "accepted": not errors,
            "required_rule_count": len(self.required_rules),
            "submitted_rule_count": self.submitted_rule_count,
            "error_count": len(errors),
            "errors": errors,
        }

    def _add(
        self,
        code: str,
        message: str,
        field: str,
        rule_id: str | None = None,
    ) -> None:
        self.findings.append(
            Finding(code=code, message=message, field=field, rule_id=rule_id)
        )

    def _load_json_file(self, path: Path, field: str) -> Any:
        try:
            if path.is_symlink() or not path.is_file():
                self._add(
                    "not_regular_file",
                    "必须指向非符号链接的普通 JSON 文件",
                    field,
                )
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            self._add("read_failed", f"读取失败：{error}", field)
        except json.JSONDecodeError as error:
            self._add("invalid_json", f"JSON 解析失败：{error}", field)
        return None

    def _validate_roots(self) -> None:
        for field, root in (
            ("source_root", self.source_root),
            ("evidence_root", self.evidence_root),
        ):
            if root.is_symlink() or not root.is_dir():
                self._add(
                    "invalid_root",
                    "根路径必须是非符号链接目录",
                    field,
                )

    def _validate_manifest(self, value: Any) -> None:
        if not isinstance(value, dict):
            self._add("invalid_type", "规则清单必须是对象", "rule_manifest")
            return
        self._check_keys(
            value,
            required={"schema_version", "codex_version", "required_rules"},
            allowed={"schema_version", "codex_version", "required_rules"},
            field="rule_manifest",
        )
        if value.get("schema_version") != RULE_MANIFEST_SCHEMA_VERSION:
            self._add(
                "manifest_schema_mismatch",
                f"规则清单版本必须为 {RULE_MANIFEST_SCHEMA_VERSION}",
                "rule_manifest.schema_version",
            )
        if value.get("codex_version") != CODEX_VERSION:
            self._add(
                "codex_version_mismatch",
                f"规则清单 Codex 版本必须为 {CODEX_VERSION}",
                "rule_manifest.codex_version",
            )
        raw_rules = value.get("required_rules")
        if not isinstance(raw_rules, list):
            self._add(
                "invalid_type", "required_rules 必须是数组", "rule_manifest.required_rules"
            )
            return
        rules: list[str] = []
        for index, item in enumerate(raw_rules):
            field = f"rule_manifest.required_rules[{index}]"
            if not isinstance(item, str) or not item.strip():
                self._add("invalid_rule_id", "规则 ID 必须是非空字符串", field)
                continue
            rules.append(item)
        duplicates = sorted({item for item in rules if rules.count(item) > 1})
        if duplicates:
            self._add(
                "duplicate_manifest_rules",
                f"规则清单存在重复项：{', '.join(duplicates)}",
                "rule_manifest.required_rules",
            )
        if len(rules) != REQUIRED_RULE_COUNT:
            self._add(
                "manifest_rule_count_mismatch",
                f"规则清单必须恰好包含 {REQUIRED_RULE_COUNT} 条，实际为 {len(rules)} 条",
                "rule_manifest.required_rules",
            )
        self.required_rules = tuple(rules)

    def _validate_submission(self, value: Any) -> None:
        if not isinstance(value, dict):
            self._add("invalid_type", "验收提交必须是对象", "submission")
            return
        required = {
            "schema_version",
            "codex_version",
            "assessment_id",
            "generated_at",
            "rule_manifest_sha256",
            "candidate_identity",
            "rules",
        }
        self._check_keys(value, required=required, allowed=required, field="submission")
        if value.get("schema_version") != ACCEPTANCE_SCHEMA_VERSION:
            self._add(
                "submission_schema_mismatch",
                f"验收提交版本必须为 {ACCEPTANCE_SCHEMA_VERSION}",
                "submission.schema_version",
            )
        if value.get("codex_version") != CODEX_VERSION:
            self._add(
                "codex_version_mismatch",
                f"验收提交 Codex 版本必须为 {CODEX_VERSION}",
                "submission.codex_version",
            )
        self._require_text(value.get("assessment_id"), "submission.assessment_id")
        self._require_timestamp(value.get("generated_at"), "submission.generated_at")
        manifest_sha = value.get("rule_manifest_sha256")
        if self._require_sha256(manifest_sha, "submission.rule_manifest_sha256"):
            try:
                actual = file_sha256(self.manifest_path)
            except OSError as error:
                self._add(
                    "read_failed",
                    f"无法计算规则清单摘要：{error}",
                    "submission.rule_manifest_sha256",
                )
            else:
                if manifest_sha != actual:
                    self._add(
                        "manifest_hash_mismatch",
                        "验收提交绑定的规则清单摘要与实际文件不一致",
                        "submission.rule_manifest_sha256",
                    )
        self._validate_candidate_identity(value.get("candidate_identity"))
        self._validate_rules(value.get("rules"))

    def _validate_candidate_identity(self, value: Any) -> None:
        field = "submission.candidate_identity"
        if not isinstance(value, dict):
            self._add("invalid_type", "candidate_identity 必须是对象", field)
            return
        required = {
            "git_commit",
            "source_tree_sha256",
            "image_reference",
            "image_digest",
            "deployed_version",
        }
        self._check_keys(value, required=required, allowed=required, field=field)
        commit = value.get("git_commit")
        if not isinstance(commit, str) or not GIT_COMMIT_RE.fullmatch(commit):
            self._add(
                "invalid_git_commit",
                "git_commit 必须是 40 位小写十六进制提交 ID",
                f"{field}.git_commit",
            )
        self._require_sha256(
            value.get("source_tree_sha256"), f"{field}.source_tree_sha256"
        )
        self._require_text(value.get("image_reference"), f"{field}.image_reference")
        image_digest = value.get("image_digest")
        if not isinstance(image_digest, str) or not IMAGE_DIGEST_RE.fullmatch(
            image_digest
        ):
            self._add(
                "invalid_image_digest",
                "image_digest 必须采用 sha256:<64 位小写十六进制> 格式",
                f"{field}.image_digest",
            )
        self._require_text(value.get("deployed_version"), f"{field}.deployed_version")

    def _validate_rules(self, value: Any) -> None:
        field = "submission.rules"
        if not isinstance(value, list):
            self._add("invalid_type", "rules 必须是数组", field)
            return
        self.submitted_rule_count = len(value)
        entries: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
        for index, item in enumerate(value):
            item_field = f"{field}[{index}]"
            if not isinstance(item, dict):
                self._add("invalid_type", "规则条目必须是对象", item_field)
                continue
            rule_id = item.get("rule_id")
            if not isinstance(rule_id, str) or not rule_id:
                self._add("invalid_rule_id", "rule_id 必须是非空字符串", item_field)
                continue
            entries.setdefault(rule_id, []).append((index, item))

        required_set = set(self.required_rules)
        submitted_set = set(entries)
        for rule_id in sorted(required_set - submitted_set):
            self._add(
                "missing_rule",
                "缺少规则验收条目",
                field,
                rule_id,
            )
        for rule_id in sorted(submitted_set - required_set):
            self._add(
                "unexpected_rule",
                "提交包含规则清单之外的条目",
                field,
                rule_id,
            )
        for rule_id, items in sorted(entries.items()):
            if len(items) > 1:
                self._add(
                    "duplicate_rule",
                    f"同一规则提交了 {len(items)} 个条目",
                    field,
                    rule_id,
                )
            if rule_id in required_set:
                index, item = items[0]
                self._validate_rule(item, index, rule_id)

    def _validate_rule(
        self, value: Mapping[str, Any], index: int, rule_id: str
    ) -> None:
        field = f"submission.rules[{index}]"
        required = {
            "rule_id",
            "implementation",
            "trigger",
            "official_evidence",
            "candidate_raw_evidence",
            "assertion",
            "environment_restoration",
        }
        self._check_keys(value, required=required, allowed=required, field=field, rule_id=rule_id)
        implementation = self._validate_implementation(
            value.get("implementation"), f"{field}.implementation", rule_id
        )
        del implementation
        self._validate_trigger(value.get("trigger"), f"{field}.trigger", rule_id)
        official_paths = self._validate_evidence_group(
            value.get("official_evidence"),
            f"{field}.official_evidence",
            rule_id,
            allowed_kinds=OFFICIAL_EVIDENCE_KINDS,
        )
        candidate_paths = self._validate_evidence_group(
            value.get("candidate_raw_evidence"),
            f"{field}.candidate_raw_evidence",
            rule_id,
            allowed_kinds=CANDIDATE_RAW_KINDS,
        )
        overlap = sorted(official_paths & candidate_paths)
        if overlap:
            self._add(
                "evidence_role_overlap",
                f"官方证据与候选证据不能引用同一文件：{', '.join(overlap)}",
                field,
                rule_id,
            )
        assertion_times = self._validate_assertion(
            value.get("assertion"),
            f"{field}.assertion",
            rule_id,
            candidate_paths,
        )
        self._validate_restoration(
            value.get("environment_restoration"),
            f"{field}.environment_restoration",
            rule_id,
            assertion_times,
        )

    def _validate_implementation(
        self, value: Any, field: str, rule_id: str
    ) -> set[str]:
        if not isinstance(value, dict):
            self._add("invalid_type", "implementation 必须是对象", field, rule_id)
            return set()
        required = {"summary", "locations"}
        self._check_keys(value, required=required, allowed=required, field=field, rule_id=rule_id)
        self._require_text(value.get("summary"), f"{field}.summary", rule_id)
        locations = value.get("locations")
        if not isinstance(locations, list) or not locations:
            self._add(
                "missing_implementation_location",
                "每条规则至少需要一个实现位置",
                f"{field}.locations",
                rule_id,
            )
            return set()
        paths: set[str] = set()
        for index, location in enumerate(locations):
            location_field = f"{field}.locations[{index}]"
            if not isinstance(location, dict):
                self._add("invalid_type", "实现位置必须是对象", location_field, rule_id)
                continue
            required_location = {
                "path",
                "sha256",
                "line_start",
                "line_end",
                "symbol",
            }
            self._check_keys(
                location,
                required=required_location,
                allowed=required_location,
                field=location_field,
                rule_id=rule_id,
            )
            resolved = self._validate_file_reference(
                location,
                root=self.source_root,
                field=location_field,
                rule_id=rule_id,
                allowed_kinds=None,
                require_kind=False,
            )
            raw_path = location.get("path")
            if isinstance(raw_path, str):
                if raw_path in paths:
                    self._add(
                        "duplicate_artifact",
                        "实现位置路径重复",
                        f"{location_field}.path",
                        rule_id,
                    )
                paths.add(raw_path)
            line_start = location.get("line_start")
            line_end = location.get("line_end")
            symbol = location.get("symbol")
            if not isinstance(line_start, int) or isinstance(line_start, bool) or line_start < 1:
                self._add(
                    "invalid_line_range",
                    "line_start 必须是大于等于 1 的整数",
                    f"{location_field}.line_start",
                    rule_id,
                )
            if (
                not isinstance(line_end, int)
                or isinstance(line_end, bool)
                or line_end < 1
                or (isinstance(line_start, int) and line_end < line_start)
            ):
                self._add(
                    "invalid_line_range",
                    "line_end 必须是不小于 line_start 的正整数",
                    f"{location_field}.line_end",
                    rule_id,
                )
            symbol_valid = self._require_text(
                symbol, f"{location_field}.symbol", rule_id
            )
            if (
                resolved is not None
                and isinstance(line_start, int)
                and not isinstance(line_start, bool)
                and isinstance(line_end, int)
                and not isinstance(line_end, bool)
                and 1 <= line_start <= line_end
            ):
                try:
                    lines = resolved.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError) as error:
                    self._add(
                        "source_read_failed",
                        f"无法读取实现源码：{error}",
                        location_field,
                        rule_id,
                    )
                else:
                    if line_end > len(lines):
                        self._add(
                            "line_range_out_of_bounds",
                            f"实现文件仅有 {len(lines)} 行",
                            location_field,
                            rule_id,
                        )
                    elif symbol_valid and symbol not in "\n".join(
                        lines[line_start - 1 : line_end]
                    ):
                        self._add(
                            "symbol_not_in_range",
                            "声明的符号未出现在指定行范围内",
                            f"{location_field}.symbol",
                            rule_id,
                        )
        return paths

    def _validate_trigger(self, value: Any, field: str, rule_id: str) -> None:
        if not isinstance(value, dict):
            self._add("invalid_type", "trigger 必须是对象", field, rule_id)
            return
        required = {"description", "preconditions", "command", "expected_observation"}
        self._check_keys(value, required=required, allowed=required, field=field, rule_id=rule_id)
        self._require_text(value.get("description"), f"{field}.description", rule_id)
        preconditions = value.get("preconditions")
        if not isinstance(preconditions, list) or not preconditions:
            self._add(
                "missing_trigger_preconditions",
                "触发条件至少需要一项前置条件",
                f"{field}.preconditions",
                rule_id,
            )
        else:
            for index, item in enumerate(preconditions):
                self._require_text(item, f"{field}.preconditions[{index}]", rule_id)
        self._validate_command(value.get("command"), f"{field}.command", rule_id)
        self._require_text(
            value.get("expected_observation"),
            f"{field}.expected_observation",
            rule_id,
        )

    def _validate_evidence_group(
        self,
        value: Any,
        field: str,
        rule_id: str,
        *,
        allowed_kinds: frozenset[str],
    ) -> set[str]:
        if not isinstance(value, dict):
            self._add("invalid_type", "证据组必须是对象", field, rule_id)
            return set()
        required = {"observation", "artifacts"}
        self._check_keys(value, required=required, allowed=required, field=field, rule_id=rule_id)
        self._require_text(value.get("observation"), f"{field}.observation", rule_id)
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            self._add(
                "missing_evidence_artifact",
                "证据组至少需要一个原始文件",
                f"{field}.artifacts",
                rule_id,
            )
            return set()
        paths: set[str] = set()
        for index, artifact in enumerate(artifacts):
            artifact_field = f"{field}.artifacts[{index}]"
            if not isinstance(artifact, dict):
                self._add("invalid_type", "证据引用必须是对象", artifact_field, rule_id)
                continue
            self._check_keys(
                artifact,
                required={"path", "sha256", "kind"},
                allowed={"path", "sha256", "kind"},
                field=artifact_field,
                rule_id=rule_id,
            )
            self._validate_file_reference(
                artifact,
                root=self.evidence_root,
                field=artifact_field,
                rule_id=rule_id,
                allowed_kinds=allowed_kinds,
                require_kind=True,
            )
            raw_path = artifact.get("path")
            if isinstance(raw_path, str):
                if raw_path in paths:
                    self._add(
                        "duplicate_artifact",
                        "同一证据组不能重复引用文件",
                        f"{artifact_field}.path",
                        rule_id,
                    )
                paths.add(raw_path)
        return paths

    def _validate_assertion(
        self,
        value: Any,
        field: str,
        rule_id: str,
        candidate_paths: set[str],
    ) -> tuple[datetime | None, datetime | None]:
        if not isinstance(value, dict):
            self._add("invalid_type", "assertion 必须是对象", field, rule_id)
            return (None, None)
        required = {"checker", "command", "result"}
        self._check_keys(value, required=required, allowed=required, field=field, rule_id=rule_id)
        checker = value.get("checker")
        checker_path: str | None = None
        checker_sha: str | None = None
        if not isinstance(checker, dict):
            self._add("invalid_type", "checker 必须是对象", f"{field}.checker", rule_id)
        else:
            self._check_keys(
                checker,
                required={"path", "sha256"},
                allowed={"path", "sha256"},
                field=f"{field}.checker",
                rule_id=rule_id,
            )
            self._validate_file_reference(
                checker,
                root=self.source_root,
                field=f"{field}.checker",
                rule_id=rule_id,
                allowed_kinds=None,
                require_kind=False,
            )
            if isinstance(checker.get("path"), str):
                checker_path = checker["path"]
            if isinstance(checker.get("sha256"), str):
                checker_sha = checker["sha256"]

        command = value.get("command")
        command_valid = self._validate_command(command, f"{field}.command", rule_id)
        if command_valid and checker_path is not None:
            normalized_checker = checker_path.replace("\\", "/")
            referenced = any(
                token.replace("\\", "/") == normalized_checker
                or token.replace("\\", "/").endswith(f"/{normalized_checker}")
                for token in command
            )
            if not referenced:
                self._add(
                    "checker_not_in_command",
                    "断言命令必须明确引用 checker 文件",
                    f"{field}.command",
                    rule_id,
                )

        result_ref = value.get("result")
        result_path: Path | None = None
        if not isinstance(result_ref, dict):
            self._add("invalid_type", "result 必须是对象", f"{field}.result", rule_id)
        else:
            self._check_keys(
                result_ref,
                required={"path", "sha256", "kind"},
                allowed={"path", "sha256", "kind"},
                field=f"{field}.result",
                rule_id=rule_id,
            )
            if result_ref.get("kind") != "assertion_result":
                self._add(
                    "invalid_artifact_kind",
                    "断言结果 kind 必须为 assertion_result",
                    f"{field}.result.kind",
                    rule_id,
                )
            result_path = self._validate_file_reference(
                result_ref,
                root=self.evidence_root,
                field=f"{field}.result",
                rule_id=rule_id,
                allowed_kinds=frozenset({"assertion_result"}),
                require_kind=True,
            )
        if result_path is None:
            return (None, None)
        result = self._load_json_artifact(result_path, f"{field}.result", rule_id)
        return self._validate_assertion_result(
            result,
            f"{field}.result",
            rule_id,
            checker_sha=checker_sha,
            command=command if command_valid else None,
            candidate_paths=candidate_paths,
        )

    def _validate_assertion_result(
        self,
        value: Any,
        field: str,
        rule_id: str,
        *,
        checker_sha: str | None,
        command: Sequence[str] | None,
        candidate_paths: set[str],
    ) -> tuple[datetime | None, datetime | None]:
        if not isinstance(value, dict):
            self._add(
                "invalid_assertion_result",
                "断言结果必须是结构化 JSON 对象；仅记录 exit=0 不构成验收",
                field,
                rule_id,
            )
            return (None, None)
        required = {
            "schema_version",
            "rule_id",
            "status",
            "started_at",
            "finished_at",
            "exit_code",
            "checker_sha256",
            "command_sha256",
            "checks",
        }
        self._check_keys(value, required=required, allowed=required, field=field, rule_id=rule_id)
        if value.get("schema_version") != ASSERTION_SCHEMA_VERSION:
            self._add(
                "assertion_schema_mismatch",
                f"断言结果版本必须为 {ASSERTION_SCHEMA_VERSION}",
                f"{field}.schema_version",
                rule_id,
            )
        if value.get("rule_id") != rule_id:
            self._add(
                "assertion_rule_mismatch",
                "断言结果 rule_id 与当前规则不一致",
                f"{field}.rule_id",
                rule_id,
            )
        if value.get("status") != "pass":
            self._add(
                "assertion_not_passed",
                "断言结果 status 必须为 pass",
                f"{field}.status",
                rule_id,
            )
        exit_code = value.get("exit_code")
        if exit_code != 0 or isinstance(exit_code, bool):
            self._add(
                "assertion_exit_nonzero",
                "断言执行 exit_code 必须为整数 0",
                f"{field}.exit_code",
                rule_id,
            )
        started_at = self._require_timestamp(
            value.get("started_at"), f"{field}.started_at", rule_id
        )
        finished_at = self._require_timestamp(
            value.get("finished_at"), f"{field}.finished_at", rule_id
        )
        if started_at is not None and finished_at is not None and finished_at < started_at:
            self._add(
                "invalid_execution_window",
                "finished_at 不能早于 started_at",
                field,
                rule_id,
            )
        if checker_sha is not None and value.get("checker_sha256") != checker_sha:
            self._add(
                "checker_hash_mismatch",
                "断言结果绑定的 checker 摘要与提交不一致",
                f"{field}.checker_sha256",
                rule_id,
            )
        if command is not None:
            expected_command_sha = command_sha256(command)
            if value.get("command_sha256") != expected_command_sha:
                self._add(
                    "command_hash_mismatch",
                    "断言结果绑定的命令摘要与提交不一致",
                    f"{field}.command_sha256",
                    rule_id,
                )
        checks = value.get("checks")
        if not isinstance(checks, list) or not checks:
            self._add(
                "missing_semantic_checks",
                "至少需要一项逐规则语义检查；仅任务成功或 exit=0 不合格",
                f"{field}.checks",
                rule_id,
            )
            return (started_at, finished_at)
        referenced_paths: set[str] = set()
        check_ids: set[str] = set()
        for index, check in enumerate(checks):
            check_field = f"{field}.checks[{index}]"
            if not isinstance(check, dict):
                self._add("invalid_type", "语义检查必须是对象", check_field, rule_id)
                continue
            required_check = {
                "id",
                "description",
                "passed",
                "expected",
                "actual",
                "evidence_paths",
            }
            self._check_keys(
                check,
                required=required_check,
                allowed=required_check,
                field=check_field,
                rule_id=rule_id,
            )
            check_id = check.get("id")
            if self._require_text(check_id, f"{check_field}.id", rule_id):
                if check_id in check_ids:
                    self._add(
                        "duplicate_check_id",
                        "同一规则内的检查 ID 必须唯一",
                        f"{check_field}.id",
                        rule_id,
                    )
                check_ids.add(check_id)
            self._require_text(
                check.get("description"), f"{check_field}.description", rule_id
            )
            if check.get("passed") is not True:
                self._add(
                    "semantic_check_failed",
                    "每项语义检查的 passed 都必须为 true",
                    f"{check_field}.passed",
                    rule_id,
                )
            for name in ("expected", "actual"):
                self._require_check_value(
                    check.get(name), f"{check_field}.{name}", rule_id
                )
            evidence_paths = check.get("evidence_paths")
            if not isinstance(evidence_paths, list) or not evidence_paths:
                self._add(
                    "missing_check_evidence",
                    "每项语义检查必须引用候选原始证据",
                    f"{check_field}.evidence_paths",
                    rule_id,
                )
                continue
            for path_index, raw_path in enumerate(evidence_paths):
                path_field = f"{check_field}.evidence_paths[{path_index}]"
                if not isinstance(raw_path, str) or raw_path not in candidate_paths:
                    self._add(
                        "unknown_candidate_evidence",
                        "断言只能引用本规则已声明的候选原始证据文件",
                        path_field,
                        rule_id,
                    )
                else:
                    referenced_paths.add(raw_path)
        unused_paths = sorted(candidate_paths - referenced_paths)
        if unused_paths:
            self._add(
                "unreferenced_candidate_evidence",
                f"候选原始证据未被任何语义检查引用：{', '.join(unused_paths)}",
                f"{field}.checks",
                rule_id,
            )
        return (started_at, finished_at)

    def _validate_restoration(
        self,
        value: Any,
        field: str,
        rule_id: str,
        assertion_times: tuple[datetime | None, datetime | None],
    ) -> None:
        if not isinstance(value, dict):
            self._add(
                "invalid_type", "environment_restoration 必须是对象", field, rule_id
            )
            return
        required = {"description", "state_pairs"}
        self._check_keys(value, required=required, allowed=required, field=field, rule_id=rule_id)
        self._require_text(value.get("description"), f"{field}.description", rule_id)
        pairs = value.get("state_pairs")
        if not isinstance(pairs, list) or not pairs:
            self._add(
                "missing_restoration_evidence",
                "至少需要一组抓包前后环境状态证据",
                f"{field}.state_pairs",
                rule_id,
            )
            return
        pair_names: set[str] = set()
        for index, pair in enumerate(pairs):
            pair_field = f"{field}.state_pairs[{index}]"
            if not isinstance(pair, dict):
                self._add("invalid_type", "状态证据对必须是对象", pair_field, rule_id)
                continue
            required_pair = {"name", "before", "after"}
            self._check_keys(
                pair,
                required=required_pair,
                allowed=required_pair,
                field=pair_field,
                rule_id=rule_id,
            )
            name = pair.get("name")
            if self._require_text(name, f"{pair_field}.name", rule_id):
                if name in pair_names:
                    self._add(
                        "duplicate_state_pair",
                        "同一规则内的状态证据对名称必须唯一",
                        f"{pair_field}.name",
                        rule_id,
                    )
                pair_names.add(name)
            before_path, before_time = self._validate_state_artifact(
                pair.get("before"), f"{pair_field}.before", rule_id
            )
            after_path, after_time = self._validate_state_artifact(
                pair.get("after"), f"{pair_field}.after", rule_id
            )
            if before_path is not None and after_path is not None:
                try:
                    same_file = os.path.samestat(before_path.stat(), after_path.stat())
                except OSError:
                    same_file = False
                if same_file:
                    self._add(
                        "same_restoration_artifact",
                        "before 与 after 必须是分别采集的两个文件，不能是同一文件或硬链接",
                        pair_field,
                        rule_id,
                    )
                if file_sha256(before_path) != file_sha256(after_path):
                    self._add(
                        "environment_not_restored",
                        "规范化环境状态在抓包前后不是逐字节一致",
                        pair_field,
                        rule_id,
                    )
            if before_time is not None and after_time is not None and after_time <= before_time:
                self._add(
                    "invalid_restoration_window",
                    "after.captured_at 必须晚于 before.captured_at",
                    pair_field,
                    rule_id,
                )
            started_at, finished_at = assertion_times
            if before_time is not None and started_at is not None and before_time > started_at:
                self._add(
                    "invalid_restoration_window",
                    "before 状态必须在规则断言开始前采集",
                    f"{pair_field}.before.captured_at",
                    rule_id,
                )
            if after_time is not None and finished_at is not None and after_time < finished_at:
                self._add(
                    "invalid_restoration_window",
                    "after 状态必须在规则断言结束后采集",
                    f"{pair_field}.after.captured_at",
                    rule_id,
                )

    def _validate_state_artifact(
        self, value: Any, field: str, rule_id: str
    ) -> tuple[Path | None, datetime | None]:
        if not isinstance(value, dict):
            self._add("invalid_type", "环境状态证据必须是对象", field, rule_id)
            return (None, None)
        required = {"path", "sha256", "kind", "captured_at"}
        self._check_keys(value, required=required, allowed=required, field=field, rule_id=rule_id)
        if value.get("kind") != "normalized_state":
            self._add(
                "invalid_artifact_kind",
                "环境状态证据 kind 必须为 normalized_state",
                f"{field}.kind",
                rule_id,
            )
        path = self._validate_file_reference(
            value,
            root=self.evidence_root,
            field=field,
            rule_id=rule_id,
            allowed_kinds=frozenset({"normalized_state"}),
            require_kind=True,
        )
        captured_at = self._require_timestamp(
            value.get("captured_at"), f"{field}.captured_at", rule_id
        )
        return (path, captured_at)

    def _validate_file_reference(
        self,
        value: Mapping[str, Any],
        *,
        root: Path,
        field: str,
        rule_id: str,
        allowed_kinds: frozenset[str] | None,
        require_kind: bool,
    ) -> Path | None:
        raw_path = value.get("path")
        relative_path = self._validate_relative_path(raw_path, f"{field}.path", rule_id)
        expected_sha = value.get("sha256")
        sha_valid = self._require_sha256(expected_sha, f"{field}.sha256", rule_id)
        kind: str | None = None
        kind_valid = False
        if require_kind:
            raw_kind = value.get("kind")
            if isinstance(raw_kind, str):
                kind = raw_kind
            if not isinstance(kind, str) or allowed_kinds is None or kind not in allowed_kinds:
                allowed = ", ".join(sorted(allowed_kinds or ()))
                self._add(
                    "invalid_artifact_kind",
                    f"kind 必须是允许值之一：{allowed}",
                    f"{field}.kind",
                    rule_id,
                )
            else:
                kind_valid = True
        if relative_path is None:
            return None
        root_resolved = root.resolve()
        current = root_resolved
        try:
            for part in relative_path.parts:
                current = current / part
                if current.is_symlink():
                    self._add(
                        "symlink_forbidden",
                        "证据和源码引用路径不能包含符号链接",
                        f"{field}.path",
                        rule_id,
                    )
                    return None
            resolved = current.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            self._add(
                "artifact_missing",
                f"引用文件不存在或无法解析：{error}",
                f"{field}.path",
                rule_id,
            )
            return None
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            self._add(
                "path_outside_root",
                "引用路径逃逸出允许的根目录",
                f"{field}.path",
                rule_id,
            )
            return None
        if not resolved.is_file():
            self._add(
                "not_regular_file",
                "引用必须指向普通文件，目录占位不构成证据",
                f"{field}.path",
                rule_id,
            )
            return None
        try:
            if resolved.stat().st_size <= 0:
                self._add(
                    "empty_artifact",
                    "空文件不构成证据",
                    f"{field}.path",
                    rule_id,
                )
                return None
            if is_placeholder_file(resolved):
                self._add(
                    "placeholder_artifact",
                    "占位文件不构成证据",
                    f"{field}.path",
                    rule_id,
                )
                return None
            actual_sha = file_sha256(resolved)
        except OSError as error:
            self._add(
                "read_failed",
                f"无法读取引用文件：{error}",
                f"{field}.path",
                rule_id,
            )
            return None
        if sha_valid and actual_sha != expected_sha:
            self._add(
                "artifact_hash_mismatch",
                "引用文件 SHA-256 与提交记录不一致",
                f"{field}.sha256",
                rule_id,
            )
        if kind_valid and kind is not None:
            self._validate_artifact_shape(resolved, kind, field, rule_id)
        return resolved

    def _validate_artifact_shape(
        self, path: Path, kind: str, field: str, rule_id: str
    ) -> None:
        """对具有稳定文件格式的证据执行最低限度的真实性检查。"""

        if kind == "pcap":
            try:
                payload = path.read_bytes()
            except OSError as error:
                self._add(
                    "read_failed",
                    f"无法检查 pcap 文件：{error}",
                    f"{field}.path",
                    rule_id,
                )
                return
            magics = {
                b"\xd4\xc3\xb2\xa1",
                b"\xa1\xb2\xc3\xd4",
                b"\x4d\x3c\xb2\xa1",
                b"\xa1\xb2\x3c\x4d",
            }
            if len(payload) < 24 or payload[:4] not in magics:
                self._add(
                    "invalid_pcap",
                    "kind=pcap 的文件必须包含合法的 pcap 全局头",
                    f"{field}.path",
                    rule_id,
                )
        elif kind == "pcapng":
            try:
                prefix = path.read_bytes()[:12]
            except OSError as error:
                self._add(
                    "read_failed",
                    f"无法检查 pcapng 文件：{error}",
                    f"{field}.path",
                    rule_id,
                )
                return
            if len(prefix) < 12 or prefix[:4] != b"\x0a\x0d\x0d\x0a":
                self._add(
                    "invalid_pcapng",
                    "kind=pcapng 的文件必须包含合法的分区头块",
                    f"{field}.path",
                    rule_id,
                )
        elif kind == "mitm_jsonl":
            try:
                lines = [
                    line
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                records = [json.loads(line) for line in lines]
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                self._add(
                    "invalid_mitm_jsonl",
                    f"kind=mitm_jsonl 的文件必须是逐行 JSON：{error}",
                    f"{field}.path",
                    rule_id,
                )
                return
            if not records or any(not isinstance(record, dict) for record in records):
                self._add(
                    "invalid_mitm_jsonl",
                    "mitm_jsonl 至少需要一个 JSON 对象记录",
                    f"{field}.path",
                    rule_id,
                )
        elif kind == "normalized_state":
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                self._add(
                    "invalid_normalized_state",
                    f"规范化环境状态必须是 JSON 对象：{error}",
                    f"{field}.path",
                    rule_id,
                )
                return
            if not isinstance(state, dict) or not state:
                self._add(
                    "invalid_normalized_state",
                    "规范化环境状态必须是包含实际状态字段的非空 JSON 对象",
                    f"{field}.path",
                    rule_id,
                )

    def _validate_relative_path(
        self, value: Any, field: str, rule_id: str
    ) -> PurePosixPath | None:
        if not isinstance(value, str) or not value.strip():
            self._add("invalid_path", "路径必须是非空字符串", field, rule_id)
            return None
        if "\\" in value:
            self._add("invalid_path", "路径必须使用 POSIX 分隔符", field, rule_id)
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or value in {".", ".."} or ".." in path.parts:
            self._add(
                "invalid_path",
                "路径必须是根目录内的相对路径，不能包含 ..",
                field,
                rule_id,
            )
            return None
        if any(part in {"", "."} for part in path.parts):
            self._add("invalid_path", "路径不能包含空段或当前目录段", field, rule_id)
            return None
        return path

    def _validate_command(
        self, value: Any, field: str, rule_id: str
    ) -> bool:
        if not isinstance(value, list) or not value:
            self._add("missing_command", "命令必须是非空参数数组", field, rule_id)
            return False
        valid = True
        for index, token in enumerate(value):
            if not isinstance(token, str) or not token.strip():
                self._add(
                    "invalid_command_token",
                    "命令参数必须是非空字符串",
                    f"{field}[{index}]",
                    rule_id,
                )
                valid = False
        if valid:
            joined = " ".join(value).strip().lower()
            if joined in TRIVIAL_COMMANDS:
                self._add(
                    "trivial_command",
                    "空操作命令不能作为触发器或断言",
                    field,
                    rule_id,
                )
                valid = False
        return valid

    def _load_json_artifact(self, path: Path, field: str, rule_id: str) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            self._add("read_failed", f"读取断言结果失败：{error}", field, rule_id)
        except json.JSONDecodeError as error:
            self._add("invalid_json", f"断言结果不是合法 JSON：{error}", field, rule_id)
        return None

    def _require_text(
        self, value: Any, field: str, rule_id: str | None = None
    ) -> bool:
        if not isinstance(value, str) or not value.strip():
            self._add("missing_text", "必须填写非空文本", field, rule_id)
            return False
        normalized = value.strip().lower().strip("<>[]{}：:。.!！")
        if normalized in PLACEHOLDER_VALUES or "待填写" in normalized or "待补充" in normalized:
            self._add("placeholder_text", "占位文本不构成验收内容", field, rule_id)
            return False
        return True

    def _require_sha256(
        self, value: Any, field: str, rule_id: str | None = None
    ) -> bool:
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            self._add(
                "invalid_sha256",
                "必须填写 64 位小写十六进制 SHA-256",
                field,
                rule_id,
            )
            return False
        return True

    def _require_check_value(
        self, value: Any, field: str, rule_id: str
    ) -> bool:
        """确保断言的期望值与实际值不是空壳。"""

        if value is None:
            self._add(
                "missing_check_value",
                "语义检查必须记录非空的期望值和实际值",
                field,
                rule_id,
            )
            return False
        if isinstance(value, str):
            return self._require_text(value, field, rule_id)
        if isinstance(value, (list, dict)) and not value:
            self._add(
                "missing_check_value",
                "语义检查的期望值和实际值不能是空集合",
                field,
                rule_id,
            )
            return False
        return True

    def _require_timestamp(
        self, value: Any, field: str, rule_id: str | None = None
    ) -> datetime | None:
        if not isinstance(value, str):
            self._add(
                "invalid_timestamp",
                "时间必须是带时区的 RFC 3339 字符串",
                field,
                rule_id,
            )
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            self._add(
                "invalid_timestamp",
                "时间必须是带时区的 RFC 3339 字符串",
                field,
                rule_id,
            )
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            self._add(
                "invalid_timestamp",
                "时间必须显式包含时区",
                field,
                rule_id,
            )
            return None
        return parsed

    def _check_keys(
        self,
        value: Mapping[str, Any],
        *,
        required: set[str],
        allowed: set[str],
        field: str,
        rule_id: str | None = None,
    ) -> None:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - allowed)
        for key in missing:
            self._add(
                "missing_field",
                f"缺少必填字段 {key}",
                f"{field}.{key}",
                rule_id,
            )
        for key in extra:
            self._add(
                "unexpected_field",
                f"不允许未知字段 {key}",
                f"{field}.{key}",
                rule_id,
            )


def file_sha256(path: Path) -> str:
    """流式计算普通文件的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_sha256(command: Sequence[str]) -> str:
    """以稳定 JSON 表达计算命令参数数组摘要。"""

    encoded = json.dumps(
        list(command), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_placeholder_file(path: Path) -> bool:
    """识别常见的空壳证据文件，不解析大型二进制抓包。"""

    if path.stat().st_size > 4096:
        return False
    try:
        text = path.read_text(encoding="utf-8").strip()
    except UnicodeError:
        return False
    normalized = text.lower().strip("<>[]{}：:。.!！\n\r\t ")
    if normalized in PLACEHOLDER_VALUES:
        return True
    return bool(
        re.fullmatch(
            r"(?:todo|tbd|placeholder|待填写|待补充|待采集|占位)(?:[\s:：_-].*)?",
            normalized,
        )
    )


def validate_acceptance(
    *,
    manifest_path: Path,
    submission_path: Path,
    source_root: Path,
    evidence_root: Path,
) -> dict[str, Any]:
    """执行完整验收并返回可持久化的结构化报告。"""

    return AcceptanceValidator(
        manifest_path=manifest_path,
        submission_path=submission_path,
        source_root=source_root,
        evidence_root=evidence_root,
    ).validate()


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _build_parser() -> argparse.ArgumentParser:
    default_manifest = Path(__file__).with_name(
        "codex_upgrade_rules_0_145_0.json"
    )
    parser = argparse.ArgumentParser(
        description=(
            "严格验证 Codex CLI 0.145.0 候选侧 42 条规则证据；"
            "不会执行验收提交中声明的任何命令。"
        )
    )
    parser.add_argument("--submission", type=Path, required=True, help="验收提交 JSON")
    parser.add_argument(
        "--manifest", type=Path, default=default_manifest, help="42 条规则清单 JSON"
    )
    parser.add_argument("--source-root", type=Path, required=True, help="候选源码根目录")
    parser.add_argument("--evidence-root", type=Path, required=True, help="证据归档根目录")
    parser.add_argument("--report", type=Path, help="可选的结构化验收报告输出路径")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = validate_acceptance(
        manifest_path=args.manifest,
        submission_path=args.submission,
        source_root=args.source_root,
        evidence_root=args.evidence_root,
    )
    if args.report is not None:
        _write_report(args.report, report)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
