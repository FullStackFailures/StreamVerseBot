"""
Text templates for StreamVerseOG Media Indexer Bot v6.3

Kept separate to keep main.py smaller.
All templates use ParseMode.HTML.
"""

# ──────────────────────────────────────────────────────────
# WELCOME MESSAGE
# ──────────────────────────────────────────────────────────

WELCOME_TEMPLATE_HTML = (
    "👋 <b>Welcome, {name}!</b>\n\n"
    "You've just joined <b>StreamVerseOG</b> — your go-to spot for the latest "
    "movies &amp; series! 🎬🍿\n\n"
    "📁 <b>All Added Shows</b> — browse the full library\n"
    "💬 <b>General Chat</b> — connect with other members\n\n"
    "We're glad to have you here. Enjoy your stay! 🎉"
)

WELCOME_BUTTON_LABEL = "💬 Go to Chat"


# ──────────────────────────────────────────────────────────
# FILE DELIVERY MESSAGE
# ──────────────────────────────────────────────────────────

DELETE_WARNING_TEMPLATE_HTML = (
    "👋 Hello <b>{name}</b>,\n\n"
    "⚠️ Please forward this file to your <b>Saved Messages</b> or any <b>Other Chat</b> to save it.\n\n"

    "<b>This file will be automatically deleted in</b>\n"
    "<b>{minutes} minute(s).</b>\n\n"

    "📢 <b>MAIN CHANNEL</b> <a href=\"{group_link}\"><b>{group_name}</b></a>\n"
    "📢 <b>BACKUP CHANNEL</b> <a href=\"{group_link_2}\"><b>{group_name_2}</b></a>\n\n"

    "📸 <b>Follow us on Instagram</b> <a href=\"{instagram_link}\"><b>{instagram_label}</b></a>"
    
)

