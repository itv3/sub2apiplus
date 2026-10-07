"""Claude 本机候选提交链端到端（Claude 升级 B3-04a）：在真实仓库的 ``--shared`` 克隆里跑 ``local-claude-candidate-chain.sh``。

为什么放真实链：要真实的 ``freeze-successor-generate``（读真实仓库的冻结台账与历史 blob）和一次真实 ``go run``（编译后端包），
读 git 历史、耗时数十秒，不进默认 make test。

* 夹具：B3-04 的候选夹具 Campaign（VC-3 暂存批准、计时账本、显式 r1）与它的门禁需求预览；夹具的 Active 目录就是真实仓库的
  ``release-catalog.json``，所以真实仓库的克隆可以直接当候选源码仓库（本机链要求两者逐字节相同）。
* 采集主机：PATH 里的假 ``ssh`` 只认取料这一条调用（``-n -o ConnectTimeout=20 ARM64 'cd … && tar -cf - …'``），在本地对夹具
  Campaign 执行同一条只读 tar，其它调用一律失败。
* Go 核对：一次真实 ``go run``（仓库当前目录、不切换选择器）证明核对小程序能编译、输出与 release-catalog.json 的字节和两个
  选择器一致；链里的 ``go`` 换成 PATH 替身（按候选目录内容给出同形的加载结果）——夹具的合成画像与 Wire 不代表真实版本，
  第一条真实链也要等 B3-04b（Go 侧 Release 钉住解耦）合入。
* 断言：三段提交各自的改动集合（A 恰好是装配器的写入清单，C 只有 release-catalog.json，D 只有承接收据）、D 收据通过冻结规划
  （A→C 只有一条冻结摘要边、未登记与人工动作为 0）与 Claude 的 source transition 校验、bundle 可验证、工作树干净、取料
  只经一次 ssh。

零请求；本文件位于 tests/，不进任何受管身份。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tools.official_client_capture import claude_upgrade_candidate as candidate
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests import test_claude_upgrade_candidate as candidate_tests
from tools.official_client_capture.tests import test_claude_vc4_driver as vc4_tests
from tools.official_client_capture.tests import test_driver_client_table as table_tests

REPO_ROOT = Path(__file__).resolve().parents[4]
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
CHAIN_SCRIPT = DRIVER / "local" / "local-claude-candidate-chain.sh"
BRANCH = "claude/b304a-chain-fixture"
ROUND, STAMP, DATE_TAG = "r1", "20261007t000000z", "20261007"

FAKE_SSH = """#!/bin/bash
# 假 ssh：只认本机候选链的取料调用，在本地对夹具 Campaign 执行同一条只读 tar；每次调用记一行。
set -euo pipefail
echo "$*" >> "$FAKE_SSH_LOG"
if [ "$#" != 5 ] || [ "$1" != -n ] || [ "$2" != -o ] || [ "$3" != ConnectTimeout=20 ] || [ "$4" != ARM64 ]; then
  echo "假 ssh：不认识的调用" >&2; exit 97
fi
case "$5" in "cd "*" && tar -cf - "*) ;; *) echo "假 ssh：不认识的远端命令" >&2; exit 98 ;; esac
exec bash -c "$5"
"""

FAKE_GO = """#!/usr/bin/env python3
# 假 go：只认候选链的目录核对（go run -mod=readonly <main.go>），按 backend 里的 Claude 目录给出与 claude_catalog_check.go
# 同形的加载结果（选择器解析成 Release，目录摘要是原始字节的 SHA-256）。
import hashlib, json, sys
from pathlib import Path
if sys.argv[1:3] != ["run", "-mod=readonly"] or len(sys.argv) != 4 or not sys.argv[3].endswith("/main.go"):
    sys.exit("假 go：不认识的调用")
raw = (Path.cwd() / "internal/officialegress/catalogdata/claude/release-catalog.json").read_bytes()
document = json.loads(raw)
releases = {row["release_sha256"]: row for row in document["releases"]}
def view(selector):
    row = releases[selector["release_sha256"]]
    return {"version": row["version"], "platform": row["platform"], "release_sha256": row["release_sha256"],
            "profile_sha256": row["profile"]["sha256"], "wire_sha256": row["wire"]["sha256"], "changeset": selector["changeset"]}
