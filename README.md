# Koruxa Optimizer

## Share with clanmates

The project is designed to run locally for each player. Share the source on GitHub, then clanmates can clone it, create their own Python environment, install `requirements.txt`, and run `start_optimizer.bat`. Each person enters their own Koruxa login and loads their characters from the game's character-selection page; they do not need to look up or type character IDs. This project does not host or share account sessions.

The repository ignore rules exclude local player, inventory, bank, mastery, market, skill, and generated report data, along with browser captures and virtual environments. Keep those files private. Before pushing changes, check `git status` and confirm that no account snapshots, credentials, or generated reports are staged.

## Set up Python

Create a virtual environment on this computer and install the project dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run from the browser

Double-click `start_optimizer.bat`. It starts a local-only service and opens the optimizer dashboard in your browser. Enter your Koruxa username and password, select **Load characters**, choose the desired character by name, and select **Run optimizer**. The local service reads the IDs from the authenticated character-selection page; they are hidden in the game's normal UI, so you do not need to look them up. Enable **Refresh market data** when you want to update inventory, bank, and market listings before ranking tasks. The dashboard shows run progress and loads the resulting report automatically. Keep the console window open until you are finished; Ctrl+C stops the local service.

The browser sends login details to the local service, which passes them to the optimizer over a pipe. They are not written to the report or saved to disk. The service listens only on this computer (`127.0.0.1`) and is not a hosted or clan-shared server. Clanmates can use the same project code by setting up their own copy and entering their own account details. Do not share your local `player.json`, `bank.json`, `inventory.json`, market snapshot, or generated reports if you want to keep account data private.

The existing **Open optimizer report** control remains available for loading a saved JSON report. Reports use schema version 1, with separate gold and XP rankings, feasibility details, price conflicts, and market snapshot age. Ongoing Net Gold/hr subtracts modeled ingredient costs and, for Thieving, estimated food replacement costs from estimated revenue. Net Gold/hr from stock excludes ingredient replacement costs while current materials last; Stockpile hours estimates when those ingredients run out. `Inputs GP/hr` shows the ongoing ingredient cost. `Feasibility` checks available inventory and bank quantities against task ingredients.

The market book reports remaining active buy-order volume and simulates filling orders from highest bid down before using NPC fallback, so effective hourly revenue falls as the visible bid depth is consumed. Refresh now fetches all market pages where pagination is exposed. Listing saturation is the percentage of active order records that are sell listings (a count-based supply-pressure signal, not a unit-volume measure). `Book` indicates whether both sides were fully paginated; older snapshots without completeness metadata display `Unknown`.

The **Transmuting** tab uses the live `alchemy/transmute-items` response: server-calculated Magic-level gold output, Golden Touch payout bonus, and effective cast duration. It also adds currently unlocked craftable outputs that are not in the live transmute list, estimating their gold from NPC sell value. Missing rune costs are inferred from the Wiki shop/NPC value brackets (1–50 Aetherite, 51–200 Voidstone, 201–1,000 Fluxite, 1,001–5,000 Prismite, over 5,000 Celestite; one rune per cast). GP/hr includes modeled gathering and production time for the item and rune, including rune crafting from Void Essence and mining the essence, plus the transmutation cast. It does not use market prices or stock rates. Action chains above current skill levels are estimated and those level gates are shown. Farming grow times, yields, level gates, and seed sourcing are included from the live farming seed data when harvest outputs can be matched to recipe ingredients. Inputs without a modeled source action are marked Unknown.

When filling gaps in item values, recipes, drops, or production times, inspect the scraped Wiki data in `data/wiki/` and its build/normalization scripts under `scripts/` before treating the information as unavailable. Prefer those sources over inventing a value; retain an Unknown only when the scrape and live game data do not support a reliable estimate.

The **Net Gold/hr** ranking uses the selected action's duration and subtracts estimated ingredient purchase costs; it does not add time to gather or craft those inputs. **Self-sourced production** ranks recipe outputs by output value divided by the full modeled time to gather and produce all materials plus the final action. It assumes materials are gathered or crafted instead of purchased, so it does not subtract their market value as an opportunity cost.

Market snapshots are timestamped. The optimizer warns when the snapshot is more than 24 hours old. Refreshing queries listings for the items referenced by the saved skill actions and can take a few minutes.

## Refresh Wiki estimates

Run the scrape and calculation steps in order:

```powershell
.\.venv\Scripts\python.exe scripts\build_wiki_skills.py
.\.venv\Scripts\python.exe scripts\build_wiki_activities.py
.\.venv\Scripts\python.exe scripts\build_wiki_items.py
.\.venv\Scripts\python.exe scripts\build_item_name_map.py
.\.venv\Scripts\python.exe scripts\normalize_activities.py
.\.venv\Scripts\python.exe scripts\calculate_wiki_profit.py
```

Wiki volume-limited profit is an estimate based on average 7-day output volume. The optimizer displays the task's theoretical production as a share of that volume. Shares above 100% exceed the full observed average volume and should not be treated as a reliable sale rate. Wiki activity pages currently do not provide Thieving action rows; Thieving calculations use the in-game action data instead.

Wiki gold estimates with an output price at least 5x away from the current lowest ask and no buy orders are excluded from the actionable gold ranking and listed separately for price review.

Refresh the combat reference data separately:

```powershell
.\.venv\Scripts\python.exe scripts\build_wiki_monsters.py
.\.venv\Scripts\python.exe scripts\normalize_wiki_monsters.py
```

This scrapes the monster index and each linked page into `data\wiki\monsters.json`, including published monster stats and per-item drop quantities and probabilities. The normalization step repairs text encoding artifacts and validates the monster/drop fields. Failed pages are listed in `data\wiki\monster_errors.json`.

The optimizer now adds solo combat targets to the gold and XP rankings. It uses the authenticated monster, equipment, per-monster mastery, and per-monster `monster-drops` APIs alongside Wiki drop tables. Live effective drop chances include character-specific gear, research, clan/Well, Bestiary, and rare-mastery effects; Wiki rows fill drops omitted from the live endpoint. Per-monster mastery `output_mult` is applied to all drop quantities. Saved active buy orders value drops (then NPC or Wiki item prices for remaining quantities), and the model estimates supply and food replacement costs. Combat candidates compare the strongest level-eligible melee, ranged, and magic weapons found in saved inventory/bank data, plus the equipped weapon when applicable. Each setup keeps the currently equipped armor; ranged requires available ammunition and Magic uses the highest-damage spell allowed by the Magic level. XP rankings show one row per monster and primary Melee, Ranged, or Magic style. Melee displays the focused rate, shared rate per Attack/Strength/Defence skill, and Hitpoints XP; Ranged and Magic display their primary XP and Hitpoints XP. Per-monster mastery XP, visible potion attack buffs, monster-specific Bestiary damage bonuses, and live combat XP bonuses are used when returned. Wiki monster stats are used when live monster rows are incomplete, and slayer-only targets are marked as requiring a task. Without a live combat bonus aggregate, other character-specific boosts may still be missing. Damage and kill-rate estimates remain approximate and should be calibrated against measured fights. Refresh market data with `optimizer.py --refresh-market` to add scraped monster drop items to the listing refresh; the existing October 8 snapshot is stale.

Thieving estimates include speed-adjusted stun time, expected damage from the mutually exclusive stun/damage chances, and an estimated replacement food cost using the cheapest priced cooking food per HP. Damage mitigation from Iron Hide and the player's configured auto-eat priority are not included.
