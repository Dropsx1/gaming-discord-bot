# 🎮 Universal Play Haven Bot

The Discord bot for **Universal Play Haven**, an SFW gaming community. It's built with **Python 3.12** and **discord.py 2.x**, keeps its data in **SQLite**, and is deployed on **Railway**.

**Setting up a server takes one command.** Invite the bot and run **`/setup`**. It creates (or reuses) every channel and role it needs and remembers their IDs, so there's no ID copying or env-var editing. One running bot can serve several servers at once.

Every command works as a **slash command** (`/lfg`) and also with the **`!` prefix** (`!lfg`). Scheduled times use **US Eastern** (America/New_York) and adjust for daylight saving automatically. The bot never pings @everyone or @here.

## Features

| Feature | Commands |
|---|---|
| **Welcome + rules gate**: welcome message pointing to #rules, and a rules panel whose **I accept** button grants the member role | automatic, `/rulespanel`, `/welcometest` |
| **Self-roles**: menus for platforms, games, interests (Creator, AI Enthusiast, Tech Head, Game Dev) and opt-in pings (Events, Challenges, Game Nights, Prompt pings) | `/roles` |
| **LFG**: clean looking-for-group posts with autocomplete and an optional LFG role ping | `/lfg` |
| **Events**: announcements with **Going / Maybe** buttons, live counts, and a ping to everyone Going 30 min before start | `/event` |
| **Prompt of the Day**: daily prompt with a reply thread; the most-reacted reply becomes **👑 Prompt King** for 24h | automatic |
| **Remix Friday**: weekly design challenge; 🔥 votes pick the **🏆 Remix Champion** (role for 7 days) | automatic |
| **Weekly digest**: newest gaming, AI and tech headlines from RSS, plus last week's Remix winner and the most active channel | automatic |
| **Giveaways**: Enter button, live entry count, optional role requirement, random winners, rerolls | `/giveaway start · end · reroll` |
| **Moderation**: warnings saved to the database, timeouts, kicks, role-hierarchy checks and staff logging | `/warn`, `/warnings`, `/timeout`, `/untimeout`, `/kick` |
| **Auto voice**: Join-to-Create temporary channels with owner controls | `/vc ...` |
| **Tickets**: private support channels with claim, close (with optional transcript), add/remove user | `/ticketpanel`, `/ticket`, `/close`, `/claim`, `/adduser`, `/removeuser` |
| **Fun**: game suggestions, conversation starters, challenges and prompts (optional Grok/xAI) | `/suggest`, `/idea`, `/prompt` |
| **AI (Grok/xAI, optional)**: chat that remembers (also by @mention), chat summaries, trivia with XP, image generation with styles, AI welcome lines, AI auto-moderation (text + images) with Undo / Timeout buttons, usage stats and daily cost caps | `/ask`, `/ask reset`, `/summarize`, `/trivia`, `/trivia-leaderboard`, `/imagine`, `/automod` (admin), `/ai-usage` (admin), `/imagine-cap` (admin) |
| **Live AI news**: new articles from OpenAI, Anthropic, Google DeepMind, The Verge, TechCrunch, VentureBeat, Ars Technica and MIT Tech Review, each posted once, with optional AI summaries and 🚨 BREAKING alerts | `/news status · on · off · test` (admin) |
| **Server setup**: creates/reuses all channels and roles and saves their IDs; link existing channels/roles by hand; show the saved config | `/setup`, `/setup-link`, `/setup-role`, `/setup-show` (admin) |
| **Utility** | `/help`, `/ping`, `/schedule-test` (admin), `!sync` (bot owner) |

**Survives restarts.** All buttons and menus keep working after a restart: role menus, tickets, giveaways, event RSVPs and the rules button. Warnings, giveaways, RSVPs, schedules and temporary roles are stored in the database. If the bot was offline when a scheduled post was due, it catches up when it's back, and it never double-posts.

## Weekly schedule (US Eastern)

