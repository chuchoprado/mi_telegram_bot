import os
import asyncio
import io
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
import openai  
import json
import logging

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levellevel)s - %(message)s',
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

        # Inicializar la aplicación de Telegram
        self.app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self._setup_handlers()
        self._init_sheets()

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
        query = f"SELECT * WHERE C = '{user_email}'"
        email_range = f"'{query}'!C2:C2000"
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
        await self.update_telegram_user(chat_id, user_email, username)
        await update.message.reply_text("✅ Email validado. Ahora puedes hablar conmigo.")

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
            user_message = update.message.text.strip()
            if not user_message:
                return

            processing_msg = await update.message.reply_text("🤖 Procesando tu solicitud...")

            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": user_message}],
                max_tokens=150
            )

            await processing_msg.delete()

            if response and response.choices:
                reply_text = response.choices[0].message.content.strip()
                await update.message.reply_text(reply_text)
            else:
                await update.message.reply_text("😕 No pude generar una respuesta en este momento.")

        except openai.OpenAIError as e:
            logger.error(f"❌ Error en OpenAI: {e}")
            await update.message.reply_text("❌ Hubo un problema con OpenAI.")

        except Exception as e:
            logger.error(f"❌ Error en handle_message: {e}")
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
