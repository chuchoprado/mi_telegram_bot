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

    def _init_db(self):
        """Inicializar la base de datos y crear las tablas necesarias."""
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
                    FOREIGN KEY (chat_id) REFERENCES users (chat_id)
                )
            ''')
            conn.commit()

    async def get_or_create_thread(self, chat_id):
        """Obtiene un thread existente o crea uno nuevo en OpenAI Assistant."""
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]

        try:
            thread = await self.client.beta.threads.create()
            self.user_threads[chat_id] = thread.id
            return thread.id

        except Exception as e:
            logger.error(f"❌ Error creando thread para {chat_id}: {e}")
            return None

    async def send_message_to_assistant(self, chat_id: int, user_message: str) -> str:
        try:
            thread_id = await self.get_or_create_thread(chat_id)
            if not thread_id:
                return "❌ No se pudo establecer conexión con el asistente."

            await self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=user_message
            )

            run = await self.client.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=self.assistant_id
            )

            while True:
                run_status = await self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run.id
                )

                if run_status.status == 'completed':
                    break
                elif run_status.status in ['failed', 'cancelled', 'expired']:
                    raise Exception(f"Run failed with status: {run_status.status}")

                await asyncio.sleep(1)

            messages = await self.client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            )

            if not messages.data or not messages.data[0].content:
                raise ValueError("La respuesta del asistente está vacía")

            assistant_message = messages.data[0].content[0].text.value

            self.conversation_history.setdefault(chat_id, []).append({
                "role": "assistant",
                "content": assistant_message
            })

            return assistant_message

        except Exception as e:
            logger.error(f"❌ Error procesando mensaje: {e}")
            return "⚠️ Ocurrió un error al procesar tu mensaje."

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
        """Procesa mensajes de texto del usuario."""
        chat_id = update.effective_chat.id
        logger.info(f"📩 Mensaje recibido del usuario {chat_id}: {user_message}")

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

            response = await self.send_message_to_assistant(chat_id, user_message)

            if response is None or not response.strip():
                raise ValueError("La respuesta del asistente está vacía")

            await update.message.reply_text(response)

        except openai.OpenAIError as e:
            logger.error(f"❌ Error en OpenAI: {e}")
            await update.message.reply_text("❌ Hubo un problema con OpenAI.")

        except Exception as e:
            logger.error(f"❌ Error procesando mensaje: {e}")
            await update.message.reply_text(
                "⚠️ Ocurrió un error al procesar tu mensaje. Por favor, intenta de nuevo."
            )

    async def process_product_query(self, chat_id: int, query: str) -> str:
        try:
            products = await self.fetch_products(query)
            if "error" in products:
                return "⚠️ Ocurrió un error al consultar los productos."

            product_list = "\n".join([f"- {product['titulo']}: {product['descripcion']} (link: {product['link']})" for product in products.get("data", [])])
            if not product_list:
                return "⚠️ No se encontraron productos."

            return f"🔍 Productos recomendados:\n{product_list}"
        except Exception as e:
            logger.error(f"❌ Error procesando consulta de productos: {e}")
            return "⚠️ Ocurrió un error al procesar tu consulta de productos."

    async def fetch_products(self, query):
        url = "https://script.google.com/macros/s/AKfycbwUieYWmu5pTzHUBnSnyrLGo-SROiiNFvufWdn5qm7urOamB65cqQkbQrkj05Xf3N3N_g/exec"
        params = {"query": query}
        
        logger.info(f"Consultando Google Sheets con: {params}")

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, params=params, follow_redirects=True)

            if response.status_code != 200:
                raise Exception(f"Error en Google Sheets API: {response.status_code}")

            logger.info(f"Respuesta de Google Sheets: {response.text}")
            return response.json()

        except httpx.TimeoutException:
            logger.error("⏳ La API de Google Sheets tardó demasiado en responder.")
            return {"error": "⏳ La consulta tardó demasiado. Inténtalo más tarde."}

        except Exception as e:
            logger.error(f"❌ Error consultando Google Sheets: {e}")
            return {"error": "Error consultando Google Sheets"}

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

    def load_verified_users(self):
        """Carga usuarios validados desde la base de datos."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT chat_id, email FROM users')
            rows = cursor.fetchall()
            for chat_id, email in rows:
                self.verified_users[chat_id] = email

    def save_verified_user(self, chat_id, email, username):
        """Guarda un usuario validado en la base de datos."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO users (chat_id, email, username)
                VALUES (?, ?, ?)
            ''', (chat_id, email, username))
            conn.commit()

    def save_conversation(self, chat_id, role, content):
        """Guarda un mensaje de conversación en la base de datos."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO conversations (chat_id, role, content)
                VALUES (?, ?, ?)
            ''', (chat_id, role, content))
            conn.commit()

    def _init_sheets(self):
        """Inicializa la conexión con Google Sheets"""
        try:
            if not os.path.exists(self.credentials_path):
                logger.error(f"Archivo de credenciales no encontrado en: {self.credentials_path}")
                return False

            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
            )

            self.sheets_service = build('sheets', 'v4', credentials=credentials)

            try:
                self.sheets_service.spreadsheets().get(
                    spreadsheetId=self.SPREADSHEET_ID
                ).execute()
                logger.info("Conexión con Google Sheets inicializada correctamente.")
                return True
            except Exception as e:
                logger.error(f"Error accediendo al spreadsheet: {e}")
                return False

        except Exception as e:
            logger.error(f"Error inicializando Google Sheets: {e}")
            return False

    async def async_init(self):
        """Inicialización asíncrona del bot"""
        try:
            await self.telegram_app.initialize()
            self.load_verified_users()
            if not self.started:
                self.started = True
                await self.telegram_app.start()
            logger.info("Bot inicializado correctamente")
        except Exception as e:
            logger.error(f"Error en async_init: {e}")
            raise

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /start"""
        try:
            chat_id = update.message.chat.id
            if chat_id in self.verified_users:
                await update.message.reply_text(
                    "👋 ¡Bienvenido de nuevo! ¿En qué puedo ayudarte hoy?"
                )
            else:
                await update.message.reply_text(
                    "👋 ¡Hola! Por favor, proporciona tu email para comenzar.\n\n"
                    "📧 Debe ser un email autorizado para usar el servicio."
                )
            logger.info(f"Comando /start ejecutado por chat_id: {chat_id}")
        except Exception as e:
            logger.error(f"Error en start_command: {e}")
            await update.message.reply_text("❌ Ocurrió un error. Por favor, intenta de nuevo.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /help"""
        try:
            help_text = (
                "🤖 *Comandos disponibles:*\n\n"
                "/start - Iniciar o reiniciar el bot\n"
                "/help - Mostrar este mensaje de ayuda\n\n"
                "📝 *Funcionalidades:*\n"
                "- Consultas sobre ejercicios\n"
                "- Recomendaciones personalizadas\n"
                "- Seguimiento de progreso\n"
                "- Recursos y videos\n\n"
                "✨ Simplemente escribe tu pregunta y te responderé."
            )
            await update.message.reply_text(help_text, parse_mode='Markdown')
            logger.info(f"Comando /help ejecutado por chat_id: {update.message.chat.id}")
        except Exception as e:
            logger.error(f"Error en help_command: {e}")
            await update.message.reply_text("❌ Error mostrando la ayuda. Intenta de nuevo.")

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enruta los mensajes según el estado de verificación del usuario"""
        try:
            chat_id = update.message.chat.id
            if chat_id in self.verified_users:
                await self.handle_message(update, context)
            else:
                await self.verify_email(update, context)
        except Exception as e:
            logger.error(f"Error en route_message: {e}")
            await update.message.reply_text(
                "❌ Ocurrió un error procesando tu mensaje. Por favor, intenta de nuevo."
            )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes recibidos después de la verificación"""
        try:
            chat_id = update.message.chat.id
            user_message = update.message.text.strip()
            if not user_message:
                return

            if "producto" in user_message.lower():
                response = await self.process_product_query(chat_id, user_message)
            else:
                response = await self.process_text_message(update, context, user_message)

            if response is None or not response.strip():
                raise ValueError("La respuesta del asistente está vacía")

            await update.message.reply_text(response)

        except openai.OpenAIError as e:
            logger.error(f"❌ Error en OpenAI: {e}")
            await update.message.reply_text("❌ Hubo un problema con OpenAI.")

        except Exception as e:
            logger.error(f"⚠️ Error inesperado: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error inesperado. Inténtalo más tarde.")

    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes de voz"""
        try:
            chat_id = update.message.chat.id
            voice_file = await update.message.voice.get_file()
            voice_file_path = f"{chat_id}_voice_note.ogg"
            await voice_file.download(voice_file_path)

            recognizer = sr.Recognizer()
            with sr.AudioFile(voice_file_path) as source:
                audio = recognizer.record(source)

            try:
                user_message = recognizer.recognize_google(audio, language='es-ES')
                logger.info(f"Transcripción de voz: {user_message}")
                await self.process_text_message(update, context, user_message)
            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ No pude entender la nota de voz. Intenta de nuevo.")
            except sr.RequestError as e:
                logger.error(f"Error en el servicio de reconocimiento de voz de Google: {e}")
                await update.message.reply_text("⚠️ Ocurrió un error con el servicio de reconocimiento de voz.")

        except Exception as e:
            logger.error(f"Error manejando mensaje de voz: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error procesando la nota de voz.")

    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verifica el email del usuario"""
        chat_id = update.message.chat.id
        user_email = update.message.text.strip().lower()
        username = update.message.from_user.username or "Unknown"

        if not '@' in user_email or not '.' in user_email:
            await update.message.reply_text("❌ Por favor, proporciona un email válido.")
            return

        try:
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
        logger.error(f"❌ Error al iniciar la aplicación: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook de Telegram"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot.telegram_app.bot
        
