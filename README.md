# 🎮 Universal Play Haven Bot

The Discord bot for **Universal Play Haven**, an SFW gaming community. It's built with **Python 3.12** and **discord.py 2.x**, keeps its data in **SQLite**, and is deployed on **Railway**.

Every command works as a **slash command** (`/lfg`) and also with the **`!` prefix** (`!lfg`). Scheduled times use **US Eastern** (America/New_York) and adjust for daylight saving automatically. The bot never pings @everyone or @here.

## Features

| Feature | Commands |
|---|---|
| **Welcome + rules gate**: welcome message pointing to #start-here, and a rules panel whose **I accept** button grants the member role | automatic, `/rulespanel`, `/welcometest` |
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
│   ├── welcome.py         # Welcome message on join
│   ├── onboarding.py      # /rulespanel + "I accept" button
│   ├── roles.py           # /roles self-role menus
│   ├── lfg.py             # /lfg
│   ├── events.py          # /event with RSVP buttons + reminders
│   ├── schedule.py        # Prompt of the Day, Remix Friday, digest, /schedule-test
│   ├── giveaways.py       # /giveaway
│   ├── moderation.py      # /warn /warnings /timeout /untimeout /kick
│   ├── fun.py             # /suggest /idea /prompt
│   ├── voice.py           # Join-to-Create + /vc
│   └── tickets.py         # Ticket system
├── utils/
│   ├── db.py              # SQLite storage + migrations
│   ├── content.py         # Picks unused prompts/themes from data/
│   ├── timeparse.py       # Reads event times like "sat 8pm"
│   ├── ai.py              # Optional Grok/xAI hook
│   └── helpers.py         # Staff checks, logging, durations
├── data/
│   ├── prompts.json       # 120 Prompt of the Day items
│   ├── remix_themes.json  # 52 Remix Friday themes
│   ├── rss_feeds.json     # Digest feeds (gaming / ai / tech)
│   ├── rules.md           # Rules text for /rulespanel
│   └── README.md          # Content file format
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
4. **Important:** in **Server Settings → Roles**, drag the bot's role **above** every role it hands out: the member role, self-roles, Prompt King, Remix Champion and giveaway/LFG roles. It also has to sit above anyone it moderates.

> Already invited the bot with an older link? Open the new link to add the thread permissions. Or, in Server Settings → Roles, give the bot's role **Create Public Threads**, **Send Messages in Threads** and **Manage Threads**.

## 2. Server setup and IDs

1. In Discord, turn on **User Settings → Advanced → Developer Mode**.
2. Right-click a channel, role, category or the server icon and choose **Copy ID**.
3. Create what you need and put the IDs in your environment variables. Only `DISCORD_TOKEN` is required; features whose IDs are missing switch off or fall back to the current channel.

| Create | Variable | Used for |
|---|---|---|
| Server (right-click its icon) | `GUILD_ID` | Instant slash-command sync |
| `#welcome` | `WELCOME_CHANNEL_ID` | Welcome messages |
| `#start-here` | `START_HERE_CHANNEL_ID` | Rules panel + role menus |
| `#lfg` | `LFG_CHANNEL_ID` | `/lfg` posts |
| `#events` | `EVENT_CHANNEL_ID` | `/event` posts |
| `#staff-logs` (private) | `LOG_CHANNEL_ID` | Mod actions and tickets (or split with `MOD_LOG_CHANNEL_ID` / `TICKET_LOG_CHANNEL_ID`) |
| `#prompt-of-the-day` (text or forum) | `PROMPT_CHANNEL_ID` | Daily prompt + Prompt King |
| `#digest` | `DIGEST_CHANNEL_ID` | Weekly digest |
| `#remix-friday` (text or forum) | `CHALLENGE_CHANNEL_ID` | Remix Friday |
| `TICKETS` category (hidden from @everyone) | `TICKET_CATEGORY_ID` | Ticket channels |
| `🔊 SQUAD ROOMS` category + `➕ Join to Create` voice channel in it | `VOICE_CATEGORY_ID`, `JOIN_TO_CREATE_CHANNEL_ID` | Temp voice channels |
| Staff role | `STAFF_ROLE_ID` | Staff-only commands and ticket access |
| Member role | `MEMBER_ROLE_ID` | Granted by the rules **I accept** button |
| LFG ping role | `LFG_ROLE_ID` | `/lfg ping_role:True` |
| Prompt King role | `PROMPT_KING_ROLE_ID` | Given for 24h |
| Remix Champion role | `REMIX_CHAMPION_ROLE_ID` | Given for 7 days |

**Ping roles:** `PROMPT_PING_ROLE_ID` and `CHALLENGE_PING_ROLE_ID` are optional. If you leave them empty, the bot pings the self-roles named **Prompt pings** and **Challenges**. Members opt into those in `/roles`, and the bot creates them automatically the first time someone picks them.

