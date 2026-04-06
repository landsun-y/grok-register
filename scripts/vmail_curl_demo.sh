#!/usr/bin/env bash
# VMAIL 联调（仅需 curl，适合宝塔/Docker 宿主机无 Python 时）
#
#   export VMAIL_API_BASE='https://vmail.liu954326053.workers.dev'
#   export VMAIL_API_KEY='你的key'
#   bash scripts/vmail_curl_demo.sh
#
# Cloudflare 部分站点会拦非浏览器 UA，下面已带 Chrome User-Agent。

set -euo pipefail

BASE="${VMAIL_API_BASE:-}"
KEY="${VMAIL_API_KEY:-}"

if [[ -z "$BASE" || -z "$KEY" ]]; then
  echo "请设置环境变量: VMAIL_API_BASE 与 VMAIL_API_KEY" >&2
  exit 2
fi

# 归一成 .../api/v1（与 email_register 一致）
API_BASE="${BASE%/}"
[[ "$API_BASE" == */api-docs ]] && API_BASE="${API_BASE%/api-docs}"
if [[ "$API_BASE" == */api/v1 ]]; then
  :
elif [[ "$API_BASE" == */api ]]; then
  API_BASE="${API_BASE}/v1"
else
  API_BASE="${API_BASE}/api/v1"
fi

UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

echo "[*] api_base=$API_BASE"
echo "[*] POST /mailboxes"
CREATE_JSON="$(curl -sS -w "\n%{http_code}" -X POST "$API_BASE/mailboxes" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "User-Agent: $UA" \
  -H "X-API-Key: $KEY" \
  -d '{"expiresIn":86400}')"
CREATE_CODE="$(printf '%s\n' "$CREATE_JSON" | tail -n 1)"
CREATE_BODY="$(printf '%s\n' "$CREATE_JSON" | sed '$d')"
echo "HTTP $CREATE_CODE"
echo "$CREATE_BODY" | jq . 2>/dev/null || echo "$CREATE_BODY"

MAILBOX_ID="$(echo "$CREATE_BODY" | jq -r '.data.id // empty')"
if [[ -z "$MAILBOX_ID" || "$MAILBOX_ID" == "null" ]]; then
  echo "创建失败或缺少 jq（可 apt/yum 安装 jq，或从 JSON 里手抄 data.id）" >&2
  exit 1
fi

echo ""
echo "[*] GET /mailboxes/$MAILBOX_ID/messages"
LIST_JSON="$(curl -sS -w "\n%{http_code}" -G "$API_BASE/mailboxes/$MAILBOX_ID/messages" \
  -H "Accept: application/json" \
  -H "User-Agent: $UA" \
  -H "X-API-Key: $KEY" \
  --data-urlencode "page=1" \
  --data-urlencode "limit=20" \
  --data-urlencode "sort=desc")"
LIST_CODE="$(printf '%s\n' "$LIST_JSON" | tail -n 1)"
LIST_BODY="$(printf '%s\n' "$LIST_JSON" | sed '$d')"
echo "HTTP $LIST_CODE"
echo "$LIST_BODY" | jq . 2>/dev/null || echo "$LIST_BODY"

MSG_ID="$(echo "$LIST_BODY" | jq -r '.data[0].id // empty' 2>/dev/null || true)"
if [[ -z "$MSG_ID" || "$MSG_ID" == "null" ]]; then
  echo ""
  echo "[*] 暂无邮件。向控制台打印的 address 发一封后再执行下面命令测详情："
  echo "curl -sS -H \"User-Agent: $UA\" -H \"X-API-Key: \$VMAIL_API_KEY\" \"$API_BASE/mailboxes/$MAILBOX_ID/messages/邮件id\""
  exit 0
fi

echo ""
echo "[*] GET /mailboxes/$MAILBOX_ID/messages/$MSG_ID"
DETAIL_JSON="$(curl -sS -w "\n%{http_code}" "$API_BASE/mailboxes/$MAILBOX_ID/messages/$MSG_ID" \
  -H "Accept: application/json" \
  -H "User-Agent: $UA" \
  -H "X-API-Key: $KEY")"
DETAIL_CODE="$(printf '%s\n' "$DETAIL_JSON" | tail -n 1)"
DETAIL_BODY="$(printf '%s\n' "$DETAIL_JSON" | sed '$d')"
echo "HTTP $DETAIL_CODE"
echo "$DETAIL_BODY" | jq . 2>/dev/null || echo "$DETAIL_BODY"
