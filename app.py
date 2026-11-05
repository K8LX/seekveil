import base64
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- Configuration ---
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is missing.")

# --- Your Core Veil-X Logic (Refactored) ---
def update_list_visibility(sessionid: str, hide: bool = True) -> dict:
    """
    Sends the GraphQL request to hide (hide=True) or unhide (hide=False) lists.
    """
    if not sessionid or any(ord(c) <= 32 or ord(c) >= 127 or c in ';,"\\' for c in sessionid):
        raise ValueError("Invalid sessionid format.")
    match = re.fullmatch(r"([1-9][0-9]*)(?::|%3[aA]).+", sessionid)
    if not match:
        raise ValueError("Invalid sessionid format: expected a numeric user ID followed by ':' or '%3A'.")
    user_id = match.group(1)
    
    token = base64.b64encode(json.dumps({
        "ds_user_id": user_id,
        "sessionid": sessionid,
        "should_use_header_over_cookies": True,
    }, separators=(",", ":")).encode()).decode("ascii")
    
    authorization = "Bearer IGT:2:" + token
    name = "IGSettingsBooleanMutation"
    
    form = {
        "method": "post",
        "format": "json",
        "server_timestamps": "true",
        "locale": "user",
        "fb_api_req_friendly_name": name,
        "client_doc_id": "2669261950526978627082629536",
        "variables": json.dumps({
            "setting_id": "hide_followers_following_list",
            "allow_error_codes": True,
            "callsite": "account_privacy",
            "value": hide,  # True = Hide, False = Unhide
        }),
    }
    
    request = urllib.request.Request(
        "https://i.instagram.com/graphql/query",
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": authorization,
            "Cookie": f"sessionid={sessionid}; ds_user_id={user_id}",
            "IG-INTENDED-USER-ID": user_id,
            "X-IG-App-ID": "124024574287414",
            "X-Auth-Type": "instagram",
            "User-Agent": "Instagram 453.0.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "identity",
            "X-GraphQL-Client-Library": "pando",
            "X-FB-Friendly-Name": name,
        },
        method="POST",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError("Redirect refused.")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RuntimeError("Response too large.")
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        with error:
            raw = error.read(16385)
        raise RuntimeError(f"HTTP {error.code}: {raw.decode('utf-8', errors='ignore')[:200]}")
    except Exception as e:
        raise RuntimeError(f"Network error: {str(e)}")

def process_ig_response(body: dict, expected_value: bool) -> str:
    """Parses the GraphQL response to confirm success."""
    data = body.get("data") if isinstance(body, dict) else None
    root = "xdt_api__v1__settings__set_bool"
    matches = [value for key, value in data.items()
               if key == root or key.startswith(root + "(")] if isinstance(data, dict) else []
    result = matches[0] if len(matches) == 1 and isinstance(matches[0], dict) else {}
    
    if (not isinstance(body, dict) or body.get("errors") or body.get("error")
            or body.get("status") == "fail" or result.get("value") is not expected_value
            or any(result.get(key) for key in (
                "error_name_value", "errorNameValue", "fallback_message", "fallbackMessage"
            ))):
        raise RuntimeError(f"Instagram did not confirm the change. Expected value={expected_value}.")
    
    action = "hidden" if expected_value else "visible"
    return f"✅ Success! Your followers/following lists are now **{action}**."

# --- Telegram Bot Handlers ---
# Store user state temporarily: {user_id: "awaiting_session_for_hide" or "awaiting_session_for_unhide"}
user_states = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔒 Hide My Lists", callback_data="hide")],
        [InlineKeyboardButton("👁 Unhide My Lists", callback_data="unhide")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome to **Veil-X Bot**.\n\n"
        "I can help you hide or unhide your Instagram followers/following lists.\n"
        "**Please note:** You will need to provide your `sessionid` cookie value.\n"
        "⚠️ **Security Warning:** Only use this on a trusted network. I will delete your message containing the cookie immediately after processing.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    action = query.data  # "hide" or "unhide"
    
    user_states[user_id] = f"awaiting_session_{action}"
    
    await query.edit_message_text(
        text=f"Please send your Instagram `sessionid` cookie value now.\n\n"
             f"**Action selected:** {action.capitalize()}\n"
             f"*(Send /cancel to abort)*",
        parse_mode="Markdown"
    )

async def handle_session_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session_cookie = update.message.text.strip()
    
    # Check state
    state = user_states.get(user_id)
    if not state or not state.startswith("awaiting_session_"):
        return  # Ignore if not in the right state
    
    action = state.replace("awaiting_session_", "")
    hide_bool = (action == "hide")
    
    # Delete the user's message containing the cookie for security
    try:
        await update.message.delete()
    except Exception:
        pass  # Ignore if deletion fails
    
    status_msg = await update.message.reply_text("🔄 Processing your request... Please wait.")
    
    try:
        # Run the blocking network request in a thread to avoid blocking the bot loop
        body = await asyncio.to_thread(update_list_visibility, session_cookie, hide_bool)
        success_msg = process_ig_response(body, hide_bool)
        await status_msg.edit_text(success_msg, parse_mode="Markdown")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}", parse_mode="Markdown")
    finally:
        # Clear state
        user_states.pop(user_id, None)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states.pop(user_id, None)
    await update.message.reply_text("Cancelled. Use /start to begin again.")

# --- Flask Keep-Alive Server (for Render) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass  # Silence logs to keep Render logs clean

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# --- Main ---
def main():
    # Start Flask health server in a daemon thread so it doesn't block bot shutdown
    threading.Thread(target=run_health_server, daemon=True).start()
    
    # Build Telegram Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_session_input))
    
    # Run the bot (this blocks the main thread)
    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    import asyncio  # Moved import here for clarity, used in handler
    main()
