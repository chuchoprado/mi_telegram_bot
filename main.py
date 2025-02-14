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
        self.verified_users = {}  # Diccionario para almacenar usuarios verificados
        self.conversation_history = {}  # Diccionario para almacenar el historial de conversaciones
        self.user_threads = {}  # Diccionario para almacenar los threads de los usuarios
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

    async def async_init(self):
        """Inicialización asíncrona"""
        await self.app.initialize()
        self.load_verified_users()  # Cargar usuarios validados
        if not self.started:
            self.started = True
            await self.app.start()

    def _setup_handlers(self):
        """Configura los manejadores de Telegram"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.route_message))

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Redirige mensajes según si el usuario ya fue verificado o no"""
        chat_id = update.message.chat.id

        if chat_id in self.verified_users:
            await self.handle_message(update, context)  # Usuario validado → Chatear
        else:
            await self.verify_email(update, context)  # Usuario no validado → Verificar email

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

    async def get_sheet_data(self, range):
        """Obtiene datos de Google Sheets"""
        if not self.sheets_service:
            return []
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.SPREADSHEET_ID,
                range=range
            ).execute()
            return result.get('values', [])
        except Exception as e:
            logger.error(f"❌ Error obteniendo datos de sheets: {e}")
            return []

    async def is_user_whitelisted(self, user_email):
        """Verifica si el usuario está en la lista blanca en Google Sheets"""
        email_range = 'C2:C2000'
        emails = await self.get_sheet_data(email_range)
        logger.info(f"📄 Emails obtenidos de Google Sheets: {emails}")

        for sublist in emails:
            if user_email in sublist:
                logger.info(f"✅ El correo {user_email} está en la lista blanca.")
                return True

        logger.info(f"❌ El correo {user_email} NO está en la lista blanca.")
        return False

    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verifica el correo electrónico proporcionado por el usuario"""
        user_email = update.message.text.strip()
        chat_id = update.message.chat.id
        username = update.message.from_user.username or "Desconocido"

        if chat_id in self.verified_users:
            await update.message.reply_text("✅ Ya estás validado. Puedes escribir libremente.")
            return

        if not await self.is_user_whitelisted(user_email):
            await update.message.reply_text(
                "❌ Tu correo no está en la lista blanca. Contacta a soporte."
            )
            return

        self.verified_users[chat_id] = user_email
        self.save_verified_user(chat_id, user_email, username)
        await self.update_telegram_user(chat_id, user_email, username)
        await update.message.reply_text("✅ Email validado. Ahora puedes hablar conmigo.")
        
        # Enviar un mensaje de bienvenida e invitar al usuario a interactuar con El Coach
        await self.send_welcome_message(chat_id, username)

    async def update_telegram_user(self, chat_id, email, username):
        """Actualiza el usuario de Telegram en la hoja de cálculo"""
        try:
            body = {
                "values": [[chat_id, username]]
            }
            email_range = 'C2:C2000'
            emails = await self.get_sheet_data(email_range)
            email_index = None
            for index, sublist in enumerate(emails):
                if email in sublist:
                    email_index = index + 2
                    break

            if email_index is None:
                logger.error(f"No se encontró el email {email}.")
                return

            range = f'whitelist!F{email_index}:G{email_index}'
            self.sheets_service.spreadsheets().values().update(
                spreadsheetId=self.SPREADSHEET_ID,
                range=range,
                valueInputOption='RAW',
                body=body
            ).execute()
        except Exception as e:
            logger.error(f"Error actualizando usuario: {e}")

    async def send_welcome_message(self, chat_id, username):
        """Envía un mensaje de bienvenida y crea un thread en OpenAI."""
        welcome_message = (
            f"¡Hola {username}! 🎉\n\n"
            "Bienvenido a El Coach. Estoy aquí para ayudarte con recomendaciones, ejercicios y más.\n\n"
            "Escríbeme cualquier pregunta y te responderé. 💪"
        )
        await self.get_or_create_thread(chat_id)  # Asegurar que el usuario tiene un thread
        await self.app.bot.send_message(chat_id=chat_id, text=welcome_message)
    
    async def get_or_create_thread(self, chat_id):
        """Obtiene un thread existente o crea uno nuevo en OpenAI Assistant."""
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]

        try:
            # Crear un nuevo thread correctamente en OpenAI Assistant
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "system", "content": "Iniciar nueva conversación"}]
            )
            thread_id = response['id']
            self.user_threads[chat_id] = thread_id  # Guardar el thread_id del usuario
            logger.info(f"🧵 Nuevo thread creado para {chat_id}: {thread_id}")
            return thread_id

        except Exception as e:
            logger.error(f"❌ Error creando thread en OpenAI para {chat_id}: {e}")
            return None

    async def send_message_to_assistant(self, chat_id, user_message):
        """Envía un mensaje al asistente en el thread correcto y obtiene la respuesta con el rol adecuado."""
        thread_id = await self.get_or_create_thread(chat_id)
        if not thread_id:
            return "❌ No se pudo establecer conexión con el asistente."

        try:
            # Enviar el mensaje del usuario al thread en OpenAI
            messages = self.conversation_history.get(chat_id, [])
            messages.append({"role": "user", "content": user_message})
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=messages
            )

            assistant_message = response['choices'][0]['message']['content']
            self.conversation_history[chat_id].append({"role": "assistant", "content": assistant_message})

            return assistant_message

        except Exception as e:
            logger.error(f"❌ Error enviando mensaje al asistente para {chat_id}: {e}")
            return "⚠️ Ocurrió un error obteniendo la respuesta."

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
        chat_id = update.effective_chat.id
        logger.info(f"📩 Mensaje recibido del usuario: {user_message}")

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

            # Obtener respuesta del Assistant en el thread correcto
            response = await self.send_message_to_assistant(chat_id, user_message)

            # Enviar la respuesta al usuario en Telegram
            await update.message.reply_text(response)

        except Exception as e:
            logger.error(f"❌ Error procesando mensaje con OpenAI: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error obteniendo la respuesta.")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /start"""
        logger.info(f"✅ Comando /start recibido de {update.message.chat.id}")
        await update.message.reply_text("¡Hola! Proporciona tu email para iniciar.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /help"""
        await update.message.reply_text(
            "📌 Puedes preguntarme sobre:\n"
            "- Recomendaciones de productos\n"
            "- Videos de ejercicios\n"
            "- Recursos disponibles\n"
            "- Instrucciones sobre el bot\n\n"
            "👉 Escribe un mensaje y te responderé."
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

# Crear instancia del bot
bot = CoachBot()

@app.on_event("startup")
async def startup_event():
    await bot.async_init()

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
