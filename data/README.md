# Universal Play Haven bot content

Note for Claude Code: place these files in the repo's `data/` folder.

- `prompts.json`: 120 Prompt of the Day items ({id, theme, text}). The bot picks a random item not yet used, records the used id in the DB table `prompt_log`, and resets the log once every item has been used.
- `remix_themes.json`: 52 Remix Friday themes ({id, title, description}). Same random-unused selection and reset logic (use `prompt_log` with a type column, or a matching table). AI-made, hand-drawn, and digital entries are all allowed.
- `rss_feeds.json`: feed URLs for the gaming, ai, and tech categories. The digest posts headlines and links pulled straight from these feeds. It does not use AI summaries.
