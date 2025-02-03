import os
from dotenv import load_dotenv
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

# Cargar variables de entorno
load_dotenv()

# Obtener variables sensibles del archivo .env
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

# Persistence file for user threads - usar path relativo o variable de entorno
THREADS_FILE = "user_threads.json"

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

async def process_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    chat_id = update.effective_chat.id
    logger.info(f"Mensaje recibido del usuario: {user_message}")
    
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # Recuperar o crear thread existente
        if str(chat_id) not in user_threads:
            thread = client.beta.threads.create()
            user_threads[str(chat_id)] = thread.id
            save_user_threads(user_threads)
        
        thread_id = user_threads[str(chat_id)]

        # Añadir el mensaje del usuario al thread
        message = client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message
        )

        # Crear la ejecución (run)
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )

        # Esperar y verificar el estado de la ejecución
        while True:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            
            if run.status == "completed":
                messages = client.beta.threads.messages.list(thread_id=thread_id)
                bot_reply = next(
                    (msg.content[0].text.value for msg in messages.data 
                     if msg.role == "assistant"), 
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

async def process_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE, voice_message):
    chat_id = update.effective_chat.id
    try:
        # Usar directorio temporal del sistema
        voice_path = f"/tmp/voice_{chat_id}.ogg"
        file = await context.bot.get_file(voice_message.file_id)
        await file.download_to_drive(voice_path)

        with open(voice_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file
            )

        # Limpiar archivo temporal
        if os.path.exists(voice_path):
            os.remove(voice_path)

        logger.info(f"Transcripción de voz: {transcription.text}")

        if str(chat_id) not in user_threads:
            thread = client.beta.threads.create()
            user_threads[str(chat_id)] = thread.id
            save_user_threads(user_threads)
        
        thread_id = user_threads[str(chat_id)]

        message = client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=transcription.text
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
                    (msg.content[0].text.value for msg in messages.data 
                     if msg.role == "assistant"), 
                    "No pude generar una respuesta."
                )
                
                # Convert text response to speech usando directorio temporal
                tts = gTTS(bot_reply, lang="es")
                audio_path = f"/tmp/response_{chat_id}.mp3"
                tts.save(audio_path)

                with open(audio_path, "rb") as audio_file:
                    await context.bot.send_voice(chat_id=chat_id, voice=audio_file)
                
                # Limpiar archivo temporal
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                
                break
            
            elif run.status in ["failed", "cancelled", "expired"]:
                logger.error(f"Run status: {run.status}")
                await update.message.reply_text("Hubo un error al procesar tu solicitud de voz.")
                break
            
            time.sleep(1)

    except Exception as e:
        logger.error(f"Error al procesar nota de voz: {e}")
        await update.message.reply_text("No pude procesar la nota de voz. Intenta nuevamente.")

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,  # Cambiado a INFO para producción
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Cliente OpenAI
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    logger.error(f"OpenAI Client Initialization Error: {e}")
    sys.exit(1)

# Servidor HTTP para mantener el bot activo
app = Flask('')

@app.route('/')
def home():
    return "El bot está activo."

def run():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))

# Google Sheets setup usando credenciales como JSON
def get_sheet():
    try:
        # Cargar credenciales desde variable de entorno
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

validated_users = {}

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

def main():
    try:
        logger.info("Iniciando la aplicación de Telegram...")
        application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", validate_email))
        application.add_handler(MessageHandler(
            filters.TEXT | filters.VOICE, 
            handle_message
        ))

        # Iniciar Flask en un hilo separado
        from threading import Thread
        Thread(target=run).start()

        logger.info("Iniciando polling...")
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error fatal en la aplicación: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