selectors = document["selectors"]
print(json.dumps({"status": "passed", "catalog_sha256": hashlib.sha256(raw).hexdigest(),
                  "validation_candidate": view(selectors["validation_candidate"]), "production_active": view(selectors["production_active"])}))
"""


def git(root: Path, *arguments: str) -> str:
    return candidate_tests.build_fixture._git(root, *arguments)


class ClaudeLocalCandidateChainTests(candidate_tests._CandidateCase):
    def parameters(self, runroot: Path, mapping: Path) -> Path:
        """Claude 参数文件：驱动测试夹具改成 Claude 的那一份，坐标换成本夹具的数据根、Campaign、候选与取料目录。"""

        fixture = driver_tests._DriverFixture(self.root / "driver-fixture")
        replacements = {"D": str(self.data), "RUNROOT": str(runroot), "NEW": candidate_tests.CAMPAIGN_ID, "CAND": candidate_tests.CANDIDATE,
                        "ROUND": ROUND, "STAMP": STAMP, "BUNDLE_BRANCH": BRANCH, "TARGET_VERSION": vc4_tests.TARGET}
        lines = []
        for line in table_tests.claude_env_text(fixture).splitlines():
            key = line.split("=", 1)[0]
            if key == "B":
                continue
            lines.append(f'{key}="{replacements[key]}"' if key in replacements else line)
        lines.append(f'GATE_MAPPING_INPUT="{mapping}"')
        path = self.root / "claude-round.env"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_real_go_checker_matches_the_repository_catalog(self) -> None:
        if shutil.which("go") is None:
            self.skipTest("本机没有 go：Go 目录核对小程序未经真实编译")
        chain = vc4_tests.load_driver("claude_catalog_chain", {})
        view = chain.go_catalog_check(REPO_ROOT)
        raw = (REPO_ROOT / candidate.RELEASE_CATALOG_RELATIVE).read_bytes()
        document = json.loads(raw)
        releases = {row["release_sha256"]: row for row in document["releases"]}
        self.assertEqual((view["status"], view["catalog_sha256"]), ("passed", hashlib.sha256(raw).hexdigest()))
        for role in ("validation_candidate", "production_active"):
            selector = document["selectors"][role]
            release = releases[selector["release_sha256"]]
            with self.subTest(role=role):
                self.assertEqual(view[role], {
                    "version": release["version"], "platform": release["platform"], "release_sha256": release["release_sha256"],
                    "profile_sha256": release["profile"]["sha256"], "wire_sha256": release["wire"]["sha256"], "changeset": selector["changeset"],
                })
        self.assertEqual(view["checker_sha256"], hashlib.sha256((DRIVER / "claude_catalog_check.go").read_bytes()).hexdigest())
        self.assertFalse(any(path.name.startswith(".claude-catalog-check-") for path in (REPO_ROOT / "backend").iterdir()), "临时目录已清理")

    def test_chain_commits_a_c_d_and_the_receipt_passes_the_claude_transition(self) -> None:
        campaign, preview = self.ready()
        runroot = self.root / "runroot"
        runroot.mkdir(mode=0o700)
        (runroot / vc4_tests.REQUIREMENTS_FILENAME).write_text(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        mapping = self.root / "gate-mapping.json"
        mapping.write_text(json.dumps(self.mapping_for(preview), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        repository = self.root / "repository"
        subprocess.run(["git", "clone", "-q", "--shared", str(REPO_ROOT), str(repository)], check=True, capture_output=True, timeout=600)
        git(repository, "checkout", "-q", "-b", BRANCH)
        base = git(repository, "rev-parse", "HEAD")
        self.assertEqual((repository / candidate.RELEASE_CATALOG_RELATIVE).read_bytes(), candidate_tests.profile_tests.ACTIVE_CATALOG.read_bytes(),
                         "夹具的 Active 目录就是仓库当前的 release-catalog.json")
        fakes = self.root / "fake-bin"
        fakes.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("go", FAKE_GO)):
            (fakes / name).write_text(body, encoding="utf-8")
            (fakes / name).chmod(0o755)
        message = self.root / "a-message.txt"
        message.write_text("feat(claude): 候选资产（B3-04a 真实链夹具）\n", encoding="utf-8")
        work = self.root / "work"
        environment = {key: value for key, value in os.environ.items() if key not in {"CLIENT", "PYTHONPATH"}}
        environment.update(
            ARM64_VC_ENV=str(self.parameters(runroot, mapping)), REPO=str(repository), PATH=f"{fakes}:{os.environ['PATH']}",
            FAKE_SSH_LOG=str(self.root / "ssh.log"), PYTHONDONTWRITEBYTECODE="1",
            GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid", GIT_COMMITTER_NAME="fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="false",
        )
        completed = subprocess.run(["bash", str(CHAIN_SCRIPT), ROUND, STAMP, str(work), DATE_TAG, base, str(message)],
                                   env=environment, capture_output=True, text=True, timeout=1200)
        self.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-3000:])
        self.assertIn("CHAIN_DONE", completed.stdout)
        self.assertEqual(len((self.root / "ssh.log").read_text(encoding="utf-8").splitlines()), 1, "取料只经一次 ssh")

        # 三段提交：A 恰好是装配器的写入清单，C 只有 release-catalog.json，D 只有承接收据。
        a_commit, c_commit, d_commit = (git(repository, "rev-parse", f"HEAD~{n}") for n in (2, 1, 0))
        self.assertEqual(git(repository, "rev-parse", "HEAD~3"), base)
        changed = lambda older, newer: sorted(git(repository, "diff", "--name-only", older, newer).splitlines())  # noqa: E731
        assets = json.loads((work / f"{ROUND}-claude-assets.json").read_text(encoding="utf-8"))
        self.assertEqual(changed(base, a_commit), assets["written"])
        self.assertEqual(changed(a_commit, c_commit), [candidate.RELEASE_CATALOG_RELATIVE.as_posix()])
        tag = f"claude-{vc4_tests.TARGET.replace('.', '')}-candidate-{ROUND}-{DATE_TAG}"
        receipt_relative = f"docs/egress/maintenance/upstream-{tag}-freeze-successor.json"
        self.assertEqual(changed(c_commit, d_commit), [receipt_relative])
        self.assertEqual((work / f"{ROUND}-commits.txt").read_text(encoding="utf-8").split(), [c_commit, d_commit])
        self.assertEqual(git(repository, "status", "--porcelain", "--untracked-files=all"), "")
        # D 收据：真实冻结规划，A→C 只有一条冻结摘要边；按 Claude 的 source transition 校验（与 record-candidate-build 同一函数）。
        receipt = json.loads((repository / receipt_relative).read_text(encoding="utf-8"))
        self.assertEqual((receipt["base_commit"], receipt["current_commit"], receipt["result"]), (a_commit, c_commit, "passed_local_evidence_successor"))
        self.assertEqual([row["path"] for row in receipt["transitions"]], [candidate.RELEASE_CATALOG_RELATIVE.as_posix()])
        self.assertEqual((receipt["unregistered_path_count"], receipt["required_manual_actions"]), (0, []))
        candidate._validate_candidate_source_transition(repository, receipt, git_commit=c_commit)
        # bundle 只含本分支在基线之后的三个提交，并能在基线所在的仓库里验证。
        bundle = work / f"{ROUND}.bundle"
        subprocess.run(["git", "-C", str(repository), "bundle", "verify", "-q", str(bundle)], check=True, capture_output=True, timeout=120)
        heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], check=True, capture_output=True, text=True, timeout=60).stdout
        self.assertEqual(heads.split(), [d_commit, f"refs/heads/{BRANCH}"])
        catalog = json.loads((repository / candidate.RELEASE_CATALOG_RELATIVE).read_text(encoding="utf-8"))
        self.assertEqual(catalog["selectors"]["validation_candidate"]["release_sha256"], assets["release_sha256"])


if __name__ == "__main__":
    unittest.main()
