"""A 组连续链的同世界接口；只构造外部来源，不代写候选结果或认证。

批准规则与原子 ID 保留，首次冻结时明确使用离线控制面画像接缝。官方字节、宿主和门禁环境是夹具来源；
候选 attempt、run、seal、compare、builder、accept、Store 与两本账均走产品实现。不能据此宣称全规则真实语义通过。
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_assertion_profile as profile
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_store as store_module
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator
from tools.official_client_capture.tests import claude_vc5_chain_fixture as sources


def freeze_control_profile(campaign: Path) -> None:
    """在首次评估前冻结一次控制面夹具画像，之后不改批准清单或画像字节。"""

    with sources.profile_seam():
        profile.freeze_approved_assertion_profile(campaign, upgrade.load_campaign_manifest(campaign))


def refresh_world(case: Any, world: dict[str, Any], *, candidate_id: str | None = None, revision: int | None = None) -> dict[str, Any]:
    """基线或 revision 变化后重新读当前真实封存结果，禁止继续持有旧阶段或旧构建的快照。"""

    campaign = world['campaign']
    candidate = candidate_id or world['candidate_id']
    revision = int(revision if revision is not None else world['revision'])
    manifest = upgrade.load_campaign_manifest(campaign)
    store, store_campaign = store_module.open_campaign_store(campaign, manifest)
    frozen = next(fact for fact in reversed(store.list_facts(store_campaign['campaign_id']))
                  if fact['fact_kind'] == 'candidate_frozen' and fact['payload']['candidate_id'] == candidate)
    build, _binding = compare.load_candidate_build_receipt(campaign, manifest, candidate, revision)
    with case.source_context():
        baseline, _commit = upgrade._current_evaluation_baseline(campaign, candidate)
        stage = upgrade._load_stage_result(campaign, 'capture-candidate', candidate, _baseline=baseline)
        attempt_root, attempt = upgrade._capture_stage_attempt_context(campaign, stage, phase='candidate', candidate_id=candidate)
    world.update(manifest=manifest, candidate_id=candidate, revision=revision, candidate_revision=revision,
                 evaluation_baseline=baseline, store=store, store_campaign=store_campaign, build=build,
                 candidate_payload=dict(frozen['payload']), architecture=str(frozen['payload']['target_architecture']),
                 stage=stage, attempt_root=attempt_root, attempt=attempt)
    # 原始字节可能在受管数据根，不能只快照 Campaign 内的清单与收据。
    roots = [campaign / 'official', campaign / 'candidates']
    roots.extend(Path(value) for row in attempt['results'] for value in row['evidence_roots'])
    official = world.get('official')
    if official is not None:
        roots.extend([official.evidence_root, official.manifest_path])
    world['trusted_roots'] = list(dict.fromkeys(roots))
    return world


def real_candidate_world(case: Any, campaign: Path, *, candidate_id: str, revision: int, expected_client_requests: int,
                         capture_layout: dict[str, Any] | None = None) -> dict[str, Any]:
    """共享世界只绑定真实候选；配置中不允许出现替换 attempt 上下文的两个接缝字段。"""

    evaluation = campaign / 'continuous-sources'
    official_side = evaluator.write_side(evaluation / 'official', capture_id='continuous-official')
    official = upgrade._load_stage_result(campaign, 'capture-official')
    source_path = sources._write_json(evaluation / 'official-source.json', sources.official_source(official_side, official['store']['evidence_package_ref']))
    source_config: dict[str, Any] = {'official_source': str(source_path)}
    if capture_layout is not None:
        # 可选布局只登记测试运行源的真实映射，与首次来源配置一起写出；不改 Campaign 或候选证据。
        source_config['capture_layout'] = dict(capture_layout)
    config_path = sources._write_json(evaluation / 'source-config.json', source_config)
    world = {'campaign': campaign, 'candidate_id': candidate_id, 'revision': revision, 'evaluation': evaluation,
             'official': official_side, 'config_path': config_path, 'gate_root': case.root / 'gate-evidence',
             'expected_client_requests': expected_client_requests, 'real_candidate': True, 'next_sequence': 9,
             'records': [], 'ledger_dir': case.ledger_dir, 'project_root': case.project_root, 'state_dir': case.state_dir}
    refresh_world(case, world)
    classification = upgrade._load_stage_result(campaign, 'classify')
    view = upgrade._effective_classification_view(campaign, candidate_id, classification)
    profile_path, _profile, binding = compare._effective_profile(campaign, view)
    rules_path = compare._bound_campaign_file(campaign, classification['approved']['target_rules'], '批准目标规则')
    world.update(profile_path=profile_path, profile_sha256=binding['sha256'], rules_path=rules_path)
    return world


def prepare_delivery_sources(case: Any, context: dict[str, Any]) -> dict[str, Any]:
    """七／八场景连续链共用实读来源，在第一次 compare 前物化新原件，禁止改写已封存收据。

    历史请求仍来自明示的评估器夹具；门禁环境由原 finalizer 生成，供独立 reader 消费。
    不替换候选 attempt、Store 包或已有来源配置，两条链共享同一初始化规则。
    """

    campaign = context['campaign']
    original_config = json.loads(context['config_path'].read_bytes())
    case.assertNotIn('attempt', original_config, '候选上下文必须由正式封存读侧读取')
    case.assertNotIn('attempt_root', original_config, '不能用来源配置替换候选 attempt')
    official = upgrade._load_stage_result(campaign, 'capture-official')
    source = sources.materialize_official_source(campaign=campaign, data_root=case.data, side=context['official'],
        package_ref=official['store']['evidence_package_ref'])
    source_path = sources._write_json(context['evaluation'] / 'materialized-official-source.json', source)
    configuration = {**original_config, 'official_source': str(source_path),
                     'gate_environment_mode': sources.REAL_GATE_ENVIRONMENT}
    context['config_path'] = sources._write_json(context['evaluation'] / 'materialized-source-config.json', configuration)
    return context


def take_sequence(world: dict[str, Any], requested: int | None = None) -> int:
    """每次提交用同世界递增序号；故障重派仍由调用方显式保留原序号。"""

    sequence = max(int(world['next_sequence']), int(requested or 0))
    world['next_sequence'] = sequence + 1
    return sequence


def strict_accept(case: Any, world: dict[str, Any]) -> dict[str, Any]:
    """真实门禁收据绑定当前候选，严格 accept 写完 VC-5；不打桩规则、摘要或成功状态。"""

    refresh_world(case, world)
    campaign = world['campaign']
    receipt = case.gate_receipt(world, f"continuous-r{world['revision']}-b{world['evaluation_baseline']}.json")
    sequence = take_sequence(world)
    action = case.accept_action(campaign, receipt, world['gate_root'], candidate_id=world['candidate_id'])
    result = case.assert_batch_passed(case.dispatch_vc5(world, sequence, action), sequence, 'VC-5')
    case.assertTrue(vc.vc_checkpoint_path(campaign, 'VC-5', revision=world['revision']).is_file())
    with sources.seams(world['config_path']):
        accepted = upgrade._load_stage_result(campaign, 'accept', world['candidate_id'])
    comparison = upgrade._load_stage_result(campaign, 'compare', world['candidate_id'])
    case.assertEqual(comparison['rules'], list(upgrade._approved_rules(campaign, world['manifest'], require_approved=True)))
    case.assertEqual((comparison['candidate_revision'], comparison['evaluation_baseline']), (world['revision'], world['evaluation_baseline']))
    world['acceptance'] = accepted
    world['accepted_batch'] = result
    return accepted
