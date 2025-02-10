import os
import sys
import logging
import traceback
import openai
import asyncio
from openai import OpenAI
import gspread
from gtts import gTTS
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# ====== CONFIGURACIÓN DE LOGGING ======
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot_debug.log"),
    ],
)
logger = logging.getLogger(__name__)

# ====== CONFIGURACIÓN DE TOKENS ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
CREDENTIALS_FILE = "/etc/secrets/credentials.json"
SPREADSHEET_NAME = "Whitelist"

# ====== CLIENTE OPENAI ======
try:
    if not OPENAI_API_KEY:
        raise ValueError("❌ La variable de entorno OPENAI_API_KEY no está definida.")
    client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("✅ OpenAI Client inicializado correctamente.")
except Exception as e:
    logger.error(f"OpenAI Client Initialization Error: {e}")
    sys.exit(1)

# ====== CONFIGURACIÓN DEL BOT DE TELEGRAM ======
application = Application.builder().token(TOKEN).build()

# ====== SERVIDOR FLASK ======
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "El bot está activo."

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Procesa las actualizaciones de Telegram."""
    try:
        update = Update.de_json(request.get_json(), application.bot)
        asyncio.run(application.update_queue.put(update))
    except Exception as e:
        logger.error(f"Error en Webhook: {e}")
        logger.error(traceback.format_exc())
    return "OK", 200

# ====== CONEXIÓN A GOOGLE SHEETS ======
def get_sheet():
    try:
        credentials_path = os.path.expanduser(CREDENTIALS_FILE)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        credentials = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
        client = gspread.authorize(credentials)
        sheet = client.open(SPREADSHEET_NAME).sheet1
        logger.info("✅ Conexión a Google Sheets exitosa.")
        return sheet
    except Exception as e:
        logger.error(f"❌ Error al conectar con Google Sheets: {e}")
        raise

# ====== GESTIÓN DE USUARIOS VALIDADOS ======
validated_users = {}
user_threads = {}

# ====== HANDLERS DE TELEGRAM ======
async def start(update: Update, context):
    """Mensaje de bienvenida."""
    await update.message.reply_text("¡Hola! Soy tu bot de MeditaHub. ¿En qué puedo ayudarte?")

async def handle_message(update: Update, context):
    chat_id = update.effective_chat.id
    user_message = update.message.text.strip().lower() if update.message.text else None
    if user_message:
        await process_text_message(update, context, user_message)

async def process_text_message(update: Update, context, user_message: str):
    """Procesar mensaje de texto con OpenAI Assistant."""
    chat_id = update.effective_chat.id
    try:
        if chat_id not in user_threads:
            thread = client.beta.threads.create()
            user_threads[chat_id] = thread.id

        thread_id = user_threads[chat_id]
        message = client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID)

        while True:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run.status == "completed":
                messages = client.beta.threads.messages.list(thread_id=thread_id)
                bot_reply = next((msg.content[0].text.value for msg in messages.data if msg.role == "assistant"), "No pude generar una respuesta.")
                await update.message.reply_text(bot_reply)
                break
            elif run.status in ["failed", "cancelled", "expired"]:
                await update.message.reply_text("Hubo un error al procesar tu solicitud.")
                break
    except Exception as e:
        logger.error(f"Error con OpenAI Assistant: {e}")
        await update.message.reply_text("Hubo un error al procesar tu solicitud. Intenta nuevamente.")

# ====== REGISTRO DE HANDLERS ======
application.add_handler(CommandHandler("start", start))  # ✅ REGISTRO DEL HANDLER
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ====== EJECUCIÓN ======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
