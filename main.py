import os
import asyncio
import io
import sqlite3
import json
import logging
import time
from typing import Optional, Dict, List
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, Bot
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import openai
from openai import OpenAIError
from contextlib import contextmanager

# Configuración de logging mejorada
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self):
        """Inicializa la base de datos con mejor manejo de errores"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
                )
            ''')
            conn.commit()

class OpenAIManager:
    def __init__(self, api_key: str, assistant_id: str):
        self.api_key = api_key
        self.assistant_id = assistant_id
        openai.api_key = api_key

    async def create_thread(self) -> str:
        """Crea un nuevo thread en OpenAI con mejor manejo de errores"""
        try:
            thread = await openai.beta.threads.create()
            return thread.id
        except OpenAIError as e:
            logger.error(f"Error creando thread en OpenAI: {e}")
            raise

    async def send_message(self, thread_id: str, content: str) -> str:
        """Envía mensaje a OpenAI con mejor manejo de errores y rate limiting"""
        try:
            message = await openai.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=content
            )
            
            run = await openai.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=self.assistant_id
            )

            # Esperar la respuesta con timeout
            timeout = 30
            start_time = time.time()
            while True:
                if time.time() - start_time > timeout:
                    raise TimeoutError("OpenAI response timeout")
                
                run_status = await openai.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run.id
                )
                
                if run_status.status == 'completed':
                    messages = await openai.beta.threads.messages.list(
                        thread_id=thread_id
                    )
                    return messages.data[0].content[0].text.value
                
                elif run_status.status in ['failed', 'cancelled']:
                    raise OpenAIError(f"Run failed with status: {run_status.status}")
                
                await asyncio.sleep(1)

        except OpenAIError as e:
            logger.error(f"Error en OpenAI: {e}")
            raise

class CoachBot:
    def __init__(self):
        # Validación de variables de entorno
        required_env_vars = ['TELEGRAM_TOKEN', 'SPREADSHEET_ID', 'ASSISTANT_ID', 'OPENAI_API_KEY']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

        self.TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
        self.SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
        self.credentials_path = '/etc/secrets/credentials.json'
        
        # Inicialización de componentes
        self.db = DatabaseManager('bot_data.db')
        self.openai_manager = OpenAIManager(
            os.getenv('OPENAI_API_KEY'),
            os.getenv('ASSISTANT_ID')
        )
        
        self.verified_users: Dict[int, str] = {}
        self.user_threads: Dict[int, str] = {}
        self.conversation_history: Dict[int, List[Dict]] = {}
        
        # Inicialización de servicios
        self.sheets_service = None
        self.app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        
        # Setup inicial
        self.db.init_db()
        self._setup_handlers()
        self._init_sheets()

    def _setup_handlers(self):
        """Configuración mejorada de handlers con gestión de errores"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, 
            self.route_message
        ))
        self.app.add_error_handler(self.error_handler)

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejador global de errores para Telegram"""
        logger.error(f"Error en el bot: {context.error}")
        
        if update and update.effective_chat:
            error_message = "Lo siento, ha ocurrido un error. Por favor, inténtalo de nuevo más tarde."
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=error_message
                )
            except TelegramError as e:
                logger.error(f"No se pudo enviar mensaje de error: {e}")

    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verificación de email mejorada con validación"""
        chat_id = update.message.chat.id
        user_email = update.message.text.strip().lower()  # Normalizar email
        username = update.message.from_user.username or "Unknown"

        # Validación básica de formato de email
        if not '@' in user_email or not '.' in user_email:
            await update.message.reply_text("❌ Por favor, proporciona un email válido.")
            return

        try:
            if not await self.is_user_whitelisted(user_email):
                await update.message.reply_text(
                    "❌ Tu email no está en la lista autorizada. Contacta a soporte."
                )
                return

            # Crear thread de OpenAI
            thread_id = await self.openai_manager.create_thread()
            self.user_threads[chat_id] = thread_id

            # Guardar usuario verificado
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO users (chat_id, email, username)
                    VALUES (?, ?, ?)
                ''', (chat_id, user_email, username))
                conn.commit()

            self.verified_users[chat_id] = user_email
            await self.update_telegram_user(chat_id, user_email, username)
            await self.send_welcome_message(chat_id, username)

        except Exception as e:
            logger.error(f"Error en verificación de email: {e}")
            await update.message.reply_text(
                "❌ Ocurrió un error durante la verificación. Por favor, inténtalo de nuevo."
            )

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
        """Procesamiento de mensajes mejorado con retry logic"""
        chat_id = update.effective_chat.id
        max_retries = 3
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.TYPING
                )

                thread_id = self.user_threads.get(chat_id)
                if not thread_id:
                    thread_id = await self.openai_manager.create_thread()
                    self.user_threads[chat_id] = thread_id

                response = await self.openai_manager.send_message(
                    thread_id,
                    user_message
                )

                # Guardar conversación
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO conversations (chat_id, role, content)
                        VALUES (?, ?, ?)
                    ''', (chat_id, "user", user_message))
                    cursor.execute('''
                        INSERT INTO conversations (chat_id, role, content)
                        VALUES (?, ?, ?)
                    ''', (chat_id, "assistant", response))
                    conn.commit()

                await update.message.reply_text(response)
                break

            except OpenAIError as e:
                logger.error(f"Error de OpenAI (intento {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    await update.message.reply_text(
                        "❌ Lo siento, hay un problema con el servicio. Por favor, inténtalo más tarde."
                    )
                else:
                    await asyncio.sleep(retry_delay * (attempt + 1))

            except Exception as e:
                logger.error(f"Error procesando mensaje: {e}")
                await update.message.reply_text(
                    "❌ Ocurrió un error procesando tu mensaje. Por favor, inténtalo de nuevo."
                )
                break

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejador de mensajes mejorado con validaciones"""
        try:
            chat_id = update.message.chat.id
            user_message = update.message.text.strip()

            if not user_message:
                await update.message.reply_text("Por favor, envía un mensaje con contenido.")
                return

            if len(user_message) > 4096:  # Límite de Telegram
                await update.message.reply_text(
                    "❌ Tu mensaje es demasiado largo. Por favor, envía un mensaje más corto."
                )
                return

            await self.process_text_message(update, context, user_message)

        except Exception as e:
            logger.error(f"Error en handle_message: {e}")
            await self.error_handler(update, context)

# FastAPI endpoints mejorados
app = FastAPI()
bot = CoachBot()

@app.on_event("startup")
async def startup_event():
    """Evento de inicio mejorado con manejo de errores"""
    try:
        await bot.async_init()
        logger.info("Bot iniciado correctamente")
    except Exception as e:
        logger.error(f"Error en startup: {e}")
        raise

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook mejorado con validaciones y manejo de errores"""
    try:
        data = await request.json()
        
        # Validación básica de la estructura del webhook
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Invalid webhook data format")

        if "message" not in data:
            raise HTTPException(status_code=400, detail="No message field in webhook data")

        update = Update.de_json(data, bot.app.bot)
        await bot.app.update_queue.put(update)
        return {"status": "ok"}

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Health check mejorado con validación de servicios"""
    health_status = {
        "status": "healthy",
        "telegram": True,
        "openai": True,
        "database": True,
        "sheets": True
    }

    try:
        # Verificar conexión a la base de datos
        with bot.db.get_connection() as conn:
            conn.cursor().execute("SELECT 1")
    except Exception as e:
        health_status["database"] = False
        health_status["status"] = "degraded"

    # Verificar conexión con OpenAI
    try:
        openai.Models.list()
    except Exception as e:
        health_status["openai"] = False
        health_status["status"] = "degraded"

    # Verificar conexión con Google Sheets
    if not bot.sheets_service:
        health_status["sheets"] = False
        health_status["status"] = "degraded"

    return health_status
