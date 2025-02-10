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
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

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
app = Flask(__name__)  # ✅ Asegurar que Flask se inicializa correctamente

@app.route("/", methods=["GET"])
def home():
    return "El bot está activo."

@app.route(f"/{TOKEN}", methods=["POST"])  # ✅ Solo aceptar POST
def webhook():
    """Procesa las actualizaciones de Telegram."""
    try:
        update = Update.de_json(request.get_json(), application.bot)
        asyncio.run(application.process_update(update))  # ✅ Se ejecuta correctamente como async
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
async def start(update: Update, context: CallbackContext):
    """Mensaje de bienvenida."""
    await update.message.reply_text("¡Hola! Soy tu bot de MeditaHub. ¿En qué puedo ayudarte?")

async def handle_message(update: Update, context: CallbackContext):
    """Maneja los mensajes de texto del usuario."""
    text = update.message.text
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"Recibí tu mensaje: {text}")

# ====== REGISTRO DE HANDLERS ======
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ====== EJECUCIÓN ======
if __name__ == "__main__":
    print("✅ Iniciando el bot de Telegram...")
    application.run_polling()
