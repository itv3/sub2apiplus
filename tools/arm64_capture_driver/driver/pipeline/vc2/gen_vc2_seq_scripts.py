#!/usr/bin/env python3
"""从集成分支的驱动脚本 vc23.sh、vc2-approve-and-stage.sh 机械改写出“批次序号可变”的版本。

为什么：驱动自带的 vc23.sh 把 VC-2／VC-3 的批次序号写死成 2～6，只适用于 VC-1 恰好只用了批次 1。Codex 的 VC-1 封存要再派发“封存预览”“封存批准”两个批次
（驱动里没有 Codex 封存脚本），所以 VC-2 起的序号必须取“下一个可用序号”SEQ0。流水线的 VC2 阶段改调用本脚本集（驱动目录里的 vc23.sh 不能改，所以另放本目录）。
断言：每处替换命中次数必须与期望一致，驱动脚本一变就整体失败，不会悄悄改错。
用法：gen_vc2_seq_scripts.py <驱动 driver 目录> <输出目录>（输出应与本目录已入库的两个 -seq.sh 逐字一致，测试守这一条）
"""
import sys
from pathlib import Path

DRIVER, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)


def edit(text, old, new, count=1):
    assert text.count(old) == count, (old[:60], text.count(old), count)
    return text.replace(old, new)


# ---------------------------------------------------------------- vc23.sh -> vc23-seq.sh
s = (DRIVER / "vc23.sh").read_text(encoding="utf-8")
s = edit(s, 'source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"\n',
         'PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}\n'
         'PIPE_HOME=${PIPE_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}\n'
         'source "${PIPE_DRIVER_DIR}/lib.sh"; cd "$D"\n'
         ': "${SEQ0:?必须设置 SEQ0（本 Campaign 下一个可用的批次序号；流水线用 control/vc/batches 下已提交的最大序号 +1 算出）}"\n')
s = edit(s, "# 用法：ARM64_VC_ENV=… bash vc23.sh\n",
         "# 用法：ARM64_VC_ENV=… SEQ0=<下一个可用批次序号> bash vc23-seq.sh\n"
         "# （由驱动 vc23.sh 机械改写：批次序号 2～6 写死 → SEQ0 起的五个连续序号；其余字节不变。生成器 gen_vc2_seq_scripts.py）\n")
s = edit(s, 'echo "=== 批次 2：prepare-profile（本轮批准输入 → 目标 Snapshot）"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 2 VC-1 action-plan-vc2-prepare-profile.json',
         'echo "=== 批次 $SEQ0：prepare-profile（本轮批准输入 → 目标 Snapshot）"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 "$SEQ0" VC-1 action-plan-vc2-prepare-profile.json')
s = edit(s, 'echo "=== 批次 3：classify 草案"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 3 VC-1 action-plan-vc2-draft.json',
         'echo "=== 批次 $((SEQ0+1))：classify 草案"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 "$((SEQ0+1))" VC-1 action-plan-vc2-draft.json')
s = edit(s, 'bash "$DRV/vc2-approve-and-stage.sh" "$JOINT"\n',
         'SEQ0="$SEQ0" bash ${PIPE_HOME}/vc2/vc2-approve-and-stage-seq.sh "$JOINT"\n')
