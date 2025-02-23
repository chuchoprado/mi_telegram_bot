import os
import asyncio
import httpx
import io
import sqlite3
import json
import logging
import openai
import time
from fastapi import FastAPI, Request
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
        # Validar variables de entorno críticas
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

        # Inicializar cliente AsyncOpenAI
        self.client = AsyncOpenAI(api_key=required_env_vars['OPENAI_API_KEY'])

        self.sheets_service = None
        self.started = False
        self.verified_users = {}
        self.conversation_history = {}
        self.user_threads = {}
        self.db_path = 'bot_data.db'

        # Inicializar la aplicación de Telegram
        self.telegram_app = Application.builder().token(self.TELEGRAM_TOKEN).build()

        self._init_db()
        self.setup_handlers()
        self._init_sheets()

    def setup_handlers(self):
        """Configura los manejadores de comandos y mensajes"""
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

    def _init_db(self):
        """Inicializar la base de datos y crear las tablas necesarias."""
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

    async def get_or_create_thread(self, chat_id):
        """Obtiene un thread existente o crea uno nuevo en OpenAI Assistant."""
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]

        try:
            thread = await self.client.beta.threads.create()
            self.user_threads[chat_id] = thread.id
            return thread.id
        except Exception as e:
            logger.error(f"Error creando thread para {chat_id}: {e}")
            return None

    async def send_message_to_assistant(self, chat_id: int, user_message: str) -> str:
        """Envía un mensaje al asistente de OpenAI y espera su respuesta."""
        try:
            thread_id = await self.get_or_create_thread(chat_id)
            if not thread_id:
                return "❌ No se pudo establecer conexión con el asistente."

            # Esperar a que no haya `run` activo
            timeout = 30  # Timeout más corto para verificar runs activos
            start_time = time.time()
            while True:
                if time.time() - start_time > timeout:
                    raise TimeoutError("Timeout esperando runs activos")
                
                active_runs = await self.client.beta.threads.runs.list(thread_id=thread_id)
                if not any(run.status == "in_progress" for run in active_runs.data):
                    break
                await asyncio.sleep(2)

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
            while True:
                if time.time() - start_time > timeout:
                    raise TimeoutError("La consulta al asistente tomó demasiado tiempo")

                run_status = await self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run.id
                )

                if run_status.status == "completed":
                    break
                elif run_status.status in ["failed", "cancelled", "expired"]:
                    raise Exception(f"Run fallido con estado: {run_status.status}")

                await asyncio.sleep(2)

            # Obtener respuesta
            messages = await self.client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            )

            if not messages.data or not messages.data[0].content:
                return "⚠️ No obtuve una respuesta válida del asistente."

            return messages.data[0].content[0].text.value.strip()

        except TimeoutError as e:
            logger.error(f"TimeoutError: {e}")
            return "⏳ El asistente tardó demasiado en responder. Intenta de nuevo más tarde."
        except Exception as e:
            logger.error(f"Error procesando mensaje: {e}")
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
            
            await context.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING
            )

            # Verificar consulta de productos
            if any(keyword in user_message.lower() for keyword in ['producto', 'comprar', 'precio', 'costo']):
                return await self.process_product_query(chat_id, user_message)

            # Usar asistente de OpenAI
            response = await self.send_message_to_assistant(chat_id, user_message)
            
            # Guardar conversación
            self.save_conversation(chat_id, "user", user_message)
            self.save_conversation(chat_id, "assistant", response)
            
            return response

        except Exception as e:
            logger.error(f"Error en process_text_message: {e}")
            return "⚠️ Ocurrió un error al procesar tu mensaje."

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes recibidos después de la verificación."""
        try:
            chat_id = update.message.chat.id
            user_message = update.message.text.strip()
            if not user_message:
                return

            response = await self.process_text_message(update, context, user_message)
            if response:
                await update.message.reply_text(response)

        except Exception as e:
            logger.error(f"Error inesperado: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error inesperado.")

    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verifica el email del usuario"""
        chat_id = update.message.chat.id
        user_email = update.message.text.strip().lower()
        username = update.message.from_user.username or "Unknown"

        if "@" not in user_email or "." not in user_email:
            await update.message.reply_text("❌ Por favor, proporciona un email válido.")
            return

        try:
            # Verificar si el usuario está en la lista blanca
            if not await self.is_user_whitelisted(user_email):
                await update.message.reply_text(
                    "❌ Tu email no está en la lista autorizada. Contacta a soporte."
                )
                return

            thread_id = await self.get_or_create_thread(chat_id)
            self.user_threads[chat_id] = thread_id

            self.save_verified_user(chat_id, user_email, username)
            await update.message.reply_text("✅ Email validado. Ahora puedes hablar conmigo.")

        except Exception as e:
            logger.error(f"❌ Error verificando email para {chat_id}: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error verificando tu email.")

    async def is_user_whitelisted(self, email: str) -> bool:
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.SPREADSHEET_ID,
                range='Usuarios!A:A'
            ).execute()

            values = result.get('values', [])
            whitelist = [email[0].lower() for email in values if email]

            return email.lower() in whitelist

        except Exception as e:
            logger.error(f"Error verificando whitelist: {e}")
            return False

# Instanciar el bot
try:
    bot = CoachBot()
except Exception as e:
    logger.error(f"Error crítico inicializando el bot: {e}")
    raise

@app.on_event("startup")
async def startup_event():
    """Evento de inicio de la aplicación"""
    try:
        await bot.async_init()
        logger.info("Aplicación iniciada correctamente")
    except Exception as e:
        logger.error(f"Error al iniciar la aplicación: {e}")
        raise

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook de Telegram"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot.telegram_app.bot)
        await bot.telegram_app.update_queue.put(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error procesando webhook: {e}")
        return {"status": "error", "message": str(e)}
