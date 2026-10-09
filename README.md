# cloud-cellar

Telegram bot tracker for the household goods at home, so that we know when we need to restock anything, and if it's a good price.

The design and the phase-by-phase build plan are in [PLAN.md](PLAN.md).

## Status

**Phase 0 (plumbing).** A webhook Lambda behind a function URL that:

- rejects any request without Telegram's secret header (`403`),
- drops updates it has already seen,
- ignores chats that aren't in `AllowedChatIds`,
- answers `/ping` with `pong`.

## Deploy

One-time setup (Telegram bot, AWS account, secrets in SSM Parameter Store under `/bettynuffbot/`) is PLAN.md §1.

Everything runs in `ap-southeast-2` (Sydney), not the `ap-southeast-1` that PLAN.md names: the AWS organization this account belongs to blocks other regions.

```bash
export AWS_PROFILE=household AWS_REGION=ap-southeast-2
sam build
sam deploy --guided         # first deploy; afterwards: sam build && sam deploy
./scripts/set_webhook.sh    # point Telegram at the function URL
```

`sam build` needs Python 3.13 on your `PATH`. The prompts, the function URL permission check and day-to-day operation are in PLAN.md §7.

## Tests

```bash
pip install boto3           # the only import outside the standard library
python3 -m unittest discover -s tests -v
```

The tests fake DynamoDB and Telegram, so they need no AWS credentials or network.

## Secrets

Nothing secret belongs in this repo. The bot token, Claude API key and webhook secret live in SSM Parameter Store; `.env` is git-ignored.
