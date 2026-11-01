#!/usr/bin/env python3
"""收尾 b9：把 Codex 指南第一、三、四部分里“当前 Active／Previous 身份”的几处按终态事实改写，输出 final-guide.md。

做什么
    按终态事实（gen_terminal_facts.py 的输出）、Campaign 文件与 VC-2 规则迁移清单，改写指南里随 Active 变化的位置，其余字节不动：
      1. 文首“当前版本”横幅；
      2. §3.1 末尾“当前 active strict wire 是 Codex CLI X”；
      3. §3.2 标题、表格、官方目标身份、VC-6 段（整节重写）；
      4. §3.5 实现表里“<上一版>／<目标版> 不可变 Snapshot”；
      5. §4.6.5～§4.6.6 业务验证模型一句里的版本字样。
    渲染完成后再做一次“残留扫描”：第一、三、四部分里仍同时出现上一版号与 active／当前 字样的行，原样打印供人工通读（不失败）。
    第四部分的“本轮实测经验”由调度者按候选清单手工撰写（需要判断，不机械渲染），用 finalize_part4.py 并入；
    第四部分 4.9 小节的“Active 终态收据坐标”表由 update_active_receipt.py 更新（先于本脚本，本脚本的输入指南应已带新 4.9 表）。
交叉核对（任何一项不符就退出，不写文件）
    * 变化规则数与继承数取自画像补丁清单；给了 --rule-migration（VC-2 批准的规则迁移清单）时两者必须一致；
    * 端点数从仓库里 Active 画像文件现数（静态 + ReturnedURL 动态），与上一版 active 行的“N 个静态端点”对不上就退出；
    * 固定回滚镜像标签从终态事实里的镜像引用推出（…:X.Y.Z-N@sha256 → vX.Y.Z-N），--rollback-label 给了就必须与推出值一致；
    * 第二部分摘要渲染前后必须逐字相同（本脚本从不动第二部分）。
--part2-mode（第二部分在本轮收尾里的处理方式，只影响横幅措辞）
    untouched（缺省，一字不动：evidence_index 已改读指南 4.9 小节，收尾不再改第二部分）、
    lead-only（旧做法：用 part2_minimal.py 只改开头段，横幅如实说明规则正文仍按上一版官方样本表述）、
    rewritten（第二部分已另行改完并走过摘要连锁，横幅声称“第二部分描述当前 Active”）。
输入
    render_guide.py --repo <仓库根> --guide <指南> --facts <终态事实 json> --campaign-json <campaign.json> \\
        --official-attempt <官方 attempt id> --official-jobs <官方 Job 数> --behavior-summary '<一句行为概述>|@文件' \\
        [--rule-migration <rule-migration-approved.json>] [--rollback-label vX.Y.Z-N] [--part2-mode untouched|lead-only|rewritten] \\
        [--eval-note '…'] --output <final-guide.md>
    ``--behavior-summary`` 必填：横幅里描述本版行为变化的一句话，须按 VC-1 事实逐条核过再写（以 ``@路径`` 开头表示从文件读取）。
输出
    --output 指定的最终指南（已存在则拒绝覆盖），并打印第二部分摘要、变化／继承规则数、端点数、回滚标签与残留扫描结果。
何时用
    b9-render-guide 步骤（b7 终态事实、b8a 的 4.9 副本之后）。
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PART2 = "# 第二部分 Codex CLI 客户端规则画像"


def part2_sha(text):
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == PART2]
    if len(starts) != 1:
        sys.exit("指南必须恰有一个第二部分")
    end = next((i for i in range(starts[0] + 1, len(lines)) if lines[i].startswith("# ")), len(lines))
    return hashlib.sha256("".join(lines[starts[0]:end]).encode()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for n in ("repo", "guide", "facts", "campaign-json", "official-attempt", "official-jobs", "output"):
        p.add_argument("--" + n, required=True)
    p.add_argument("--rollback-label", default="", help="固定回滚镜像标签（形如 vX.Y.Z-N）；缺省从终态事实的镜像引用推出，给了就必须一致")
    p.add_argument("--rule-migration", default="", help="VC-2 批准的规则迁移清单 rule-migration-approved.json（ARM64 control/<inputs>/ 下）；给了就核对变化规则")
    p.add_argument("--part2-mode", choices=("untouched", "lead-only", "rewritten"), default="untouched")
    p.add_argument("--eval-note", default="", help="VC-5 验收的特别说明（如 approval-revision 的评估基线）；缺省不写")
    p.add_argument("--behavior-summary", required=True, help="横幅里描述本版行为变化的一句话（须按 VC-1 事实逐条核过）；以 @ 开头表示从该路径的文件读取")
    a = p.parse_args()
    repo = Path(a.repo)
    text = Path(a.guide).read_text()
    facts = json.loads(Path(a.facts).read_text())
    campaign = json.loads(Path(a.campaign_json).read_text())
    identity = campaign["official_identity"]
    target, prev = facts["target"]["version"], facts["target"]["previous_version"]
    retained = facts["retained_runtime_profiles"][0]
    retired = re.search(r"/profiles/(\d+\.\d+\.\d+)/", retained["path"]).group(1)
    chain = facts["campaign_chain"]
    cand, attempt = chain[1]["candidate_id"], chain[1]["attempt_id"]
    dep, canonical = facts["production_deployment"], facts["canonical"]
    def tag(v):  # 主.次.补丁 -> 主+次三位（与既有终态收据文件名一致）
        return f"{int(v.split('.')[0])}{int(v.split('.')[1]):03d}"
    receipt_name = f"CODEX_CLI_{tag(prev)}_TO_{tag(target)}"
    terminal = f"egress/maintenance/{receipt_name}_TERMINAL_STATE_RECEIPT.json"
    removal = f"egress/maintenance/{receipt_name}_RUNTIME_PROFILE_REMOVAL_RECEIPT.json"
    active_sha, prev_sha = facts["target"]["profile_sha256"], facts["target"]["previous_profile_sha256"]
    short_target = target.rsplit(".", 1)[0]

    # ---- 回滚镜像标签：从镜像引用里推出 v<tag>，与参数核对
    ref = dep["rollback_image_reference"]
    m_tag = re.search(r":(\d+\.\d+\.\d+(?:-\d+)?)@sha256:", ref)
    derived_label = ("v" + m_tag.group(1)) if m_tag else ""
    if a.rollback_label and derived_label and a.rollback_label != derived_label:
        sys.exit(f"--rollback-label {a.rollback_label} 与终态事实镜像引用推出的 {derived_label} 不一致，先查事实再渲染")
    rollback_label = a.rollback_label or derived_label
    if not rollback_label:
        sys.exit("镜像引用里没有版本标签，且没给 --rollback-label")

    # ---- 本版变化规则与目标规则总数：补丁清单（仓库）＋规则清单（仓库）＋可选的 VC-2 迁移清单（交叉核对）
    ver_us = target.replace(".", "_")
    patches = json.loads((repo / f"tools/official_client_capture/profile_rule_patches_{ver_us}.json").read_text())
    changed_by_patch = sorted({rule for patch in patches["rule_patches"] for rule in patch["rule_ids"]})
    total_rules = len(json.loads((repo / f"tools/official_client_capture/codex_upgrade_rules_{ver_us}.json").read_text())["required_rules"])
    changed = changed_by_patch
    if a.rule_migration:
        mig = json.loads(Path(a.rule_migration).read_text())
        by_class = {}
        for entry in mig["entries"]:
            by_class.setdefault(entry["classification"], []).append(entry["target_rule"])
        changed_by_migration = sorted(r for cls, rules in by_class.items() if cls != "inherit" for r in rules)
        if len(mig["entries"]) != total_rules:
            sys.exit(f"迁移清单 {len(mig['entries'])} 项与规则清单 {total_rules} 条不符")
        if changed_by_migration != changed_by_patch:
            sys.exit(f"VC-2 迁移清单的非继承规则 {changed_by_migration} 与画像补丁清单涉及的规则 {changed_by_patch} 不一致")
        changed = changed_by_migration
    inherited = total_rules - len(changed)
    patch_paths = [patch["path"].lstrip("/").replace("/", ".") for patch in patches["rule_patches"]]

    def once(old, new):
        nonlocal text
        if text.count(old) != 1:
            sys.exit(f"命中 {text.count(old)} 次（应为 1）：{old[:70]!r}")
        text = text.replace(old, new)

    if changed:
        changed_text = "、".join(f"`{r}`" for r in changed) + " 变化"
    else:
        changed_text = "无变化规则（VC-2 迁移清单全部为 inherit，画像补丁清单为空）"
    behavior = a.behavior_summary
    if behavior.startswith("@"):
        behavior = Path(behavior[1:]).read_text(encoding="utf-8").strip()
    if not behavior.strip():
        sys.exit("--behavior-summary 不能为空")

    # ---- 1. 文首横幅
    banner = re.search(r"(?m)^> \*\*当前版本\*\*：.*$", text)
    if not banner:
        sys.exit("找不到文首“当前版本”横幅")
    old_banner = banner.group(0)
    exited = [retired] + [v for v in re.findall(r"`(\d+\.\d+\.\d+)`", old_banner.split("已退出")[0]) if v not in (target, prev, retired)]
    exited = [v for i, v in enumerate(exited) if v not in exited[:i]]
    exited_text = "、".join(f"`{v}`" for v in exited[:-1]) + f" 与 `{exited[-1]}`"
    if a.part2_mode == "rewritten":
        part2_sentence = f"第二部分描述当前 Active {target} 的规则：本轮 {total_rules} 条目标规则中 {inherited} 条继承，{changed_text}；{behavior}"
    elif a.part2_mode == "lead-only":
        part2_sentence = (
            f"本轮 {total_rules} 条目标规则中 {inherited} 条继承，{changed_text}；{behavior}"
            f"第二部分只在开头段补写了当前 Active 与 {target} 官方样本的 Campaign，规则正文没有做本轮收尾改写，仍按 {prev} 官方样本与 {target} 源码复核表述，"
            "其中的版本取值（UA、`client_version`）以本文 §3.2 与 Runtime Catalog 为准。")
    else:
        part2_sentence = (
            f"本轮 {total_rules} 条目标规则中 {inherited} 条继承，{changed_text}；{behavior}"
            f"第二部分的规则正文未做本轮收尾改写，仍按 {prev} 官方样本与 {target} 源码复核表述，其中的版本取值（UA、`client_version`）与“当前 Active／Previous”以本文 §3.2 与 Runtime Catalog 为准。")
    new_banner = (
        f"> **当前版本**：Active 为 `codex-cli {target}`，Previous 为 `codex-cli {prev}`；{exited_text} "
        f"已退出 Runtime Catalog（运行投影已移除，画像字节按 §4.6.7 第 1 类保留为冻结历史制品）。完整生产身份见本文 §3.2，生产激活、回滚与 {retired} 退休的机器事实见 "
        f"[`{short_target} 终态收据`]({terminal})。" + part2_sentence)
    text = text[:banner.start()] + new_banner + text[banner.end():]

    # ---- 2. §3.1 末尾
    once(f"当前 active strict wire 是 Codex CLI {prev}；", f"当前 active strict wire 是 Codex CLI {target}；")

    # ---- 3. §3.2 整节
    start = text.index(f"## 3.2 Codex {prev} active 画像与发布执行契约")
    end = text.index("启动期解码、结构校验或摘要核对失败即阻止启动", start)
    prev_row = re.search(r"(?m)^\| active \| " + re.escape(prev) + r"；`[0-9a-f]{64}` \| (.*) \| 生产默认 \|$", text[start:end])
    if not prev_row:
        sys.exit("找不到上一版 §3.2 的 active 行，无法生成 previous 行")
    # 端点数：现数仓库里 Active 画像文件，与上一版 active 行的“N 个静态端点”对照
    selector = json.loads((repo / "backend/internal/officialegress/catalogdata/runtime/release-catalog.json").read_text())
    snap_rel = selector["snapshot_catalog"]["path"].split("catalogdata/runtime/")[-1]
    snaps = {row["version"]: row for row in json.loads((repo / "backend/internal/officialegress/catalogdata/runtime" / snap_rel).read_text())["snapshots"]}
    profile = json.loads((repo / "backend/internal/officialegress/catalogdata/runtime" / snaps[target]["file"]).read_text())
    n_dynamic = sum(1 for e in profile["Endpoints"] if e.get("HostFromResponse"))
    n_static = len(profile["Endpoints"]) - n_dynamic
    m_prev_n = re.match(r"(\d+) 个静态端点", prev_row.group(1))
    if not m_prev_n or int(m_prev_n.group(1)) != n_static or n_dynamic != 1:
        sys.exit(f"端点数与上一版不同或结构不符：Active 画像 静态 {n_static}／动态 {n_dynamic}，上一版 active 行 {prev_row.group(1)[:40]!r}；“与 {prev} 相同”的措辞不成立，手工改")
    old_sec = text[start:end]
    m_old_ret = re.search(r"(\d+\.\d+\.\d+) 按 §4\.6\.7 第 1 类退休", old_sec)
    m_earlier = re.search(r"此前退休的(.*?)与 0\.149\.1 两份画像同样原地保留", old_sec, re.S)
    if not m_old_ret or not m_earlier:
        sys.exit("旧 §3.2 里找不到“X 按 §4.6.7 第 1 类退休”或“此前退休的…”清单，无法延续退休版本列表")
    earlier_retired = f"{m_old_ret.group(1)}、" + re.sub(r"\s+", " ", m_earlier.group(1)).strip()
    if patch_paths:
        optional = f"可选节新增字段（相对 {prev}）：{'、'.join('`' + x + '`' for x in patch_paths)}"
    else:
        optional = f"可选节相对 {prev} 无新增字段（画像补丁清单为空，两份画像只差版本坐标与 Digest）"
    section = f"""## 3.2 Codex {target} active 画像与发布执行契约

