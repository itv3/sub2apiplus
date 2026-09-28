"""修好接着跑第 56 项：候选 direct 抓包里出口守护探针握手的声明式排除。

背景
----
ARM64 出口守护 ``sub2api-egress-guard.service`` 在网关容器的网络命名空间里按策略
``probe_urls``（``https://api.ipify.org``、``https://ipv4.icanhazip.com``、
``https://ifconfig.me/ip``，``probe_refresh_seconds=15``）周期探测公网出口；候选 direct
sidecar 在同一命名空间按 ``tcp port 443`` 抓包，于是这些探针的 ClientHello（带
``h2``／``http/1.1`` ALPN、31 个 cipher）被录进 ``direct/codex-http-*/egress.pcap``。
断言器此前把 pcap 里每个 ClientHello 都当成被测客户端的 ``tls_client_hello`` 观测，
SPEC-TLS-001 的 ``alpn-absent`` 与 SPEC-PROTO-001 的 ``no-alpn`` 只按 transport／ca_mode
选样，探针样本让两条判据在候选侧失败。

修复口径
--------
* 排除集合只来自证据标签声明（候选 direct pcap 规则的 ``environment_probe_sni``），
  由编目器原样写进 capture manifest，断言器按 manifest 声明把命中的 ClientHello
  改记为 ``environment_probe_client_hello``——仍是可见观测，只是不再属于候选出站面；
* 每条选 ``tls_client_hello`` 的 check 在 ``actual.environment_probe_exclusion`` 里给出
  被排除的记录数、主机与记录号，不静默丢弃；
* 官方侧声明禁止携带排除集合；没有声明时断言器逐字保持旧行为，判据本身不放宽；
* 排除集合不得包含被测出站面域名（chatgpt.com、openai.com、oaiusercontent.com 及其子域）。

本文件先以合成 pcap 复现 ARM64 上的失败形态，再验证声明、编目、manifest、断言与
审计各环节，并锁定仓库 0.157.0 声明的取值与出处。
"""

from __future__ import annotations

import copy
import json
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from tools.official_client_capture import build_evidence_catalog as catalog
from tools.official_client_capture import candidate_rule_assertion as checker
from tools.official_client_capture.acceptance_contract import (
    build_contract_payload as build_acceptance_contract,
    expected_check_ids_for_side,
)
from tools.official_client_capture.candidate_rule_assertion import (
    AssertionConfigurationError,
    _select_observations,
    _validate_capture_manifest,
    evaluate_rule,
    file_sha256,
    load_observations,
    load_profile,
    project_capture_manifest,
)

# 这两个名字是审计契约的一部分（写进封存的机器结果），测试按字面量钉死，
# 再由 test_checker_constants_are_pinned 核对断言器常量与之一致。
ENVIRONMENT_PROBE_RECORD_TYPE = "environment_probe_client_hello"
ENVIRONMENT_PROBE_AUDIT_FIELD = "environment_probe_exclusion"


TOOL_ROOT = Path(__file__).resolve().parents[1]
PROFILE_0157 = TOOL_ROOT / "candidate_rule_expectations_0_157_0.json"
RULES_0157 = TOOL_ROOT / "codex_upgrade_rules_0_157_0.json"
DECLARATION_0157 = TOOL_ROOT / "codex_upgrade_evidence_labels_0_157_0.json"
SCHEMA_PATH = TOOL_ROOT / "candidate_capture_manifest.schema.json"

