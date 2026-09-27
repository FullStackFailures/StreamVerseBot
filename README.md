# StreamVerseOG — Telegram Media Indexer Bot

A Python-based Telegram media indexing, deep-link delivery, and backup-publishing bot.

## What This Project Does

The application watches a configured Telegram supergroup and automatically turns admin-posted media into organized searchable index posts.

Core capabilities in the supplied codebase:

- Admin-only automatic media indexing in a Telegram forum/supergroup.
- Intelligent title normalization and quality extraction.
- Quality buttons sorted from higher to lower quality (for example 4K → 1080p → 720p).
- Split-archive detection for patterns such as `.zip.001`, `.rar.003`, and `.part01.rar`.
- Debounced batching so multiple related posts can become one index entry.
- MongoDB Atlas persistence for download records, bundles, movie/post records, and publish records.
- Admin private-message upload flow that creates a Telegram deep link and optionally a GPLinks short URL.
- `/makepost` guided wizard for movie/series posts, posters, languages, qualities, seasons, and file bundles.
- Recursive delivery of bundles from Telegram file IDs stored in MongoDB.
- Automatic deletion of delivered file messages after a configurable delay.
- Member welcome messages with a link into the configured forum topic.
- Closed forum-topic support: the bot can temporarily reopen a topic, post, and close it again when it has `Manage Topics` rights.
- Backup publishing through the `🚀 Publish` button.
- Bulk disaster-recovery publishing through `/republishall`.
- Up to eight optional private MiddleMan mirror groups for disaster recovery.
- Flask health endpoints for hosting/monitoring.
- Token-redacted logging to avoid exposing the Telegram bot token in logs.

## Project Architecture

```text
Telegram Supergroup
        │
        │ admin media/text posts
        ▼
handle_message()
        │
        │ debounce + grouping + deduplication
        ▼
process_batch()
        │
        ├── title/quality/archive detection
        ├── build inline download buttons
        ├── save movie metadata → MongoDB `movies`
        ├── post searchable entry → ALL_ADDED_SHOWS
        └── optionally mirror → dead MiddleMan clones

Admin private chat
        │
        │ upload / forward media
        ▼
handle_private_upload()
        │
        ├── /makepost wizard
        │       ├── poster
        │       ├── title
        │       ├── languages
        │       ├── qualities
        │       ├── seasons (series)
        │       ├── files
        │       └── publish to group
        │
        └── quick-link mode
                ├── generate token
                ├── Telegram deep link
                ├── optional GPLinks short URL
                └── save → MongoDB `downloads` / `bundles`

Member opens deep link
        │
        ▼
/start <token> / /send <token>
        │
        ▼
MongoDB lookup
        │
        ▼
Telegram sends file/bundle
        │
        └── delivered file messages auto-delete later

Admin presses 🚀 Publish
        │
        ▼
MongoDB `movies`
        │
        ▼
Recreate post in Backup group
        │
        ├── optional Backup Search index
        └── save latest publish location → `publish_records`
```

## Repository Contents

```text
SV.File.Post.Bot.In.Group/
├── main.py                 # Entire application: bot, indexing, MongoDB, wizard, backup, Flask health server
├── welcome_templates.py    # Welcome + file-delivery warning HTML templates
├── requirements.txt        # Pinned Python dependencies
├── .python-version         # Python version used by the project: 3.13.5
├── .env                    # Local secrets/config; DO NOT publish this file
├── .gitignore
└── .git/                   # Git metadata included in the supplied ZIP
```

### Existing `requirements.txt`

The ZIP already contains the required dependency file, so **do not create a second `requirements.txt`**.

```text
python-telegram-bot==22.8
flask==3.0.2
python-dotenv==1.0.1
werkzeug==3.0.1
httpx==0.28.1
pymongo==4.10.1
dnspython==2.7.0
```

All other imports used by the application are from the Python standard library or the local `welcome_templates.py` module.

## Requirements

### Software

- Python **3.13.5** is the project's declared version.
- A Telegram bot created through BotFather.
- A Telegram supergroup configured as a forum/topic-enabled group for the indexing workflow.
- A second Backup supergroup for the publish/recovery workflow.
- MongoDB Atlas.
- Optional: GPLinks API credentials if you want short URLs instead of raw Telegram deep links.

## Windows Setup — PowerShell

### 1. Extract the project

Put the project somewhere convenient, for example:

```powershell
D:\Projects\SV.File.Post.Bot.In.Group
```

Then open PowerShell in that directory:

```powershell
cd D:\Projects\SV.File.Post.Bot.In.Group
```

### 2. Verify Python

```powershell
python --version
```