active／previous 画像均以内容寻址 Snapshot 保存 exec／TUI 身份、feature、端点、Header／Body
闭集与顺序、压缩、TLS、连接、条件状态和文件上传编排：

| mode | 版本与画像摘要 | 端点闭集 | 用途 |
|---|---|---|---|
| active | {target}；`{active_sha}` | {n_static} 个静态端点（含 `wham_settings_user`、`wham_accounts_check`，与 {prev} 相同）+ {n_dynamic} 个 ReturnedURL 动态端点；{optional} | 生产默认 |
| previous | {prev}；`{prev_sha}` | {prev_row.group(1)} | 受控回滚和历史复算 |

当前 Active 的官方目标身份为 tag `rust-v{target}`（commit
`{identity['git_commit']}`）、`{identity['package']['target']}` 包
SHA-256 `{identity['package']['asset_sha256']}` 和 ARM64 二进制
SHA-256 `{identity['binary_sha256']}`。原始证据源为 Campaign
`{chain[0]['campaign_id']}` 的 attempt `{a.official_attempt}`；
{a.official_jobs} 个官方 Job 全部 complete。官方证据由该 Campaign 的 VC-1 自采，后续分类、验收、生产激活与 {retired} 退休由同一
Campaign 的 canonical 链承接（候选 `{cand}`，attempt `{attempt}`；
{total_rules} 项迁移为 inherit {inherited}、change {len(changed)}，目标规则 {total_rules} 条{('；' + a.eval_note) if a.eval_note else ''}）。