# ARM64 出口守护策略 codex-01561-arm64-via-dmit（/etc/sub2api-egress/policy.json，
# 2026-09-28 只读核对时为 revision 3）的 probe_urls。声明里的排除集合必须恰好是这些
# URL 的主机名；策略更换探针时，本常量、声明与指南条目必须一起改并重新审核。
EGRESS_GUARD_PROBE_URLS = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://ifconfig.me/ip",
)
EGRESS_GUARD_PROBE_HOSTS = sorted(
    urlsplit(url).hostname for url in EGRESS_GUARD_PROBE_URLS
)
# 仓库 0.157.0 声明里应当携带排除集合的规则：三个候选 direct 作业的 sidecar pcap。
EXPECTED_PROBE_RULES = {
    ("candidate-core-direct", "direct/codex-http-*/egress.pcap"),
    ("candidate-core-direct", "direct/codex-ws-*/egress.pcap"),
    ("candidate-ws-handshake-repeat", "direct/codex-ws-*/egress.pcap"),
    ("candidate-compact-direct", "direct/codex-compact-*/egress.pcap"),
}
DIRECT_PREFIX = "c01570-unit-candidate-direct-core"
# 负例：（取值, 期望拒绝原因）。前五条是被测出站面域名及其子域，其余是格式问题。
PROTECTED_OR_MALFORMED_HOSTS = (
    (["chatgpt.com"], "被测出站面域名"),
    (["api.openai.com"], "被测出站面域名"),
    (["auth.openai.com", "ifconfig.me"], "被测出站面域名"),
    (["region-candidate-0145.oaiusercontent.com"], "被测出站面域名"),
    (["ab.chatgpt.com"], "被测出站面域名"),
    ([], "非空主机名数组"),
    ("ifconfig.me", "非空主机名数组"),
    (["IFCONFIG.ME"], "非法主机名"),
    (["ifconfig.me:443"], "非法主机名"),
    (["*.ipify.org"], "非法主机名"),
    (["ifconfig.me."], "非法主机名"),
    (["localhost"], "非法主机名"),
    ([443], "非法主机名"),
    (["ifconfig.me", "api.ipify.org"], "严格升序且不重复"),
    (["ifconfig.me", "ifconfig.me"], "严格升序且不重复"),
)


# ---------------------------------------------------------------------------
# 合成 pcap：以太网 + IPv4 + TCP + TLS ClientHello，只用标准库构造
# ---------------------------------------------------------------------------


def client_hello(sni: str, *, cipher_count: int, alpn: list[str]) -> bytes:
    """构造一条最小 TLS ClientHello 记录（含 SNI、可选 ALPN 与一个无关扩展）。"""

    ciphers = b"".join(struct.pack(">H", 0x1301 + index) for index in range(cipher_count))
    name = sni.encode("ascii")
    server_name = struct.pack(">HBH", len(name) + 3, 0, len(name)) + name
    extensions = struct.pack(">HH", 0, len(server_name)) + server_name
    if alpn:
        protocols = b"".join(bytes([len(item)]) + item.encode("ascii") for item in alpn)
        body = struct.pack(">H", len(protocols)) + protocols
        extensions += struct.pack(">HH", 16, len(body)) + body
    # supported_versions：让扩展序列不只有 SNI／ALPN，贴近真实握手形态。
    extensions += struct.pack(">HH", 43, 3) + b"\x02\x03\x04"
    hello = (
        b"\x03\x03"
        + bytes(32)
        + b"\x00"
        + struct.pack(">H", len(ciphers))
        + ciphers
        + b"\x01\x00"
        + struct.pack(">H", len(extensions))
        + extensions
    )
    handshake = b"\x01" + struct.pack(">I", len(hello))[1:] + hello
    return b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake


def ethernet_packet(payload: bytes, destination: str) -> bytes:
    tcp = struct.pack(">HHIIBBHHH", 40000, 443, 1, 0, 5 << 4, 0x18, 65535, 0, 0)
    tcp += payload
    ip = struct.pack(
        ">BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(tcp),
        0,
        0,
        64,
        6,
        0,
        socket.inet_aton("172.25.0.3"),
        socket.inet_aton(destination),
    )
    return b"\x00" * 12 + b"\x08\x00" + ip + tcp


