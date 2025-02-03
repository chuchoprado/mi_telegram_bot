import os
import sys
import logging
import traceback
import openai
import json
from openai import OpenAI
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

# Obtener variables sensibles del entorno
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ASSISTANT_ID = os.getenv('ASSISTANT_ID')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GOOGLE_CREDENTIALS = os.getenv('GOOGLE_CREDENTIALS')
SPREADSHEET_NAME = os.getenv('SPREADSHEET_NAME', 'Whitelist')

# Verificar que las variables de entorno existan
required_env_vars = ['TELEGRAM_BOT_TOKEN', 'ASSISTANT_ID', 'OPENAI_API_KEY', 'GOOGLE_CREDENTIALS']

for var in required_env_vars:
    if not os.getenv(var):
        raise ValueError(f"La variable de entorno {var} no está configurada")

# Persistence file for user threads
THREADS_FILE = os.getenv('THREADS_FILE', '/etc/secrets/credentials.json')

def load_user_threads():
    """Load existing user threads from file."""
    try:
        if os.path.exists(THREADS_FILE):
            with open(THREADS_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Error loading user threads: {e}")
        return {}

def save_user_threads(user_threads):
    """Save user threads to file."""
    try:
        with open(THREADS_FILE, 'w') as f:
            json.dump(user_threads, f)
    except Exception as e:
        logger.error(f"Error saving user threads: {e}")

# Modify global user_threads to use persistence
user_threads = load_user_threads()

# ====== CONFIGURACIÓN DE LOGGING ======
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/etc/secrets/bot_debug.log')
    ]
)
logger = logging.getLogger(__name__)

# ====== CLIENTE OPENAI ======
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    logger.error(f"OpenAI Client Initialization Error: {e}")
    sys.exit(1)

# ====== CREDENCIALES ======
def get_sheet():
    try:
        credentials_dict = json.loads(GOOGLE_CREDENTIALS)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
        client = gspread.authorize(credentials)
        sheet = client.open(SPREADSHEET_NAME).sheet1
        logger.info("✅ Conexión a Google Sheets exitosa.")
        return sheet
    except Exception as e:
        logger.error(f"❌ Error al conectar con Google Sheets: {e}")
        raise

# ====== SERVIDOR HTTP ======
app = Flask(__name__)

@app.route('/')
def home():
    return "El bot está activo."

def run():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))

# ====== PROCESAMIENTO DE MENSAJES ======
async def process_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    chat_id = update.effective_chat.id
    logger.info(f"Mensaje recibido del usuario: {user_message}")
    await update.message.reply_text("Procesando mensaje...")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text.strip().lower() if update.message.text else None
    if user_message:
        await process_text_message(update, context, user_message)

# ====== CONFIGURACIÓN DEL BOT ======
def main():
    try:
        logger.debug("Iniciando la aplicación de Telegram...")
        application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Bienvenido!")))
        application.add_handler(MessageHandler(filters.TEXT, handle_message))

        from threading import Thread
        Thread(target=run).start()

        logger.debug("Iniciando polling...")
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error fatal en la aplicación: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