VC-6 在 ARM64 上完成：四阶段激活（canary → 切换 → 回滚 → 恢复，每阶段一次真实业务请求均 HTTP 200）所用镜像为
`{dep['activation_image_id']}`，退休 {retired} 运行投影后重建的切换镜像
ID 为 `{dep['final_image_id']}`，固定回滚镜像为 {rollback_label}
`{dep['rollback_image_reference']}`（{dep['rollback_version']}）；
canonical checkpoint 为 `{canonical['final_checkpoint_sequence']:08d}`，执行集合为{'空' if not canonical['execute_item_ids_remaining'] else '非空'}。收尾时 ARM64 运行本地构建的退休后切换镜像；{target} 随下一次发版
进入 GHCR 发布镜像，DMIT 等生产机更新按发布流程另行批准。机器事实分别见
[`{retired} Runtime Profile 退休收据`]({removal})
和 [`{short_target} 终态收据`]({terminal})。
{retired} 按 §4.6.7 第 1 类退休：Catalog、selector 与运行投影均已移除，画像字节 `{retained['sha256'][:8]}…`（{retired} 期 Active）因被
此前各版本的晋升／激活／终态收据逐文件登记而原地保留。此前退休的 {earlier_retired}
与 0.149.1 两份画像同样原地保留，分别仍由既往终态收据批准。不得据此认为这些文件已删除。