Recommended for this project:

```text
Python 3.13.5
```

If multiple Python installations exist, verify the launcher:

```powershell
py -0p
```

### 3. Create a virtual environment

```powershell
py -3.13 -m venv .venv
```

### 4. Activate the virtual environment

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks script activation, run once for your user account:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then activate again:

```powershell
.\.venv\Scripts\Activate.ps1
```

### 5. Upgrade pip

```powershell
python -m pip install --upgrade pip
```

### 6. Install project dependencies

```powershell
python -m pip install -r requirements.txt
```

You can also use:

```powershell
pip install -r requirements.txt
```

Using `python -m pip` is preferred because it guarantees pip belongs to the active Python environment.

## Linux / macOS Setup

```bash
cd /path/to/SV.File.Post.Bot.In.Group
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Environment Configuration

The application uses `python-dotenv` and loads values from `.env` at startup.

Create or edit:

```text
.env
```

Do **not** commit real secrets to GitHub.

### Minimum configuration

These are the values that the `Config` class requires or practically needs for the main workflow:

```dotenv
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN

# Primary MiddleMan / indexing supergroup
GROUP_CHAT_ID=-1001234567890

# Topic containing the searchable "ALL ADDED SHOWS" index
ALL_ADDED_SHOWS=3

# Topic where member-welcome messages are sent
SEARCH_SHOWS_HERE=1

# Telegram user IDs allowed to run admin operations
ADMIN_IDS=123456789,987654321

# Backup/public supergroup
BACKUP_GROUP_CHAT_ID=-1009876543210
BACKUP_ALL_ADDED_SHOWS=3

# MongoDB Atlas
MONGODB_URI=mongodb+srv://USERNAME:PASSWORD@CLUSTER.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB_NAME=StreamVerseOG
```

### Demo .env (Repo already contains these Environmental Vairables in .env file)

```
# ============================
# BOT TOKEN
# ============================
BOT_TOKEN=Your Bot Token Here

# ============================
# ADMIN TINGS
# ============================
ADMIN_IDS=195---masked----

# ============================
# IGNORED TOPICS
# ============================
IGNORED_TOPICS=1,1796,2837

# ============================
# CURRENT MAIN MIDDLE.MAN GROUP
# ============================
GROUP_CHAT_ID=-1003----masked----
ALL_ADDED_SHOWS=7
TOPIC_MAP=OTT SHOWS:11|MOVIES (ENGLISH):13|DRAMAS (SERIES):8|MOVIES (HINDI):5|SERIES (ENGLISH):10|MARVELS & DC (MOVIES):15|MARVELS & DC (SERIES):16|SERIES (HINDI):12|Spiderman - BND will be added here on 31st july:2|DRAMAS (MOVIES):9|ANIME (SERIES):14|ANIME (MOVIES):132|DRAGON BALL COLLECTION:3654

# ============================
# MIDDLE.MAN: MIRROR 1
# ============================
MIDDLEMAN_MIRROR_1_CHAT_ID=-10043----masked----
MIDDLEMAN_MIRROR_1_TOPIC_MAP=OTT SHOWS:13|MOVIES (ENGLISH):7|DRAMAS (SERIES):4|MOVIES (HINDI):15|SERIES (ENGLISH):8|MARVELS & DC (MOVIES):10|MARVELS & DC (SERIES):9|SERIES (HINDI):12|Spiderman - BND will be added here on 31st july:6|DRAMAS (MOVIES):5|ANIME (SERIES):11|ANIME (MOVIES):17|DRAGON BALL COLLECTION:3654
MIDDLEMAN_MIRROR_1_ALL_ADDED_SHOWS=3

# ============================
# DISPOSABLE BACKUP GROUP (MAIN SV.OG GROUP, AFTER THE MAIN GROUP GOOT RISKED)
# ============================
BACKUP_GROUP_CHAT_ID=-10043----masked----
BACKUP_ALL_ADDED_SHOWS=50
SEARCH_SHOWS_HERE=1
BACKUP_TOPIC_MAP=OTT SHOWS:34|MOVIES (ENGLISH):35|DRAMAS (SERIES):36|MOVIES (HINDI):37|SERIES (ENGLISH):38|MARVELS & DC (MOVIES):39|MARVELS & DC (SERIES):41|SERIES (HINDI):43|Spiderman - BND will be added here on 31st july:44|DRAMAS (MOVIES):45|ANIME (SERIES):47|ANIME (MOVIES):46|DRAGON BALL COLLECTION:3654
# ============================
# Optional But Important
# ============================
HOW_TO_LINK=https://t.me/c/3935135937/9/481
PORT=10000
DEBOUNCE_SEC=3
FLUSH_INTERVAL=1
SEEN_TTL_SEC=21600
MAX_BUTTONS=20
MAX_BUTTON_LEN=56


