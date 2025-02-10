# main.py
import os
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from openai import OpenAI
import json
import logging
from typing import Dict

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
        # Configuración de tokens y credenciales desde variables de entorno
        self.TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
        self.SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
        
        # Inicializar servicios
        self.sheets_service = self._setup_sheets_service()
        self.openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        self.assistant = None
        
        # Crear la aplicación de Telegram
        self.app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self._setup_handlers()

    def _setup_sheets_service(self):
        """Configura el servicio de Google Sheets usando credenciales de servicio."""
        try:
            # Cargar credenciales desde la variable de entorno
            credentials_json = os.getenv('GOOGLE_CREDENTIALS')
            if not credentials_json:
                raise ValueError("No se encontraron las credenciales de Google")
            
            credentials_dict = json.loads(credentials_json)
            credentials = ServiceAccountCredentials.from_service_account_info(
                credentials_dict,
                scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
            )
            
            return build('sheets', 'v4', credentials=credentials)
        except Exception as e:
            logger.error(f"Error al configurar Google Sheets: {e}")
            return None

    def _setup_handlers(self):
        """Configura los manejadores de comandos de Telegram."""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.app.add_error_handler(self.error_handler)

    async def get_database_content(self):
        """Obtiene el contenido de la base de datos."""
        try:
            if not self.sheets_service:
                return []
            
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.SPREADSHEET_ID,
                range='A1:Z1000'
            ).execute()
            return result.get('values', [])
        except Exception as e:
            logger.error(f"Error al obtener datos de Google Sheets: {e}")
            return []

    async def setup_assistant(self):
        """Configura el asistente de OpenAI."""
        if self.assistant is None:
            try:
                data = await self.get_database_content()
                database_content = json.dumps({"database_content": data})
                
                file = self.openai_client.files.create(
                    file=database_content,
                    purpose='assistants'
                )
                
                self.assistant = self.openai_client.beta.assistants.create(
                    name="El Coach Assistant",
                    instructions="""
                    Eres un asistente que proporciona recomendaciones basadas únicamente 
                    en la base de datos proporcionada. Solo debes recomendar productos, 
                    videos o recursos que estén listados en la base de datos.
                    """,
                    model="gpt-4-turbo-preview",
                    tools=[{"type": "retrieval"}],
                    file_ids=[file.id]
                )
                
                logger.info("Asistente configurado exitosamente")
            except Exception as e:
                logger.error(f"Error al configurar el asistente: {e}")

    # ... [Resto de métodos del bot permanecen igual] ...

# Crear instancia del bot
bot = CoachBot()

@app.post("/webhook")
async def webhook(update: Dict):
    """Endpoint para el webhook de Telegram."""
    try:
        # Convertir el dict a un objeto Update de python-telegram-bot
        telegram_update = Update.de_json(update, bot.app.bot)
        # Procesar la actualización
        await bot.app.process_update(telegram_update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def root():
    """Endpoint de salud para Render."""
    return {"status": "Bot is running"}

# Archivo requirements.txt
"""
fastapi==0.109.2
uvicorn==0.27.1
python-telegram-bot==20.7
google-auth==2.27.0
google-auth-oauthlib==1.2.0
google-auth-httplib2==0.2.0
google-api-python-client==2.116.0
openai==1.11.1
python-dotenv==1.0.1
gunicorn==21.2.0
"""

# Archivo Dockerfile
"""
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:10000", "main:app"]
"""