| When | What happens |
|---|---|
| Daily 6:05 PM | Crown yesterday's **Prompt King** (most reactions on a reply; ties go to the earliest) |
| Daily 6:07 PM | Post **Prompt of the Day**, with a reply thread and a ping to the Prompt pings role |
| Monday 10:13 AM | Post the **weekly digest** |
| Friday 4:11 PM | Kick off **Remix Friday** with a theme, rules, an entry thread and a ping to the Challenges role |
| Sunday 5:09 PM | "Voting closes soon" reminder in the Remix thread |
| Sunday 6:00 PM | Count 🔥 votes, announce the **Remix Champion** and lock the thread |
| Every 10 min | Remove expired Prompt King / Remix Champion roles and catch up on anything missed |

To move a time, edit the `*_TIME` values in `config.py`. To pause everything, set `SCHEDULE_ENABLED=false`.

## Project structure

```
gaming-discord-bot/
├── main.py                # Startup, cog loading, slash-command sync, error handling
├── config.py              # Settings from env vars + EDITABLE role lists and schedule times
├── cogs/
│   ├── general.py         # /help, /ping, !sync
│   ├── setup.py           # /setup + /setup-show (automatic server setup)
│   ├── welcome.py         # Welcome message on join
│   ├── onboarding.py      # /rulespanel + "I accept" button
│   ├── roles.py           # /roles self-role menus
│   ├── lfg.py             # /lfg
│   ├── events.py          # /event with RSVP buttons + reminders
│   ├── schedule.py        # Prompt of the Day, Remix Friday, digest, /schedule-test
│   ├── giveaways.py       # /giveaway
│   ├── moderation.py      # /warn /warnings /timeout /untimeout /kick
│   ├── fun.py             # /suggest /idea /prompt
│   ├── ai_features.py     # /ask (+ @mention chat), /imagine, AI automod, /ai-usage (Grok/xAI)
│   ├── ai_extras.py       # /summarize, /trivia, /trivia-leaderboard
│   ├── news.py            # Live AI news feed + /news
│   ├── voice.py           # Join-to-Create + /vc
│   └── tickets.py         # Ticket system
├── utils/
│   ├── db.py              # SQLite storage + migrations
│   ├── guildconfig.py     # Per-server channel/role IDs (saved by /setup, env vars as fallback)
│   ├── content.py         # Picks unused prompts/themes from data/
│   ├── news.py            # RSS article parsing for the AI news feed
│   ├── timeparse.py       # Reads event times like "sat 8pm"
│   ├── ai.py              # Optional Grok/xAI client (text, images, moderation)
│   └── helpers.py         # Staff checks, logging, durations
├── data/
│   ├── prompts.json       # 120 Prompt of the Day items
│   ├── remix_themes.json  # 52 Remix Friday themes
│   ├── rss_feeds.json     # Digest feeds (gaming / ai / tech)
│   ├── rules.md           # Rules text for /rulespanel
│   └── README.md          # Content file format
├── tests/                 # python tests/test_setup.py, tests/test_multiserver.py
├── requirements.txt
├── Procfile               # worker: python main.py
├── railway.json           # Start command + restart policy
└── .env.example           # Every setting, documented
```

---

## 1. Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications>, click **New Application**, name it and click **Create**. Copy the **Application ID** from General Information.
2. Open the **Bot** tab:
   - Click **Reset Token** and copy the token. This is your `DISCORD_TOKEN`; never share or commit it.
   - Under **Privileged Gateway Intents**, enable **Server Members Intent** and **Message Content Intent**, then save. Voice States is a standard intent, so you don't need to turn anything on for it.
   - Optional: turn off **Public Bot** so only you can add it to servers.