# 管理 token 补丁：库整体从别处同步过来后，DB 里唯一的 admin 用户邮箱与 compose .env 的 ADMIN_EMAIL 对不上，
# lib.sh 的 ensure_admin_token 里 jwtgen 报 USER_NOT_FOUND，而该函数把 stderr 丢到 /dev/null、test -s 失败后 set -e 静默退出
# （vc23.out 只剩“管理 token：force 重签”一行，流水线 STOPPED rc=2）。lib.sh 属受管驱动不能改，所以在本脚本里换成同逻辑的函数：
# 先用 .env 的 ADMIN_EMAIL 签；签不出就回落到 DB 里恰好一个的 active admin。不改 compose .env、不改 DB。
TOKEN_FUNC = """# 管理 token 补丁：管理 token 签发（见 gen_vc2_seq_scripts.py 的说明）。其余逻辑与 lib.sh 的 ensure_admin_token force 相同。
ensure_admin_token_dbadmin() {
  local st="$D/state/$UP"
  echo "管理 token：force 重签（先用 .env 的 ADMIN_EMAIL，对不上 DB 管理员时回落到 DB 里唯一的 active admin）"
  mkdir -p "$st"; chmod 700 "$st"
  if [ -e "$st/admin-token" ]; then mv "$st/admin-token" "$st/admin-token.superseded-$(date -u +%Y%m%dt%H%M%Sz)"; fi
  ( cd "$COMPOSE_DIR" && set -a && . ./.env && set +a
    DB_IP="$(docker inspect sub2apiplus-postgres --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")"
    sign_with() { DATABASE_HOST="$DB_IP" DATABASE_PORT=5432 DATABASE_USER="$POSTGRES_USER" DATABASE_PASSWORD="$POSTGRES_PASSWORD" DATABASE_DBNAME="$POSTGRES_DB" DATABASE_SSLMODE=disable JWT_SECRET="$JWT_SECRET" JWT_EXPIRE_HOUR="${JWT_EXPIRE_HOUR:-24}" timeout 30 "$JWTGEN_BIN" -email "$1" 2>/dev/null | sed -n "s/^JWT=//p" | head -1; }
    # 脚本带 pipefail／set -e：jwtgen 非零退出时必须吞掉，才能走到下面的回落分支
    tok="$(sign_with "$ADMIN_EMAIL" || true)"
    if [ -z "$tok" ]; then
      mapfile -t emails < <(docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" sub2apiplus-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select email from users where role='admin' and status='active' order by id")
      [ "${#emails[@]}" = 1 ] || { echo "管理 token 回落失败：DB 里 active admin 不是恰好一个（${#emails[@]} 个）" >&2; exit 3; }
      echo "NOTE: .env 的 ADMIN_EMAIL 在 DB 里不存在，回落到 DB 的唯一 active admin（邮箱长度 ${#emails[0]}）" >&2
      tok="$(sign_with "${emails[0]}" || true)"
    fi
    printf "%s" "$tok"
  ) > "$st/admin-token.tmp"
  test -s "$st/admin-token.tmp" || { echo "管理 token 签发失败（jwtgen 无输出）" >&2; return 3; }
  printf "%s" "$(cat "$st/admin-token.tmp")" > "$st/admin-token"; rm -f "$st/admin-token.tmp"; chmod 400 "$st/admin-token"
  python3 -c "
import base64,json,sys,time
t=open(sys.argv[1]).read().strip(); p=t.split('.')[1]; p+='='*(-len(p)%4); d=json.loads(base64.urlsafe_b64decode(p)); print('token exp 剩余分钟:', (int(d['exp'])-int(time.time()))//60)" "$st/admin-token"
}
"""
s = edit(s, 'echo "=== 管理 token"; ensure_admin_token force\n', 'echo "=== 管理 token"; ensure_admin_token_dbadmin\n')
s = edit(s, ': "${SEQ0:?必须设置 SEQ0（本 Campaign 下一个可用的批次序号；流水线用 control/vc/batches 下已提交的最大序号 +1 算出）}"\n',
         ': "${SEQ0:?必须设置 SEQ0（本 Campaign 下一个可用的批次序号；流水线用 control/vc/batches 下已提交的最大序号 +1 算出）}"\n' + TOKEN_FUNC)
(OUT / "vc23-seq.sh").write_text(s, encoding="utf-8")

# ---------------------------------------------------------------- vc2-approve-and-stage.sh -> vc2-approve-and-stage-seq.sh
t = (DRIVER / "vc2-approve-and-stage.sh").read_text(encoding="utf-8")
t = edit(t, 'source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"\n',
         'PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}\n'
         'PIPE_HOME=${PIPE_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}\n'
         'source "${PIPE_DRIVER_DIR}/lib.sh"; cd "$D"\n'
         ': "${SEQ0:?必须设置 SEQ0（与 vc23-seq.sh 同一个值）}"\n')
t = edit(t, 'echo "=== 批次 4：批准预览"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 4 VC-1 action-plan-vc2-preview.json',
         'echo "=== 批次 $((SEQ0+2))：批准预览"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 "$((SEQ0+2))" VC-1 action-plan-vc2-preview.json')
t = edit(t, 'echo "=== 批次 5：批准"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 5 VC-1 action-plan-vc2-approve.json',
         'echo "=== 批次 $((SEQ0+3))：批准"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 "$((SEQ0+3))" VC-1 action-plan-vc2-approve.json')
t = edit(t, 'echo "=== 批次 6：VC-3 stage-profile"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-3 6 VC-2 action-plan-vc3-stage-profile.json',
         'echo "=== 批次 $((SEQ0+4))：VC-3 stage-profile"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-3 "$((SEQ0+4))" VC-2 action-plan-vc3-stage-profile.json')
t = t.replace("# VC-2 五件套已准备且草案完成后，顺序执行预览、离线门、批准与 VC-3。\n",
              "# VC-2 五件套已准备且草案完成后，顺序执行预览、离线门、批准与 VC-3。\n# （由驱动 vc2-approve-and-stage.sh 机械改写：批次序号 4～6 写死 → SEQ0+2～SEQ0+4；其余字节不变。）\n", 1)
(OUT / "vc2-approve-and-stage-seq.sh").write_text(t, encoding="utf-8")
for name in ("vc23-seq.sh", "vc2-approve-and-stage-seq.sh"):
    (OUT / name).chmod(0o755)
print("已生成 vc23-seq.sh、vc2-approve-and-stage-seq.sh ->", OUT)
