#!/usr/bin/python3
"""收尾部署核验（``codex_closeout.py`` 的 deployment_check 动作，在 ARM64 上跑；只读、零业务请求）。

做什么
    核对 ARM64 生产网关停在 VC-6 退休后的最终镜像：容器运行且健康、镜像 ID 等于最终镜像、固定 IP、``/health`` 返回 ok。
    最终镜像、画像摘要、目标版本都不写成字面量，而是从配置里登记的三份输入凭证现取并交叉核对：
        arguments = [退休切换阶段 stage.json, 激活收据 activation-receipt.json, 激活事实文件]
    * stage.json（control/vc6-retire-deploy-<STAMP>/stages/retire_switch/stage.json）给出最终镜像 image_id、active_version、profile_digest；
    * activation-receipt.json（control/vc6-arm64-<TS>/activation-receipt.json）给出目标版本与画像摘要，两处必须一致；
    * 激活事实文件（ARM64 /root/docker/sub2apiplus/app/data/<ctag>-production-activation-fact.json）必须是 profile_mode=active、
      codex_version=目标版本、profile_digest 与 image_id 等于上面核对过的值。
输入
    deployment_check.py <stage.json> <activation-receipt.json> <激活事实文件> [--container 名] [--network 名] [--ip 地址] [--port 端口]
    环境变量（由 codex_closeout.py 注入）：CODEX_CLOSEOUT_OPERATION_KEY、CODEX_CLOSEOUT_REVIEW_SHA256、CODEX_CLOSEOUT_RESULT。
    容器名、网络名、固定 IP 的缺省值是 ARM64 生产网关的现行部署（sub2apiplus / sub2apiplus_sub2api-network / 172.20.0.2:8080），部署变了用选项覆盖。
输出
    全部通过时写执行凭证 deployment-check-receipt.json（结果文件同目录）与 codex-closeout-action-result/v1 结果，退出码 0；
    任何一项不符即退出 1、不写通过结果。结果或凭证已存在时拒绝覆盖。
何时用
    由 closeout.sh 的 b10b-prepare 把本脚本放进 ARM64 收尾目录并写进收尾配置；收尾工具在发布阶段自己调用，人不直接跑。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONTAINER = "sub2apiplus"
DEFAULT_NETWORK = "sub2apiplus_sub2api-network"
DEFAULT_IP = "172.20.0.2"
DEFAULT_PORT = 8080


def fail(message):
    print("部署核验未通过：" + message, file=sys.stderr)
    raise SystemExit(1)


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    key, review, result_path = (environ.get(name) for name in ("CODEX_CLOSEOUT_OPERATION_KEY", "CODEX_CLOSEOUT_REVIEW_SHA256", "CODEX_CLOSEOUT_RESULT"))
    if not key or not review or not result_path:
        fail("缺少收尾动作环境变量")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("stage")
    parser.add_argument("activation")
    parser.add_argument("fact")
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--network", default=DEFAULT_NETWORK)
    parser.add_argument("--ip", default=DEFAULT_IP)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        fail("用法：deployment_check.py <retire_switch/stage.json> <activation-receipt.json> <激活事实文件> [--container 名] [--network 名] [--ip 地址] [--port 端口]")
    stage = json.loads(Path(args.stage).read_text())
    activation = json.loads(Path(args.activation).read_text())
    fact_path = Path(args.fact)

    final_image = stage.get("image_id")
    checks = stage.get("checks") or {}
    version = checks.get("active_version")
    profile_digest = checks.get("profile_digest")
    if stage.get("status") != "pass" or not final_image or not version or not profile_digest:
        fail("退休切换阶段凭证不完整或未通过")
    final_state = activation.get("final_state") or {}
    if final_state.get("active_version") != version or final_state.get("profile_digest") != profile_digest:
        fail("激活收据与退休切换阶段的版本或画像摘要不一致")

    result_file = Path(result_path)
    inspect = subprocess.run(["docker", "inspect", args.container, "--format", "{{json .}}"], capture_output=True, text=True)
    if inspect.returncode:
        fail("docker inspect 失败：" + inspect.stderr.strip()[-200:])
    container = json.loads(inspect.stdout)
    state = container.get("State", {})
    image_id = container.get("Image")
    health = (state.get("Health") or {}).get("Status")
    if image_id != final_image or state.get("Status") != "running" or health != "healthy":
        fail(f"容器状态不符：image={image_id} status={state.get('Status')} health={health}")
    ips = {name: net.get("IPAddress") for name, net in (container.get("NetworkSettings", {}).get("Networks") or {}).items()}
    if ips.get(args.network) != args.ip:
        fail(f"固定 IP 不符：{ips}")
    try:
        with urllib.request.urlopen(f"http://{args.ip}:{args.port}/health", timeout=10) as response:
            health_body = json.loads(response.read().decode())
    except Exception as error:  # noqa: BLE001 - 任何失败都按未通过处理
        fail(f"/health 请求失败：{error}")
    if health_body.get("status") != "ok":
        fail(f"/health 返回 {health_body}")
    fact_bytes = fact_path.read_bytes()
    fact = json.loads(fact_bytes)
    if (fact.get("profile_mode") != "active" or fact.get("codex_version") != version or fact.get("profile_digest") != profile_digest
            or fact.get("image_id") != final_image):
        fail("激活事实不符：" + json.dumps({k: fact.get(k) for k in ("profile_mode", "codex_version", "profile_digest", "image_id")}, ensure_ascii=False))
    receipt = {
        "schema_version": "codex-closeout-deployment-check/v1",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "operation_key": key, "review_sha256": review,
        "host": "ARM64", "container": args.container, "image_id": image_id, "state": state.get("Status"), "health": health,
        "networks": ips, "health_endpoint": health_body,
        "activation_fact": {"path": str(fact_path), "sha256": hashlib.sha256(fact_bytes).hexdigest(),
                            "profile_mode": fact.get("profile_mode"), "codex_version": fact.get("codex_version"),
                            "profile_digest": fact.get("profile_digest"), "image_id": fact.get("image_id")},
        "business_requests": 0,
    }
    receipt_path = result_file.parent / "deployment-check-receipt.json"
    if receipt_path.exists() or result_file.exists():
        fail("核验凭证或结果已存在，拒绝覆盖")
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.chmod(receipt_path, 0o600)
    binding = {"path": str(receipt_path), "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    result = {"schema_version": "codex-closeout-action-result/v1", "status": "passed", "operation_key": key,
              "review_sha256": review, "execution_receipt": binding}
    result_file.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.chmod(result_file, 0o600)
    print(json.dumps({"status": "passed", "image_id": image_id, "health": health, "receipt": binding}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
