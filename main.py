from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from openai import OpenAI
import os.path
import pickle
import json
import logging

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class CoachBot:
    def __init__(self, telegram_token, spreadsheet_id, credentials_path):
        # Configuración de tokens y credenciales
        self.TELEGRAM_TOKEN = telegram_token
        self.SPREADSHEET_ID = spreadsheet_id
        self.CREDENTIALS_PATH = /etc/secrets/credentials.json
        self.SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        
        # Inicializar servicios
        self.sheets_service = self._setup_sheets_service()
        self.openai_client = OpenAI()  # Asegúrate de tener configurada tu API key
        self.assistant = None
        
        # Crear la aplicación de Telegram
        self.app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self._setup_handlers()

    def _setup_sheets_service(self):
        """Configura el servicio de Google Sheets."""
        creds = None
        token_path = 'token.pickle'
        
        if os.path.exists(token_path):
            with open(token_path, 'rb') as token:
                creds = pickle.load(token)
                
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.CREDENTIALS_PATH, self.SCOPES)
                creds = flow.run_local_server(port=0)
            
            with open(token_path, 'wb') as token:
                pickle.dump(creds, token)
        
        return build('sheets', 'v4', credentials=creds)

    def _setup_handlers(self):
        """Configura los manejadores de comandos de Telegram."""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.app.add_error_handler(self.error_handler)

    def get_database_content(self):
        """Obtiene el contenido de la base de datos."""
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.SPREADSHEET_ID,
                range='A1:Z1000'  # Ajusta según tus necesidades
            ).execute()
            return result.get('values', [])
        except Exception as e:
            logger.error(f"Error al obtener datos de Google Sheets: {e}")
            return []

    async def setup_assistant(self):
        """Configura el asistente de OpenAI."""
        if self.assistant is None:
            try:
                # Obtener datos de la base de datos
                data = self.get_database_content()
                
                # Crear archivo con los datos
                database_content = json.dumps({
                    "database_content": data
                })
                
                # Subir archivo a OpenAI
                file = self.openai_client.files.create(
                    file=database_content,
                    purpose='assistants'
                )
                
                # Crear asistente
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

    async def get_assistant_response(self, user_message):
        """Obtiene una respuesta del asistente de OpenAI."""
        try:
            if self.assistant is None:
                await self.setup_assistant()
            
            # Crear thread y añadir mensaje
            thread = self.openai_client.beta.threads.create()
            self.openai_client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content=user_message
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
                    return "Lo siento, hubo un error al procesar tu mensaje."
            
            # Obtener mensajes
            messages = self.openai_client.beta.threads.messages.list(
                thread_id=thread.id
            )
            
            # Retornar última respuesta del asistente
            for message in messages.data:
                if message.role == "assistant":
                    return message.content[0].text.value
                    
            return "No se pudo obtener una respuesta."
            
        except Exception as e:
            logger.error(f"Error al obtener respuesta del asistente: {e}")
            return "Lo siento, ocurrió un error al procesar tu solicitud."

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /start."""
        await update.message.reply_text(
            "¡Hola! Soy El Coach Bot. Puedo ayudarte con recomendaciones de productos y recursos. "
            "¿En qué puedo ayudarte hoy?"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /help."""
        await update.message.reply_text(
            "Puedes preguntarme sobre:\n"
            "- Recomendaciones de productos\n"
            "- Videos de ejercicios\n"
            "- Recursos disponibles\n"
            "Solo necesitas escribir tu pregunta y te responderé con información de nuestra base de datos."
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes de texto recibidos."""
        try:
            # Informar al usuario que estamos procesando su mensaje
            processing_message = await update.message.reply_text(
                "Procesando tu solicitud, por favor espera un momento..."
            )
            
            # Obtener respuesta del asistente
            response = await self.get_assistant_response(update.message.text)
            
            # Eliminar mensaje de procesamiento y enviar respuesta
            await processing_message.delete()
            await update.message.reply_text(response)
            
        except Exception as e:
            logger.error(f"Error al manejar mensaje: {e}")
            await update.message.reply_text(
                "Lo siento, ocurrió un error al procesar tu mensaje. Por favor, intenta nuevamente."
            )

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los errores del bot."""
        logger.error(f"Error: {context.error} - Update: {update}")
        await update.message.reply_text(
            "Lo siento, ocurrió un error inesperado. Por favor, intenta nuevamente más tarde."
        )

    def run(self):
        """Inicia el bot."""
        logger.info("Iniciando el bot...")
        self.app.run_polling()
