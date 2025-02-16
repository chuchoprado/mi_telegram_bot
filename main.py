import os
import asyncio
import io
import sqlite3
import json
import logging
import time
from fastapi import FastAPI, Request
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
import openai
import speech_recognition as sr

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Crear la aplicación FastAPI
app = FastAPI()

# Variable global para almacenar logs
logs = []

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
        self.conversation_history = {}
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

    def load_verified_users(self):
        """Carga usuarios validados desde la base de datos."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT chat_id, email FROM users')
        rows = cursor.fetchall()
        for chat_id, email in rows:
            self.verified_users[chat_id] = email
        conn.close()

    def save_verified_user(self, chat_id, email, username):
        """Guarda un usuario validado en la base de datos."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (chat_id, email, username)
            VALUES (?, ?, ?)
        ''', (chat_id, email, username))
        conn.commit()
        conn.close()

    def save_conversation(self, chat_id, role, content):
        """Guarda un mensaje de conversación en la base de datos."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO conversations (chat_id, role, content)
            VALUES (?, ?, ?)
        ''', (chat_id, role, content))
        conn.commit()
        conn.close()

    def _init_sheets(self):
        """Inicializa la conexión con Google Sheets"""
        try:
            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=['https://www.googleapis.com/auth/spreadsheets']
            )
            self.sheets_service = build('sheets', 'v4', credentials=credentials)
            logger.info("Conexión con Google Sheets inicializada correctamente.")
        except Exception as e:
            logger.error(f"Error inicializando Google Sheets: {e}")

    async def async_init(self):
        """Inicialización asíncrona del bot"""
        try:
            await self.app.initialize()
            self.load_verified_users()
            if not self.started:
                self.started = True
                await self.app.start()
            logger.info("Bot inicializado correctamente")
        except Exception as e:
            logger.error(f"Error en async_init: {e}")
            raise

    def _setup_handlers(self):
        """Configura los manejadores de comandos y mensajes"""
        try:
            self.app.add_handler(CommandHandler("start", self.start_command))
            self.app.add_handler(CommandHandler("help", self.help_command))
            self.app.add_handler(MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self.route_message
            ))
            self.app.add_handler(MessageHandler(
                filters.VOICE,
                self.handle_voice_message
            ))
            logger.info("Handlers configurados correctamente")
        except Exception as e:
            logger.error(f"Error en setup_handlers: {e}")
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

            await self.process_text_message(update, context, user_message)

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

    async def get_or_create_thread(self, chat_id):
        """Obtiene un thread existente o crea uno nuevo en OpenAI Assistant."""
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]

        try:
            # Crear un nuevo thread en OpenAI Assistant
            thread = openai.beta.threads.create()
            if not thread or not thread.id:
                raise Exception("OpenAI no devolvió un thread válido.")

            # Guardar el thread_id en el diccionario
            self.user_threads[chat_id] = thread.id
            logger.info(f"🧵 Nuevo thread creado para {chat_id}: {thread.id}")
            return thread.id

        except Exception as e:
            logger.error(f"❌ Error creando thread en OpenAI para {chat_id}: {e}")
            return None
        
    async def send_message_to_assistant(self, chat_id, user_message):
        """Envía un mensaje al asistente en el thread correcto y obtiene la respuesta."""
        thread_id = await self.get_or_create_thread(chat_id)
        if not thread_id:
            return "❌ No se pudo establecer conexión con el asistente."

        try:
            # Crear un mensaje en el thread
            message = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": user_message}]
            )

            # Obtener la respuesta del asistente
            assistant_message = message.choices[0].message['content'].strip()
            self.conversation_history.setdefault(chat_id, []).append({"role": "assistant", "content": assistant_message})

            return assistant_message

        except openai.error.OpenAIError as oe:
            logger.error(f"❌ Error en OpenAI para {chat_id}: {oe}")
            return "⚠️ Ocurrió un error obteniendo la respuesta de OpenAI."

        except Exception as e:
            logger.error(f"❌ Error enviando mensaje al asistente para {chat_id}: {e}")
            return "⚠️ Ocurrió un error obteniendo la respuesta."

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
        chat_id = update.effective_chat.id
        logger.info(f"📩 Mensaje recibido del usuario: {user_message}")

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

            response = await self.send_message_to_assistant(chat_id, user_message)
            await update.message.reply_text(response)

        except Exception as e:
            logger.error(f"❌ Error procesando mensaje con OpenAI: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error obteniendo la respuesta.")
            
    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# Manejo de errores mejorado para la creación del bot
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
        logger.error(f"Error en startup: {e}")
        raise

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook de Telegram"""
    try:
        data = await request.json()
        logger.info(f"📩 Webhook recibido: {json.dumps(data, indent=2)}")

        if "message" in data and "date" not in data["message"]:
            logger.error("❌ Error: 'date' no encontrado en el mensaje.")
            return {"status": "error", "message": "'date' no encontrado"}

        update = Update.de_json(data, bot.app.bot)
        await bot.app.update_queue.put(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ Error en webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def health_check():
    return {"status": "alive"}