# ============================
# GPLinks Integration
# ============================
BOT_USERNAME=StreamVerseOG_Bot
GPLINKS_API_KEY=1a0bf106c----maksed-----d3e
GPLINKS_API_URL=https://api.gplinks.com/api

# ============================
# Database
# ============================
MONGODB_URI=mongodb+srv://Streamverseog:----masked----streamverseogclust----masked----ppName=StreamverseogCluster
MONGODB_DB_NAME=StreamVerseOG

# ===================================
# Groups and Social Media Links
# ===================================
GROUP_NAME=@StreamVerseOG
GROUP_JOIN_LINK=https://t.me/+EWKC3rFWgbozZjBl
GROUP_NAME_2=@StreamVerseOG.2

GROUP_JOIN_LINK_2=https://t.me/+gM8qYrBxoVs0NTA1
INSTAGRAM_LABEL=@StreamVerseOG

INSTAGRAM_LINK=https://instagram.com/streamverseog

DELETE_DELAY_SEC=120
```

### Visit https://t.me/+gM8qYrBxoVs0NTA1 to know about "SEARCH_SHOWS_HERE, ALL_ADDED_SHOWS etc" so you understand what they are.

### Full environment variable reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BOT_TOKEN` | **Yes** | — | Telegram Bot API token from BotFather. |
| `GROUP_CHAT_ID` | **Yes** | — | Primary MiddleMan supergroup ID. |
| `ALL_ADDED_SHOWS` | **Yes** | — | Primary searchable/index forum topic ID. |
| `SEARCH_SHOWS_HERE` | **Yes** | — | Topic used for the member welcome/deep-link destination. |
| `ADMIN_IDS` | **Yes** | — | Comma-separated Telegram user IDs authorized as admins. |
| `BACKUP_GROUP_CHAT_ID` | **Yes** | — | Backup supergroup ID. |
| `BACKUP_ALL_ADDED_SHOWS` | **Yes** | — | Backup Search/index topic ID. |
| `MONGODB_URI` | **Yes** | — | MongoDB Atlas connection string. |
| `MONGODB_DB_NAME` | No | `StreamVerseOG` | MongoDB database name. |
| `BOT_USERNAME` | No | empty | Bot username; if empty, the bot resolves it with `getMe()` at startup. |
| `GPLINKS_API_KEY` | No | empty | GPLinks API key. Empty means the normal Telegram deep link is used instead of a short link. |
| `GPLINKS_API_URL` | No | `https://api.gplinks.com/api` | GPLinks API endpoint. |
| `TOPIC_MAP` | No | empty | Named primary topics used by `/makepost`, in `Name:ID|Name:ID` form. |
| `BACKUP_TOPIC_MAP` | No | empty | Backup topic remapping by topic name. Recommended when Backup topic IDs differ from MiddleMan IDs. |
| `IGNORED_TOPICS` | No | empty | Comma-separated topic IDs that must never be indexed. |
| `HOW_TO_LINK` | No | code default | URL used by the ZIP/split-archive guide button. |
| `PORT` | No | `10000` | Flask health-server port. |
| `DEBOUNCE_SEC` | No | `3.0` | Time window used to combine related messages before indexing. |
| `FLUSH_INTERVAL` | No | `1.0` | Background flush-loop interval. |
| `SEEN_TTL_SEC` | No | `21600` | Seen-cache TTL in seconds (6 hours by default). |
| `MAX_BUTTONS` | No | `20` | Maximum index buttons per post, including the Publish control. |
| `MAX_BUTTON_LEN` | No | `56` | Maximum inline-button label length. |
| `GROUP_NAME` | No | `StreamVerseOG` | Primary group name shown in delivery notices. |
| `GROUP_JOIN_LINK` | No | empty | Primary group join URL shown in delivery notices. |
| `GROUP_NAME_2` | No | `StreamVerseOG Backup` | Backup group name shown in delivery notices. |
| `GROUP_JOIN_LINK_2` | No | empty | Backup group join URL shown in delivery notices. |
| `INSTAGRAM_LABEL` | No | `Instagram` | Instagram label in the delivery notice. |
| `INSTAGRAM_LINK` | No | empty | Instagram URL in the delivery notice. |
| `DELETE_DELAY_SEC` | No | `120` | Seconds before delivered file/bundle messages are auto-deleted. |

### Topic map format

`TOPIC_MAP` is parsed as:

```dotenv
TOPIC_MAP=Movies (Hindi):1234|Movies (English):1235|OTT Shows:1236
```