"""
    text = text[:start] + section + text[end:]

    # ---- 4. §3.5 实现表
    prevprev = re.search(r"(\d+\.\d+\.\d+)／" + re.escape(prev) + " 不可变 Snapshot", text)
    if not prevprev:
        sys.exit("找不到 §3.5“…／上一版 不可变 Snapshot”")
    once(prevprev.group(0), f"{prev}／{target} 不可变 Snapshot")

    # ---- 5. 第四部分业务验证模型一句（保留已有的轮次列表，末尾追加目标版本；两处模型名取自原句，不写死）
    sentence = re.search(r"（(\d+\.\d+[^`（）]*?) 轮用 `([^`]+)` 经 `/v1/chat/completions`：主线 `([^`]+)` 从未经该接口验证，\n回滚镜像以 [0-9.]+ 等旧身份出站也未必能用）", text)
    if not sentence:
        sys.exit("找不到第四部分“业务验证模型”那一句")
    rounds, lite_model, main_model = sentence.group(1), sentence.group(2), sentence.group(3)
    new_rounds = rounds if re.search(re.escape(target), rounds) else f"{rounds}／{target}"
    once(sentence.group(0), f"（{new_rounds} 轮用 `{lite_model}` 经 `/v1/chat/completions`：主线 `{main_model}` 从未经该接口验证，\n回滚镜像以 {prev} 等旧身份出站也未必能用）")

    if part2_sha(text) != part2_sha(Path(a.guide).read_text()):
        sys.exit("自检失败：第二部分被改动（不应发生）")
    out = Path(a.output)
    if out.exists():
        sys.exit(f"输出已存在，拒绝覆盖：{out}")
    out.write_text(text)
    print(f"已写 {out}；第二部分摘要未变 {part2_sha(text)[:12]}…；变化规则 {len(changed)} 条、继承 {inherited} 条；端点 静态 {n_static}／动态 {n_dynamic}；回滚标签 {rollback_label}")

    # ---- 残留扫描：第一、三、四部分里仍同时有上一版号与 active／当前 字样的行（只提示）
    lines = text.splitlines()
    p2 = next(i for i, l in enumerate(lines) if l == PART2)
    p3 = next(i for i in range(p2 + 1, len(lines)) if lines[i].startswith("# ") and lines[i] != PART2)
    hits = [(i + 1, l) for i, l in enumerate(lines) if not (p2 <= i < p3) and prev in l and re.search(r"(?i)\bactive\b|当前", l)]
    print(f"残留扫描：第一／三／四部分里仍含 {prev} 且带 active／当前 字样的行 {len(hits)} 处（通读判断是否为历史叙述）：")
    for n, l in hits[:40]:
        print(f"  L{n}: {l[:140]}")


if __name__ == "__main__":
    main()
