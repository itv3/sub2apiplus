#!/bin/bash
# 本机候选提交链：拉回采集主机上本轮 VC-3 catalog 与门禁需求 → A（纳入 catalog-stage + release graph + 门禁映射/计划）
#   → C（只改两个冻结路径）→ freeze-successor → D（承接收据）→ bundle（基于各仓库副本都有的基准 commit）。
# 用法：bash local-candidate-chain.sh <ROUND> <STAMP> <工作目录> <DATE_TAG YYYYMMDD> <BASE_COMMIT> <A 段提交说明文件>
#   环境：REPO（仓库根，默认 ~/Developer/sub2apiplus）。产物：<工作目录>/<ROUND>-commits.txt（C DC）、<工作目录>/<ROUND>.bundle。
set -Eeuo pipefail
ROUND="$1"; STAMP="$2"; SP="$3"; DATE_TAG="$4"; BASE="$5"; A_MESSAGE="$6"
REPO=${REPO:-$HOME/Developer/sub2apiplus}
test -f "$A_MESSAGE"
test "$(git -C $REPO status --porcelain | wc -l | tr -d ' ')" = 0
rm -rf "$SP/$ROUND-catalog" && mkdir -p "$SP/$ROUND-catalog"
ssh -n -o ConnectTimeout=20 ARM64 "cd /root/docker/capture-cli/data/control && tar -cf - c0154-vc3-candidate-catalog-$ROUND-$STAMP" > "$SP/$ROUND-catalog.tar"
tar -xf "$SP/$ROUND-catalog.tar" -C "$SP/$ROUND-catalog"
ssh -n -o ConnectTimeout=20 ARM64 "D=/root/docker/capture-cli/data; NEW=\$D/evidence/campaigns/c0154-formal-vc5-$ROUND-$STAMP; cd \$D; export PYTHONPATH=.; python3 - \"\$NEW\" <<'PY'
import sys, json
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
c=Path(sys.argv[1]); m=cu._require_formal_campaign(c); cl=cu._load_stage_result(c,'classify')
_,_,req=cu._load_vc3_gate_requirements(c,m,cl)
print(json.dumps(req,ensure_ascii=False))
PY" > "$SP/$ROUND-gate-requirements.json"
CAT=$(ls -d "$SP/$ROUND-catalog"/c0154-vc3-candidate-catalog-$ROUND-*)
cd "$REPO"
python3 - "$CAT" "$SP/$ROUND-gate-requirements.json" <<'PY'
import sys, json, shutil, os, hashlib
from pathlib import Path
src=Path(sys.argv[1]); reqpath=Path(sys.argv[2]); repo=Path('.')
stage=repo/'docs/egress/lifecycle/codex-0154-candidate/catalog-stage'
shutil.rmtree(stage); shutil.copytree(src, stage)
rg=[p for p in (src/'catalogdata/runtime/release-graphs').iterdir()]; assert len(rg)==1
rel='catalogdata/runtime/release-graphs/'+rg[0].name; d=repo/'backend/internal/officialegress'/rel
assert not d.exists(); shutil.copyfile(rg[0], d); print("added", rg[0].name[:12])
for rel2 in ['catalogdata/runtime/snapshot-catalogs/e072a7d38d166795dff603d0e3316155da7b13c4b448985ab93e0bd896911479.json','catalogdata/runtime/profiles/0.154.0/31d8654f6892d37129a2639f1bb48e87b7b8648d67ce754f4ae9379a671b99e3.json','profilecontract/testdata/snapshots/0.154.0/31d8654f6892d37129a2639f1bb48e87b7b8648d67ce754f4ae9379a671b99e3.json']:
    assert (repo/'backend/internal/officialegress'/rel2).read_bytes()==(src/rel2).read_bytes()
for rel3 in ['catalogdata/runtime/release-catalog.json','releasecontract/testdata/release-graph.json']:
    shutil.copyfile(src/rel3, repo/'backend/internal/officialegress'/rel3)
for root in [stage, repo/'backend/internal/officialegress/catalogdata', repo/'backend/internal/officialegress/releasecontract/testdata']:
    for p in root.rglob('*'): os.chmod(p, 0o755 if p.is_dir() else 0o644)
sys.path.insert(0,'tools/official_client_capture')
import codex_upgrade_vc_artifacts as v
req=json.load(open(reqpath)); reqp=v.validate_gate_requirements(req)
byid={r['gate_id']:r for r in reqp['requirements']}
mp=repo/'docs/egress/lifecycle/codex-0154-candidate/gate-mapping.json'; old=json.load(open(mp))
gates=[{"gate_id":g['gate_id'],"test_id":g['test_id'],"working_directory":g['working_directory'],"command":g['command'],"requirement_sha256":v.digest(byid[g['gate_id']])} for g in old['gates']]
assert {g['gate_id'] for g in gates}==set(byid); gates.sort(key=lambda g:g['gate_id'])
mapping={"schema_version":old['schema_version'],"requirements_sha256":reqp['requirements_sha256'],"gates":gates}
mp.write_text(json.dumps(mapping,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding='utf-8')
plan=v.build_gate_plan(req,mapping,mapping_sha256=hashlib.sha256(mp.read_bytes()).hexdigest())
(repo/'docs/egress/lifecycle/codex-0154-candidate/gate-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding='utf-8')
print("plan", plan['campaign_id'], "mapping requirements_sha256", mapping['requirements_sha256'][:16])
PY
git add backend/internal/officialegress/catalogdata/runtime/release-graphs/ docs/egress/lifecycle/codex-0154-candidate/
git commit -q -F "$A_MESSAGE"
git add backend/internal/officialegress/catalogdata/runtime/release-catalog.json backend/internal/officialegress/releasecontract/testdata/release-graph.json
git commit -q -m "feat(codex-0154): 切换候选 RuntimeCatalog 指向 $ROUND 候选 release graph

VC-4 第二段：只修改两个已冻结路径，作为候选源码树的 Git commit。冻结承接收据在下一提交单独登记。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
A=$(git rev-parse HEAD~1); C=$(git rev-parse HEAD)
TAG="codex-0154-candidate-$ROUND-$DATE_TAG"; RECEIPT="docs/egress/maintenance/upstream-$TAG-freeze-successor.json"
python3 -m tools.upstream_merge freeze-successor-generate --before "$A" --after "$C" --tag "$TAG" --output "$REPO/$RECEIPT" --reason "按上游合并流程规则在最终 revision 一次性登记冻结台账的精确后继摘要（${TAG}）；旧收据保持只读，不改变官方客户端画像、Persona、wire 或生产代码。" 2>&1 | grep -o '"result": *"[^"]*"\|"unregistered_path_count": *[0-9]*' | tr '\n' ' '; echo
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['result']=='passed_local_evidence_successor' and d['unregistered_path_count']==0 and d['current_commit']==sys.argv[2], d; print('successor ok', d['changed_path_count'])" "$RECEIPT" "$C"
git add "$RECEIPT"
git commit -q -m "chore(codex-0154): 登记 $ROUND 候选 RuntimeCatalog 切换的冻结承接摘要

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
DC=$(git rev-parse HEAD); echo "$C $DC" > "$SP/$ROUND-commits.txt"; echo "A=$A C=$C DC=$DC RECEIPT=$RECEIPT"
git bundle create "$SP/$ROUND.bundle" "$BASE..codex/vc5-framework-closure" 2>&1 | tail -1
git log --oneline -4
echo "CHAIN_DONE"
