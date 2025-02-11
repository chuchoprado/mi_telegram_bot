import os
import asyncio
import io
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openai import OpenAI
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
        self.openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        self.sheets_service = None
        self.assistant = None
        
        # Inicializar la aplicación de Telegram
        self.app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self._setup_handlers()
        self._init_sheets()

        # Inicialización asíncrona sin bloquear el event loop
        asyncio.create_task(self.async_init())

    async def async_init(self):
        """Inicialización asíncrona"""
        await self.app.initialize()

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /start"""
        logger.info(f"✅ Comando /start recibido de {update.message.chat.id}")

        await update.message.reply_text(
            "¡Hola! Soy El Coach Bot. ¿En qué puedo ayudarte hoy?"
        )

    def _init_sheets(self):
        """Inicializa la conexión con Google Sheets"""
        try:
            creds_dict = json.loads(os.getenv('GOOGLE_CREDENTIALS', '{}'))
            credentials = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
            )
            self.sheets_service = build('sheets', 'v4', credentials=credentials)
        except Exception as e:
            logger.error(f"Error inicializando Google Sheets: {e}")

    def _setup_handlers(self):
        """Configura los manejadores de Telegram"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def get_sheet_data(self):
        """Obtiene datos de Google Sheets"""
        if not self.sheets_service:
            return []
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.SPREADSHEET_ID,
                range='A1:Z1000'
            ).execute()
            return result.get('values', [])
        except Exception as e:
            logger.error(f"Error obteniendo datos de sheets: {e}")
            return []

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

            # Inicializar asistente si no existe
            if not self.assistant:
                # Obtener datos de sheets
                data = await self.get_sheet_data()
                
                # Crear archivo para el asistente con un objeto IO compatible con OpenAI
                data_str = json.dumps({"data": data})
                file = self.openai_client.files.create(
                    file=("data.json", io.StringIO(data_str)),
                    purpose='assistants'
                )
                
                # Crear asistente
                self.assistant = self.openai_client.beta.assistants.create(
                    name="Coach Assistant",
                    instructions="Asistente para recomendaciones basadas en la base de datos.",
                    model="gpt-4-turbo-preview",
                    tools=[{"type": "retrieval"}],
                    file_ids=[file.id]
                )

            # Crear thread
            thread = self.openai_client.beta.threads.create()
            
            # Añadir mensaje del usuario
            self.openai_client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content=update.message.text
            )
            
            # Ejecutar el asistente
            run = self.openai_client.beta.threads.runs.create(
                thread_id=thread.id,
                assistant_id=self.assistant.id
            )
            
            # Esperar respuesta
            while True:
                run_status = self.openai_client.beta.threads.runs.retrieve(
                    thread_id=thread.id,
                    run_id=run.id
                )
                if run_status.status == 'completed':
                    break
                elif run_status.status in ['failed', 'cancelled', 'expired']:
                    await processing_msg.delete()
                    await update.message.reply_text(
                        "Lo siento, hubo un error. Por favor intenta nuevamente."
                    )
                    return

            # Obtener la respuesta
            messages = self.openai_client.beta.threads.messages.list(
                thread_id=thread.id
            )
            
            await processing_msg.delete()
            
            # Enviar respuesta
            for message in messages.data:
                if message.role == "assistant":
                    await update.message.reply_text(message.content[0].text.value)
                    return

        except Exception as e:
            logger.error(f"Error en handle_message: {e}")
            await update.message.reply_text(
                "Lo siento, ocurrió un error. Por favor intenta más tarde."
            )

# Crear instancia del bot
bot = CoachBot()

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

@app.get("/logs")
async def get_logs():
    """Devuelve los últimos logs"""
    return {"logs": logs}
