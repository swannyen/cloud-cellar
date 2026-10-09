#!/usr/bin/env bash
# Point Telegram at the deployed function URL, then print the webhook status.
# Needs AWS_PROFILE and AWS_REGION in the environment. Prints no secrets.
set -euo pipefail
STACK=${STACK_NAME:-nuffnuffbot}
PREFIX=${PARAM_PREFIX:-/bettynuffbot}
URL=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text)
# An empty url would remove the webhook instead of setting it.
[[ "$URL" == https://* ]] || { echo "No WebhookUrl output on stack $STACK" >&2; exit 1; }
TOKEN=$(aws ssm get-parameter --name "$PREFIX/telegram-token" --with-decryption --query Parameter.Value --output text)
SECRET=$(aws ssm get-parameter --name "$PREFIX/webhook-secret" --with-decryption --query Parameter.Value --output text)
curl -sS "https://api.telegram.org/bot${TOKEN}/setWebhook" \
  -d "url=${URL}" \
  -d "secret_token=${SECRET}" \
  -d "drop_pending_updates=true" \
  --data-urlencode 'allowed_updates=["message","callback_query"]'
echo
curl -sS "https://api.telegram.org/bot${TOKEN}/getWebhookInfo"; echo
