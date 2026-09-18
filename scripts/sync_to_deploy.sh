#!/usr/bin/env bash
# 单向同步：运行仓库 → 桌面发布副本（约定 2026-09-18）
#
# 规矩：只改运行仓库 /home/q/ai_berkshire_web，桌面副本
# /mnt/c/Users/13531/Desktop/ai_berkshire_deploy 只由本脚本单向覆盖。
# 反向覆盖会删掉第 4 层新鲜度门闸、恢复已下架技能（实测过）。
#
# 用法:
#   bash scripts/sync_to_deploy.sh              # 干跑，只列出会发生的变动
#   bash scripts/sync_to_deploy.sh --apply      # 真同步
#   bash scripts/sync_to_deploy.sh --apply --dest /some/path
#
# 不动的东西：.git（保留副本自己的历史）、.env（密钥）、venv/、data/、
# logs/、各类缓存与运行产物 —— 这些是副本本地状态，同步会毁掉它们。
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="/mnt/c/Users/13531/Desktop/ai_berkshire_deploy"
APPLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dest) DEST="$2"; shift ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
  shift
done

[ -d "$DEST" ] || { echo "目标不存在: $DEST"; exit 1; }
command -v rsync >/dev/null || { echo "未安装 rsync"; exit 1; }

# 同步的代码与配置目录（--delete 只在这些目录内生效，副本自己的产物不受影响）
DIRS=(app scripts tests templates static eval/cassettes docs .github)
FILES=(requirements.txt ruff.toml start.sh README.md LICENSE .env.example .gitignore)

EXCLUDES=(
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '*.pyo'
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude '.mypy_cache/'
  --exclude 'eval/runs/' --exclude '.env'
  # 历史备份/死代码不进发布副本（运行仓库里留着备查，副本要干净）
  --exclude '*.bak' --exclude '*.bak_*' --exclude '*.bak.*'
  --exclude 'harness.bak_*' --exclude '*.orig' --exclude '*.rej'
)

FLAGS=(-a --delete --itemize-changes --human-readable)
[ "$APPLY" -eq 1 ] || FLAGS+=(--dry-run)

echo "源  : $SRC"
echo "目标: $DEST"
echo "模式: $([ "$APPLY" -eq 1 ] && echo '真同步' || echo '干跑（不写入）')"
echo

OUT="$(mktemp)"
rc=0
for d in "${DIRS[@]}"; do
  [ -e "$SRC/$d" ] || continue
  rsync "${FLAGS[@]}" "${EXCLUDES[@]}" "$SRC/$d" "$DEST/" >>"$OUT" 2>&1 || rc=1
done
for f in "${FILES[@]}"; do
  [ -e "$SRC/$f" ] || continue
  rsync "${FLAGS[@]}" "${EXCLUDES[@]}" "$SRC/$f" "$DEST/" >>"$OUT" 2>&1 || rc=1
done

DEL=$(grep -c '^\*deleting' "$OUT" || true)
NEW=$(grep -cE '^>f\+\+\+\+\+\+\+\+\+' "$OUT" || true)
CHG=$(grep -cE '^>f\.|^>f\s' "$OUT" || true)
TOT=$(grep -cE '^>f' "$OUT" || true)

echo "=== 变动汇总 ==="
echo "  新增文件: $NEW"
echo "  修改文件: $CHG"
echo "  删除文件: $DEL（多为已下架技能或旧文件在副本里的残留）"
echo
echo "=== 明细（前 40 条） ==="
grep -E '^(>f|\*deleting)' "$OUT" | head -40
[ "$(grep -cE '^(>f|\*deleting)' "$OUT")" -gt 40 ] && echo "  …（完整清单：$OUT）"

if [ "$APPLY" -eq 1 ]; then
  echo
  echo "=== 同步后校验：两边关键文件是否一致 ==="
  bad=0
  for f in app/harness/guards.py app/harness/runner.py app/core/context.py app/skills/registry.py requirements.txt; do
    if diff -q "$SRC/$f" "$DEST/$f" >/dev/null 2>&1; then
      echo "  一致  $f"
    else
      echo "  不一致 $f"; bad=1
    fi
  done
  echo "  已下架技能是否仍在副本: $(ls "$DEST"/app/skills/ | grep -cE '^(quality-screen|wechat-article|dyp-ask|investment-checklist)\.md$') 个"
  [ "$bad" -eq 0 ] && echo "同步完成：关键文件全部一致" || { echo "同步后仍有差异，请检查"; exit 1; }
else
  echo
  echo "（干跑结束，未写入任何文件。加 --apply 执行）"
  rm -f "$OUT"
fi
exit $rc
