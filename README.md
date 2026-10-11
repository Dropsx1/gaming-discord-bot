# 🎮 Gaming Discord Bot

A production-ready Discord bot for an SFW gaming community, built with **Python 3.11+** and **discord.py 2.x**, ready to deploy on **Railway**.

Every command works as a **slash command** (`/lfg`) and as a **prefix command** (`!lfg`).

## Features

| Feature | Commands |
|---|---|
| **Welcome**: greets new members and points them to #start-here, roles and LFG (optional auto member role) | automatic, `/welcometest` |
| **LFG**: clean embed with game, region and platform (autocomplete), optional rank and notes, optional LFG role ping | `/lfg` |
| **Events**: announcement embed with RSVP reactions and an optional role ping | `/event` |
| **Self-roles**: persistent select menus for platforms and main games | `/roles` |
| **Fun**: random game suggestions and conversation starters | `/suggest` |
| **AI placeholder**: game challenges and prompts, with a ready-made Grok/xAI hook | `/idea`, `/prompt` |
| **Moderation**: warn, timeout (`10m`, `2h`, `1d`), untimeout and kick, with role-hierarchy checks, DMs and staff logging | `/warn`, `/timeout`, `/untimeout`, `/kick` |
| **Auto voice**: Join-to-Create temporary channels with owner controls, deleted when empty | `/vc ...` |
| **Tickets**: button panel, private channels, claim, close with optional transcript, add/remove user | `/ticketpanel`, `/ticket`, `/close`, `/claim`, `/adduser`, `/removeuser` |
| **Utility** | `/help`, `/ping`, `!sync` (bot owner) |

**Survives restarts:** the role menus and ticket buttons are persistent views. Ticket state is kept in the channel topic and temp-VC ownership in channel permissions, so you don't need a database.

## Project structure

```
gaming-discord-bot/
├── main.py              # Bot startup, cog loading, slash sync, error handling
├── config.py            # Env vars + EDITABLE role/game lists
├── cogs/
│   ├── general.py       # /help, /ping, !sync
│   ├── welcome.py       # on_member_join
│   ├── lfg.py           # /lfg
│   ├── events.py        # /event
│   ├── roles.py         # /roles self-role menus
│   ├── moderation.py    # /warn /timeout /untimeout /kick
│   ├── fun.py           # /suggest /idea /prompt
│   ├── voice.py         # Join-to-Create + /vc
│   └── tickets.py       # Ticket system
├── utils/
│   ├── helpers.py       # Staff checks, log channel, autocomplete
│   └── ai.py            # Grok/xAI placeholder
├── requirements.txt
├── Procfile             # worker: python main.py
├── railway.json         # Start command + restart policy
├── .python-version      # Python version for Railway
└── .env.example
```

---

## 1. Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications>, click **New Application** and name it.
2. **Bot** tab:
   - Click **Reset Token** and copy the token. This is your `DISCORD_TOKEN`. Never share or commit it.
   - Under **Privileged Gateway Intents**, enable:
     - ✅ **Server Members Intent** (welcome messages, member lookups)
     - ✅ **Message Content Intent** (`!` prefix commands)
   - Voice States is a standard intent, so you don't need a toggle for it. The bot enables it in code.