Topic names are normalized for matching, so differences in capitalization, punctuation, and spacing are tolerated when resolving a name.

### Ignored topics

Use comma-separated Telegram forum topic IDs:

```dotenv
IGNORED_TOPICS=1,1796,2837
```

Messages in those topics are ignored by the automatic indexer.

## Telegram Bot Setup

### 1. Create the bot

Open **@BotFather** in Telegram and create a bot.

Save the token as:

```dotenv
BOT_TOKEN=...
```

Never publish the token.

### 2. Add the bot to the Primary MiddleMan group

Add the bot to the primary supergroup and promote it to an administrator.

For the forum-topic workflow, the bot needs the ability to manage topics because the code can:

1. reopen a closed topic,
2. publish the index post,
3. close the topic again.

Make sure **Manage Topics** is enabled.

The bot also needs normal permission to send messages in the target topics.

### 3. Add the bot to the Backup group

Promote the bot to an administrator in the Backup group as well.

Give it permission to send messages and manage topics when the target Backup topic is closed.

### 4. Add the bot to mirror groups (optional)

Every configured mirror must also contain the bot and have the required posting/topic-management permissions.

### 5. Bot privacy mode

This application intentionally processes ordinary group messages from configured admins. Telegram states that bot admins receive all messages in groups, so adding the bot as an administrator is the important setup step for this design.

## Finding Telegram Chat IDs and Topic IDs

The configuration uses two different types of identifiers:

- **Chat ID**: the supergroup ID, usually a negative number such as `-100...`.
- **Topic ID**: the forum `message_thread_id` used to target a specific topic.

The application itself uses these IDs when sending messages with `message_thread_id=`.

When debugging indexing, the application also logs batch/topic information. Check the terminal logs after posting a test message.

Do not confuse a group chat ID with a forum topic ID.

## MongoDB Atlas Setup

MongoDB Atlas is required for the current code. The application fails startup if it cannot connect to MongoDB because persistent link storage is mandatory.

### 1. Create an Atlas project/cluster

Create a MongoDB Atlas cluster.

Official documentation:

- https://www.mongodb.com/docs/atlas/connect-to-database-deployment/
- https://www.mongodb.com/docs/atlas/driver-connection/

### 2. Create a Database User

Create a MongoDB database user with permissions to use the database.

Example:

```text
Username: streamversebot
Password: <strong-random-password>
```

### 3. Configure Atlas Network Access

Add the IP address of the machine that runs the bot to the Atlas IP Access List.

For temporary testing, some setups use `0.0.0.0/0`, but this exposes the cluster to all source IPs and should not be the preferred production configuration.

### 4. Get the connection string

In Atlas, choose **Connect → Drivers**, copy the generated URI, and place it into `.env`:

```dotenv
MONGODB_URI=mongodb+srv://streamversebot:YOUR_PASSWORD@YOUR_CLUSTER.mongodb.net/?retryWrites=true&w=majority
```

Set:

```dotenv
MONGODB_DB_NAME=StreamVerseOG
```

If a MongoDB username, password, database name, or other URI component contains reserved URL characters, URL-encode those characters before putting them into the connection string.

### 5. Collections created automatically

You do **not** have to manually create the collections.

On startup, the code selects the configured database and uses:

```text
downloads
bundles
movies
publish_records
```

Indexes created automatically:

```text
downloads.token         UNIQUE
bundles.token           UNIQUE
publish_records.movie_id UNIQUE
```

### What MongoDB stores

The bot does not store the raw media file bytes in MongoDB.

Examples of stored information include:

- Telegram `file_id` values.
- File type/name/caption metadata.
- Deep-link tokens.
- GPLinks URLs.
- Bundle child-token arrays.
- Movie/post text and buttons.
- Telegram source-message identifiers.
- Backup publish locations and timestamps.

This keeps the database focused on metadata and references rather than acting as the media storage layer.

## Start the Application

After Python dependencies and `.env` are configured:

```powershell
python main.py
```

Linux/macOS:

```bash
python3 main.py
```

### Successful startup indicators

A healthy startup should show log messages indicating:

- MongoDB connected.
- Bot username resolved or loaded.
- Admin commands configured.
- Flask health server started.
- Bot polling started.

The application uses Telegram **polling**, not webhooks.

## Flask Health Endpoints

The Python process starts a Flask server in a daemon thread.

### Root health endpoint

```text
GET /
```

Returns JSON similar to:

```json
{
  "status": "ok",
  "indexed": 0,
  "zip_hints": 0,
  "skipped_dupes": 0,
  "errors": 0,
  "uptime_sec": 0,
  "welcomes_sent": 0,
  "auto_deleted": 0,
  "backup_indexed": 0
}
```

