#!/usr/bin/env bash
# 部署后的自检：把 BASE 和 PASSKEY 换成你自己的。
#
#   ./curl-check.sh https://your.site <32位passkey> [已知的pieces_hash]
#
# 期望输出：
#   · method   → 405（只支持 POST）
#   · bad pass → 403 passkey 不存在
#   · real     → ret=0 且 data 里出现这个 pieces_hash
set -u
BASE="${1:?用法: ./curl-check.sh https://your.site <passkey> [pieces_hash]}"
PK="${2:?缺少 passkey}"
PH="${3:-}"
URL="$BASE/api/pieces-hash"

echo "== 1. 方法检查（应为 405）"
curl -s -o /dev/null -w "  HTTP %{http_code}\n" "$URL"

echo "== 2. 坏 passkey（应为 403）"
curl -s -X POST "$URL" -H 'Accept: application/json' -H 'Content-Type: application/json' \
     -d '{"passkey":"00000000000000000000000000000000","pieces_hash":["0000000000000000000000000000000000000000"]}'
echo

if [ -n "$PH" ]; then
  echo "== 3. 真实查询"
  curl -s -X POST "$URL" -H 'Accept: application/json' -H 'Content-Type: application/json' \
       -d "{\"passkey\":\"$PK\",\"pieces_hash\":[\"$PH\"]}"
  echo
else
  echo "== 3. 跳过真实查询（没给 pieces_hash）"
fi
