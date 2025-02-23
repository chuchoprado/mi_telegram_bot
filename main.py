import os
import asyncio
import httpx
import io
import sqlite3
import json
import logging
import openai
import time
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openai import AsyncOpenAI
import speech_recognition as sr
import requests
from contextlib import closing
from httpx import TimeoutException
from typing import Optional
import uvicorn

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Crear la aplicación FastAPI
app = FastAPI()

class CoachBot:
    def __init__(self):
        required_env_vars = {
            'TELEGRAM_TOKEN': os.getenv('TELEGRAM_TOKEN'),
            'SPREADSHEET_ID': os.getenv('SPREADSHEET_ID'),
            'ASSISTANT_ID': os.getenv('ASSISTANT_ID'),
            'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY')
        }

        missing_vars = [var for var, value in required_env_vars.items() if not value]
        if missing_vars:
            raise EnvironmentError(f"Faltan variables de entorno requeridas: {', '.join(missing_vars)}")

        self.TELEGRAM_TOKEN = required_env_vars['TELEGRAM_TOKEN']
        self.SPREADSHEET_ID = required_env_vars['SPREADSHEET_ID']
        self.assistant_id = required_env_vars['ASSISTANT_ID']
        self.credentials_path = '/etc/secrets/credentials.json'

        self.client = AsyncOpenAI(api_key=required_env_vars['OPENAI_API_KEY'])

        self.sheets_service = None
        self.started = False
        self.verified_users = {}
        self.conversation_history = {}
        self.user_threads = {}
        self.db_path = 'bot_data.db'

        self.telegram_app = Application.builder().token(self.TELEGRAM_TOKEN).build()

        self._init_db()
        self.setup_handlers()
        self._init_sheets()

    def setup_handlers(self):
        try:
            self.telegram_app.add_handler(CommandHandler("start", self.start_command))
            self.telegram_app.add_handler(CommandHandler("help", self.help_command))
            self.telegram_app.add_handler(MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self.route_message
            ))
            self.telegram_app.add_handler(MessageHandler(
                filters.VOICE,
                self.handle_voice_message
            ))
            logger.info("Handlers configurados correctamente")
        except Exception as e:
            logger.error(f"Error en setup_handlers: {e}")
            raise

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            help_text = "🤖 *Comandos disponibles:*\n/start - Iniciar\n/help - Ayuda"
            await update.message.reply_text(help_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error en help_command: {e}")
            await update.message.reply_text("❌ Error mostrando la ayuda. Intenta de nuevo.")

    def _init_db(self):
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        chat_id INTEGER PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        username TEXT
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS conversations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER,
                        role TEXT,
                        content TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (chat_id) REFERENCES users (chat_id)
                    )
                ''')
                conn.commit()
        except Exception as e:
            logger.error(f"Error inicializando base de datos: {e}")
            raise

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
       """Maneja el comando /start"""
    try:
        chat_id = update.message.chat.id
        if chat_id in self.verified_users:
            await update.message.reply_text("👋 ¡Bienvenido de nuevo! ¿En qué puedo ayudarte hoy?")
        else:
            await update.message.reply_text(
                "👋 ¡Hola! Por favor, proporciona tu email para comenzar.\n"
                "📧 Debe ser un email autorizado para usar el servicio."
            )
        logger.info(f"Comando /start ejecutado por chat_id: {chat_id}")
    except Exception as e:
        logger.error(f"Error en start_command: {e}")
        await update.message.reply_text("❌ Ocurrió un error. Por favor, intenta de nuevo.")

    async def send_message_to_assistant(self, chat_id: int, user_message: str) -> str:
    """Envía un mensaje al asistente de OpenAI y espera su respuesta."""
    try:
        thread_id = await self.get_or_create_thread(chat_id)
        if not thread_id:
            return "❌ No se pudo establecer conexión con el asistente."

        # Esperar a que no haya run activo
        timeout = 30  # Timeout más corto para verificar runs activos
        start_time = time.time()
        while time.time() - start_time <= timeout:
            active_runs = await self.client.beta.threads.runs.list(thread_id=thread_id)
            if not any(run.status == "in_progress" for run in active_runs.data):
                break
            await asyncio.sleep(2)
        else:
            raise TimeoutError("⏳ Timeout esperando runs activos.")

        # Enviar mensaje del usuario
        await self.client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message
        )

        # Iniciar ejecución
        run = await self.client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=self.assistant_id
        )

        # Esperar respuesta con timeout
        timeout = 60
        start_time = time.time()
        while time.time() - start_time <= timeout:
            run_status = await self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id
            )

            if run_status.status == "completed":
                break
            elif run_status.status in ["failed", "cancelled", "expired"]:
                logger.error(f"🚨 Run fallido con estado: {run_status.status}")
                return f"⚠️ Error: El asistente falló con estado {run_status.status}."

            await asyncio.sleep(2)
        else:
            raise TimeoutError("⏳ La consulta al asistente tomó demasiado tiempo.")

        # Obtener respuesta
        messages = await self.client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=1
        )

        if not messages.data or not messages.data[0].content:
            logger.warning("⚠️ OpenAI devolvió una respuesta vacía.")
            return "⚠️ No obtuve una respuesta válida del asistente. Intenta de nuevo."

        return messages.data[0].content[0].text.value.strip()

    except TimeoutError as e:
        logger.error(f"⏳ TimeoutError: {e}")
        return "⏳ El asistente tardó demasiado en responder. Intenta de nuevo más tarde."
    except Exception as e:
        logger.error(f"❌ Error procesando mensaje: {e}")
        return "⚠️ Ocurrió un error al procesar tu mensaje."

async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los mensajes de voz recibidos por el usuario"""
    try:
        chat_id = update.message.chat.id
        voice_file = await update.message.voice.get_file()
        voice_file_path = f"{chat_id}_voice_note.ogg"

        try:
            await voice_file.download(voice_file_path)

            recognizer = sr.Recognizer()
            with sr.AudioFile(voice_file_path) as source:
                audio = recognizer.record(source)

            user_message = recognizer.recognize_google(audio, language='es-ES')
            logger.info(f"Transcripción de voz: {user_message}")

            response = await self.process_text_message(update, context, user_message)
            await update.message.reply_text(response)

        finally:
            # Limpiar archivo temporal
            if os.path.exists(voice_file_path):
                os.remove(voice_file_path)

    except sr.UnknownValueError:
        await update.message.reply_text("⚠️ No pude entender la nota de voz. Intenta de nuevo.")
    except sr.RequestError as e:
        logger.error(f"Error en reconocimiento de voz: {e}")
        await update.message.reply_text("⚠️ Ocurrió un error con el servicio de reconocimiento de voz.")
    except Exception as e:
        logger.error(f"Error manejando mensaje de voz: {e}")
        await update.message.reply_text("⚠️ Ocurrió un error procesando la nota de voz.")

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str) -> str:
    """Procesa los mensajes de texto recibidos."""
    try:
        chat_id = update.message.chat.id
        
        if not user_message.strip():
            return "⚠️ No se recibió un mensaje válido."

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.TYPING
        )

        # Verificar consulta de productos
        if any(keyword in user_message.lower() for keyword in ['producto', 'comprar', 'precio', 'costo']):
            return await self.process_product_query(chat_id, user_message)

        # Usar asistente de OpenAI
        response = await self.send_message_to_assistant(chat_id, user_message)
        
        if not response.strip():
            return "⚠️ No se obtuvo una respuesta válida del asistente."

        # Guardar conversación
        self.save_conversation(chat_id, "user", user_message)
        self.save_conversation(chat_id, "assistant", response)
        
        return response

    except Exception as e:
        logger.error(f"❌ Error en process_text_message: {e}", exc_info=True)
        return "⚠️ Ocurrió un error al procesar tu mensaje."

async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los mensajes recibidos después de la verificación."""
    try:
        chat_id = update.message.chat.id
        user_message = update.message.text.strip()

        if not user_message:
            await update.message.reply_text("⚠️ No se recibió un mensaje válido.")
            return

        response = await self.process_text_message(update, context, user_message)
        if response.strip():
            await update.message.reply_text(response)
        else:
            await update.message.reply_text("⚠️ No se obtuvo una respuesta válida.")

    except Exception as e:
        logger.error(f"❌ Error inesperado en handle_message: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Ocurrió un error inesperado.")

async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verifica el email del usuario"""
    try:
        chat_id = update.message.chat.id
        user_email = update.message.text.strip().lower()
        username = update.message.from_user.username or "Unknown"

        if "@" not in user_email or "." not in user_email:
            await update.message.reply_text("❌ Por favor, proporciona un email válido.")
            return

        # Verificar si el usuario está en la lista blanca
        if not await self.is_user_whitelisted(user_email):
            await update.message.reply_text(
                "❌ Tu email no está en la lista autorizada. Contacta a soporte."
            )
            return

        thread_id = await self.get_or_create_thread(chat_id)
        if thread_id:
            self.user_threads[chat_id] = thread_id

        self.save_verified_user(chat_id, user_email, username)
        await update.message.reply_text("✅ Email validado. Ahora puedes hablar conmigo.")

    except Exception as e:
        logger.error(f"❌ Error verificando email para {chat_id}: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Ocurrió un error verificando tu email.")

async def is_user_whitelisted(self, email: str) -> bool:
    """
    Verifica si el correo electrónico del usuario está en la lista blanca.
    Args:
        email (str): Dirección de correo del usuario.
    Returns:
        bool: True si el correo está en la lista blanca, False en caso contrario.
    """
    if not email or "@" not in email:
        logger.warning("❌ Email inválido proporcionado para verificación en la whitelist.")
        return False

    try:
        result = self.sheets_service.spreadsheets().values().get(
            spreadsheetId=self.SPREADSHEET_ID,
            range='Usuarios!A:A'
        ).execute()

        values = result.get('values', [])
        if not values:
            logger.warning("⚠️ La whitelist está vacía.")
            return False

        whitelist = {row[0].strip().lower() for row in values if row and row[0]}
        return email.strip().lower() in whitelist

    except Exception as e:
        logger.error(f"❌ Error verificando la whitelist: {str(e)}", exc_info=True)
        return False

# Inicializar el bot con manejo adecuado de errores
def init_bot() -> Optional[CoachBot]:
    try:
        bot = CoachBot()
        logger.info("✅ Bot inicializado correctamente")
        return bot

    except Exception as e:
        logger.error(f"🚨 Error crítico inicializando el bot: {str(e)}", exc_info=True)
        return None

bot = init_bot()
if not bot:
    raise RuntimeError("❌ No se pudo inicializar el bot.")

@app.on_event("startup")
async def startup_event():
    """Manejador del evento de inicio de la aplicación."""
    try:
        await bot.async_init()
        logger.info("✅ Aplicación iniciada correctamente")
    except Exception as e:
        logger.error(f"🚨 Error al iniciar la aplicación: {str(e)}", exc_info=True)
        raise RuntimeError(f"❌ Fallo en el inicio de la aplicación: {str(e)}")

@app.post("/webhook")
async def webhook(request: Request):
    """Manejador del webhook de Telegram."""
    try:
        if not bot:
            raise HTTPException(status_code=500, detail="❌ El bot no está inicializado.")

        data = await request.json()
        if not data:
            raise HTTPException(status_code=400, detail="❌ Datos de solicitud no válidos.")

        update = Update.de_json(data, bot.telegram_app.bot)
        await bot.telegram_app.update_queue.put(update)
        return {"status": "ok"}

    except ValueError as e:
        logger.error(f"❌ JSON inválido en la solicitud del webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail="❌ Formato de JSON no válido.")

    except Exception as e:
        logger.error(f"❌ Error procesando webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn

    # Obtener el puerto del ambiente o usar un valor por defecto
    port = int(os.getenv("PORT", 8000))

    # Configurar uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False  # Deshabilitar reload en producción
    )
