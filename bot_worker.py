import os
import threading
import asyncio
import base64
import json
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

    async def discover_api(self) -> dict:
        """Discover available Gradio API endpoints."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(f"{HF_SPACE_URL}/gradio_api/info")
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                print(f"API discovery error: {e}")
        return {}

    async def gradio_predict(self, text: str) -> dict:
        """
        Use Gradio's async API to call the Space.
        Gradio uses: POST /gradio_api/call/<endpoint> -> returns event_id
        Then poll: GET /gradio_api/call/<endpoint>/<event_id>
        """
        # First, try to discover the correct endpoint
        api_info = await self.discover_api()
        print(f"API Info: {json.dumps(api_info)[:500]}")

        # Try common endpoint names for agent/chat spaces
        possible_endpoints = ["/chat", "/predict", "/agent", "/run"]
        
        # If we can find endpoints from API info, use those
        if "named_endpoints" in api_info:
            endpoints = list(api_info["named_endpoints"].keys())
            if endpoints:
                possible_endpoints = endpoints + possible_endpoints
        
        async with httpx.AsyncClient(timeout=90.0) as client:
            for endpoint in possible_endpoints:
                try:
                    print(f"Trying endpoint: {endpoint}")
                    
                    # Step 1: Submit job
                    submit_url = f"{HF_SPACE_URL}/gradio_api/call{endpoint}"
                    resp = await client.post(
                        submit_url,
                        json={"data": [text]},  # Gradio expects data array
                        timeout=30.0
                    )
                    
                    if resp.status_code != 200:
                        print(f"  Status {resp.status_code}: {resp.text[:200]}")
                        continue
                    
                    result_data = resp.json()
                    event_id = result_data.get("event_id")
                    
                    if not event_id:
                        print(f"  No event_id in response: {result_data}")
                        continue
                    
                    print(f"  Got event_id: {event_id}")
                    
                    # Step 2: Poll for result
                    poll_url = f"{HF_SPACE_URL}/gradio_api/call{endpoint}/{event_id}"
                    max_polls = 60  # 30 seconds max
                    
                    for _ in range(max_polls):
                        poll_resp = await client.get(poll_url, timeout=30.0)
                        poll_data = poll_resp.json()
                        
                        status = poll_data.get("status")
                        print(f"  Poll status: {status}")
                        
                        if status == "complete":
                            output = poll_data.get("output", {})
                            data = output.get("data", [])
                            if data and len(data) > 0:
                                response_text = str(data[0]) if data[0] is not None else "No response"
                            else:
                                response_text = str(output) if output else "No response"
                            
                            return {
                                "response": response_text,
                                "screenshot": ""
                            }
                        
                        elif status == "error":
                            return {
                                "response": f"❌ Space error: {poll_data.get('message', 'Unknown error')}",
                                "screenshot": ""
                            }
                        
                        elif status in ("pending", "generating"):
                            await asyncio.sleep(0.5)
                            continue
                        else:
                            # Unknown status, keep polling
                            await asyncio.sleep(0.5)
                    
                    return {
                        "response": "⏱️ Request timed out. The HF Space might be starting up (cold start takes ~30-60s). Try again!",
                        "screenshot": ""
                    }
                    
                except Exception as e:
                    print(f"  Endpoint {endpoint} failed: {e}")
                    continue
            
            # If all Gradio endpoints failed, try the custom /api/chat as fallback
            try:
                print("Trying fallback /api/chat...")
                resp = await client.post(
                    f"{HF_SPACE_URL}/api/chat",
                    json={"message": text},
                    timeout=30.0
                )
                print(f"  /api/chat status: {resp.status_code}")
                print(f"  /api/chat body: {resp.text[:500]}")
                
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except:
                        return {"response": resp.text, "screenshot": ""}
                else:
                    return {
                        "response": f"❌ HF Space returned {resp.status_code}. The Space might be asleep or the endpoint doesn't exist.\n\nBody: {resp.text[:300]}",
                        "screenshot": ""
                    }
            except Exception as e:
                return {
                    "response": f"❌ All endpoints failed. Last error: {str(e)}",
                    "screenshot": ""
                }

    async def forward_to_hf(self, text: str) -> dict:
        """Forward message to HF Space with proper error handling."""
        try:
            return await self.gradio_predict(text)
        except Exception as e:
            import traceback
            print(f"forward_to_hf error: {traceback.format_exc()}")
            return {"response": f"❌ Error: {str(e)}", "screenshot": ""}

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

        result = await self.forward_to_hf(f"Go to {url} and summarize what you see")
        await self._send_result(update, msg, result, f"🌐 {url[:50]}")

    async def search_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("❌ Usage: /search <query>")
            return

        query = " ".join(context.args)
        await update.message.chat.send_action(action="typing")
        msg = await update.message.reply_text(f"🔍 Searching: {query}...")

        result = await self.forward_to_hf(f"Search for {query}")
        await self._send_result(update, msg, result, f"🔍 {query}")

    async def screenshot_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.chat.send_action(action="upload_photo")
        msg = await update.message.reply_text("📸 Getting screenshot...")

        result = await self.forward_to_hf("Take a screenshot of the current page")
        await self._send_result(update, msg, result, "📸 Screenshot")

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                # Try Gradio health first
                resp = await client.get(f"{HF_SPACE_URL}/gradio_api/heartbeat", timeout=10.0)
                if resp.status_code == 200:
                    await update.message.reply_text("🤖 *HF Space Status*\n\n✅ Online (Gradio API)", parse_mode="Markdown")
                    return
            except:
                pass
            
            try:
                resp = await client.get(f"{HF_SPACE_URL}/health", timeout=10.0)
                data = resp.json()
                status = "✅ Online" if data.get("status") == "ok" else "❌ Issue"
                browser = "🟢 Active" if data.get("browser_active") else "🔴 Inactive"
                await update.message.reply_text(f"🤖 *HF Space Status*\n\n{status}\n🌐 Browser: {browser}", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ HF Space unreachable:\n```\n{str(e)[:200]}\n```", parse_mode="Markdown")

    async def handle_msg(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        await update.message.chat.send_action(action="typing")
        msg = await update.message.reply_text("🤔 Thinking...")

        result = await self.forward_to_hf(text)

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

            result = await self.forward_to_hf(search_query)
            await self._send_result_to_query(query, result, f"🌐 {search_query[:50]}")

    async def _send_result(self, update: Update, msg, result: dict, header: str, reply_markup=None):
        response = result.get("response", "No response")
        screenshot = result.get("screenshot", "")

        # Ensure response is a string
        if not isinstance(response, str):
            response = str(response)

        if screenshot and "," in screenshot:
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
                try:
                    await msg.edit_text(f"{header}\n\n{response[:3000]}\n\n⚠️ Screenshot error: {str(e)[:100]}", parse_mode="Markdown")
                except:
                    await update.message.reply_text(f"{header}\n\n{response[:3000]}", parse_mode="Markdown")
        else:
            try:
                await msg.edit_text(f"{header}\n\n{response[:3000]}", reply_markup=reply_markup, parse_mode="Markdown")
            except Exception as e:
                # If edit fails, send new message
                await update.message.reply_text(f"{header}\n\n{response[:3000]}", reply_markup=reply_markup, parse_mode="Markdown")

    async def _send_result_to_query(self, query, result: dict, header: str):
        response = result.get("response", "No response")
        screenshot = result.get("screenshot", "")

        if not isinstance(response, str):
            response = str(response)

        if screenshot and "," in screenshot:
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
        """Async entry point."""
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

        # Test connection to HF Space
        print("🔍 Testing HF Space connection...")
        test_result = await self.forward_to_hf("Hello")
        print(f"Test result: {test_result.get('response', 'No response')[:200]}")

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        print("✅ Bot polling started")
        # Keep running
        while True:
            await asyncio.sleep(3600)

def run_bot():
    """Run bot in a thread with its own event loop."""
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
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    print("🤖 Bot thread started")

    # Start Flask server for Render health check
    print(f"🌐 Starting health server on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True)
