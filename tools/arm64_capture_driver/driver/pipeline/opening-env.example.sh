# 开工检查（entry-preflight.sh 的 model-availability 与 main-security-scan 两项）用的环境变量模板。
# 使用前复制成 <PREP>/opening-env.sh 并按本轮改（REPLACE_ 开头的值必须替换）；start-entry.sh 会 source <PREP>/opening-env.sh。
# 必须在“启动入口的那个 shell 里”source（不是参数文件的键）；手工起时：
#   source <PREP>/opening-env.sh
#   ARM64_VC_ENV=<本轮参数文件> setsid -f bash <驱动目录>/entry.sh > $RUNROOT/entry.out 2>&1 < /dev/null
# 空跑同理：source 之后再 bash entry-dryrun.sh start …（入口空跑会白名单透传这六个变量）。
#
# 模型可用性：指向采集主机上 sub2apiplus 网关本身（容器映射 0.0.0.0:28080，/v1 是 OpenAI 风格入口），密钥取参数文件里 API_KEY_ID 对应的
# 网关 API Key——由 start-entry.sh 每次起跑前从 sub2apiplus 库读出写到 0600 文件（只读查询，不改任何账号／Key 的状态），不把密钥落进本文件或命令行。
# MODEL_PROBE_MINIMAL=1：除了拉模型列表，再对 MAIN_MODEL／LITE_MODEL 各发一个最小 /responses 请求（max_output_tokens=16），证明整条链
# （Key→分组→账号→上游）真的能出这两个模型；模型不在列表或最小请求被拒，当场拦下。
export MODEL_PROBE_URL=http://127.0.0.1:28080/v1
export MODEL_PROBE_KEY_FILE=${PREP}/probe-real.key
export MODEL_PROBE_MINIMAL=1
# 采集主机没有 gh：Security Scan 一项必然未通过，必须带理由跳过（记为 skipped，不是 passed）。
# 理由里写：在本机用 gh -R itv3/sub2apiplus 核对到的 main 头提交、Security Scan run id、结论与时间；起跑前若 main 头换了
# （例如集成分支已合入 main），要在本机重查新 main 头并同步改这里
# （命令：gh run list -R itv3/sub2apiplus --workflow security-scan.yml --branch main --limit 3）。
export SECURITY_SCAN_REPO=itv3/sub2apiplus
export OPENING_CHECKS_SKIP=main-security-scan
export OPENING_CHECKS_SKIP_REASON="REPLACE_采集主机未装 gh；某日期在本机核对：main 头提交、Security Scan run id 与结论"
