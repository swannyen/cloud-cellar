# cloud-cellar

Telegram bot tracker for the household goods at home, so that we know when we need to restock anything, and if it's a good price.

The design and the phase-by-phase build plan are in [PLAN.md](PLAN.md).

## Status

**Phase 1 (logging from chat), parts 1a and 1b.** A webhook Lambda behind a function URL that:

- rejects any request without Telegram's secret header (`403`), drops updates it has already seen, and ignores chats that aren't in `AllowedChatIds`,
- reads each line of a message with plain code when it follows the format (`+ toilet paper 2x10 $12.90 @fairprice`, `- dish soap`, `low shampoo`, `have rice 2x5kg`, `? detergent 3kg $8.95 @shengsiong`). The store and the numbers are always read by code, never by AI,
- counts everything in the item's base unit (pieces, rolls, sheets, g, ml) and never guesses what a number means: `2pcs` is two pieces, `2x10rolls` is two packs of ten, and a bare `2` is two packs only once the item's pack size is known. Otherwise nothing is recorded and the bot asks,
- matches the item by name or saved alias, or by a close spelling, with no AI. A brand taught with `/brand` is taken off the front of the name first and noted on the entry (`kans facial masks` is facial masks, brand kans). It asks Claude only for an item name it can't place (a small call) and for lines that aren't in the format but look like a log (the full free-text parse, whose answer is checked against the message before anything is recorded),
- shows **Same item** / **New item** buttons under any match it had to guess. One tap saves the name for good, or moves the entry to a new item,
- records the events in DynamoDB, keeps each item's stock and status (`ok`, `low`, `out`) up to date, and confirms in one line per event with the total in the base unit,
- answers `/items`, `/undo` (repeat it to step back through the last 24 hours), `/alias`, `/set <item> pack <n>`, `/unit <word> = <unit word>`, `/brand <name>`, `/stats`, `/help` and `/ping`.

Set `FREE_TEXT_AI` to `"off"` in [template.yaml](template.yaml) to stop the free-text calls: those lines then get a hint about the format instead. `/stats` shows how many lines needed AI this month.

Run-out predictions and nudges (Phase 2) and the price verdicts (Phase 3) are not built yet.

## Deploy

One-time setup (Telegram bot, AWS account, secrets in SSM Parameter Store under `/bettynuffbot/`) is PLAN.md §1.

Everything runs in `ap-southeast-2` (Sydney).

```bash
export AWS_PROFILE=household AWS_REGION=ap-southeast-2
sam build
sam deploy --guided         # first deploy; afterwards: sam build && sam deploy
./scripts/set_webhook.sh    # point Telegram at the function URL
```

`sam build` needs Python 3.13 on your `PATH`. The prompts, the function URL permission check and day-to-day operation are in PLAN.md §7.

After a deploy that adds commands, paste the command list from PLAN.md §1.1 step 7 into BotFather's `/setcommands`, so Telegram suggests them.

After a deploy that changes how stock is counted, recompute the figures cached on each item (it shows the changes first, and only saves with `--write`):

```bash
python scripts/recompute_items.py           # shows before -> after, saves nothing
python scripts/recompute_items.py --write   # saves it
```

## Tests

```bash
pip install boto3           # the only import outside the standard library
python -m unittest discover -s tests -v
```

The tests fake DynamoDB, Telegram and Claude, so they need no AWS credentials or network.

## Free-text eval

`tests/fixtures/freetext.jsonl` holds free-text messages with the events each one should produce. To run them against the live Claude API (one call per message, about four US cents in total):

```bash
export AWS_PROFILE=household AWS_REGION=ap-southeast-2
python3 scripts/eval_freetext.py
```

It grades what would be recorded, after anything the model gave that isn't written in the message has been dropped. It prints accuracy per field and lists every message it got wrong. The target is at least 90% of messages fully correct and no events on ordinary chatter. Add real messages from the group to the fixture file as you collect them, and rerun this after changing `FREE_TEXT_PROMPT` in `src/ai.py`.

## Secrets

Nothing secret belongs in this repo. The bot token, Claude API key and webhook secret live in SSM Parameter Store; `.env` is git-ignored.