3. **OAuth2 → URL Generator**:
   - Scopes: ✅ `bot` ✅ `applications.commands`
   - Bot permissions:
     - Manage Roles, Manage Channels, Kick Members, Moderate Members
     - View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions
     - Mention Everyone (only needed to ping roles that aren't "mentionable")
     - Connect, Move Members
     - Use Application Commands

     Or use this ready-made URL (replace `YOUR_CLIENT_ID` with the Application ID from **General Information**):
     ```
     https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=1101945621586&scope=bot+applications.commands
     ```
4. Open the URL and invite the bot to your server.
5. **Important:** in **Server Settings → Roles**, drag the bot's role **above** every role it should assign (self-roles, member role) and above the members it should moderate.

## 2. Server setup and getting IDs

1. In Discord, go to **User Settings → Advanced** and turn on **Developer Mode**.
2. Right-click a channel, role, category or the server icon and choose **Copy ID**.
3. Create these, then copy their IDs into your environment variables:

| What to create | Variable |
|---|---|
| Server (right-click server icon) | `GUILD_ID` |
| `#welcome` text channel | `WELCOME_CHANNEL_ID` |
| `#start-here` text channel | `START_HERE_CHANNEL_ID` |
| `#lfg` text channel | `LFG_CHANNEL_ID` |
| `#events` text channel | `EVENT_CHANNEL_ID` |
| `#staff-logs` (private to staff) | `LOG_CHANNEL_ID` |
| Staff role | `STAFF_ROLE_ID` |
| LFG ping role (e.g. `@LFG`) | `LFG_ROLE_ID` |
| Ticket category (e.g. `TICKETS`, hidden from @everyone) | `TICKET_CATEGORY_ID` |
| Voice category for temp channels (e.g. `🔊 SQUAD ROOMS`) | `VOICE_CATEGORY_ID` |
| `➕ Join to Create` voice channel (inside that category) | `JOIN_TO_CREATE_CHANNEL_ID` |

Every variable except `DISCORD_TOKEN` is optional. A feature whose IDs are missing either turns itself off or falls back to the current channel. See [`.env.example`](.env.example) for the full list, including `MOD_LOG_CHANNEL_ID`, `TICKET_LOG_CHANNEL_ID`, `MEMBER_ROLE_ID`, `VC_NAME_TEMPLATE`, `VC_DEFAULT_LIMIT`, `TICKET_DM_TRANSCRIPT` and others.

> 💡 Give the temp-voice category to the bot alone. On startup the bot deletes empty temp channels in that category, and it only touches channels it created (identified by the owner's permission overwrite).

### Editing self-roles and game lists

Open `config.py` and edit `PLATFORM_ROLES` / `GAME_ROLES`:

```python
GAME_ROLES = [
    # (label in menu,  role name OR role ID,  emoji or None)
    ("Valorant",       "Valorant",            None),
    ("Minecraft",      123456789012345678,    "⛏️"),
]
```

- Roles are matched by exact name or by ID. With `SELF_ROLES_AUTO_CREATE=true` (the default), roles that don't exist yet are created the first time someone picks them.
- A menu can hold at most 25 options.
- After editing, redeploy and run `/roles` again to post a fresh menu. Old menus keep working, but they show the old options.
- `LFG_GAMES`, `LFG_REGIONS` and `LFG_PLATFORMS` control the `/lfg` autocomplete. Users can still type anything.

## 3. Run locally

```bash
git clone https://github.com/<you>/gaming-discord-bot.git
cd gaming-discord-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # then fill in DISCORD_TOKEN, GUILD_ID, etc.
python main.py
```

## 4. Deploy on Railway

1. Push this repo to GitHub.
2. Go to <https://railway.com>, then **New Project → Deploy from GitHub repo** and pick `gaming-discord-bot`. Authorize the Railway GitHub app if it asks.
3. Open the service, go to the **Variables** tab and add your variables. Paste them one at a time, or use **Raw Editor** and paste the contents of your `.env`. At minimum, set:
   - `DISCORD_TOKEN`
   - `GUILD_ID`
   - plus the channel, role and category IDs from step 2.
4. Railway detects Python from `requirements.txt` and `.python-version`, then runs `python main.py` (set in `railway.json`, with `Procfile` as a fallback). The bot needs no public port or domain, so don't generate one.
5. Saving variables triggers a deploy. Open **Deployments → View Logs**. You should see:
   ```
   [INFO] bot: Loaded cog: cogs.welcome
   ...
   [INFO] bot: Synced 20 slash commands to guild 123...
   [INFO] bot: Logged in as YourBot#1234
   ```
6. **Restart:** use **Deployments → ⋮ → Restart** (or **Redeploy**). Every push to `main` deploys automatically, and the restart policy brings the bot back if it crashes.

> If slash commands don't show up, make sure `GUILD_ID` is set and the bot was invited with the `applications.commands` scope. Then restart Discord (Ctrl+R). Without `GUILD_ID`, global commands can take up to an hour to appear.

## 5. Usage examples

### First-time setup (staff)
```
/roles          → posts the self-role menu in the current channel (put it in #start-here)
/ticketpanel    → posts the "Open Ticket" button panel (put it in #support)
/welcometest    → previews the welcome message
```

### LFG and events
```
/lfg game:Valorant region:NA platform:PC rank:Gold 2 notes:Need 2 for ranked ping_role:True
!lfg "Rocket League" EU Crossplay "Diamond" "chill 2s" yes

/event title:Among Us Night time:Saturday 8PM EST description:Bring your sus energy ping_role:@Events
!event "Among Us Night" "Sat 8PM EST" "Bring your sus energy" @Events
```

### Fun
```
/suggest type:game      /suggest type:convo      !suggest convo
/idea theme:Valorant    /prompt theme:retro      !idea competitive
```

### Moderation (needs Moderate Members / Kick Members)
```
/warn member:@User reason:Spamming
/timeout member:@User duration:30m reason:Cool off       (also: 30, 2h, 1d, 1h30m; max 28d)
/untimeout member:@User
/kick member:@User reason:Repeated rule breaks
```
Each action DMs the member, if their DMs are open, and is logged to the staff log channel.

### Auto voice channels
1. Join **➕ Join to Create** and you're moved into a new channel such as `🎮 YourName's Room`.
2. Control it with:
```
/vc info                  show owner / limit / lock status
/vc rename name:Ranked Grind     (Discord allows 2 renames per 10 minutes)
/vc limit limit:5         (0 = unlimited)
/vc lock / /vc unlock
/vc permit member:@Friend     let someone in while locked
/vc kick member:@User         remove someone and block rejoining
/vc transfer member:@User     hand over ownership
/vc claim                 take over if the owner left
```
The channel is deleted as soon as it's empty.

### Tickets
- Members click **🎫 Open Ticket** or use `/ticket` (`!ticket` also works). The bot creates a private `#ticket-name` channel that only the member, staff and the bot can see.
- In the ticket:
  - **🔒 Close** (owner or staff) asks for confirmation: close with a transcript, close without, or cancel. The bot posts "Deleting in 5 seconds…", sends the log and transcript to the ticket log channel, waits 5 seconds, then deletes the channel.
  - **✋ Claim** (staff) marks the ticket as yours.
  - `/close transcript:False`, `/claim`, `/adduser member:@User` and `/removeuser member:@User` (staff) do the same from the command line.
- Each member can have one open ticket at a time.

## Adding Grok / xAI later

`/idea` and `/prompt` go through `utils/ai.py`. That file contains a commented, ready-to-paste xAI API call. Set `XAI_API_KEY` (and `XAI_MODEL`) in Railway, fill in `generate()`, and redeploy. If the API fails, the commands fall back to the built-in lists, so they never break.

## Troubleshooting

| Problem | Fix |
|---|---|
| `DISCORD_TOKEN is not set` | Add the variable in Railway → Variables |
| `PrivilegedIntentsRequired` | Enable the Server Members and Message Content intents in the Developer Portal |
| "My role needs to be above these roles" | Move the bot's role higher in Server Settings → Roles |
| Role ping shows but doesn't notify | Make the role mentionable, or grant the bot **Mention Everyone** |
| Temp VC not created | Check `JOIN_TO_CREATE_CHANNEL_ID`, and that the bot has Manage Channels + Move Members in the category |
| Tickets say "not configured" | Set `TICKET_CATEGORY_ID` to a **category** ID, not a channel ID |
| Duplicate slash commands | You synced both globally and to a guild at some point. Kick and re-invite the bot, or leave `GUILD_ID` set and wait for the old global ones to expire |
| `PyNaCl is not installed` warning | Safe to ignore. The bot doesn't play audio |

## License

MIT. Use it, remix it, GG.
