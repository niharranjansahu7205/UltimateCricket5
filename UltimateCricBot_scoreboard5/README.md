# UltimateCricBot 🏏

Premium hand-cricket mini game for Telegram groups, built for **@UltimatecricBot**.

## Features
- `/cricket` — start a 1v1 hand-cricket match in a group
- **Unlimited simultaneous matches** — any number of matches can run at once,
  even multiple matches inside the same group at the same time (each match
  is tracked independently by its own message, not shared per-group state)
- Toss (Head/Tail), Bat/Bowl choice, secret number picks in strict turn order
  (batter picks first, then bowler — never simultaneous)
- Game modes: Default, 1-3 Mode, No 5 Mode
- **1st innings**: no over limit — batting continues until the batter is OUT
- **2nd innings (chasing)**: ends either on OUT, or the instant the target
  is reached/passed — that player wins immediately, no need to get out
- Premium formatting: bold scoreboards, emoji dividers, styled result cards
- `/mystats` and `/leaderboard` — persistent SQLite stats per group
- `/cancelcricket` — cancel your match. If you have more than one active
  match in the group, reply to that specific match's message with the
  command to cancel just that one.
- 🎬 **Shot & wicket animations** — plays a short MP4 clip after every ball
  based on the run scored (1/2/3/4/6) or on OUT
- 🚫 **Owner-only moderation** — `/banuser`, `/unbanuser`, `/banlist`.
  Bans are global (block a user in every group the bot is in).
- 📣 **Owner-only broadcast** — `/broadcast <text>`, or reply to a photo
  with `/broadcast <caption>`, to message every group and user the bot
  has ever seen a message from.
- `/tictac` — Tic-Tac-Toe with a Join button; whoever starts it is ❌ and
  always moves first, whoever joins is ⭕.
- `/teamcricket` — full **Team Cricket** mode, 2-12 players per side (see below).

## Team Cricket (/teamcricket)

A full team-vs-team cricket match — captains, toss, batting/bowling order,
real strike rotation, dynamic all-out, target chase, scorecards, Player of
the Match, and its own leaderboard. Completely separate module
(`teamcricket.py` + its own database tables) from `/cricket` — nothing here
touches the 1v1 engine, stats, or any other feature.

**Setup**
- `/teamcricket` opens a lobby: **Join Team A** / **Join Team B**, **Leave
  Team**, **Start Match**, **Cancel**.
- Team size: **2-12 players per side** (`TEAM_MIN_PLAYERS` /
  `TEAM_MAX_PLAYERS` in `teamcricket.py`). Every combination from 2v2 up to
  12v12 is allowed; a 13th player on a full team is rejected.
- The **first player to join each team automatically becomes its captain**
  (👑). Only a captain can press Start, call the toss, pick the batting
  order, or pick the bowler for their team.

**Match flow**
- **Toss**: an automatic coin flip; Team A's captain calls Heads/Tails,
  the winning captain picks Bat First or Bowl First.
- **Batting order**: before each innings, the batting captain taps their
  players in the order they should bat (no duplicates possible — picked
  players are removed from the pool).
- **Bowler selection**: before each over, the bowling captain picks any
  player on their team *except* whoever bowled the previous over.
