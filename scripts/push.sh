#!/usr/bin/env bash
# 带重试的 git push。这台机器的网络对 github.com 时通时断，单次 push 失败很常见。
# 用法: ./scripts/push.sh [branch]
set -uo pipefail

BRANCH="${1:-main}"
MAX_TRY="${PUSH_MAX_TRY:-5}"

cd "$(dirname "$0")/.." || exit 1

for i in $(seq 1 "$MAX_TRY"); do
  echo "[push] 第 $i/$MAX_TRY 次尝试推送 $BRANCH ..."
  if git push -u origin "$BRANCH" 2>&1; then
    echo "[push] 成功"
    exit 0
  fi
  if [ "$i" -lt "$MAX_TRY" ]; then
    sleep $((i * 3))
  fi
done

echo "[push] 失败：$MAX_TRY 次尝试后仍未推送成功" >&2
exit 1
