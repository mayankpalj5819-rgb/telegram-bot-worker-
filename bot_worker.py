import os
import threading
import asyncio
import base64
import json
import time
from io import BytesIO
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from flask import Flask

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
HF_SPACE_URL = os.getenv("HF_SPACE_URL", "https://mayank2028-agent.hf.space")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set")

flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "Bot is running", 200

class BotWorker:
    def __init__(self):
        self.app: Application = None

    async def forward_to_hf(self, text: str, user_id: int = None) -> dict:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            try:
                payload = {
                    "message": text,
                    "user_id": str(user_id) if user_id else "telegram_user"
                }
                
                print(f"→ HF Space: {HF_SPACE_URL}/api/chat")
                print(f"→ Payload: {json.dumps(payload)[:200]}")
                
                resp = await client.post(
                    f"{HF_SPACE_URL}/api/chat",
                    json=payload,
                    timeout=90.0
                )
                
                print(f"← Status: {resp.status_code}")
                print(f"← Body: {resp.text[:500]}")
                
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if "error" in data:
                            return {
                                "response": f"❌ HF Space error: {data['error']}",
                                "screenshot": "",
                                "mode": "error"
                            }
                        return {
                            "response": data.get("response", "No response"),
                            "screenshot": data.get("screenshot", ""),
                            "mode": data.get("mode", "chat")
                        }
                    except Exception as e:
                        return {
                            "response": f"❌ Invalid JSON: {str(e)}\nRaw: {resp.text[:300]}",
                            "screenshot": "",
                            "mode": "error"
                        }
                else:
                    return {
                        "response": f"❌ HF Space returned {resp.status_code}: {resp.text[:300]}",
                        "screenshot": "",
                        "mode": "error"
                    }
                    
            except httpx.TimeoutException:
                return {
                    "response": "⏱️ Request timed out. The HF Space might be waking up from sleep (cold start takes ~30-60s). Please try again!",
                    "screenshot": "",
                    "mode": "error"
                }
            except Exception as e:
                import traceback
                print(f"Error in forward_to_hf: {traceback.format_exc()}")
                return {
                    "response": f"❌ Error: {str(e)[:200]}",
                    "screenshot": "",
                    "mode": "error"
                }

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome = """🤖 *AI Agent Browser Bot*

I connect to your HF Space AI Agent.

*Commands:*
/start - Show this message
/browse <url> - Browse a website
/search <query> - Search the web
/screenshot - Get current screenshot
/status - Check HF Space status

Or just send me any message!"""
        await update.message.reply_text(welcome, parse_mode="Markdown")

    async def browse_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("❌ Usage: /browse <url>")
            return

        url = context.args[0]
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        await update.message.chat.send_action(action="typing")
        msg = await update.message.reply_text(f"🌐 Browsing {url}...")

        result = await self.forward_to_hf(f"Go to {url} and summarize what you see", update.effective_user.id)
        await self._send_result(update, msg, result, f"🌐 {url[:50]}")

    async def search_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("❌ Usage: /search <query>")
            return

        query = " ".join(context.args)
        await update.message.chat.send_action(action="typing")
        msg = await update.message.reply_text(f"🔍 Searching: {query}...")

        result = await self.forward_to_hf(f"Search for {query}", update.effective_user.id)
        await self._send_result(update, msg, result, f"🔍 {query}")

    async def screenshot_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.chat.send_action(action="upload_photo")
        msg = await update.message.reply_text("📸 Getting screenshot...")

        result = await self.forward_to_hf("Take a screenshot of the current page", update.effective_user.id)
        await self._send_result(update, msg, result, "📸 Screenshot")

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(f"{HF_SPACE_URL}/health", timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    status = "✅ Online" if data.get("status") == "ok" else "❌ Issue"
                    browser = "🟢 Active" if data.get("browser_active") else "🔴 Inactive"
                    await update.message.reply_text(
                        f"🤖 *HF Space Status*\n\n{status}\n🌐 Browser: {browser}",
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text(f"❌ HF Space returned {resp.status_code}")
            except Exception as e:
                await update.message.reply_text(f"❌ HF Space unreachable:\n```\n{str(e)[:200]}\n```", parse_mode="Markdown")

    async def handle_msg(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        user_id = update.effective_user.id
        
        await update.message.chat.send_action(action="typing")
        msg = await update.message.reply_text("🤔 Thinking...")

        result = await self.forward_to_hf(text, user_id)

        keyboard = [[InlineKeyboardButton("🌐 Browse for this", callback_data=f"browse:{text[:100]}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await self._send_result(update, msg, result, "🤖 AI Agent", reply_markup)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data = query.data
        if data.startswith("browse:"):
            search_query = data[7:]
            await query.edit_message_text(f"🌐 Browsing: {search_query}...")

            result = await self.forward_to_hf(search_query, update.effective_user.id)
            await self._send_result_to_query(query, result, f"🌐 {search_query[:50]}")

    async def _send_result(self, update: Update, msg, result: dict, header: str, reply_markup=None):
        response = result.get("response", "No response")
        screenshot = result.get("screenshot", "")

        if not isinstance(response, str):
            response = str(response)

        if screenshot and isinstance(screenshot, str) and "," in screenshot:
            try:
                img_data = base64.b64decode(screenshot.split(",")[1])
                await msg.delete()
                await update.message.reply_photo(
                    photo=BytesIO(img_data),
                    caption=f"{header}\n\n{response[:900]}",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Screenshot error: {e}")
                try:
                    await msg.edit_text(f"{header}\n\n{response[:3000]}\n\n⚠️ Screenshot error", parse_mode="Markdown")
                except:
                    await update.message.reply_text(f"{header}\n\n{response[:3000]}", reply_markup=reply_markup)
        else:
            try:
                await msg.edit_text(f"{header}\n\n{response[:3000]}", reply_markup=reply_markup, parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"{header}\n\n{response[:3000]}", reply_markup=reply_markup)

    async def _send_result_to_query(self, query, result: dict, header: str):
        response = result.get("response", "No response")
        screenshot = result.get("screenshot", "")

        if not isinstance(response, str):
            response = str(response)

        if screenshot and isinstance(screenshot, str) and "," in screenshot:
            try:
                img_data = base64.b64decode(screenshot.split(",")[1])
                await query.message.reply_photo(
                    photo=BytesIO(img_data),
                    caption=f"{header}\n\n{response[:900]}",
                    parse_mode="Markdown"
                )
            except:
                await query.edit_message_text(f"{header}\n\n{response[:3000]}", parse_mode="Markdown")
        else:
            try:
                await query.edit_message_text(f"{header}\n\n{response[:3000]}", parse_mode="Markdown")
            except Exception as e:
                await query.message.reply_text(f"{header}\n\n{response[:3000]}", parse_mode="Markdown")

    async def run_async(self):
        self.app = Application.builder().token(TOKEN).build()

        self.app.add_handler(CommandHandler("start", self.start_cmd))
        self.app.add_handler(CommandHandler("browse", self.browse_cmd))
        self.app.add_handler(CommandHandler("search", self.search_cmd))
        self.app.add_handler(CommandHandler("screenshot", self.screenshot_cmd))
        self.app.add_handler(CommandHandler("status", self.status_cmd))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_msg))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        print(f"🤖 Bot worker starting...")
        print(f"🔗 HF Space: {HF_SPACE_URL}")

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        print("✅ Bot polling started")

        while True:
            await asyncio.sleep(3600)


def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    worker = BotWorker()
    try:
        loop.run_until_complete(worker.run_async())
    except Exception as e:
        print(f"Bot error: {e}")
        import traceback
        print(traceback.format_exc())
    finally:
        loop.close()


if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    print("🤖 Bot thread started")

    print(f"🌐 Starting health server on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True)
