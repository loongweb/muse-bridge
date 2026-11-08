#!/usr/bin/env bash
# 把 skill 安装到 ~/.workbuddy/skills/muse-bridge。
# 安全约定：绝不覆盖、绝不删除任何已存在的目录；已安装就直接报错退出。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="${MUSE_BRIDGE_INSTALL_DIR:-$HOME/.workbuddy/skills/muse-bridge}"

for item in SKILL.md scripts assets references; do
  if [ ! -e "$SRC/$item" ]; then
    echo "缺少 $SRC/$item —— 请在完整的仓库副本里运行本脚本。" >&2
    exit 1
  fi
done

if [ -e "$DST" ]; then
  echo "目标已存在：$DST" >&2
  echo "本脚本不会覆盖或删除它。如需更新，请自行备份后处理，或直接 git pull 已 clone 到该路径的仓库。" >&2
  exit 1
fi

mkdir -p "$(dirname "$DST")" "$DST"
for item in SKILL.md scripts assets references; do
  cp -R "$SRC/$item" "$DST/"
done
chmod 755 "$DST"/scripts/*.py "$DST"/assets/*.sh 2>/dev/null || true

echo "已安装到 $DST"
echo "下一步："
echo "  cd \"$DST/scripts\" && python3 selftest.py     # 预期 PASS=23 FAIL=0"
echo "  然后在 WorkBuddy 里说「帮我跟 Muse 搭个会合点」。"
