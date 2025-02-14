import os
import asyncio
import io
import sqlite3
from fastapi import FastAPI, Request
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
import openai
import json
import logging
from tenacity import retry, stop_after_attempt, wait_fixed

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
        self.TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
        self.SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
        self.assistant_id = os.getenv('ASSISTANT_ID')
        self.credentials_path = '/etc/secrets/credentials.json'
        openai.api_key = os.getenv('OPENAI_API_KEY')
        self.sheets_service = None
        self.started = False
        self.verified_users = {}
        self.user_threads = {}
        self.db_path = 'bot_data.db'
        self._init_db()

        # Inicializar la aplicación de Telegram
        self.app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self._setup_handlers()
        self._init_sheets()

    def _init_db(self):
        """Inicializa la base de datos SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                email TEXT,
                username TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id INTEGER,
                role TEXT,
                content TEXT
            )
        ''')
        conn.commit()
        conn.close()

    async def async_init(self):
        """Inicialización asíncrona"""
        await self.app.initialize()
        if not self.started:
            self.started = True
            await self.app.start()

    def _setup_handlers(self):
        """Configura los manejadores de Telegram"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.route_message))

    async def get_or_create_thread(self, chat_id):
        """Obtiene un thread existente o crea uno nuevo para cada usuario."""
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]

        try:
            thread = openai.beta.threads.create()
            thread_id = thread.id
            self.user_threads[chat_id] = thread_id
            logger.info(f"🧵 Nuevo thread creado para {chat_id}: {thread_id}")
            return thread_id
        except Exception as e:
            logger.error(f"❌ Error creando thread en OpenAI: {e}")
            return None

    async def send_message_to_assistant(self, chat_id, user_message):
        """Envía un mensaje al asistente en el thread correcto y obtiene la respuesta."""
        thread_id = await self.get_or_create_thread(chat_id)
        if not thread_id:
            return "❌ No se pudo establecer conexión con el asistente."

        try:
            openai.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=user_message
            )

            run = openai.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=self.assistant_id
            )

            while True:
                run_status = openai.beta.threads.runs.retrieve(run.id, thread_id=thread_id)
                if run_status.status == "completed":
                    break
                await asyncio.sleep(1)

            messages = openai.beta.threads.messages.list(thread_id=thread_id)
            last_message = messages.data[0]
            return last_message.content[0].text.value

        except Exception as e:
            logger.error(f"❌ Error enviando mensaje al asistente: {e}")
            return "⚠️ Ocurrió un error obteniendo la respuesta."

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        user_message = update.message.text.strip()

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            response = await self.send_message_to_assistant(chat_id, user_message)
            await update.message.reply_text(response)

        except openai.OpenAIError as e:
            logger.error(f"❌ Error en OpenAI: {e}")
            await update.message.reply_text("❌ Hubo un problema con OpenAI.")

        except Exception as e:
            logger.error(f"❌ Error en handle_message: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error inesperado. Inténtalo más tarde.")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"✅ Comando /start recibido de {update.message.chat.id}")
        await update.message.reply_text("¡Hola! Estoy listo para conversar contigo.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📌 Puedes preguntarme sobre:\n"
            "- Recomendaciones de productos\n"
            "- Videos de ejercicios\n"
            "- Recursos disponibles\n"
            "- Instrucciones sobre el bot\n\n"
            "👉 Escribe un mensaje y te responderé."
        )

# Crear instancia del bot
bot = CoachBot()

@app.on_event("startup")
async def startup_event():
    await bot.async_init()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"📩 Webhook recibido: {json.dumps(data, indent=2)}")
        update = Update.de_json(data, bot.app.bot)
        await bot.app.update_queue.put(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ Error en webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def health_check():
    return {"status": "alive"}
