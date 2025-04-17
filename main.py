import os
import asyncio
import httpx
import sqlite3
import json
import logging
import openai
import time
from fastapi import FastAPI, Request
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI
import speech_recognition as sr
import requests
from contextlib import closing
import string
from gtts import gTTS
from pydub import AudioSegment
import tempfile
import subprocess
import re

def remove_source_references(text: str) -> str:
    """
    Elimina las referencias de fuente como "" del texto.
    """
    return re.sub(r'\【[\d:]+†source\】', '', text)

def normalizeText(text: str) -> str:
    return text.lower().strip()

def convertOgaToWav(oga_path, wav_path):
    try:
        subprocess.run(["ffmpeg", "-i", oga_path, wav_path], check=True)
        return True
    except Exception as e:
        logger.error("Error al convertir el archivo de audio: " + str(e))
        return False

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = FastAPI()

class CoachBot:
    def __init__(self):
        # Validar las variables de entorno requeridas
        required_env_vars = {
            'TELEGRAM_TOKEN': os.getenv('TELEGRAM_TOKEN'),
            'ASSISTANT_ID': os.getenv('ASSISTANT_ID'),
            'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY')
        }
        missing_vars = [var for var, value in required_env_vars.items() if not value]
        if missing_vars:
            raise EnvironmentError(f"Faltan las siguientes variables de entorno requeridas: {', '.join(missing_vars)}")
        self.TELEGRAM_TOKEN = required_env_vars['TELEGRAM_TOKEN']
        self.assistant_id = required_env_vars['ASSISTANT_ID']

        # Inicializar el cliente AsyncOpenAI
        self.client = AsyncOpenAI(api_key=required_env_vars['OPENAI_API_KEY'])
        self.started = False
        self.conversation_history = {}
        self.user_threads = {}
        self.pending_requests = set()
        self.db_path = 'bot_data.db'
        self.user_preferences = {}

        # Diccionario para bloquear chats y evitar procesamiento concurrente de mensajes
        self.locks = {}

        # Comandos de voz (utilizando comandos en inglés)
        self.voice_commands = {
            "activate voice": self.enable_voice_responses,
            "deactivate voice": self.disable_voice_responses,
            "speed": self.set_voice_speed,
        }

        # Inicializar la aplicación de Telegram
        self.telegram_app = Application.builder().token(self.TELEGRAM_TOKEN).build()

        self._init_db()
        self.setup_handlers()
        self._load_user_preferences()

    def _init_db(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    role TEXT,
                    content TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_preferences (
                    chat_id INTEGER PRIMARY KEY,
                    voice_responses BOOLEAN DEFAULT 0,
                    voice_speed FLOAT DEFAULT 1.0
                )
            ''')
            conn.commit()

    def _load_user_preferences(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT chat_id, voice_responses, voice_speed FROM user_preferences')
            rows = cursor.fetchall()
            for chat_id, voice_responses, voice_speed in rows:
                self.user_preferences[chat_id] = {
                    'voice_responses': bool(voice_responses),
                    'voice_speed': voice_speed
                }

    def save_user_preference(self, chat_id, voice_responses=None, voice_speed=None):
        pref = self.user_preferences.get(chat_id, {'voice_responses': False, 'voice_speed': 1.0})
        if voice_responses is not None:
            pref['voice_responses'] = voice_responses
        if voice_speed is not None:
            pref['voice_speed'] = voice_speed
        self.user_preferences[chat_id] = pref
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO user_preferences (chat_id, voice_responses, voice_speed)
                VALUES (?, ?, ?)
            ''', (chat_id, int(pref['voice_responses']), pref['voice_speed']))
            conn.commit()

    async def enable_voice_responses(self, chat_id):
        self.save_user_preference(chat_id, voice_responses=True)
        return "✅ Respuestas de voz activadas. Ahora responderé con mensajes de voz."

    async def disable_voice_responses(self, chat_id):
        self.save_user_preference(chat_id, voice_responses=False)
        return "✅ Respuestas de voz desactivadas. Ahora responderé con mensajes de texto."

    async def set_voice_speed(self, chat_id, text):
        try:
            parts = text.lower().split("speed")
            if len(parts) < 2:
                return "⚠️ Por favor, especifica un valor para la velocidad, por ejemplo: 'speed 1.5'"
            speed_text = parts[1].strip()
            speed = float(speed_text)
            if speed < 0.5 or speed > 2.0:
                return "⚠️ La velocidad debe estar entre 0.5 (lento) y 2.0 (rápido)."
            self.save_user_preference(chat_id, voice_speed=speed)
            return f"✅ Velocidad de voz ajustada a {speed}x."
        except ValueError:
            return "⚠️ No pude entender el valor de la velocidad. Usa un número como 0.8, 1.0, 1.5, etc."

    async def process_voice_command(self, chat_id, text):
        text_lower = text.lower()
        if "activate voice" in text_lower or "turn on voice" in text_lower:
            return await self.enable_voice_responses(chat_id)
        if "deactivate voice" in text_lower or "turn off voice" in text_lower:
            return await self.disable_voice_responses(chat_id)
        if "speed" in text_lower:
            return await self.set_voice_speed(chat_id, text_lower)
        return None

    async def get_or_create_thread(self, chat_id):
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]
        try:
            thread = await self.client.beta.threads.create()
            self.user_threads[chat_id] = thread.id
            return thread.id
        except Exception as e:
            logger.error(f"❌ Error creando el hilo para {chat_id}: {e}")
            return None

    async def send_message_to_assistant(self, chat_id: int, user_message: str) -> str:
        if chat_id in self.pending_requests:
            return "⏳ Ya estoy procesando tu solicitud anterior. Por favor espera."
        
        # Usar un lock específico para cada chat_id
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        
        # Bloqueo para evitar que dos mensajes se procesen simultáneamente
        async with lock:
            self.pending_requests.add(chat_id)
            try:
                # Verificar si hay una ejecución activa antes de continuar
                thread_id = await self.get_or_create_thread(chat_id)
                if not thread_id:
                    self.pending_requests.remove(chat_id)
                    return "❌ No se pudo establecer la conexión con el asistente."
                
                # Enviar el mensaje al hilo de OpenAI
                await self.client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=user_message
                )
                
                # Crear la ejecución de OpenAI
                run = await self.client.beta.threads.runs.create(
                    thread_id=thread_id,
                    assistant_id=self.assistant_id
                )
                start_time = time.time()
                
                # Esperar a que la ejecución se complete
                while True:
                    run_status = await self.client.beta.threads.runs.retrieve(
                        thread_id=thread_id,
                        run_id=run.id
                    )
                    if run_status.status == 'completed':
                        break
                    elif run_status.status in ['failed', 'cancelled', 'expired']:
                        logger.error(f"Detalles del estado de ejecución: {run_status}")
                        if hasattr(run_status, 'last_error') and run_status.last_error and run_status.last_error.code == 'rate_limit_exceeded':
                            raise Exception("Cuota de OpenAI excedida: " + run_status.last_error.message)
                        raise Exception(f"La ejecución falló con el estado: {run_status.status}")
                    elif time.time() - start_time > 60:
                        raise TimeoutError("La consulta al asistente tomó demasiado tiempo.")
                    await asyncio.sleep(1)
                
                # Obtener la respuesta del asistente
                messages = await self.client.beta.threads.messages.list(
                    thread_id=thread_id,
                    order="desc",
                    limit=1
                )
                
                if not messages.data or not messages.data[0].content:
                    self.pending_requests.remove(chat_id)
                    return "⚠️ La respuesta del asistente está vacía. Intenta de nuevo más tarde."
                
                assistant_message = messages.data[0].content[0].text.value
                assistant_message = remove_source_references(assistant_message)
                self.conversation_history.setdefault(chat_id, []).append({
                    "role": "assistant",
                    "content": assistant_message
                })
                
                return assistant_message
            except Exception as e:
                logger.error(f"❌ Error procesando el mensaje: {e}")
                error_message = str(e)
                if "Can't add messages to" in error_message:
                    return "⏳ Por favor espera, aún estoy procesando tu solicitud anterior."
                return "⚠️ Hubo un error procesando tu mensaje: " + error_message
            finally:
                if chat_id in self.pending_requests:
                    self.pending_requests.remove(chat_id)

    def setup_handlers(self):
        try:
            self.telegram_app.add_handler(CommandHandler("start", self.start_command))
            self.telegram_app.add_handler(CommandHandler("help", self.help_command))
            self.telegram_app.add_handler(CommandHandler("voice", self.voice_settings_command))
            self.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.route_message))
            self.telegram_app.add_handler(MessageHandler(filters.VOICE, self.handle_voice_message))
            logger.info("Handlers configurados correctamente")
        except Exception as e:
            logger.error(f"Error en setup_handlers: {e}")
            raise

    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            chat_id = update.message.chat.id
            voice_file = await update.message.voice.get_file()
            oga_file_path = f"{chat_id}_voice_note.oga"
            await voice_file.download_to_drive(oga_file_path)
            wav_file_path = f"{chat_id}_voice_note.wav"
            if not convertOgaToWav(oga_file_path, wav_file_path):
                await update.message.reply_text("⚠️ No se pudo procesar el archivo de audio.")
                return
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_file_path) as source:
                audio = recognizer.record(source)
            try:
                user_message = recognizer.recognize_google(audio, language='es-ES')  # Cambié a español
                logger.info("Transcripción de voz: " + user_message)
                await update.message.reply_text(f"📝 Tu mensaje: \"{user_message}\"")
                response = await self.process_text_message(update, context, user_message)
                await update.message.reply_text(response)
            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ No pude entender la nota de voz. Por favor intenta de nuevo.")
            except sr.RequestError as e:
                logger.error("Error del servicio de reconocimiento de voz de Google: " + str(e))
                await update.message.reply_text("⚠️ Hubo un error con el servicio de reconocimiento de voz.")
        except Exception as e:
            logger.error("Error manejando la nota de voz: " + str(e))
            await update.message.reply_text("⚠️ Hubo un error procesando tu nota de voz.")
        finally:
            try:
                if os.path.exists(oga_file_path):
                    os.remove(oga_file_path)
                if os.path.exists(wav_file_path):
                    os.remove(wav_file_path)
            except Exception as e:
                logger.error("Error eliminando archivos temporales: " + str(e))

    async def voice_settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        pref = self.user_preferences.get(chat_id, {'voice_responses': False, 'voice_speed': 1.0})
        voice_status = "activadas" if pref['voice_responses'] else "desactivadas"
        help_text = (
            "🎙 *Configuración de voz*\n\n"
            f"Estado actual: Respuestas de voz {voice_status}\n"
            f"Velocidad actual: {pref['voice_speed']}x\n\n"
            "*Comandos disponibles:*\n"
            "- 'Activar voz' - Para recibir respuestas de voz\n"
            "- 'Desactivar voz' - Para recibir respuestas de texto\n"
            "- 'Velocidad X.X' - Para ajustar la velocidad (entre 0.5 y 2.0)\n\n"
            "También puedes usar estos comandos en cualquier mensaje."
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')

    def save_conversation(self, chat_id, role, content):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO conversations (chat_id, role, content)
                VALUES (?, ?, ?)
            ''', (chat_id, role, content))
            conn.commit()

    async def async_init(self):
        try:
            await self.telegram_app.initialize()
            if not self.started:
                self.started = True
                # Polling loop is disabled as we use webhooks.
                # await self.telegram_app.start()
            logger.info("Bot inicializado correctamente")
        except Exception as e:
            logger.error(f"Error en async_init: {e}")
            raise

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            chat_id = update.message.chat.id
            await update.message.reply_text("👋 ¡Bienvenido! ¿Cómo puedo ayudarte hoy?")
            logger.info(f"Comando /start ejecutado por chat_id: {chat_id}")
        except Exception as e:
            logger.error(f"Error en start_command: {e}")
            await update.message.reply_text("❌ Ocurrió un error. Por favor intenta de nuevo.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            help_text = (
                "🤖 *Comandos disponibles:*\n\n"
                "/start - Iniciar o reiniciar el bot\n"
                "/help - Mostrar este mensaje de ayuda\n"
                "/voice - Configurar respuestas de voz\n\n"
                "📝 *Características:*\n"
                "- Consultas sobre ejercicios\n"
                "- Recomendaciones personalizadas\n"
                "- Seguimiento de progreso\n"
                "- Recursos y videos\n"
                "- Notas de voz (enviar o recibir mensajes de voz)\n\n"
                "✨ Simplemente escribe tu pregunta o envía una nota de voz."
            )
            await update.message.reply_text(help_text, parse_mode='Markdown')
            logger.info(f"Comando /help ejecutado por chat_id: {update.message.chat.id}")
        except Exception as e:
            logger.error(f"Error en help_command: {e}")
            await update.message.reply_text("❌ Error al mostrar la ayuda. Por favor intenta de nuevo.")

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await self.handle_message(update, context)
        except Exception as e:
            logger.error(f"Error en route_message: {e}")
            await update.message.reply_text(
                "❌ Ocurrió un error procesando tu mensaje. Por favor intenta de nuevo."
            )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            chat_id = update.message.chat.id
            user_message = update.message.text.strip()
            if not user_message:
                return
            response = await asyncio.wait_for(
                self.process_text_message(update, context, user_message),
                timeout=60.0
            )
            if response is None or not response.strip():
                raise ValueError("La respuesta del asistente está vacía")
            self.save_conversation(chat_id, "user", user_message)
            self.save_conversation(chat_id, "assistant", response)
            await update.message.reply_text(response)
        except asyncio.TimeoutError:
            logger.error(f"⏳ Timeout procesando el mensaje para {chat_id}")
            await update.message.reply_text("⏳ La operación está tardando demasiado. Por favor intenta de nuevo más tarde.")
        except openai.OpenAIError as e:
            logger.error(f"❌ Error con OpenAI: {e}")
            await update.message.reply_text("❌ Hubo un problema con OpenAI.")
        except Exception as e:
            logger.error(f"⚠️ Error inesperado: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error inesperado. Por favor intenta de nuevo más tarde.")

    async def text_to_speech(self, text, speed=1.0):
        """Convierte el texto a voz con ajuste de velocidad."""
        try:
            temp_dir = os.path.join(os.getcwd(), 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            temp_filename = f"voice_{int(time.time())}.mp3"
            temp_path = os.path.join(temp_dir, temp_filename)
            tts = gTTS(text=text, lang='es')  # Cambio de idioma a español
            tts.save(temp_path)
            if speed != 1.3:
                song = AudioSegment.from_mp3(temp_path)
                new_song = song.speedup(playback_speed=speed)
                new_song.export(temp_path, format="mp3")
            return temp_path
        except Exception as e:
            print(f"Error en text_to_speech: {e}")
            return None

try:
    bot = CoachBot()
except Exception as e:
    logger.error("Error crítico al inicializar el bot: " + str(e))
    raise

@app.on_event("startup")
async def startup_event():
    try:
        await bot.async_init()
        logger.info("Aplicación iniciada correctamente")
    except Exception as e:
        logger.error("❌ Error al iniciar la aplicación: " + str(e))

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot.telegram_app.bot)
        await bot.telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error("❌ Error procesando el webhook: " + str(e))
        return {"status": "error", "message": str(e)}