- **Ball-by-ball**: same secret-number fairness as `/cricket` — the striker
  picks a hidden number from **0-6** first, then the bowler picks one too
  (neither can see the other's pick). If the numbers **match**, it's a
  wicket; otherwise the striker's number is the runs scored. 0 is included
  so dot balls are a real pickable outcome, not just "no match happened
  to land on 0".
- **Strike rotation** (real cricket rules): strike changes on **odd runs**
  (1, 3, 5) and at the **end of every over** — both apply independently,
  so an odd run on the last ball of an over swaps strike twice (net: no
  change).
- **Wickets / dynamic all-out**: a team of size *N* is all out after
  **N-1 wickets** (not a hard-coded 10) — e.g. a 2-player team is all out
  after 1 wicket, a 12-player team after 11.
- **Overs**: default `TEAM_OVERS = 3` per innings, but **customizable per match**: tap 1/2/3/5/10 ov in the lobby (creator or a captain, before Start) or use `/teamcricket 7` for any value 1-20 (`TEAM_OVERS_MIN/MAX`). 6 legal balls per over.
- **Target/chase**: the 2nd innings ends the instant the target is
  reached (no wasted balls), when the overs run out, or when the team is
  all out. Win by runs, win by wickets, or an exact **tie** — a tie is
  tracked as its own outcome and never counts as a loss for anyone.
- **Scorecards & Player of the Match**: full batting (runs/balls/4s/6s/SR)
  and bowling (overs/runs/wickets/economy) scorecards after the match,
  plus an automatically-computed Player of the Match based on actual
  performance.

**Other commands**
- `/teamleaderboard` — top Team Cricket players in this group (wins,
  losses, ties, runs, wickets).
- `/teammatches` — recent completed Team Cricket matches in this group.
- `/teamhelp` — full in-chat rules summary.

**Reliability**
- Every match has its own `asyncio.Lock`, so two near-simultaneous taps
  (double-join, double ball confirm, double start) can't corrupt state.
- Every callback is namespaced `tc:<match_id>:<action>` and validated
  against the match's real current stage/team/captain before anything
  happens — a stray button from an old or unrelated match is rejected.
- If a captain or player doesn't respond within 60 seconds
  (`TURN_TIMEOUT_SECONDS`), the bot auto-resolves with a safe default
  (e.g. auto-calls heads, auto-picks the next eligible bowler, auto-fills
  the rest of the batting order in join order) so a match can never get
  permanently stuck.
- Full match history, ball-by-ball events, and per-player stats are saved
  to SQLite (`team_matches`, `team_match_players`, `team_player_stats`,
  `team_match_events` — separate tables from the 1v1 `player_stats`
  table, so nothing here can corrupt `/mystats` or `/leaderboard`).

**Known simplifications** (documented honestly, not hidden):
- The bowler-selection and batting-order UIs are simple tap-to-pick
  button flows rather than free-text entry — kept deliberately simple
  for reliability on mobile.
- `/teamleaderboard` ranks **individual players'** Team Cricket record
  (since teams are formed fresh each match, not persistent named clubs).
- Turn-timeout scheduling uses `asyncio.create_task` + `asyncio.sleep`
  (works correctly for a single bot process); it is not a distributed
  job queue, so it won't survive the bot process restarting mid-wait —
  on restart, any match that was mid-wait is simply gone (same as every
  other in-memory match in this bot, including `/cricket` and `/tictac`).
- MP4 media integration reuses the existing safe-fallback pattern from
  `/cricket` (`media/` folder) but ships with no team-specific clips by
  default — the mode works fully in text-only mode, which is also what
  happens for `/cricket` if its `media/` folder is empty.

## Solo-mode test peek (developer tool)

For testing `/cricket` (1v1): set the env var **`TEST_PEEK=1`** and make sure your
Telegram ID is in `OWNER_IDS`. While the bowler is choosing, the bowler keyboard
gets an extra **🔍 Test: peek batter's pick** button. Only bot owners can use it;
it shows the batter's number as a private popup (nothing is posted to the chat)
and is logged. With `TEST_PEEK` unset (default) the button doesn't exist and the
callback refuses everyone. **Turn it off in production** — it reveals secret picks.

## Look & feel

- **Live match message**: header with overs, team + innings, score, `▰▰▰▱▱` overs
  progress bar, run rate, target / need / required rate in a chase, striker `*`
  and non-striker with their runs (balls), bowler figures, and *this over only*
  (`1️⃣ │ 4️⃣ │ ▫️ │ ▫️`) plus the previous over once a new one starts.
- **Result image card** (`resultcard.py`, Pillow): when a match ends the bot posts
  a shareable PNG — both scores, winner badge, top bat / top bowl of each side,
  Player of the Match and the match key. Fonts are bundled in `assets/fonts`
  (DejaVu, free licence). Emoji and scripts the font can't draw are dropped
  from the *image only* (the text result keeps them). If the card fails for any
  reason it is skipped — the normal result message is always sent.

## Scoreboard popup (📊 button)

Under the 0-6 number buttons of a running Team Cricket match there is a
**📊 Scoreboard** button. Tapping it shows the live score as a popup (score,
overs, run rate, target, current over ball-by-ball, striker/non-striker,
bowler). It is read-only, sends nothing to the chat, and reads from SQLite
(`team_live_state` + `team_match_events`) so it also works after a restart.
callback_data = `sb:<match_id>:p`. (The pinned auto-updating `/scoreboard`
message was removed.)

## Substitutes (/sub)

Mid-match player replacement, between balls (after the toss; not while a ball
is being picked).

1. The team **captain** (or the match creator) **replies to the new player's
   message** with `/sub`.
2. Pick which player is replaced (captains only see their own team; the creator
   sees both).
3. The new player taps **✅ Accept & Join** (nobody is forced into a match).

Rules: max `TEAM_MAX_SUBS` (3) per team; the newcomer takes over the slot — batting
slot yet to come, a spot at the crease, or the current over — and starts from 0;
the replaced player keeps his runs/wickets (scorecard, result card, career stats
and `/history` show both, with 🔁); a replaced captain's crown passes to the
newcomer; team size (and the all-out rule) is unchanged; a replaced player can't
return. Storage: `team_match_players.subbed_out` + table `team_match_subs`.

## Match history (/history)

Every Team Cricket match has a random **5-character key** (letters + digits, no look-alike characters like 0/O/1/I) (🔑 shown on the scoreboard, the final
result card and in `/teammatches`). `/history <key>` (e.g. `/history K7M3X`; case-insensitive, `#K7M3X` also works) replays that match from SQLite: result + Player of the Match, full
batting/bowling scorecards (who got whom out), and every ball — who faced it,
who bowled it, runs/wicket. Long matches are split into pages (max 8).
`/history` alone lists recent keys. Only works in the group the match was played in.
Batters who never faced a ball are not listed (batting order of finished innings isn't stored).

Tests: `pip install pytest && python -m pytest -q tests/`

## Setting up owners (for /banuser, /unbanuser, /banlist, /addachivement, /deleteachivement)

Only the configured owners can use these commands (no limit on how many —
add as many as you need). Set them via the
`OWNER_IDS` environment variable — a comma-separated list of numeric
Telegram user IDs:

```
OWNER_IDS=111111111,222222222,333333333
```

To find your numeric Telegram user ID, message a bot like @userinfobot
or @RawDataBot — it'll reply with your ID.

If `OWNER_IDS` isn't set, the bot falls back to the placeholder list in
`admin.py` (`OWNER_IDS_FALLBACK`) — edit that file directly and replace
the `0, 0, 0` with your real IDs if you'd rather not use an env variable.

### Using ban commands
- **Ban**: reply to the user's message with `/banuser`, or run
  `/banuser <user_id>` directly.
- **Unban**: same pattern with `/unbanuser`.
- **See who's banned**: `/banlist`.

### Using /broadcast
- **Text only**: `/broadcast Hello everyone! New update is live 🏏`
- **Photo + caption**: reply to a photo message with
  `/broadcast Check out this update!`
- Sends to every group and private chat the bot has ever received a
  message from (tracked automatically — no setup needed). Chats the bot
  was removed from, or users who blocked it, are skipped automatically.

## Achievements

Owners can award or remove tournament achievements for any user. Users
check their own with `/myachivements`. Achievements are global (same
in every group), like bans.

- **Award**: reply to the user's message with `/addachivement <tournament name>`,
  or without a reply: `/addachivement <username> <tournament name>`
  (e.g. `/addachivement rahul Summer Cup 2026`).
- **Remove**: reply with `/deleteachivement`, or `/deleteachivement <username>`
  (no tournament name needed) — the bot shows a button for each of that
  user's achievements, tap one to delete just that one.
- **View your own**: any user runs `/myachivements`.

Note: targeting by username only works if the bot has already seen
that user send a message somewhere (tracked automatically in the
background). If it says the username isn't found, reply to one of
their messages instead.

## Adding your shot/wicket animations

Drop your own MP4 files into the `media/` folder using these exact filenames:

| Event         | Filename            |
|---------------|----------------------|
| 1 run         | `media/run_1.mp4`   |
| 2 runs        | `media/run_2.mp4`   |
| 3 runs        | `media/run_3.mp4`   |
| 4 runs        | `media/run_4.mp4`   |
| 5 runs        | `media/run_5.mp4`   |
| 6 (Six)       | `media/six.mp4`     |
| Out           | `media/out.mp4`     |

Notes:
- If a file is missing, the bot just skips the animation for that event —
  it never breaks the match.
- Keep clips short (a few seconds, small file size) so they load fast in
  the group chat and don't slow the game down.
- Commit these MP4 files to your GitHub repo along with the rest of the
  code so they deploy with the bot on Railway.

## Local Setup

```bash
pip install -r requirements.txt
export BOT_TOKEN="your-telegram-bot-token"
python bot.py
```

## Deploying on Railway

1. Push this folder to a GitHub repo.
2. On [Railway](https://railway.app), create a **New Project → Deploy from GitHub repo**.
3. In the Railway project's **Variables** tab, add:
   - `BOT_TOKEN` = your bot token from @BotFather
   - `DB_PATH` = `/data/ultimatecric.db` (recommended, see volume note below)
   - `OWNER_IDS` = your owners' numeric Telegram IDs, comma-separated
     (any number of owners is fine)
   - `BOT_USERNAME` = your bot's username without the @ (e.g. `UltimatecricketBot`)
     — used to build the "Add me in your group" button
   - `OFFICIAL_GROUP_LINK` = your official group's invite link
     — used for the "Join our official bot group" button
4. (Recommended) Add a **Volume** in Railway, mount it at `/data`, so
   `DB_PATH=/data/ultimatecric.db` persists stats across redeploys.
   Without a volume, SQLite writes to the container's ephemeral disk and
   resets on every redeploy.
5. Railway will detect `railway.json` / `Procfile` and run `python bot.py`
   automatically using the Nixpacks Python builder (from `requirements.txt`).
6. Bot runs in polling mode — no public URL/webhook needed.

## Files
- `bot.py` — entry point, registers all handlers
- `game.py` — full game state machine (toss → bat/bowl → balls → result)
- `stats.py` — /mystats and /leaderboard
- `database.py` — SQLite persistence layer
- `style.py` — all premium text formatting in one place
