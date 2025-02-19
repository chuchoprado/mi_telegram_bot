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

# Variable global para almacenar logs
logs = []

class CoachBot:
    def __init__(self):
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
        openai.api_key = required_env_vars['OPENAI_API_KEY']
        
        self.credentials_path = '/etc/secrets/credentials.json'
        self.sheets_service = None
        self.started = False
        self.verified_users = {}
        self.conversation_history = {}
        self.user_threads = {}
        self.db_path = 'bot_data.db'
        self._init_db()
        self.app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self._setup_handlers()
        self._init_sheets()

    def _init_db(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
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

    def _init_sheets(self):
        try:
            if not os.path.exists(self.credentials_path):
                logger.error(f"Archivo de credenciales no encontrado en: {self.credentials_path}")
                return False
                
            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
            )
            self.sheets_service = build('sheets', 'v4', credentials=credentials)
            return True
        except Exception as e:
            logger.error(f"Error inicializando Google Sheets: {e}")
            return False

    async def send_message_to_assistant(self, chat_id, user_message):
        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Tú eres un asistente útil."},
                    {"role": "user", "content": user_message}
                ]
            )
            return response['choices'][0]['message']['content'].strip()
        except Exception as e:
            logger.error(f"Error en OpenAI: {e}")
            return "Error obteniendo la respuesta de OpenAI."

    async def handle_assistant_response(self, assistant_function_call):
        if assistant_function_call['name'] == 'fetch_sheet_data':
            query = assistant_function_call['arguments']['query']
            response = requests.get(
                f"https://script.google.com/macros/s/AKfycbwUieYWmu5pTzHUBnSnyrLGo-SROiiNFvufWdn5qm7urOamB65cqQkbQrkj05Xf3N3N_g/exec?query={requests.utils.quote(query)}"
            )
            return response.json()

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
        chat_id = update.effective_chat.id
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            response = await self.send_message_to_assistant(chat_id, user_message)
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"Error procesando mensaje con OpenAI: {e}")
            await update.message.reply_text("Error obteniendo la respuesta.")

    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        voice_file = await update.message.voice.get_file()
        voice_file_path = f"{chat_id}_voice_note.ogg"
        await voice_file.download(voice_file_path)

        recognizer = sr.Recognizer()
        with sr.AudioFile(voice_file_path) as source:
            audio = recognizer.record(source)
        
        try:
            user_message = recognizer.recognize_google(audio, language='es-ES')
            await self.process_text_message(update, context, user_message)
        except sr.UnknownValueError:
            await update.message.reply_text("⚠️ No pude entender la nota de voz. Intenta de nuevo.")
        except sr.RequestError as e:
            logger.error(f"Error en el servicio de reconocimiento de voz de Google: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error con el servicio de reconocimiento de voz.")

# Manejo de errores mejorado para la creación del bot
try:
    bot = CoachBot()
except Exception as e:
    logger.error(f"Error crítico inicializando el bot: {e}")
    raise

@app.on_event("startup")
async def startup_event():
    try:
        await bot.async_init()
        logger.info("Aplicación iniciada correctamente")
    except Exception as e:
        logger.error(f"Error en startup: {e}")
        raise

@app.get("/")
async def health_check():
    return {"status": "alive"}