### Ping endpoint

```text
GET /ping
```

Returns:

```text
pong
```

### Local test

With the bot running:

```powershell
curl http://127.0.0.1:10000/ping
```

or:

```powershell
curl http://127.0.0.1:10000/
```

The actual port comes from `PORT`.

## First-Run Smoke Test

Use this order to verify the whole system.

### Test 1 — Startup

Run:

```powershell
python main.py
```

Confirm the logs show successful MongoDB connection.

### Test 2 — Admin authentication

As a configured admin, open the bot in a private chat and run:

```text
/status
```

The bot should return runtime statistics.

### Test 3 — Automatic indexing

1. Go to the configured primary MiddleMan supergroup.
2. Post a test media message as a configured admin.
3. Include a recognizable title such as a movie or series name.
4. Wait roughly `DEBOUNCE_SEC` seconds.
5. Check `ALL_ADDED_SHOWS`.

The bot should create one formatted index post with quality/download buttons.

### Test 4 — Duplicate prevention

Post the same title again.

The in-memory seen cache should prevent a second index post while the key is still cached.

To clear the cache as an admin:

```text
/clearcache
```

### Test 5 — Standalone quick-link upload

In the bot's private chat, as an admin, send/forward a supported file.

The bot should generate:

- a token,
- a Telegram deep link,
- a GPLinks short URL when `GPLINKS_API_KEY` is configured,
- a MongoDB `downloads` record.

The reply tells you to place the generated link into a group post.

### Test 6 — Redemption

Open the generated Telegram deep link from another Telegram account and verify that the bot delivers the file.

The bot will also send the delivery warning and schedule deletion of the delivered file/bundle messages.

### Test 7 — Auto-delete test

Run as an admin:

```text
/testdeletemessage
```

This sends a controlled demo message in the current chat and schedules its deletion using `DELETE_DELAY_SEC`.

### Test 8 — `/makepost`

Open a private chat with the bot as an admin and run:

```text
/makepost
```

Follow the inline-button wizard.

### Test 9 — Backup Publish

After a movie index post has a:

```text
🚀 Publish
```

button, press it as an authorized admin.

The bot should recreate the stored post in the configured Backup group and create the corresponding Backup Search entry when needed.

## `/makepost` Wizard Flow

The wizard is private/admin-only.

The exact state flow can vary depending on whether you choose Movie or Series, but the current implementation supports:

1. Movie/Series selection.
2. Optional poster upload.
3. Title entry.
4. Language entry.
5. Quality selection.
6. Multiple seasons for series posts.
7. File uploads per quality/season.
8. Explicit `Upload Complete` handling for series file groups.
9. Topic selection using `TOPIC_MAP`.
10. Preview.
11. Editing.
12. Final post.
13. Automatic Search-index cross-post into `ALL_ADDED_SHOWS`.
14. Storage in MongoDB for later Backup publishing.

### Series behavior

Series posts can collect multiple seasons such as:

```text
S01, S02, S03
```

Files are grouped according to the current wizard state, not inferred from the filename.

### Poster behavior

The code expects a Telegram **photo** for the poster step, not an arbitrary document containing an image.

## Automatic Media Indexing

The main group handler only reacts to messages that satisfy all of these conditions:

- message is in `GROUP_CHAT_ID`;
- sender is a configured admin;
- sender is not a bot;
- message is not already in `ALL_ADDED_SHOWS`;
- message is not inside an `IGNORED_TOPICS` topic.

### Debouncing and batching

Messages are grouped in memory using `DEBOUNCE_SEC` and processed by the background flush loop.

Default:

```dotenv
DEBOUNCE_SEC=3.0
FLUSH_INTERVAL=1.0
```

This allows multiple quality variants or related split-archive parts to be merged into one logical index entry.

### Supported media/file patterns

The current code recognizes common Telegram file/media types and has explicit split-archive detection for examples such as:

```text
Movie.zip.001
Movie.zip.002
Movie.rar.001
Movie.part01.rar
Movie.part002.zip
```

The general media filename checks also cover common extensions such as:

```text
.mkv
.mp4
.avi
.mov
.wmv
.flv
.m4v
.ts
.zip
.rar
.7z
.tar
.gz
.001
.002
...
```

### Quality detection

The title/quality parser recognizes many common tags, including:

