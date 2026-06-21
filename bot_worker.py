import os
import asyncio
import base64
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

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
HF_SPACE_URL = os.getenv("HF_SPACE_URL", "https://mayank2028-agent.hf.space")

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set")

class BotWorker:
    def __init__(self):
        self.app: Application = None
        
    async def forward_to_hf(self, text: str) -> dict:
        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(
                    f"{HF_SPACE_URL}/api/chat",
                    json={"message": text},
                    timeout=90.0
                )
                return resp.json()
            except Exception as e:
                return {"response": f"❌ HF Space error: {str(e)}", "screenshot": ""}
    
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
                await msg.edit_text(f"{header}\n\n{response[:3000]}\n\n⚠️ Screenshot error", parse_mode="Markdown")
        else:
            await msg.edit_text(f"{header}\n\n{response[:3000]}", reply_markup=reply_markup, parse_mode="Markdown")
    
    async def _send_result_to_query(self, query, result: dict, header: str):
        response = result.get("response", "No response")
        screenshot = result.get("screenshot", "")
        
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
            await query.edit_message_text(f"{header}\n\n{response[:3000]}", parse_mode="Markdown")
    
    def run(self):
        self.app = Application.builder().token(TOKEN).build()
        
        self.app.add_handler(CommandHandler("start", self.start_cmd))
        self.app.add_handler(CommandHandler("browse", self.browse_cmd))
        self.app.add_handler(CommandHandler("search", self.search_cmd))
        self.app.add_handler(CommandHandler("screenshot", self.screenshot_cmd))
        self.app.add_handler(CommandHandler("status", self.status_cmd))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_msg))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        
        print(f"🤖 Bot worker starting...")
        print(f"   HF Space: {HF_SPACE_URL}")
        self.app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    worker = BotWorker()
    worker.run()
