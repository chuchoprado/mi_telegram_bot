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

        # Inicialización asíncrona sin bloquear el event loop
        asyncio.create_task(self.async_init())

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
        user_email = update.message.text
        chat_id = update.message.chat.id
        username = update.message.from_user.username  # Obtener el username de Telegram

        if not await self.is_user_whitelisted(user_email):
            await update.message.reply_text(
                "Lo siento, tu correo no está en nuestra Base de Datos. No puedes acceder al bot, si deseas contacta con nuestro equipo en www.meditahub.com."
            )
            return

        await self.update_telegram_user(chat_id, user_email, username)
        await update.message.reply_text(
            "¡Gracias! Tu correo ha sido verificado y ahora puedes usar las funcionalidades del Coach e iniciar tu reto de 21 días."
        )

        # Continuar con el asistente de OpenAI
        await self.handle_message(update, context)

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
            logger.error(f"Error obteniendo datos de sheets: {e}")
            return []

    async def is_user_whitelisted(self, user_email):
        """Verifica si el usuario está en la lista blanca"""
        email_range = 'C2:C2000'  # Cambiar el rango para buscar en toda la hoja
        emails = await self.get_sheet_data(email_range)
        logger.info(f"Emails obtenidos de Google Sheets: {emails}")
        for sublist in emails:
            if user_email in sublist:
                logger.info(f"El correo {user_email} está en la lista blanca.")
                return True
        logger.info(f"El correo {user_email} NO está en la lista blanca.")
        return False

    async def update_telegram_user(self, chat_id, email, username):
        """Actualiza el usuario de Telegram en la hoja de cálculo"""
        try:
            body = {
                "values": [[chat_id, username]]
            }
            # Busca el índice del email en la hoja de cálculo
            email_range = 'C2:C2000'
            emails = await self.get_sheet_data(email_range)
            email_index = None
            for index, sublist in enumerate(emails):
                if email in sublist:
                    email_index = index + 2  # +2 porque Google Sheets es 1-based y hay un header
                    break

            if email_index is None:
                logger.error(f"No se encontró el email {email} en la lista blanca.")
                return

            range = f'whitelist!F{email_index}:G{email_index}'  # Actualiza las celdas correspondientes al email
            self.sheets_service.spreadsheets().values().update(
                spreadsheetId=self.SPREADSHEET_ID,
                range=range,
                valueInputOption='RAW',
                body=body
            ).execute()
        except Exception as e:
            logger.error(f"Error actualizando usuario de Telegram: {e}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /help"""
        await update.message.reply_text(
            "Puedes preguntarme sobre:\n"
            "- Recomendaciones de productos\n"
            "- Videos de ejercicios\n"
            "- Recursos disponibles"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes recibidos"""
        try:
            # Mensaje de procesamiento
            processing_msg = await update.message.reply_text(
                "Procesando tu solicitud..."
            )

            # Generar una respuesta usando OpenAI
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": update.message.text}
                ]
            )

            await processing_msg.delete()
            
            # Enviar respuesta
            await update.message.reply_text(response.choices[0].message.content.strip())

        except Exception as e:
            logger.error(f"Error en handle_message: {e}")
            await update.message.reply_text(
                "Lo siento, ocurrió un error. Por favor intenta más tarde."
            )

    async def test_google_sheets_connection(self):
        """Prueba la conexión con Google Sheets"""
        try:
            test_range = 'A1:A1'  # Rango de prueba
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
        logger.info(f"📩 Webhook recibido: {json.dumps(data, indent=2)}")  # 🔥 Muestra todo el JSON recibido

        # Validar si el update tiene un campo 'date' antes de procesarlo
        if "message" in data and "date" not in data["message"]:
            logger.error("❌ Error: 'date' no encontrado en el mensaje recibido.")
            return {"status": "error", "message": "'date' no encontrado en el mensaje"}

        update = Update.de_json(data, bot.app.bot)
        await bot.app.update_queue.put(update)  # 🔥 Ahora los mensajes se procesan correctamente
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ Error en webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def health_check():
    """Endpoint de verificación"""
    return {"status": "alive"}

@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
async def health_check():
    """Endpoint de verificación de estado"""
    return {"status": "alive"}

@app.get("/test_google_sheets")
async def test_google_sheets():
    """Endpoint para probar la conexión con Google Sheets"""
    result = await bot.test_google_sheets_connection()
    if result:
        return {"status": "Google Sheets API está funcionando correctamente", "data": result}
    else:
        return {"status": "Error probando la conexión con Google Sheets"}