```text
4K / 2160p
1080p
720p
480p
360p
UHD / FHD / HD / SD
BluRay / WEB-DL / WEBRip / HDRip / HDTV
x264 / x265 / H.264 / H.265 / HEVC
HDR / 10bit / Dolby / Atmos
Hindi / English / Tamil / Telugu / Kannada / Malayalam / Bengali / Punjabi / Marathi / Gujarati / Urdu
dubbed / dual audio / multi audio
```

These tags are used both for display and for quality-button ordering.

## Supported Private File Types

The private upload handler can work with the following Telegram payload types:

```text
document
video
audio
animation
voice
video_note
photo
```

## Deep Links and GPLinks

When an admin uploads a file privately, the bot creates a random token using `secrets.token_hex(8)`.

The token becomes a Telegram deep link similar to:

```text
https://t.me/YOUR_BOT?start=TOKEN
```

If `GPLINKS_API_KEY` is configured, the bot attempts to convert that URL into a GPLinks short URL.

If GPLinks fails or is not configured, the bot falls back to the original Telegram deep link.

## Bundle Storage

Bundles are stored in MongoDB as documents containing `child_tokens`.

The child tokens can point to other download records or nested bundles.

The delivery function recursively resolves these records, while maintaining a visited-token set to avoid recursive loops.

## Automatic Delivery Deletion

After a member redeems a file or bundle with `/start` or `/send`, the bot:

1. sends the file(s),
2. sends a warning message,
3. schedules the delivered file/bundle messages for deletion after `DELETE_DELAY_SEC` seconds.

The warning message itself is intentionally kept; delivered media messages are the ones scheduled for deletion.

Default:

```dotenv
DELETE_DELAY_SEC=120
```

Telegram's Bot API currently limits message deletion to messages sent less than 48 hours ago, while outgoing bot messages can be deleted by the bot in private chats, groups, and supergroups. Keep the configured delay reasonable and within the platform's deletion rules.

## Backup Publishing

The application has a built-in disaster-recovery publishing system.

### Single-post publish

Each stored movie/index post can receive:

```text
🚀 Publish
```

When an admin presses it, the bot:

1. loads the movie document from MongoDB;
2. recreates the post in the current Backup group;
3. recreates the poster when one is stored;
4. recreates the inline buttons;
5. creates a Backup Search index for named-topic `/makepost` posts;
6. stores the latest Backup message location in `publish_records`;
7. changes the original button label to `✅ Published` while preserving the callback so it can be pressed again later.

### Why the movie is stored in MongoDB

The publish workflow is intentionally based on the MongoDB `movies` document instead of forwarding the original Telegram message.

That allows the bot to recreate the content even when you need to move it into a replacement Backup group.

## `/republishall`

Run as an admin:

```text
/republishall
```

The command:

- reads every movie document from MongoDB;
- republishes them into the **current** Backup configuration;
- uses `BACKUP_TOPIC_MAP` for named-topic remapping;
- sends auto-indexed movies directly to `BACKUP_ALL_ADDED_SHOWS`;
- runs as a background task;
- reports progress every 25 posts;
- waits between posts to reduce flood pressure;
- continues after individual failures;
- returns a final success/failure summary.

Only one `/republishall` operation can run at a time.

## Backup Topic Remapping

A new Telegram Backup group normally has its own topic IDs.

If MiddleMan and Backup topic IDs are identical, `BACKUP_TOPIC_MAP` may be unnecessary.

For a fresh Backup group, it is safer to map by topic name:

```dotenv
TOPIC_MAP=Movies (Hindi):1234|Movies (English):1235|OTT Shows:1236

BACKUP_TOPIC_MAP=Movies (Hindi):21|Movies (English):22|OTT Shows:23
```

The code matches topic names using case/punctuation/spacing normalization.

## Dead MiddleMan Mirrors

The project supports up to **8** optional private mirror groups.

For example:

```dotenv
MIDDLEMAN_MIRROR_1_CHAT_ID=-1001111111111
MIDDLEMAN_MIRROR_1_TOPIC_MAP=Movies (Hindi):11|Movies (English):13|OTT Shows:15
MIDDLEMAN_MIRROR_1_ALL_ADDED_SHOWS=7

MIDDLEMAN_MIRROR_2_CHAT_ID=-1002222222222
MIDDLEMAN_MIRROR_2_TOPIC_MAP=Movies (Hindi):21|Movies (English):23|OTT Shows:25
MIDDLEMAN_MIRROR_2_ALL_ADDED_SHOWS=17
```

Numbering can contain gaps.

Each mirror is a private disaster-recovery copy. The bot recreates the post rather than forwarding it, while preserving the same MongoDB movie ID in the `🚀 Publish` callback.

This lets an admin publish to the currently configured Backup group from a mirror if the primary MiddleMan becomes unavailable.

## Admin Commands