3. Invite the bot. Replace `YOUR_CLIENT_ID` with your Application ID, open the link and pick your server:
   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=1428363136082&scope=bot+applications.commands
   ```
   That link grants these permissions:
   - Roles and moderation: Manage Roles, Manage Channels, Kick Members, Moderate Members
   - Messages: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions
   - Threads: Create Public Threads, Send Messages in Threads, Manage Threads
   - Voice: Connect, Move Members
   - Use Application Commands, plus Mention Everyone (only so the bot can ping roles that aren't marked mentionable; the bot itself never pings @everyone or @here)
4. **Important:** in **Server Settings → Roles**, drag the bot's role **above** every role it hands out: the member role, self-roles, Prompt King, Remix Champion and giveaway/LFG roles. It also has to sit above anyone it moderates. Roles that `/setup` creates are placed below the bot automatically, but this step still matters for roles that already exist.

> Already invited the bot with an older link? Open the new link to add the thread permissions. Or, in Server Settings → Roles, give the bot's role **Create Public Threads**, **Send Messages in Threads** and **Manage Threads**.

## 2. Set up the server: run `/setup`

In your server, as an admin, run:

```
/setup dry_run:True     preview: lists what would be created or reused, changes nothing
/setup                  do it
/setup-show             see the channel/role IDs saved for this server
/setup-role setting:Member (I accept button) role:@Verified
                        use an existing role instead of the one /setup made
/setup-link key:Prompt of the Day channel channel:#daily-prompt
/setup-link key:Member role role:@Verified
                        point ANY setting at an existing channel, category or role
