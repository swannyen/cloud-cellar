# cloud-cellar

Telegram bot tracker for the household goods at home, so that we know when we need to restock anything, and if it's a good price.

The design and the phase-by-phase build plan are in [PLAN.md](PLAN.md).

## Status

**Phase 1 (logging from chat).** A webhook Lambda behind a function URL that:

- rejects any request without Telegram's secret header (`403`), drops updates it has already seen, and ignores chats that aren't in `AllowedChatIds`,
- sends each text message in the group to Claude, which picks out what was bought, counted, opened, finished or price-checked,
- records those events in DynamoDB, keeps each item's status (`ok`, `low`, `out`) up to date, and confirms in one line per event,
- answers `/items`, `/undo`, `/help` and `/ping`.

Run-out predictions and nudges (Phase 2) and the price verdicts (Phase 3) are not built yet.

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

The tests fake DynamoDB, Telegram and Claude, so they need no AWS credentials or network.

## Parser eval

`tests/fixtures/messages.jsonl` holds example messages with the events each one should produce. To run them against the live Claude API (one small call per message, about two US cents in total):

```bash
export AWS_PROFILE=household AWS_REGION=ap-southeast-2
python3 scripts/eval_parser.py
```

It prints accuracy per field and lists every message it got wrong. The target is at least 90% of messages fully correct and no events on ordinary chatter. Add real messages from the group to the fixture file as you collect them, and rerun this after changing the prompt in `src/parser.py`.

## Secrets

Nothing secret belongs in this repo. The bot token, Claude API key and webhook secret live in SSM Parameter Store; `.env` is git-ignored.
