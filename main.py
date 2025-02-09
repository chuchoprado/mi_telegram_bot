import os
import sys
import logging
import traceback
import openai
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

# ====== CONFIGURACIÓN DE LOGGING ======
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot_debug.log')
    ]
)
logger = logging.getLogger(__name__)

# ====== CLIENTE OPENAI ======
try:"OPENAI_API_KEY")
except Exception as e:
    logger.error(f"OpenAI Client Initialization Error: {e}")
    sys.exit(1)

# ====== CREDENCIALES ======
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ASSISTANT_ID = "ASSISTANT_ID"
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

    # Manejar estado de espera de email
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

    # Verificar si el usuario está validado
    if chat_id not in validated_users:
        await validate_email(update, context)
        return

    # Procesar mensaje de texto
    if user_message:
        await process_text_message(update, context, user_message)
    
    # Procesar nota de voz
    if voice_message:
        await process_voice_message(update, context, voice_message)

async def process_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    chat_id = update.effective_chat.id
    logger.info(f"Mensaje recibido del usuario: {user_message}")
    
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # Crear o recuperar el thread para este usuario
        if chat_id not in user_threads:
            thread = client.beta.threads.create()
            user_threads[chat_id] = thread.id
        
        thread_id = user_threads[chat_id]

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
                # Obtener los mensajes del thread
                messages = client.beta.threads.messages.list(thread_id=thread_id)
                
                # Obtener la última respuesta del asistente
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
            
            # Pequeña pausa para no sobrecargar la API
            time.sleep(1)

    except Exception as e:
        logger.error(f"Error al interactuar con OpenAI Assistant: {e}")
        await update.message.reply_text("Hubo un error al procesar tu solicitud. Intenta nuevamente.")

async def process_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE, voice_message):
    chat_id = update.effective_chat.id
    try:
        # Download the voice message
        file = await context.bot.get_file(voice_message.file_id)
        voice_path = f"/tmp/voice_{chat_id}.ogg"
        await file.download_to_drive(voice_path)

        # Transcribe the voice message
        with open(voice_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file
            )

        logger.info(f"Transcripción de voz: {transcription.text}")

        # Get Assistant's text response
        thread = client.beta.threads.create()
        message = client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=transcription.text
        )

        # Create run
        run = client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=ASSISTANT_ID
        )

        # Wait for run completion
        while True:
            run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
            
            if run.status == "completed":
                # Get Assistant's response
                messages = client.beta.threads.messages.list(thread_id=thread.id)
                
                bot_reply = next(
                    (msg.content[0].text.value for msg in messages.data 
                     if msg.role == "assistant"), 
                    "No pude generar una respuesta."
                )
                
                # Convert text response to speech
                tts = gTTS(bot_reply, lang="es")
                audio_path = f"/tmp/response_{chat_id}.mp3"
                tts.save(audio_path)

                # Send voice response
                with open(audio_path, "rb") as audio_file:
                    await context.bot.send_voice(chat_id=chat_id, voice=audio_file)
                
                break
            
            elif run.status in ["failed", "cancelled", "expired"]:
                logger.error(f"Run status: {run.status}")
                await update.message.reply_text("Hubo un error al procesar tu solicitud de voz.")
                break
            
            time.sleep(1)

    except Exception as e:
        logger.error(f"Error al procesar nota de voz: {e}")
        await update.message.reply_text("No pude procesar la nota de voz. Intenta nuevamente.")

def main():
    try:
        logger.debug("Iniciando la aplicación de Telegram...")
        application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", validate_email))
        application.add_handler(MessageHandler(
            filters.TEXT | filters.VOICE, 
            handle_message
        ))

        logger.debug("Iniciando polling...")
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error fatal en la aplicación: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()