All admin commands are protected by the configured `ADMIN_IDS` list.

| Command | Purpose |
|---|---|
| `/start <token>` | Redeem a stored download/bundle token. |
| `/send <token>` | Alternate redemption command. |
| `/makepost` | Start the guided movie/series post wizard (private chat). |
| `/status` | Runtime statistics and cache information. |
| `/flush` | Immediately process all currently pending indexing batches. |
| `/clearcache` | Clear the in-memory seen/ZIP caches and pending message-group map. |
| `/reindex <title>` | Remove a title from the in-memory seen cache so it can be indexed again. |
| `/testdeletemessage` | Test the delivery warning and auto-delete mechanism. |
| `/republishall` | Bulk-publish all stored movies into the current Backup group. |
| `/help` | Display bot help/features for admins. |

### `/reindex` limitation

`/reindex` only affects the in-memory seen cache.

It does **not** delete the existing MongoDB `movies` document or historical Backup publish records.

## Health Monitoring / Hosting

The process is suitable for a hosting environment that can keep one long-running Python process alive and expose an HTTP port.

The startup model is:

```text
Main Python process
├── Telegram polling loop
└── Flask health server thread
```

The application reads the hosting platform's port through:

```dotenv
PORT=10000
```

A typical process/start command is:

```bash
python main.py
```

A hosting platform can use `/ping` or `/` as a health-check endpoint.

## Persistence and Restart Behavior

### Persisted in MongoDB

The following survive application restarts because they are stored in MongoDB:

- download records;
- bundle records;
- movie/post records;
- publish records.

### In memory only

The following are **not** persistent across a process restart:

- pending batches;
- seen-title cache;
- ZIP cache;
- media-group map;
- active `/makepost` drafts;
- current `/republishall` running flag;
- runtime counters/statistics.

This means a restart resets temporary state, but it does not wipe the MongoDB-backed catalog.

## Logging

Logs are written to stdout/stderr using Python's logging system.

The code contains an explicit token-redaction filter designed to prevent the Telegram bot token from appearing in logs.

Typical useful log markers include:

```text
[MONGO]
[INDEX]
[ZIP-INDEX]
[DUPE]
[WELCOME]
[LINK-GEN]
[REDEEM]
[DEPLOY]
[REPUBLISH-ALL]
[MIRROR]
[AUTO-DELETE]
```

## Security Checklist

Before putting the project on GitHub or a public server:

- Never commit `.env`.
- Never paste `BOT_TOKEN` into public issues or screenshots.
- Never publish `MONGODB_URI` credentials.
- Never publish `GPLINKS_API_KEY`.
- Use a dedicated MongoDB database user for the bot instead of reusing a personal Atlas credential.
- Restrict MongoDB Atlas Network Access to the application's real source IPs whenever practical.
- Keep the Backup group private if it is intended only for recovery.
- Keep the MiddleMan mirror groups private.
- Review Telegram administrator permissions and only grant what the bot actually needs.
- Rotate credentials immediately if they are accidentally exposed.

## Troubleshooting

### `RuntimeError: Required env var missing: ...`

A required `.env` value is absent.

Check:

```text
BOT_TOKEN
GROUP_CHAT_ID
ALL_ADDED_SHOWS
SEARCH_SHOWS_HERE
ADMIN_IDS
BACKUP_GROUP_CHAT_ID
BACKUP_ALL_ADDED_SHOWS
MONGODB_URI
```

### MongoDB connection fails at startup

Check all of the following:

1. `MONGODB_URI` is correct.
2. The Atlas database user exists and has access.
3. The password is correct and URL-encoded if it contains reserved characters.
4. The application's current IP is in the Atlas IP Access List.
5. Outbound network access is available.
6. The Atlas cluster is running.

The application deliberately fails startup when MongoDB is unavailable.

### Bot starts but does not index admin posts

Check:

- bot is inside the exact `GROUP_CHAT_ID`;
- bot is an administrator;
- posting account's Telegram user ID is in `ADMIN_IDS`;
- the content is not being posted inside `ALL_ADDED_SHOWS`;
- the topic is not in `IGNORED_TOPICS`;
- the message is actually in the forum/supergroup expected by the code;
- wait for `DEBOUNCE_SEC` or run `/flush` as an admin.

### Bot cannot post to a closed topic

Give the bot administrator rights with **Manage Topics** enabled in that supergroup.

### `/makepost` topic buttons are missing

Configure `TOPIC_MAP`:

```dotenv
TOPIC_MAP=Movies (Hindi):1234|Movies (English):1235|OTT Shows:1236
```

The `ALL_ADDED_SHOWS` button is separately added by the code.