def pcap_bytes(hellos: list[bytes]) -> bytes:
    """经典 pcap（微秒精度、以太网链路）；每条 ClientHello 各占一个包。"""

    output = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for index, hello in enumerate(hellos):
        packet = ethernet_packet(hello, f"104.18.32.{index + 1}")
        output += struct.pack("<IIII", 1_790_000_000 + index, 0, len(packet), len(packet))
        output += packet
    return output


def codex_http_hello() -> bytes:
    """被测客户端默认 HTTP 握手：30 个 cipher、不 offer ALPN（SPEC-TLS-001／PROTO-001 合规）。"""

    return client_hello("chatgpt.com", cipher_count=30, alpn=[])


def codex_ws_hello() -> bytes:
    """被测客户端 WS 握手（rustls）：10 个 cipher、不 offer ALPN。"""

    return client_hello("chatgpt.com", cipher_count=10, alpn=[])


def probe_hellos() -> list[bytes]:
    """出口守护三条探针握手：带 h2／http/1.1 ALPN、31 个 cipher（ARM64 实测形态）。"""

    return [
        client_hello(host, cipher_count=31, alpn=["h2", "http/1.1"])
        for host in ("api.ipify.org", "ifconfig.me", "ipv4.icanhazip.com")
    ]


def load_0157_profile() -> dict:
    return load_profile(
        PROFILE_0157,
        RULES_0157,
        verify_frozen_digest=False,
        expected_codex_version="0.157.0",
        expected_profile_sha256=file_sha256(PROFILE_0157),
    )


def repository_declaration() -> dict:
    return catalog.load_label_declaration(
        DECLARATION_0157, expected_codex_version="0.157.0"
    )


def without_probe_declarations(declaration: dict) -> dict:
    """去掉全部排除声明：等价于修复前的声明，也等价于官方侧的声明形态。"""

    stripped = copy.deepcopy(declaration)
    for entry in stripped["entries"]:
        for rule in entry["rules"]:
            rule.pop("environment_probe_sni", None)
    return stripped


def checks_by_id(checks: list[dict]) -> dict[str, dict]:
    return {check["id"]: check for check in checks}