**Rules gate:** to make new members accept the rules before seeing the server, deny **View Channels** to @everyone on your main categories and allow it for the member role. Leave `#welcome` and `#start-here` visible to @everyone. Keep `MEMBER_ROLE_ON_JOIN=false`.

Every other setting is listed with a comment in [`.env.example`](.env.example).

### Editing content

| What | Where |
|---|---|
| Self-role menus (platforms, games, interests, pings) | `PLATFORM_ROLES`, `GAME_ROLES`, `INTEREST_ROLES`, `PING_ROLES` in `config.py`. Each entry is `(label, role name or role ID, emoji)`, max 25 per menu |
| Schedule times | `*_TIME` values in `config.py` |
| Prompts / Remix themes | `data/prompts.json`, `data/remix_themes.json`. Keep every `id` unique. Items are picked at random from the unused ones; the rotation resets once every item has been used |
| Digest feeds | `data/rss_feeds.json` (the newest 3 headlines per feed) |
| Rules text | `data/rules.md`. Run `/rulespanel` again after editing |

After changing anything in `config.py` or `data/`, push to GitHub. Railway redeploys automatically.

## 3. Deploy on Railway

1. Go to <https://railway.com>, choose **New Project → Deploy from GitHub repo** and pick `Dropsx1/gaming-discord-bot`. Authorize the Railway GitHub app if asked.
2. **Add a volume so data survives redeploys.** Without one, warnings, giveaways, RSVPs and schedules are wiped on every deploy.
   - Press `⌘K` / `Ctrl+K` (or right-click the project canvas), choose **New Volume**, and connect it to the bot's service.
   - Set the **mount path** to **`/data`**.
   - The bot stores its database at `/data/bot.db` automatically. You can also set `DB_PATH=/data/bot.db` yourself.
   - ⚠️ **Don't** mount the volume at `/app/data`. That would hide the bundled prompts, themes and rules.
3. Open the service's **Variables** tab. Click **Raw Editor** and paste your settings: at minimum `DISCORD_TOKEN` and `GUILD_ID`, plus the IDs from step 2.
4. Railway installs `requirements.txt` and starts `python main.py` (from `railway.json`, with `Procfile` as a backup). The bot doesn't need a public domain or port.
5. Check **Deployments → View Logs**. A healthy start looks like:
   ```
   [INFO] bot.db: Database ready at /data/bot.db
   [INFO] bot: Loaded cog: cogs.schedule
   [INFO] bot: Synced 24 slash commands to guild 123...
   [INFO] bot: Logged in as YourBot#1234
   [INFO] bot.schedule: Scheduler started (timezone America/New_York)
   ```
   If you see `No Railway volume attached`, go back to step 2.
6. **Restart:** use **Deployments → ⋮ → Restart**. Every push to `main` redeploys, and the restart policy brings the bot back if it crashes.

### Run locally (optional)

```bash
git clone https://github.com/Dropsx1/gaming-discord-bot.git
cd gaming-discord-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # fill in DISCORD_TOKEN, GUILD_ID, ...
python main.py          # database goes to ./data/bot.db
```

## 4. First-time setup in Discord (staff)

```
/rulespanel        in #start-here: rules + "I accept" button
/roles             in #start-here: self-role menus
/ticketpanel       in #support: "Open Ticket" button
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

`/idea` and `/prompt` use the built-in lists unless you set `XAI_API_KEY` (from <https://console.x.ai>) and `XAI_MODEL`. If the key is set but the model isn't, the bot logs the model names your key can use at startup. Scheduled posts never use AI; they always come from `data/`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `DISCORD_TOKEN is not set` | Add it under Railway → Variables |
| `PrivilegedIntentsRequired` | Enable the Server Members and Message Content intents in the Developer Portal |
| `No Railway volume attached` in logs | Add a volume mounted at `/data` (Deploy step 2) |
| `data/prompts.json is missing` | The volume is mounted at `/app/data`; change the mount path to `/data` |
| Slash commands don't appear | Set `GUILD_ID`, invite with the `applications.commands` scope, restart Discord (Ctrl+R) |
| "My role needs to be above these roles" | Move the bot's role higher in Server Settings → Roles |
| Posted, but no thread / "couldn't create a thread" | Give the bot Create Public Threads + Send Messages in Threads in that channel |
| Role ping shows but doesn't notify | Make the role mentionable, or keep the bot's Mention Everyone permission |
| Prompt or Remix didn't post | Check that the channel ID is set and look for `Job prompt:` / `Job remix_kickoff:` in the logs. Try `/schedule-test` |
| Digest says a feed couldn't be loaded | That site was down or changed its feed URL; edit `data/rss_feeds.json` |
| New members can't see anything after accepting | Allow View Channels for the member role on your categories, and check `MEMBER_ROLE_ID` |
| Duplicate slash commands | Commands were once synced globally too. Keep `GUILD_ID` set; the global copies expire, or re-invite the bot |
| `PyNaCl is not installed` warning | Safe to ignore. The bot doesn't play audio |

## License

MIT.