/setup-link key:ASK_CHANNEL_ID channel:#ask-the-bot
                        where /ask works (default: #bot-commands)
/setup-link key:NEWS_CHANNEL_ID channel:#ai-news
                        where the live AI news feed posts (see "Live AI news")
```

`/setup-link` covers every setting `/setup` knows (channels, categories, roles, log channels, ping roles). Start typing in `key` and pick from the list, or use the name, e.g. `PROMPT_CHANNEL_ID` / `member_role`. It checks the target is the right kind (text, voice, category or role), and the same role safety rules as `/setup-role` apply.

`/setup-role` points any of the bot's roles (Member, Staff, Prompt King, Remix Champion, the ping roles) at a role you already have, and takes effect immediately. Roles the bot hands out (Member, Prompt King, Remix Champion) must sit below the bot's role and can't carry moderator permissions, since anyone who qualifies gets them.

`/setup` creates anything that's missing and **reuses** anything that already exists with the same name. Names are compared without emoji, dividers or capitals, so a hand-made `📅┃prompt-of-the-day` or `#📖〢「rules` counts as `prompt-of-the-day` / `rules`, in any category. It finds everything that exists *before* creating anything, so if Discord rate-limits it, only items that are truly missing wait. It's safe to run again: the second run creates nothing. It never edits or deletes channels or roles that already exist. If something looks wrong, such as a role above the bot or a "private" category that @everyone can see, it tells you instead of changing it.

| What | Created by `/setup` | Used for |
|---|---|---|
| Roles | **Staff**, **Member**, **Prompt King**, **Remix Champion**, **LFG** | Staff commands and ticket access, rules button, 24h/7-day winner roles, `/lfg` pings |
| 📁 Server | `#welcome`, `#rules`, `#announcements`, `#self-roles` (read-only for members) | Welcome messages, rules panel, `/event` posts, role menus |
| 📁 Community | `#general-chat`, `#looking-for-group`, `#giveaways`, `#clips-and-media`, `#bot-commands` | Chat, `/lfg` posts, giveaways, ticket panel |
| 📁 AI & Create Lab | `#prompt-of-the-day`, `#tech-digest`, `#weekly-challenge`, `#ai-art`, `#tech-talk` | Daily prompt, weekly digest, Remix Friday. Members reply in threads |
| 📁 Staff (hidden) | `#mod-log` | Mod actions, ticket logs and transcripts |
| 📁 Tickets (hidden) | (ticket channels appear here) | Private support tickets |
| 📁 Voice | 🔊 `➕ Join to Create` | Temporary voice channels |

The hidden categories are visible only to **Staff** and the bot. After `/setup`:
1. Give the **Staff** role to your moderators. For `/timeout` and `/kick`, the role also needs Discord's **Moderate Members** / **Kick Members** permissions.
2. Run `/rulespanel` in `#rules`, `/roles` in `#self-roles` and `/ticketpanel` in `#bot-commands` (see step 4).
3. Optional rules gate: deny **View Channels** to @everyone on your main categories and allow it for **Member**. New members then have to click **I accept** in `#rules` to see the server.

**Ping roles** (Events, Challenges, Game Nights, Prompt pings) aren't created up front. Members opt into them in `/roles`, the bot creates each one the first time someone picks it, and the scheduler pings **Prompt pings** / **Challenges** automatically.

### Moving to another server (or adding one)

1. Invite the bot to the new server with the link from step 1.
2. Run `/setup` there.

That's it. Slash commands appear in each server the bot joins automatically, scheduled posts go out in every server that has the channels configured, and each server keeps its own settings. To leave the old server, kick the bot from it. For this multi-server mode, leave `GUILD_ID` **empty** in Railway: when `GUILD_ID` is set, slash commands only sync to that one server.

### Advanced: configure with env vars instead

You can still set channel/role IDs as env vars (see [`.env.example`](.env.example)), for example to point the bot at existing channels with different names. IDs saved by `/setup` always win. Env-var IDs only apply to the **home** server: the one in `GUILD_ID`, or every server when `GUILD_ID` is empty. To find an ID, turn on **User Settings → Advanced → Developer Mode**, then right-click the channel/role and choose **Copy ID**. The optional `PROMPT_PING_ROLE_ID` / `CHALLENGE_PING_ROLE_ID` override the ping roles named above.

### Editing content

| What | Where |
|---|---|
| Self-role menus (platforms, games, interests, pings) | `PLATFORM_ROLES`, `GAME_ROLES`, `INTEREST_ROLES`, `PING_ROLES` in `config.py`. Each entry is `(label, role name or role ID, emoji)`, max 25 per menu |
| Schedule times | `*_TIME` values in `config.py` |
| Prompts / Remix themes | `data/prompts.json`, `data/remix_themes.json`. Keep every `id` unique. Items are picked at random from the unused ones; the rotation resets once every item has been used |
| Digest feeds | `data/rss_feeds.json` (the newest 3 headlines per feed) |
| AI news feeds | `ai_news` in `data/rss_feeds.json` (`{name, url}`; the `ai` feeds are included too). News limits: `NEWS_*` in `config.py` or as env vars |
| Rules text | `data/rules.md`. Run `/rulespanel` again after editing |
| Server name in text | Write `{server}` in `rules.md`, a prompt or a Remix theme. The bot fills in the name of the server it's posting in (e.g. "📜 {server} Rules" → "📜 Nexus Arcade Rules") |

After changing anything in `config.py` or `data/`, push to GitHub. Railway redeploys automatically.

## 3. Deploy on Railway

1. Go to <https://railway.com>, choose **New Project → Deploy from GitHub repo** and pick `Dropsx1/gaming-discord-bot`. Authorize the Railway GitHub app if asked.
2. **Add a volume so data survives redeploys.** Without one, warnings, giveaways, RSVPs and schedules are wiped on every deploy.
   - Press `⌘K` / `Ctrl+K` (or right-click the project canvas), choose **New Volume**, and connect it to the bot's service.
   - Set the **mount path** to **`/data`**.
   - The bot stores its database at `/data/bot.db` automatically. You can also set `DB_PATH=/data/bot.db` yourself.
   - ⚠️ **Don't** mount the volume at `/app/data`. That would hide the bundled prompts, themes and rules.
3. Open the service's **Variables** tab and add **`DISCORD_TOKEN`**. That's the only required variable. Channel and role IDs come from `/setup`. Leave `GUILD_ID` empty to use the bot in any server, or set it to limit slash commands to one server.
4. Railway installs `requirements.txt` and starts `python main.py` (from `railway.json`, with `Procfile` as a backup). The bot doesn't need a public domain or port.
5. Check **Deployments → View Logs**. A healthy start looks like:
   ```
   [INFO] bot.db: Database ready at /data/bot.db
   [INFO] bot: Loaded cog: cogs.schedule
   [INFO] bot.guildconfig: Loaded saved config for 1 server(s)
   [INFO] bot: Synced 26 slash commands to guild 123...
   [INFO] bot: Logged in as YourBot#1234
   [INFO] bot.schedule: Scheduler started (timezone America/New_York)
   ```
   If you see `No Railway volume attached`, go back to step 2.
6. **Restart:** use **Deployments → ⋮ → Restart**. Every push to `main` redeploys, and the restart policy brings the bot back if it crashes.
7. In Discord, run **`/setup`** (step 2).

### Run locally (optional)

```bash
git clone https://github.com/Dropsx1/gaming-discord-bot.git
cd gaming-discord-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # fill in DISCORD_TOKEN (everything else is optional)
python main.py          # database goes to ./data/bot.db
python -m pytest tests/   # offline tests, no token needed (pip install pytest)
```

## 4. First-time setup in Discord (staff)

```
/setup             creates/reuses every channel and role (step 2)
/rulespanel        in #rules: rules + "I accept" button
/roles             in #self-roles: self-role menus
/ticketpanel       in #bot-commands: "Open Ticket" button
/welcometest       preview the welcome message
```

Then test the scheduled posts without waiting (admins only). Test runs don't ping anyone unless you add `ping:True`, and they don't replace the real scheduled post:
```
/schedule-test job:prompt          /schedule-test job:prompt_king
/schedule-test job:digest          /schedule-test job:remix_kickoff
/schedule-test job:remix_reminder  /schedule-test job:remix_close
/schedule-test job:cleanup
```

## 5. Command reference

### Community
```
/lfg game:Valorant region:NA platform:PC rank:Gold 2 notes:Need 2 for ranked ping_role:True
/event title:Among Us Night time:sat 8pm description:Bring your sus energy ping_role:@Events
/suggest type:game   /suggest type:convo   /idea theme:Valorant   /prompt theme:retro
```
`/event` times are Eastern and accept `8pm`, `sat 8pm`, `tomorrow 7:30pm`, `10/31 8pm`, `2026-10-31 20:00` or `in 2h`. Members see the time in their own time zone. Clicking Going or Maybe again removes your RSVP.

### Giveaways (staff)
```
/giveaway start prize:Steam gift card duration:2d winners:1 required_role:@Members
/giveaway end message:<pick from list>
/giveaway reroll message:<pick from list> winners:1
```
Durations run from 1 minute to 30 days (`30m`, `2h`, `1d12h`), with 1 to 20 winners. Winners come from entrants still in the server who still have the required role. Winners are DMed. Rerolls never pick a previous winner. For `end` and `reroll`, pick the giveaway from the list or paste a message link.

### Moderation (needs Moderate Members / Kick Members)
```
/warn member:@User reason:Spamming        (saved; DM'd with their warning count)
/warnings member:@User                    (last 10 warnings, works even if they left)
/timeout member:@User duration:30m reason:Cool off    (30, 2h, 1d, 1h30m; max 28d)
/untimeout member:@User
/kick member:@User reason:Repeated rule breaks
```

### Auto voice channels
Join **➕ Join to Create** to get your own channel, then:
```
/vc info   /vc rename name:Ranked Grind   /vc limit limit:5   /vc lock   /vc unlock
/vc permit member:@Friend   /vc kick member:@User   /vc transfer member:@User   /vc claim
```
The channel is deleted when it's empty. Discord allows 2 renames per 10 minutes.

### Tickets
Members click **🎫 Open Ticket** or use `/ticket`. Inside a ticket:
- **🔒 Close**: owner or staff; you can close with or without a transcript.
- **✋ Claim**: staff.
- `/adduser` and `/removeuser`: staff.

### Remix Friday and Prompt King rules
- **Remix entries:** each person's first post in the thread with an image, video or link counts. Votes are 🔥 reactions; self-votes and bot reactions don't count. Ties go to the earliest entry.
- **Prompt King:** the reply in the prompt thread with the most reactions wins.

## Optional: Grok / xAI

Everything AI is optional. Set `XAI_API_KEY` (from <https://console.x.ai>) in Railway. Without it, every AI command replies with a friendly "AI is off" message and auto-moderation does nothing. Scheduled posts never use AI; they always come from `data/`.

| Feature | Needs | Model |
|---|---|---|
| `/idea`, `/prompt` | `XAI_API_KEY` + `XAI_MODEL` (otherwise built-in lists) | `XAI_MODEL` |
| `/ask`, @mention chat, `/summarize`, ticket summaries, news recaps | `XAI_API_KEY` | `XAI_SMART_MODEL` (default `grok-4.7`, xAI's flagship chat model) |
| AI auto-moderation (text), `/imagine` safety check + prompt enhancement, news scoring, `/trivia`, welcome lines | `XAI_API_KEY` | `XAI_FAST_MODEL` (a cheap non-reasoning model; defaults to `XAI_MODEL`) |
| AI auto-moderation (images) | `XAI_API_KEY` + `/automod images on` | `XAI_VISION_MODEL` (default: `XAI_SMART_MODEL`; grok-4.7 takes image input) |
| `/imagine` | `XAI_API_KEY` | `XAI_IMAGE_MODEL` (default `grok-imagine-image`) |

At startup the bot logs the model names your key can use, and warns if a configured model isn't one of them.

**Daily cost caps.** Every AI call is priced (xAI's list prices in `utils/ai.py`) and counted per feature per day (saved in SQLite, so restarts don't reset it). When a feature reaches its cap it behaves as if the AI were unavailable (the usual friendly message) until midnight in `TIMEZONE`. Defaults (USD/day, whole bot): ask 2, imagine 2, summarize 1, automod-image 1, mention 1, automod 0.5, news 0.5, trivia 0.5, ticket-summary 0.5, others 0.25. Override with e.g. `AI_DAILY_COST_CAPS=ask=5,imagine=3`.

`XAI_SMART_REASONING` (default `low`) sets grok-4.7's reasoning effort for chat; empty = the model default (`high`, slower and pricier). If a model rejects it, the bot retries once without it.

### /ask (chat that remembers)
```
/ask chat question:What's a good beginner build in Elden Ring?
/ask chat question:And which weapon fits it?      (remembers the last answer)
/ask reset                                         forget this channel's / thread's conversation
@Bot what about armor?                             same as /ask, in the /ask channel or its threads
```
- The bot remembers the last `ASK_MEMORY_TURNS` (6) exchanges per channel or thread for `ASK_MEMORY_HOURS` (24 h), stored in SQLite. Each remembered question carries the asker's name, so several members can share one conversation.
- **🧵 Continue in thread** under an answer opens a thread that starts with the conversation so far.
- Mentioning the bot uses the same memory, cooldown and daily limit. `@Bot help` (a prefix command) still runs the command.
- Short, safe-for-work answers. NSFW requests are refused.
- Works in #bot-commands by default. Change it with `/setup-link key:ASK_CHANNEL_ID channel:#somewhere`. Staff can use it anywhere.
- Limits: 1 question per 30 s per user, 30 per day per user (`ASK_DAILY_LIMIT`), shared by `/ask` and mentions.

### /summarize
```
/summarize messages:100          only you see it
/summarize messages:50 public:True   (staff) post it for everyone
```
- Summarizes the last N (5-200) non-bot messages into **topics**, **decisions** and **open questions**.
- Limits: 1 per 2 min per user, `SUMMARIZE_DAILY_LIMIT` (10) per day.
- **Tickets:** staff can close with **Close + transcript + AI summary** (or `/close ai_summary:True`) to post a summary to the ticket log.

### /trivia
```
/trivia topic:Zelda difficulty:hard
/trivia-leaderboard
```
- The AI writes one multiple-choice question; everyone answers with the buttons within 30 s. First correct answer: 10/20/35 XP (easy/medium/hard); later correct answers get half. XP is saved per server.
- Limits: one question per channel every 20 s, `TRIVIA_DAILY_LIMIT` (20) questions started per user per day.

### /imagine
```
/imagine prompt:a fox knight style:pixel art aspect_ratio:16:9
```
- **Styles:** none, anime, pixel art, cinematic, logo, poster, 3d. **Aspect ratio:** auto (model picks), 1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3 (values `grok-imagine-image` supports).
- **Enhance** (on by default): after the safety checks, short prompts (≤ 20 words) are rewritten into a detailed prompt in the chosen style. The rewrite is shown under the image; if it fails or trips the NSFW filter, your own prompt is used.
- **🔁 Variation** makes another image from the same prompt; **🎛️ Remix** opens a box to edit the prompt (which goes through every check again). Both count against the clicker's own limits.
- Works only in #ai-art and #bot-commands.
- NSFW prompts are blocked twice: a keyword check first (free), then a quick AI moderation check before anything is generated.
- Limits: 1 image per 2 min per user, 5 per day per user (`IMAGINE_DAILY_LIMIT`), 50 per day for the whole server (`IMAGINE_GUILD_DAILY_CAP`, or `/imagine-cap images:100` per server).

### AI welcome messages
With a key, new members get a short personalized greeting line (on the fast model) at the top of the usual welcome embed. If the AI is off, slow (8 s), fails, says something unsafe, or more than 20 people join in an hour, the normal template is used. Turn it off with `WELCOME_AI=false`. `/welcometest` previews it.

### AI auto-moderation (admin)
```
/automod status     /automod on     /automod off
/automod sensitivity level:low|med|high
/automod mode mode:delete|warn       warn = keep the message, DM a warning, log it
/automod images enabled:on|off       check PNG/JPEG uploads with the vision model (off by default)
/automod stats days:30               flags and Undo clicks (false-positive rate) by reason and verdict
```
- The AI also sees the 3 messages before a flagged one, so banter isn't mistaken for harassment.
- Image checks: at most 2 images per message and one check per member every 30 s.
- Off by default. It watches public text channels and their threads. It skips staff, bots, private channels, the mod log and tickets.
- Each message first goes through a free keyword/regex filter. Only suspicious messages are sent to the AI, which classifies them as ok / toxic / nsfw / spam. A user's messages are checked by the AI at most once every 10 s.
  - **low**: slurs, threats, scams (free nitro…), NSFW words, 8+ mentions
  - **med** (default): low, plus insults, Discord invite links, 5+ mentions and character spam
  - **high**: med, plus any swearing and long all-caps messages
- If the AI flags a message, the bot deletes it, DMs the author a short warning, and posts the message, user, reason and AI verdict to the mod log. The log post has **↩️ Undo** (re-posts the message and marks it a false positive) and **🔇 Timeout 10m** buttons, which only Staff can use and which keep working after restarts.
- The bot needs **Manage Messages** to delete messages and **Moderate Members** for the timeout button.

### /ai-usage (admin)
Shows today's and this month's AI calls per feature (`ask`, `imagine`, `imagine-check`, `automod`, `news`), with tokens, images, failures and how many users. It also lists the current limits. Every AI call is logged in the `ai_usage` table. "Today" and limits reset at midnight in `TIMEZONE`.

## Live AI news

A channel that posts new AI articles as they come out.

```
/setup-link key:NEWS_CHANNEL_ID channel:#ai-news     turn it on: pick the channel
/news status      what's on, posted today, how many are waiting
/news off         pause it          /news on      resume it
/news test        post the latest article right now
/news daily on|off|now   optional daily AI-written recap of the top stories (posts ~NEWS_DAILY_RECAP_HOUR:30)
```

- The same story from several outlets is merged: it posts once, with "Also covered by …" in the footer.
- A feed that answers HTTP 429 (rate limited) is skipped for 30 min (doubling up to 6 h, or the feed's Retry-After). That never counts as a failure or alerts the mod log.
- With a key, the Monday weekly digest starts with an AI-written recap of the week's top-scored stories.

- **Sources:** OpenAI, Anthropic, Google DeepMind, The Verge AI, TechCrunch AI, VentureBeat AI, Ars Technica AI and MIT Technology Review AI (the `ai_news` list in `data/rss_feeds.json`), plus the digest's `ai` feeds. Anthropic has no official RSS feed, so a community mirror is used.
- **How it works:** every 20 minutes the bot checks every feed. Each article is saved by its URL, so it's posted **once** per server, even if a feed lists it again or adds tracking codes to the link. Each post shows the title, source, a 1–2 sentence summary, a link and the article's image when there is one.
- **No flood:** articles more than 24 hours old are never posted, so turning it on (or the first run) doesn't dump the backlog.
- **Limits:** at most **1 post per 30 minutes** and **12 per day** (midnight to midnight in `TIMEZONE`). Extra articles wait in a queue and the best go first. No normal posts during **quiet hours, 1–7 AM** Eastern.
- **With `XAI_API_KEY`:** the AI writes the summary and scores each article's importance from 1 to 10 (shown in the footer); the queue is ordered by that score. A **9 or 10** is **🚨 BREAKING**: a red embed that posts right away, even in quiet hours, and pings the role named **AI News** (or the role set with `/setup-link key:NEWS_ROLE_ID`). Breaking news is capped at 3 a day as a safety net. Make the role mentionable so the ping notifies, and add it to your ping self-roles if members should opt in. The AI only looks at new articles from the last 24 hours, at most 20 per check; its calls show as `news` in `/ai-usage`.
- **Without a key:** the feeds' own descriptions are used as summaries, newest first, and nothing is marked breaking.
- **Problems** go to the mod log: a feed that fails 3 checks in a row (it's skipped until it works again), or a post the bot couldn't make (missing permissions). A bad feed never stops the others. The bot needs **Send Messages** and **Embed Links** in the news channel.
- **Tuning (optional env vars):** `NEWS_POLL_MINUTES` (20), `NEWS_MIN_GAP_MINUTES` (30), `NEWS_DAILY_LIMIT` (12), `NEWS_BREAKING_SCORE` (9), `NEWS_BREAKING_DAILY_LIMIT` (3), `NEWS_QUIET_START` / `NEWS_QUIET_END` (1 / 7).

## Troubleshooting

| Problem | Fix |
|---|---|
| `DISCORD_TOKEN is not set` | Add it under Railway → Variables |
| `PrivilegedIntentsRequired` | Enable the Server Members and Message Content intents in the Developer Portal |
| `No Railway volume attached` in logs | Add a volume mounted at `/data` (Deploy step 2) |
| `data/prompts.json is missing` | The volume is mounted at `/app/data`; change the mount path to `/data` |
| Slash commands don't appear | Invite with the `applications.commands` scope and restart Discord (Ctrl+R). If `GUILD_ID` is set to a different server, clear it. The owner can also type `!sync` in the server |
| `/setup` says it's missing permissions | Give the bot's role **Manage Roles** and **Manage Channels**, or re-invite it with the link in step 1 |
| A feature says it isn't configured | Run `/setup` (it only adds what's missing), then check `/setup-show` |
| "My role needs to be above these roles" | Move the bot's role higher in Server Settings → Roles |
| Posted, but no thread / "couldn't create a thread" | Give the bot Create Public Threads + Send Messages in Threads in that channel |
| Role ping shows but doesn't notify | Make the role mentionable, or keep the bot's Mention Everyone permission |
| Prompt or Remix didn't post | Check `/setup-show` lists `PROMPT_CHANNEL_ID` / `CHALLENGE_CHANNEL_ID`, and look for `Job prompt` / `Job remix_kickoff` in the logs. Try `/schedule-test` |
| AI news isn't posting | `/news status`: is it on, with a channel? It waits 30 min between posts, stops at 12 a day and pauses 1–7 AM. Only articles from the last 24 hours are posted. Try `/news test` |
| Digest says a feed couldn't be loaded | That site was down or changed its feed URL; edit `data/rss_feeds.json` |
| New members can't see anything after accepting | Allow View Channels for the **Member** role on your categories, and check `/setup-show` lists `MEMBER_ROLE_ID` |
| Duplicate slash commands | Commands were once synced globally too. The global copies expire, or re-invite the bot |
| `PyNaCl is not installed` warning | Safe to ignore. The bot doesn't play audio |

## License

MIT.
