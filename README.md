# cloud-cellar

Telegram bot tracker for the household goods at home, so that we know when we need to restock anything, and if it's a good price.

The design and the phase-by-phase build plan are in [PLAN.md](PLAN.md).

## Status

**Phases 1 (logging from chat) and 2 (the price book) are deployed. So are expiry dates and `/use`, brought forward from Phase 5. So are descriptions on each line.** A webhook Lambda behind a function URL that:

- rejects any request without Telegram's secret header (`403`), drops updates it has already seen, and ignores chats that aren't in `AllowedChatIds`,
- reads each line of a message with plain code when it follows the format (`+ toilet paper 2x10 $12.90 @fairprice`, `- dish soap`, `low shampoo`, `have rice 2x5kg`, `? detergent 3kg $8.95 @shengsiong`). The store and the numbers are always read by code, never by AI,
- counts everything in the item's base unit (pieces, rolls, sheets, g, ml) and never guesses what a number means: `2pcs` is two pieces, `2x10rolls` is two packs of ten, and a bare `2` is two packs only once the item's pack size is known. Otherwise nothing is recorded and the bot asks,
- matches the item by name or saved alias, or by a close spelling, with no AI. A brand taught with `/brand` is taken off the front of the name first and noted on the entry (`kans facial masks` is facial masks, brand kans). It asks Claude only for an item name it can't place (a small call) and for lines that aren't in the format but look like a log (the full free-text parse, whose answer is checked against the message before anything is recorded),
- is more careful still with a sentence than with the format. A number that doesn't say what it counts (`got 30 eggs`: 30 eggs, or 30 packs?) is asked about, and one total for several items (`eggs, milk and rice, $31.40 total`) is recorded as the price of none of them,
- takes the same entries as commands: `/add` and `/bought` work like `+`, `/used` like `-`, and `/stocktake` like `have`,
- adds up a stocktake: the lines of a count, and the counts of other brands later the same day, make one total (a brand counted again that day replaces its earlier figure), and "I have another 3 bottles" adds to it. Each count's reply gives the new total,
- reads a known store written last on a line, after the price or quantity, as the store: `/add body wash 800ml $2.58 taobao`,
- reads an expiry date on a line, written as `exp 12/27`, `exp 31/12/2026` or the way it's said in a sentence (`expires in April 2028`), keeps it with that purchase or count line, and uses stock in expiry order. `/use <item>` says which one to use next: the one that expires first,
- keeps the brand on every line, so `/list <item>` can say how much of each brand there is, and a description too ("pink", "green tea fragrance"), so `/use` can say which bottle it means. A brand the bot knows (taught with `/brand` or on an earlier entry) is kept even when it is short or looks like an ordinary word (`na hair shampoo`),
- adds something found at home with `/add hand wash 3x400g exp 07/2029` (or "found 2 more bottles…" in a sentence), without counting everything again. `/add` and `+` take a price if there is one; `/bought` needs one,
- reads a quantity on a `-` or `/used` line as how much was used (`- toilet paper 2` takes two packs off the stock). `/used hand wash` with no amount is one bottle: the next one to use, at its own size. `- hand wash` with no amount means it has run out, and with a brand (`- aesop hand wash`) only that brand,
- shows **Same item** / **New item** buttons under any match it had to guess. One tap saves the name for good, or moves the entry to a new item,
- records the events in DynamoDB, keeps each item's stock and status (`ok`, `low`, `out`) up to date, and confirms in one line per event with the total in the base unit,
- answers `/listitems` (what's in the cellar and how much), `/list <item>` (one item: how much is left, and an estimate of which brands that is, taking the oldest purchase to be used first), `/items` (the same items with their IDs), `/undo` (repeat it to step back through the last 24 hours), `/alias`, `/rename <item> = <new name>`, `/set <item> pack <n>`, `/unit <word> = <unit word>`, `/brand <name>`, `/listbrands`, `/stats`, `/help` and `/ping`,
- takes back what it was taught with `/forget alias|brand|unit|store <name>`. Entries are never removed by it; forgetting a brand also takes that brand off the entries that carry it, since the bot knows brands from those too,
- takes the date of an old receipt on any entry line, always day first: `+ rice 5kg $12 @ntuc on 15/09/2026`. The reply spells the date out, and a date to come or one it can't read records nothing and shows the format,
- says how each price compares with earlier ones (first, best, good, typical or above usual), on `+` and `?` lines alike, and answers `/price <item>` with the best, usual and latest prices. Prices are SGD unless an entry says otherwise. Every entry is a true log and counts towards stock, but repeats of one price on one day count as one price.

Set `FREE_TEXT_AI` to `"off"` in [template.yaml](template.yaml) to stop the free-text calls: those lines then get a hint about the format instead. `/stats` shows how many lines needed AI this month.

**Built, not deployed yet: the stock table is protected.** In [template.yaml](template.yaml) the table is kept if the stack is deleted or a change would replace it, DynamoDB refuses to delete it while deletion protection is on, and point-in-time recovery keeps 35 days of restore points. `scripts/export_csv.py` (below) copies everything to the laptop.

The daily message (Phase 3a), and run-out predictions, nudges and stock-up suggestions (Phase 3b), are not built yet.

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

## Backup

A launchd job on the Mac backs the table up on the 1st and the 15th of each month. It checks every day at 10:00 and at login, and exports only when there is no backup yet since the last 1st or 15th, so a day the Mac was off is made up the next time it's on. A failed export puts up a notification.

```bash
PYTHON=<a Python with boto3> ./scripts/install_backup_schedule.sh   # install, or update after changing the export code
./scripts/install_backup_schedule.sh --run-now                      # one backup now, through the job
./scripts/install_backup_schedule.sh --uninstall                    # remove the job; the backups stay

AWS_PROFILE=household AWS_REGION=ap-southeast-2 python3 scripts/export_csv.py   # a backup by hand
```

Each backup is two files in `~/cloud-cellar-backups`: `cellar-<date-time>.csv`, one row per entry for a spreadsheet, and `.json`, every record as stored. The folder is outside the repo because the files hold the family's messages, and outside `~/Documents` because macOS doesn't let launchd jobs read or write there. For the same reason, the installer copies the export code to `~/Library/Application Support/cloud-cellar-backup`, so run it again after changing `scripts/export_csv.py` or `src/`. The log is `~/Library/Logs/cloud-cellar-backup.log`. Restoring from point-in-time recovery is in PLAN.md §7.4.

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
