import os
import threading
import asyncio
import base64
import json
import time
import signal
import sys
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
        self._stop_event = asyncio.Event()
        self.last_request_time = 0
        self.session_timeout = 300  # 5 minutes

    async def forward_to_hf(self, text: str, user_id: int = None) -> dict:
        """Forward message to HF Space with proper error handling."""
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            try:
                current_time = time.time()
                
                # Try the main API endpoint
                resp = await client.post(
                    f"{HF_SPACE_URL}/api/chat",
                    json={"message": text, "user_id": str(user_id) if user_id else "default"},
                    timeout=90.0
                )
                
                print(f"API Response Status: {resp.status_code}")
                print(f"API Response Body (first 500 chars): {resp.text[:500]}")
                
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        self.last_request_time = current_time
                        return data
                    except:
                        return {"response": resp.text, "screenshot": ""}
                else:
                    return await self._try_alternative_endpoints(client, text, user_id)
                    
            except httpx.TimeoutException:
                return {
                    "response": "⏱️ Request timed out. The HF Space might be waking up from sleep (cold start takes ~30-60s). Please try again!",
                    "screenshot": ""
                }
            except Exception as e:
                import traceback
                print(f"Error in forward_to_hf: {traceback.format_exc()}")
                return {
                    "response": f"❌ Error connecting to HF Space: {str(e)[:200]}",
                    "screenshot": ""
                }

    async def _try_alternative_endpoints(self, client, text, user_id):
        """Try different API endpoints if the main one fails."""
        endpoints_to_try = [
            ("/api/predict", {"data": [text]}),
            ("/gradio_api/call/predict", {"data": [text]}),
            ("/predict", {"message": text}),
            ("/chat", {"message": text}),
        ]
        
        for endpoint, payload in endpoints_to_try:
            try:
                print(f"Trying alternative endpoint: {endpoint}")
                resp = await client.post(
                    f"{HF_SPACE_URL}{endpoint}",
                    json=payload,
                    timeout=30.0
                )
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except:
                        return {"response": resp.text, "screenshot": ""}
            except Exception as e:
                print(f"  {endpoint} failed: {e}")
                continue
        
        return {
            "response": "❌ Could not connect to HF Space. All endpoints failed.",
            "screenshot": ""
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
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                for endpoint in ["/health", "/gradio_api/heartbeat", "/"]:
                    try:
                        resp = await client.get(f"{HF_SPACE_URL}{endpoint}", timeout=5.0)
                        if resp.status_code == 200:
                            await update.message.reply_text(
                                f"🤖 *HF Space Status*\n\n✅ Online (responded to {endpoint})",
                                parse_mode="Markdown"
                            )
                            return
                    except:
                        continue
                        
                await update.message.reply_text("❌ HF Space unreachable", parse_mode="Markdown")
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
        
        if not response and "data" in result:
            response = str(result["data"])
        if not response and "output" in result:
            response = str(result["output"])

        if not isinstance(response, str):
            response = str(response)

        # Detect stale Wikipedia responses
        if "wikipedia" in response.lower() and "wikipedia" not in header.lower():
            if hasattr(update, 'message') and update.message:
                user_text = update.message.text or ""
                if "wikipedia" not in user_text.lower():
                    response = "⚠️ The agent returned a stale response. The browser may still have Wikipedia loaded from a previous session.\n\nTry: /browse <new-url> to navigate elsewhere first."

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
                print(f"Edit message error: {e}")
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
        """Async entry point with proper shutdown handling."""
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
        await self.app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

        print("✅ Bot polling started")
        
        # Keep running until stop event is set
        try:
            while not self._stop_event.is_set():
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        
        # Graceful shutdown
        print("🛑 Shutting down bot...")
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        print("✅ Bot stopped")

    def stop(self):
        """Signal the bot to stop."""
        self._stop_event.set()

def run_bot():
    """Run bot in a thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    worker = BotWorker()
    
    # Handle signals for graceful shutdown
    def signal_handler(signum, frame):
        print(f"Received signal {signum}, stopping bot...")
        worker.stop()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
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
