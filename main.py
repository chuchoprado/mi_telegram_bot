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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = FastAPI()

def remove_source_references(text: str) -> str:
    return re.sub(r'\【[\d:]+\u2020source\】', '', text)

def normalizeText(text: str) -> str:
    return text.lower().strip()

def convertOgaToWav(oga_path, wav_path):
    try:
        subprocess.run(["ffmpeg", "-i", oga_path, wav_path], check=True)
        return True
    except Exception as e:
        logger.error("Error al convertir el archivo de audio: " + str(e))
        return False

class CoachBot:
    def __init__(self):
        self.TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        self.ASSISTANT_ID = os.getenv("ASSISTANT_ID")

        if not self.TELEGRAM_TOKEN or not self.OPENAI_API_KEY or not self.ASSISTANT_ID:
            raise EnvironmentError("Faltan variables de entorno necesarias")

        self.client = AsyncOpenAI(api_key=self.OPENAI_API_KEY)
        self.telegram_app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self.task_queue = asyncio.Queue()

        self.db_path = 'bot_data.db'
        self.user_preferences = {}
        self.user_threads = {}

        self._init_db()
        self._load_user_preferences()
        self.setup_handlers()

    def _init_db(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS conversations (chat_id INTEGER, role TEXT, content TEXT)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS user_preferences (chat_id INTEGER PRIMARY KEY, voice_responses BOOLEAN DEFAULT 0, voice_speed FLOAT DEFAULT 1.0)''')
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

    def setup_handlers(self):
        self.telegram_app.add_handler(CommandHandler("start", self.start_command))
        self.telegram_app.add_handler(CommandHandler("voice", self.voice_settings_command))
        self.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.route_message))
        self.telegram_app.add_handler(MessageHandler(filters.VOICE, self.handle_voice_message))

    async def async_init(self):
        await self.telegram_app.initialize()
        asyncio.create_task(self.handle_queue())
        logger.info("Bot inicializado correctamente")

    async def handle_queue(self):
        while True:
            chat_id, update, context, message = await self.task_queue.get()
            try:
                response = await self.get_openai_response(chat_id, message)
                await self.send_response(update, chat_id, response)
                self.save_conversation(chat_id, "user", message)
                self.save_conversation(chat_id, "assistant", response)
            except Exception as e:
                logger.error(f"Error procesando mensaje en la cola: {e}")
                await update.message.reply_text("❌ Error procesando tu mensaje.")
            finally:
                self.task_queue.task_done()

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        message = update.message.text.strip()
        await self.task_queue.put((chat_id, update, context, message))
        await update.message.reply_text("⏳ Estoy procesando tu mensaje...")

    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        voice_file = await update.message.voice.get_file()
        oga_file = f"voice_{chat_id}.oga"
        wav_file = f"voice_{chat_id}.wav"
        await voice_file.download_to_drive(oga_file)
        if convertOgaToWav(oga_file, wav_file):
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_file) as source:
                audio = recognizer.record(source)
            try:
                user_text = recognizer.recognize_google(audio, language="es-ES")
                await self.task_queue.put((chat_id, update, context, user_text))
                await update.message.reply_text(f"🗣️ Has dicho: {user_text}")
            except:
                await update.message.reply_text("⚠️ No pude entender la nota de voz.")
        else:
            await update.message.reply_text("⚠️ Error procesando audio.")
        os.remove(oga_file)
        os.remove(wav_file)

    async def get_openai_response(self, chat_id, message):
        thread_id = await self.get_or_create_thread(chat_id)
        await self.client.beta.threads.messages.create(thread_id=thread_id, role="user", content=message)
        run = await self.client.beta.threads.runs.create(thread_id=thread_id, assistant_id=self.ASSISTANT_ID)
        while True:
            run_status = await self.client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run_status.status == 'completed':
                break
            await asyncio.sleep(1)
        messages = await self.client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)
        return remove_source_references(messages.data[0].content[0].text.value)

    async def get_or_create_thread(self, chat_id):
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]
        thread = await self.client.beta.threads.create()
        self.user_threads[chat_id] = thread.id
        return thread.id

    async def send_response(self, update, chat_id, text):
        pref = self.user_preferences.get(chat_id, {"voice_responses": False, "voice_speed": 1.0})
        if pref["voice_responses"]:
            path = await self.text_to_speech(text, pref["voice_speed"])
            if path:
                with open(path, "rb") as audio:
                    await update.message.reply_voice(voice=audio)
                os.remove(path)
            else:
                await update.message.reply_text(text)
        else:
            await update.message.reply_text(text)

    async def text_to_speech(self, text, speed):
        try:
            temp_path = f"tts_{int(time.time())}.mp3"
            gTTS(text=text, lang="es").save(temp_path)
            if speed != 1.0:
                audio = AudioSegment.from_mp3(temp_path)
                audio = audio.speedup(playback_speed=speed)
                audio.export(temp_path, format="mp3")
            return temp_path
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return None

    def save_conversation(self, chat_id, role, content):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)', (chat_id, role, content))
            conn.commit()

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("👋 ¡Hola! Soy tu Coach MeditaHub. Envíame un mensaje o nota de voz para comenzar.")

    async def voice_settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        pref = self.user_preferences.get(chat_id, {"voice_responses": False, "voice_speed": 1.0})
        msg = (
            f"🎙 Configuración de voz:\n"
            f"Respuestas de voz: {'activadas' if pref['voice_responses'] else 'desactivadas'}\n"
            f"Velocidad: {pref['voice_speed']}x"
        )
        await update.message.reply_text(msg)

bot = CoachBot()

@app.on_event("startup")
async def startup_event():
    await bot.async_init()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot.telegram_app.bot)
    await bot.telegram_app.process_update(update)
    return {"status": "ok"}