### Backup Publish lands in the wrong topic

For a new Backup group, configure `BACKUP_TOPIC_MAP` using the **Backup group's own topic IDs**:

```dotenv
BACKUP_TOPIC_MAP=Movies (Hindi):21|Movies (English):22|OTT Shows:23
```

### GPLinks short URLs do not work

Check:

```dotenv
GPLINKS_API_KEY=...
GPLINKS_API_URL=https://api.gplinks.com/api
```

The code automatically falls back to the original Telegram deep link if shortening fails.

### Delivered files do not auto-delete

Run:

```text
/testdeletemessage
```

Then inspect logs.

Also verify that `DELETE_DELAY_SEC` is reasonable and that the message has not already been deleted/changed by the user or Telegram.

### `npm install` says `package.json` is missing

That is expected for this project. This is a Python bot.

Use:

```powershell
python -m pip install -r requirements.txt
python main.py
```

not:

```powershell
npm install
```

## Validation Performed on the Supplied ZIP

The supplied archive was inspected directly.

Verified facts from the archive:

- Project root contains `main.py`, `welcome_templates.py`, `requirements.txt`, `.env`, `.python-version`, and `.gitignore`.
- No `package.json` exists.
- No Expo configuration exists.
- No Android/Gradle project exists.
- No iOS/Xcode project exists.
- No `eas.json` exists.
- Python syntax compilation passed for `main.py` and `welcome_templates.py`.
- The declared Python version is `3.13.5`.
- MongoDB Atlas is initialized on application startup.
- The code creates the required MongoDB indexes automatically.

## Expo Go / APK / Android Build — Not Applicable to This ZIP

There is **no mobile application in this archive**.

Therefore, these commands are intentionally **not** part of the actual setup:

```text
npx expo start
npx expo start --tunnel
npx expo run:android
eas build -p android
```

There is no React Native source tree to load into Expo Go and no Android project from which to generate an APK.

If this repository is later expanded with a separate Expo mobile client, that client should live in its own directory/repository with its own `package.json` and Expo configuration. It would then have an independent Node/npm/EAS toolchain from this Python bot.

## No Traditional Build Step

This project does not need a compile/build command for normal operation.

Development/runtime is simply:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

The Python interpreter executes `main.py` directly.

For a production deployment, use a persistent Python process or suitable process manager/hosting service and expose the Flask health port.

## Useful Development Commands

### Check Python version

```powershell
python --version
```

### Check installed packages

```powershell
python -m pip list
```

### Reinstall exactly from requirements

```powershell
python -m pip install --upgrade --force-reinstall -r requirements.txt
```

### Syntax-check the source without starting the bot

```powershell
python -m py_compile main.py welcome_templates.py
```

### Run the bot

```powershell
python main.py
```

### Stop the bot

Press:

```text
Ctrl+C
```

## Current Code-Version Note

The archive contains mixed internal version labels:

- the large `main.py` header references versions around `v6.5`;
- some status/help strings still say `v6.6`;
- startup logging identifies the runtime mode as `v6.11` and lists the later backup/mirror capabilities.

For operational documentation, this README describes the **actual behavior present in the supplied source code**, rather than treating the older banner strings as the authoritative version number.

## Official References

- Telegram Bot API: https://core.telegram.org/bots/api
- Telegram Bot Features / Privacy Mode: https://core.telegram.org/bots/features
- MongoDB Atlas connection guide: https://www.mongodb.com/docs/atlas/connect-to-database-deployment/
- MongoDB driver connection guidance: https://www.mongodb.com/docs/atlas/driver-connection/

## License / Content Responsibility

### License

This project is released under the **MIT License**. You are free to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of this software, subject to the terms of the MIT License included in the `LICENSE` file.

The original repository owner is **[The Fullstack Failures](https://github.com/FullStackFailures)**.

### Content and Usage Responsibility

You are solely responsible for how you use, modify, deploy, or redistribute this project.

The original repository owner, **[The Fullstack Failures](https://github.com/FullStackFailures)**, is **not responsible or liable for any illegal, abusive, harmful, unauthorized, or otherwise inappropriate use** of this software by users, contributors, or third parties.

This includes, but is not limited to, unauthorized distribution of copyrighted material, privacy violations, abuse of third-party services, fraudulent activity, malicious usage, or any other activity that violates applicable laws, regulations, platform rules, or third-party terms of service.

By using, modifying, deploying, or redistributing this project, you acknowledge that you are responsible for your own actions and for ensuring that your implementation and usage comply with all applicable laws and third-party terms.

The software is provided **"AS IS"**, without warranty of any kind, as described in the MIT License.

