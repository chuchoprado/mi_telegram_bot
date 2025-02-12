import os
import asyncio
import io
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
import openai  # Importa la biblioteca de OpenAI
import json
import logging

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
        openai.api_key = os.getenv('OPENAI_API_KEY')  # Configura la clave de API de OpenAI
        self.sheets_service = None
        self.started = False  # Añadir bandera

        # Inicializar la aplicación de Telegram
        self.app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self._setup_handlers()
        self._init_sheets()

    async def async_init(self):
        """Inicialización asíncrona"""
        await self.app.initialize()
        if not self.started:  # Verificar si ya ha sido iniciado
            self.started = True
            await self.app.start()  # 🔥 Ahora sí arranca el bot

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /start"""
        logger.info(f"✅ Comando /start recibido de {update.message.chat.id}")

        await update.message.reply_text(
            "¡Hola! Bienvenido al Coach Meditahub, por favor proporciona tu email para acceder a tu asistente e iniciar tu reto de 21 días."
        )

    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verifica el correo electrónico proporcionado por el usuario"""
        user_email = update.message.text.strip()
        chat_id = update.message.chat.id
        username = update.message.from_user.username or "Desconocido"  # Manejo de usuario sin username

        if not await self.is_user_whitelisted(user_email):
            await update.message.reply_text(
                "❌ Lo siento, tu correo no está en nuestra Base de Datos. No puedes acceder al bot. Si deseas, contacta con nuestro equipo en www.meditahub.com."
            )
            return

        await self.update_telegram_user(chat_id, user_email, username)
        await update.message.reply_text(
            "✅ ¡Gracias! Tu correo ha sido verificado y ahora puedes usar las funcionalidades del Coach e iniciar tu reto de 21 días.\n"
            "Escríbeme cualquier pregunta para comenzar 😊"
        )

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

    def _setup_handlers(self):
        """Configura los manejadores de Telegram"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.verify_email))

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes recibidos"""
        try:
            user_message = update.message.text.strip()
            
            if not user_message:
                return

            processing_msg = await update.message.reply_text("🤖 Procesando tu solicitud, un momento...")

            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": user_message}],
                max_tokens=150
            )

            await processing_msg.delete()

            if response and "choices" in response and response["choices"]:
                reply_text = response["choices"][0]["message"]["content"].strip()
                await update.message.reply_text(reply_text)
            else:
                await update.message.reply_text("😕 Lo siento, no pude generar una respuesta en este momento.")

        except openai.error.OpenAIError as e:
            logger.error(f"❌ Error en OpenAI: {e}")
            await update.message.reply_text("❌ Hubo un problema al obtener una respuesta. Inténtalo de nuevo más tarde.")

        except Exception as e:
            logger.error(f"❌ Error en handle_message: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error inesperado. Inténtalo más tarde.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /help"""
        await update.message.reply_text(
            "Puedes preguntarme sobre:\n"
            "- Recomendaciones de productos\n"
            "- Videos de ejercicios\n"
            "- Recursos disponibles"
        )

    async def test_google_sheets_connection(self):
        """Prueba la conexión con Google Sheets"""
        try:
            test_range = 'A1:A1'
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.SPREADSHEET_ID,
                range=test_range
            ).execute()
            logger.info("Google Sheets API está funcionando correctamente.")
            return result.get('values', [])
        except Exception as e:
            logger.error(f"Error probando la conexión con Google Sheets: {e}")
            return []

# Crear instancia del bot
bot = CoachBot()

@app.on_event("startup")
async def startup_event():
    await bot.async_init()

@app.post("/webhook")
async def webhook(request: Request):
    """Endpoint para el webhook de Telegram"""
    try:
        data = await request.json()
        logger.info(f"📩 Webhook recibido: {json.dumps(data, indent=2)}")

        if "message" in data and "date" not in data["message"]:
            logger.error("❌ Error: 'date' no encontrado en el mensaje recibido.")
            return {"status": "error", "message": "'date' no encontrado en el mensaje"}

        update = Update.de_json(data, bot.app.bot)
        await bot.app.update_queue.put(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ Error en webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def health_check():
    return {"status": "alive"}

@app.get("/test_google_sheets")
async def test_google_sheets():
    """Endpoint para probar la conexión con Google Sheets"""
    result = await bot.test_google_sheets_connection()
    if result:
        return {"status": "Google Sheets API está funcionando correctamente", "data": result}
    else:
        return {"status": "Error probando la conexión con Google Sheets"}