class CandidateDirectBundle:
    """按候选 direct 采集形状造根，经编目器与 finalize 生成真实形态的 capture manifest。

    s1：只有被测客户端握手；s2：被测握手之间夹着三条探针（与 ARM64 codex-http-s2 同形）。
    """

    def __init__(self, root: Path, declaration: dict) -> None:
        self.bundle = root / "assertion-bundle"
        source = self.bundle / DIRECT_PREFIX
        layout = {
            "direct/codex-http-s1/egress.pcap": [codex_http_hello(), codex_http_hello()],
            "direct/codex-http-s2/egress.pcap": [
                codex_http_hello(),
                *probe_hellos(),
                codex_http_hello(),
                codex_http_hello(),
            ],
            "direct/codex-ws-s1/egress.pcap": [codex_ws_hello(), codex_http_hello()],
            "direct/codex-ws-s2/egress.pcap": [
                codex_ws_hello(),
                *probe_hellos(),
                codex_http_hello(),
            ],
        }
        for relative, hellos in layout.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pcap_bytes(hellos))
        self.catalog = catalog.build_catalog(
            declaration,
            {"candidate-core-direct": [(DIRECT_PREFIX, source)]},
            side="candidate",
        )
        self.manifest_value = catalog.finalize_manifest(
            self.catalog["manifest_draft"],
            self.bundle,
            codex_version="0.157.0",
            capture_id="unit-item56",
        )
        self.manifest = root / "capture-manifest.json"
        self.manifest.write_text(
            json.dumps(self.manifest_value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def observations(self):
        return load_observations(self.manifest, self.bundle, "0.157.0")


class EnvironmentProbeReproductionTest(unittest.TestCase):
    """复现 ARM64 失败形态，并证明声明式排除只作用于候选 direct pcap。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = load_0157_profile()

    def setUp(self) -> None:
        self.workdir = tempfile.TemporaryDirectory(prefix="item56-")
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)

    def _evaluate(self, declaration: dict, rule_id: str) -> dict[str, dict]:
        bundle = CandidateDirectBundle(self.root / rule_id, declaration)
        manifest, observations = bundle.observations()
        return checks_by_id(
            evaluate_rule(self.profile, rule_id, observations, manifest, side="candidate")
        )

    def test_probe_hellos_fail_tls001_and_proto001_without_declaration(self) -> None:
        """没有排除声明时（修复前／官方侧形态），探针握手照旧让判据失败——判据未被放宽。"""

        declaration = without_probe_declarations(repository_declaration())
        tls = self._evaluate(declaration, "SPEC-TLS-001")
        self.assertFalse(tls["alpn-absent"]["passed"], tls["alpn-absent"])
        self.assertIn(True, tls["alpn-absent"]["actual"]["values"])
        # cipher-count 本来就按 data.sni == chatgpt.com 选样，不受探针影响。
        self.assertTrue(tls["cipher-count"]["passed"], tls["cipher-count"])
        self.assertTrue(tls["http-clienthello-count"]["passed"])
        self.assertEqual(tls["http-clienthello-count"]["actual"]["matched_count"], 8)
        for check in tls.values():
            self.assertNotIn(ENVIRONMENT_PROBE_AUDIT_FIELD, check["actual"])
        proto = self._evaluate(declaration, "SPEC-PROTO-001")
        self.assertFalse(proto["no-alpn"]["passed"], proto["no-alpn"])
        self.assertIn(["h2", "http/1.1"], proto["no-alpn"]["actual"]["values"])

    def test_repository_declaration_excludes_probes_for_tls001_and_proto001(self) -> None:
        """仓库 0.157.0 声明给候选 direct pcap 声明了探针主机：两条判据按被测握手通过。"""

        tls = self._evaluate(repository_declaration(), "SPEC-TLS-001")
        self.assertTrue(tls["alpn-absent"]["passed"], tls["alpn-absent"])
        self.assertEqual(tls["alpn-absent"]["actual"]["values"], [False] * 5)
        self.assertTrue(tls["http-clienthello-count"]["passed"])
        self.assertEqual(tls["http-clienthello-count"]["actual"]["matched_count"], 5)
        self.assertTrue(tls["cipher-count"]["passed"])
        audit = tls["alpn-absent"]["actual"][ENVIRONMENT_PROBE_AUDIT_FIELD]
        self.assertEqual(
            audit,
            {
                "record_type": ENVIRONMENT_PROBE_RECORD_TYPE,
                "excluded_count": 3,
                "excluded_hosts": EGRESS_GUARD_PROBE_HOSTS,
                "excluded_record_ids": [
                    f"{DIRECT_PREFIX}/direct/codex-http-s2/egress.pcap#packet-{index}"
                    for index in (2, 3, 4)
                ],
            },
        )
        self.assertEqual(
            tls["http-clienthello-count"]["actual"][ENVIRONMENT_PROBE_AUDIT_FIELD], audit
        )
        # cipher-count 的 selector 本来就排除了探针（sni 条件），不应伪造审计记录。
        self.assertNotIn(ENVIRONMENT_PROBE_AUDIT_FIELD, tls["cipher-count"]["actual"])
        proto = self._evaluate(repository_declaration(), "SPEC-PROTO-001")
        self.assertTrue(proto["no-alpn"]["passed"], proto["no-alpn"])
        self.assertEqual(proto["no-alpn"]["actual"]["values"], [[]] * 5)
        self.assertEqual(proto["no-alpn"]["actual"][ENVIRONMENT_PROBE_AUDIT_FIELD], audit)
        # h1-wire 选 http_request，与 pcap 排除无关：不出现审计字段。
        self.assertNotIn(ENVIRONMENT_PROBE_AUDIT_FIELD, proto["h1-wire"]["actual"])

    def test_every_client_hello_selector_reports_the_exclusion(self) -> None:
        """EP-002 的 A01 SNI 取值同样不再混入探针主机，并附带同一份审计。"""

        ep002 = self._evaluate(repository_declaration(), "SPEC-EP-002")
        chatgpt = ep002["chatgpt-sni"]
        self.assertTrue(chatgpt["passed"])
        self.assertEqual(set(chatgpt["actual"]["values"]), {"chatgpt.com"})
        self.assertEqual(
            chatgpt["actual"][ENVIRONMENT_PROBE_AUDIT_FIELD]["excluded_hosts"],
            EGRESS_GUARD_PROBE_HOSTS,
        )

    def test_probe_records_remain_observable_and_counts_unchanged(self) -> None:
        """探针握手改记为环境探针观测而非丢弃：总观测数与修复前一致，逐条可查。"""

        declared = CandidateDirectBundle(self.root / "declared", repository_declaration())
        plain = CandidateDirectBundle(
            self.root / "plain", without_probe_declarations(repository_declaration())
        )
        _, declared_observations = declared.observations()
        _, plain_observations = plain.observations()
        self.assertEqual(len(declared_observations), len(plain_observations))
        probes = [
            item
            for item in declared_observations
            if item.record_type == ENVIRONMENT_PROBE_RECORD_TYPE
        ]
        self.assertEqual(len(probes), 6)
        self.assertEqual(sorted({item.data["sni"] for item in probes}), EGRESS_GUARD_PROBE_HOSTS)
        self.assertTrue(all(item.data["alpn_protocols"] == ["h2", "http/1.1"] for item in probes))
        self.assertFalse(
            any(item.record_type == ENVIRONMENT_PROBE_RECORD_TYPE for item in plain_observations)
        )
        # 除探针被改记类型外，两份观测逐条一致（record_id、数据与标签都不变）。
        by_id = {item.record_id: item for item in plain_observations}
        for item in declared_observations:
            original = by_id[item.record_id]
            self.assertEqual(item.data, original.data)
            self.assertEqual(item.labels, original.labels)
            if item.record_type != ENVIRONMENT_PROBE_RECORD_TYPE:
                self.assertEqual(item.record_type, original.record_type)
            else:
                self.assertEqual(original.record_type, "tls_client_hello")

    def test_seal_selector_precheck_still_hits_codex_hellos(self) -> None:
        """seal 预检（每个 check 至少命中一条观测）在排除后仍由被测握手满足。"""

        bundle = CandidateDirectBundle(self.root / "seal", repository_declaration())
        _, observations = bundle.observations()
        rules = {rule["rule_id"]: rule for rule in self.profile["rules"]}
        for rule_id, check_id in (
            ("SPEC-TLS-001", "http-clienthello-count"),
            ("SPEC-TLS-001", "cipher-count"),
            ("SPEC-TLS-001", "alpn-absent"),
            ("SPEC-PROTO-001", "no-alpn"),
        ):
            rule = rules[rule_id]
            check = next(item for item in rule["checks"] if item["id"] == check_id)
            matched = _select_observations(observations, check["select"], rule["scenario_ids"])
            self.assertTrue(matched, (rule_id, check_id))
            self.assertTrue(all(item.data["sni"] == "chatgpt.com" for item in matched))

    def test_projection_keeps_declaration_and_same_result(self) -> None:
        """改造 5 的逐规则投影逐字保留排除声明，投影输入下的结果与整份 manifest 一致。"""

        bundle = CandidateDirectBundle(self.root / "projection", repository_declaration())
        manifest, observations = bundle.observations()
        projection = project_capture_manifest(
            self.profile, "SPEC-TLS-001", manifest, bundle.bundle, "0.157.0"
        )
        http_artifacts = [
            item
            for item in projection["artifacts"]
            if "/direct/codex-http-" in item["path"]
        ]
        self.assertEqual(len(http_artifacts), 2)
        for artifact in http_artifacts:
            self.assertEqual(artifact["environment_probe_sni"], EGRESS_GUARD_PROBE_HOSTS)
        projection_path = self.root / "projection.json"
        projection_path.write_text(json.dumps(projection), encoding="utf-8")
        projected_manifest, projected_observations = load_observations(
            projection_path, bundle.bundle, "0.157.0"
        )
        self.assertEqual(
            evaluate_rule(
                self.profile, "SPEC-TLS-001", projected_observations, projected_manifest, side="candidate"
            ),
            evaluate_rule(self.profile, "SPEC-TLS-001", observations, manifest, side="candidate"),
        )

    def test_acceptance_contract_is_unchanged_by_the_exclusion(self) -> None:
        """排除不改变验收契约：check 全集与侧别限定仍由批准画像单独决定。"""

        contract = build_acceptance_contract(self.profile)
        bundle = CandidateDirectBundle(self.root / "contract", repository_declaration())
        manifest, observations = bundle.observations()
        for rule_id in ("SPEC-TLS-001", "SPEC-PROTO-001", "SPEC-EP-002"):
            checks = evaluate_rule(self.profile, rule_id, observations, manifest, side="candidate")
            self.assertEqual(
                sorted(check["id"] for check in checks),
                sorted(expected_check_ids_for_side(contract, rule_id, "candidate")),
            )

    def test_official_side_rejects_probe_declaration_in_manifest(self) -> None:
        """判据层纵深防护：官方侧断言遇到带排除声明的 manifest 直接失败关闭。"""

        bundle = CandidateDirectBundle(self.root / "official", repository_declaration())
        manifest, observations = bundle.observations()
        with self.assertRaisesRegex(AssertionConfigurationError, "官方侧 capture manifest"):
            evaluate_rule(self.profile, "SPEC-TLS-001", observations, manifest, side="official")
        # 未给侧别（全集评估）与候选侧照常执行，且探针同样被排除。
        for side in (None, "candidate"):
            checks = checks_by_id(
                evaluate_rule(self.profile, "SPEC-TLS-001", observations, manifest, side=side)
            )
            self.assertTrue(checks["alpn-absent"]["passed"], side)
        # 官方侧形态（无声明）的 manifest 照常执行，结果中不出现审计字段。
        plain = CandidateDirectBundle(
            self.root / "official-plain", without_probe_declarations(repository_declaration())
        )
        plain_manifest, plain_observations = plain.observations()
        official = checks_by_id(
            evaluate_rule(
                self.profile, "SPEC-TLS-001", plain_observations, plain_manifest, side="official"
            )
        )
        self.assertFalse(official["alpn-absent"]["passed"])
        self.assertNotIn(ENVIRONMENT_PROBE_AUDIT_FIELD, official["alpn-absent"]["actual"])

    def test_checker_constants_are_pinned(self) -> None:
        """断言器的记录类型与审计字段名必须与本文件钉死的字面量一致。"""

        self.assertEqual(checker.ENVIRONMENT_PROBE_RECORD_TYPE, ENVIRONMENT_PROBE_RECORD_TYPE)
        self.assertEqual(checker.ENVIRONMENT_PROBE_AUDIT_FIELD, ENVIRONMENT_PROBE_AUDIT_FIELD)
        # 新记录类型不得混进画像可选的记录类型，否则某个 selector 可能直接选中探针。
        from tools.official_client_capture.acceptance_contract import KNOWN_RECORD_TYPES

        self.assertNotIn(ENVIRONMENT_PROBE_RECORD_TYPE, KNOWN_RECORD_TYPES)


class EnvironmentProbeDeclarationContractTest(unittest.TestCase):
    """声明侧约束：只许候选侧 pcap 规则声明、主机名规范、不得覆盖被测出站面。"""

    def setUp(self) -> None:
        self.workdir = tempfile.TemporaryDirectory(prefix="item56-decl-")
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)

    def _load(self, declaration: dict) -> dict:
        path = self.root / "declaration.json"
        path.write_text(json.dumps(declaration, ensure_ascii=False), encoding="utf-8")
        return catalog.load_label_declaration(path, expected_codex_version="0.157.0")

    @staticmethod
    def _rule(declaration: dict, job_id: str, glob: str) -> dict:
        entry = next(item for item in declaration["entries"] if item["job_id"] == job_id)
        return next(item for item in entry["rules"] if item["glob"] == glob)

    def test_repository_0157_declares_guard_probes_only_on_candidate_direct_pcaps(self) -> None:
        declaration = repository_declaration()
        declared = {
            (entry["job_id"], rule["glob"]): rule
            for entry in declaration["entries"]
            for rule in entry["rules"]
            if "environment_probe_sni" in rule
        }
        self.assertEqual(set(declared), EXPECTED_PROBE_RULES)
        sides = {entry["job_id"]: entry["side"] for entry in declaration["entries"]}
        for (job_id, glob), rule in declared.items():
            self.assertEqual(sides[job_id], "candidate")
            self.assertEqual(rule["parser"], "pcap_client_hello")
            self.assertTrue(glob.startswith("direct/"), glob)
            self.assertEqual(rule["environment_probe_sni"], EGRESS_GUARD_PROBE_HOSTS)
            # 出处写进 rationale，审核时能追溯到出口守护策略。
            self.assertIn("probe_urls", rule["rationale"])
            self.assertIn("sub2api-egress-guard", rule["rationale"])

    def test_historical_declarations_are_not_rewritten(self) -> None:
        """只改目标版本 0.157.0；历史版本声明不追溯加排除，官方封存证据的编目口径不动。"""

        for path in sorted(TOOL_ROOT.glob("codex_upgrade_evidence_labels_*.json")):
            if path == DECLARATION_0157:
                continue
            self.assertNotIn("environment_probe_sni", path.read_text(encoding="utf-8"), path.name)

    def test_official_side_declaration_is_rejected(self) -> None:
        declaration = json.loads(DECLARATION_0157.read_text(encoding="utf-8"))
        rule = self._rule(declaration, "official-core", "direct/codex-http/*/traffic.pcap")
        rule["environment_probe_sni"] = list(EGRESS_GUARD_PROBE_HOSTS)
        with self.assertRaisesRegex(catalog.EvidenceCatalogError, "只允许候选侧"):
            self._load(declaration)

    def test_non_pcap_rule_declaration_is_rejected(self) -> None:
        declaration = json.loads(DECLARATION_0157.read_text(encoding="utf-8"))
        rule = self._rule(
            declaration,
            "candidate-compact-direct",
            "ingress/codex-compact-*/codex-ingress-http.jsonl",
        )
        rule["environment_probe_sni"] = list(EGRESS_GUARD_PROBE_HOSTS)
        with self.assertRaisesRegex(catalog.EvidenceCatalogError, "pcap_client_hello"):
            self._load(declaration)

    def test_protected_and_malformed_hosts_are_rejected(self) -> None:
        """逐类核对拒绝原因，避免“字段未知”这类无关拒绝把负例伪装成通过。"""

        for hosts, reason in PROTECTED_OR_MALFORMED_HOSTS:
            with self.subTest(hosts=hosts):
                declaration = json.loads(DECLARATION_0157.read_text(encoding="utf-8"))
                rule = self._rule(
                    declaration, "candidate-core-direct", "direct/codex-http-*/egress.pcap"
                )
                rule["environment_probe_sni"] = hosts
                with self.assertRaisesRegex(catalog.EvidenceCatalogError, reason):
                    self._load(declaration)

    def test_catalog_writes_declaration_only_onto_declared_artifacts(self) -> None:
        bundle = CandidateDirectBundle(self.root, repository_declaration())
        for artifact in bundle.manifest_value["artifacts"]:
            self.assertEqual(artifact["parser"], "pcap_client_hello")
            self.assertEqual(artifact["environment_probe_sni"], EGRESS_GUARD_PROBE_HOSTS)
        plain = CandidateDirectBundle(
            self.root / "plain", without_probe_declarations(repository_declaration())
        )
        for artifact in plain.manifest_value["artifacts"]:
            self.assertNotIn("environment_probe_sni", artifact)
        # 除排除声明外，manifest 的每个字段都与无声明时逐字相同。
        stripped = [
            {key: value for key, value in artifact.items() if key != "environment_probe_sni"}
            for artifact in bundle.manifest_value["artifacts"]
        ]
        self.assertEqual(stripped, plain.manifest_value["artifacts"])

    def test_conflicting_declarations_for_one_artifact_are_rejected(self) -> None:
        """同一原件被两条规则命中时，排除集合必须一致，否则失败关闭。"""

        declaration = repository_declaration()
        entry = next(
            item for item in declaration["entries"] if item["job_id"] == "candidate-core-direct"
        )
        duplicate = copy.deepcopy(entry["rules"][0])
        duplicate["scenario_ids"] = ["A02"]
        duplicate["environment_probe_sni"] = ["api.ipify.org"]
        entry["rules"].append(duplicate)
        with self.assertRaisesRegex(catalog.EvidenceCatalogError, "environment_probe_sni"):
            CandidateDirectBundle(self.root, declaration)


class EnvironmentProbeManifestContractTest(unittest.TestCase):
    """断言器对 manifest 的独立校验与 schema 定义必须一致。"""

    @staticmethod
    def _manifest(**artifact_overrides) -> dict:
        artifact = {
            "path": "direct/codex-http-s2/egress.pcap",
            "sha256": "0" * 64,
            "kind": "pcap",
            "parser": "pcap_client_hello",
            "scenario_ids": ["A01"],
            "labels": {"transport": "http", "ca_mode": "system", "surface": "direct"},
            "environment_probe_sni": list(EGRESS_GUARD_PROBE_HOSTS),
        }
        artifact.update(artifact_overrides)
        return {
            "schema_version": "codex-candidate-capture-manifest/v1",
            "codex_version": "0.157.0",
            "capture_id": "unit",
            "status": "complete",
            "artifacts": [artifact],
        }

    def test_valid_declaration_is_accepted(self) -> None:
        artifacts = _validate_capture_manifest(self._manifest(), "0.157.0")
        self.assertEqual(artifacts[0]["environment_probe_sni"], EGRESS_GUARD_PROBE_HOSTS)

    def test_declaration_on_non_pcap_parser_is_rejected(self) -> None:
        manifest = self._manifest(
            path="relay/conn001.client_to_upstream.bin",
            kind="relay_binary",
            parser="h1_request_stream",
        )
        with self.assertRaisesRegex(AssertionConfigurationError, "pcap_client_hello"):
            _validate_capture_manifest(manifest, "0.157.0")

    def test_protected_or_malformed_hosts_are_rejected(self) -> None:
        for hosts, reason in PROTECTED_OR_MALFORMED_HOSTS:
            with self.subTest(hosts=hosts):
                with self.assertRaisesRegex(AssertionConfigurationError, reason):
                    _validate_capture_manifest(
                        self._manifest(environment_probe_sni=hosts), "0.157.0"
                    )

    def test_schema_defines_the_field_for_pcap_only(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        artifact_schema = schema["properties"]["artifacts"]["items"]
        field = artifact_schema["properties"]["environment_probe_sni"]
        self.assertEqual(field["type"], "array")
        self.assertEqual(field["minItems"], 1)
        self.assertTrue(field["uniqueItems"])
        # schema 与断言器用同一条主机名正则，二者不能各执一词。
        self.assertEqual(field["items"]["pattern"], checker.ENVIRONMENT_PROBE_HOST_RE.pattern)
        conditions = [
            item
            for item in artifact_schema["allOf"]
            if item["if"]["required"] == ["environment_probe_sni"]
        ]
        self.assertEqual(len(conditions), 1)
        self.assertEqual(
            conditions[0]["then"]["properties"]["parser"]["const"], "pcap_client_hello"
        )


if __name__ == "__main__":
    unittest.main()
