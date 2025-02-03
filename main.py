import os
import sys
import logging
import traceback
import openai
import json
import gspread
from gtts import gTTS
from flask import Flask
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import time

# ====== CONFIGURACIÓN DE LOGGING ======
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/tmp/bot_debug.log')
    ]
)
logger = logging.getLogger(__name__)

# ====== CLIENTE OPENAI ======
try:
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error(f"OpenAI Client Initialization Error: {e}")
    sys.exit(1)

# ====== CREDENCIALES ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
CREDENTIALS_FILE = "/etc/secrets/credentials.json"
SPREADSHEET_NAME = "Whitelist"

# ====== SERVIDOR HTTP ======
app = Flask('')

@app.route('/')
def home():
    return "El bot está activo."

# ====== GOOGLE SHEETS ======
def get_sheet():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(credentials)
        sheet = client.open(SPREADSHEET_NAME).sheet1
        logger.info("✅ Conexión a Google Sheets exitosa.")
        return sheet
    except Exception as e:
        logger.error(f"❌ Error al conectar con Google Sheets: {e}")
        raise

validated_users = {}
user_threads = {}

async def validate_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.debug(f"Validate email called for chat_id: {chat_id}")
    
    if chat_id in validated_users:
        await update.message.reply_text("✅ Ya estás validado. Puedes interactuar conmigo.")
        return

    await update.message.reply_text("Por favor, proporciona tu email para validar el acceso:")
    context.user_data["state"] = "waiting_email"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text.strip().lower() if update.message.text else None
    voice_message = update.message.voice

    logger.debug(f"Handling message for chat_id: {chat_id}")

    if context.user_data.get("state") == "waiting_email":
        try:
            sheet = get_sheet()
            emails = [email.lower() for email in sheet.col_values(3)[1:]]
            if user_message in emails:
                username = update.effective_user.username or f"user_{chat_id}"
                email_row = emails.index(user_message) + 2
                sheet.update_cell(email_row, 6, username)
                validated_users[chat_id] = user_message
                context.user_data["state"] = "validated"
                logger.info(f"✅ Usuario validado: {username} con email {user_message}")
                await update.message.reply_text(f"✅ Acceso concedido. ¡Bienvenido, {username}!")
                return
            else:
                await update.message.reply_text("❌ Email no válido. Inténtalo nuevamente.")
                return
        except Exception as e:
            logger.error(f"Error durante la validación: {e}")
            await update.message.reply_text("❌ Hubo un error al validar tu email. Intenta más tarde.")
            return

    if chat_id not in validated_users:
        await validate_email(update, context)
        return

    if user_message:
        await process_text_message(update, context, user_message)
    
    if voice_message:
        await process_voice_message(update, context, voice_message)

async def process_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    chat_id = update.effective_chat.id
    logger.info(f"Mensaje recibido del usuario: {user_message}")
    
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        if chat_id not in user_threads:
            thread = client.beta.threads.create()
            user_threads[chat_id] = thread.id
        thread_id = user_threads[chat_id]
        message = client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message
        )
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )
        while True:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run.status == "completed":
                messages = client.beta.threads.messages.list(thread_id=thread_id)
                bot_reply = next(
                    (msg.content[0].text.value for msg in messages.data if msg.role == "assistant"), 
                    "No pude generar una respuesta."
                )
                logger.info(f"Respuesta del bot: {bot_reply}")
                await update.message.reply_text(bot_reply)
                break
            elif run.status in ["failed", "cancelled", "expired"]:
                logger.error(f"Run status: {run.status}")
                await update.message.reply_text("Hubo un error al procesar tu solicitud.")
                break
            time.sleep(1)
    except Exception as e:
        logger.error(f"Error al interactuar con OpenAI Assistant: {e}")
        await update.message.reply_text("Hubo un error al procesar tu solicitud. Intenta nuevamente.")

def main():
    try:
        logger.debug("Iniciando la aplicación de Telegram...")
        application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", validate_email))
        application.add_handler(MessageHandler(filters.TEXT, handle_message))
        from threading import Thread
        Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': int(os.getenv('PORT', 8080))}).start()
        logger.debug("Iniciando polling...")
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error fatal en la aplicación: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
