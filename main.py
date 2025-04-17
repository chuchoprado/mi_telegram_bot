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
    Removes source reference markers like "" from the text.
    """
    return re.sub(r'\【[\d:]+†source\】', '', text)

def normalizeText(text: str) -> str:
    return text.lower().strip()

def convertOgaToWav(oga_path, wav_path):
    try:
        subprocess.run(["ffmpeg", "-i", oga_path, wav_path], check=True)
        return True
    except Exception as e:
        logger.error("Error converting audio file: " + str(e))
        return False

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = FastAPI()

class CoachBot:
    def __init__(self):
        # Validate required environment variables
        required_env_vars = {
            'TELEGRAM_TOKEN': os.getenv('TELEGRAM_TOKEN'),
            'ASSISTANT_ID': os.getenv('ASSISTANT_ID'),
            'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY')
        }
        missing_vars = [var for var, value in required_env_vars.items() if not value]
        if missing_vars:
            raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")
        self.TELEGRAM_TOKEN = required_env_vars['TELEGRAM_TOKEN']
        self.assistant_id = required_env_vars['ASSISTANT_ID']

        # Initialize AsyncOpenAI client
        self.client = AsyncOpenAI(api_key=required_env_vars['OPENAI_API_KEY'])
        self.started = False
        self.conversation_history = {}
        self.user_threads = {}
        self.pending_requests = set()
        self.db_path = 'bot_data.db'
        self.user_preferences = {}

        # Dictionary for locks per chat to prevent concurrent message processing
        self.locks = {}

        # Voice commands (using English commands)
        self.voice_commands = {
            "activate voice": self.enable_voice_responses,
            "deactivate voice": self.disable_voice_responses,
            "speed": self.set_voice_speed,
        }

        # Initialize the Telegram Application
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
        return "✅ Voice responses activated. I'll now reply with voice messages."

    async def disable_voice_responses(self, chat_id):
        self.save_user_preference(chat_id, voice_responses=False)
        return "✅ Voice responses deactivated. I'll reply with text messages."

    async def set_voice_speed(self, chat_id, text):
        try:
            parts = text.lower().split("speed")
            if len(parts) < 2:
                return "⚠️ Please specify a value for speed, for example: 'speed 1.5'"
            speed_text = parts[1].strip()
            speed = float(speed_text)
            if speed < 0.5 or speed > 2.0:
                return "⚠️ Speed must be between 0.5 (slow) and 2.0 (fast)."
            self.save_user_preference(chat_id, voice_speed=speed)
            return f"✅ Voice speed set to {speed}x."
        except ValueError:
            return "⚠️ I couldn't understand the speed value. Use a number like 0.8, 1.0, 1.5, etc."

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
            logger.error(f"❌ Error creating thread for {chat_id}: {e}")
            return None

    async def send_message_to_assistant(self, chat_id: int, user_message: str) -> str:
        if chat_id in self.pending_requests:
            return "⏳ I'm already processing your previous request. Please wait."
        self.pending_requests.add(chat_id)
        try:
            thread_id = await self.get_or_create_thread(chat_id)
            if not thread_id:
                self.pending_requests.remove(chat_id)
                return "❌ Could not establish connection with the assistant."
            await self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=user_message
            )
            run = await self.client.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=self.assistant_id
            )
            start_time = time.time()
            while True:
                run_status = await self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run.id
                )
                if run_status.status == 'completed':
                    break
                elif run_status.status in ['failed', 'cancelled', 'expired']:
                    logger.error(f"Run status details: {run_status}")
                    if hasattr(run_status, 'last_error') and run_status.last_error and run_status.last_error.code == 'rate_limit_exceeded':
                        raise Exception("OpenAI quota exceeded: " + run_status.last_error.message)
                    raise Exception(f"Run failed with status: {run_status.status}")
                elif time.time() - start_time > 60:
                    raise TimeoutError("The assistant query took too long.")
                await asyncio.sleep(1)
            messages = await self.client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            )
            if not messages.data or not messages.data[0].content:
                self.pending_requests.remove(chat_id)
                return "⚠️ The assistant's response is empty. Please try again later."
            assistant_message = messages.data[0].content[0].text.value
            # Remove any source reference markers from the assistant's message
            assistant_message = remove_source_references(assistant_message)
            self.conversation_history.setdefault(chat_id, []).append({
                "role": "assistant",
                "content": assistant_message
            })
            return assistant_message
        except Exception as e:
            logger.error(f"❌ Error processing message: {e}")
            error_message = str(e)
            if "Can't add messages to" in error_message:
                return "⏳ Please wait, I'm still processing your previous request."
            return "⚠️ There was an error processing your message: " + error_message
        finally:
            if chat_id in self.pending_requests:
                self.pending_requests.remove(chat_id)

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str) -> str:
        chat_id = update.message.chat.id
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            try:
                if not user_message.strip():
                    return "⚠️ No valid message received."
                voice_command_response = await self.process_voice_command(chat_id, user_message)
                if voice_command_response:
                    return voice_command_response
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                # Directly send the user message to the assistant without product search logic
                response = await self.send_message_to_assistant(chat_id, user_message)
                if not response.strip():
                    logger.error("⚠️ OpenAI returned an empty response.")
                    return "⚠️ I did not receive a valid response from the assistant. Please try again."
                self.save_conversation(chat_id, "user", user_message)
                self.save_conversation(chat_id, "assistant", response)
                return response
            except Exception as e:
                logger.error(f"❌ Error in process_text_message: {e}", exc_info=True)
                return "⚠️ There was an error processing your message."

    def setup_handlers(self):
        try:
            self.telegram_app.add_handler(CommandHandler("start", self.start_command))
            self.telegram_app.add_handler(CommandHandler("help", self.help_command))
            self.telegram_app.add_handler(CommandHandler("voice", self.voice_settings_command))
            self.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.route_message))
            self.telegram_app.add_handler(MessageHandler(filters.VOICE, self.handle_voice_message))
            logger.info("Handlers configured successfully")
        except Exception as e:
            logger.error(f"Error in setup_handlers: {e}")
            raise

    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            chat_id = update.message.chat.id
            voice_file = await update.message.voice.get_file()
            oga_file_path = f"{chat_id}_voice_note.oga"
            await voice_file.download_to_drive(oga_file_path)
            wav_file_path = f"{chat_id}_voice_note.wav"
            if not convertOgaToWav(oga_file_path, wav_file_path):
                await update.message.reply_text("⚠️ Could not process the audio file.")
                return
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_file_path) as source:
                audio = recognizer.record(source)
            try:
                user_message = recognizer.recognize_google(audio, language='en-US')
                logger.info("Voice transcription: " + user_message)
                await update.message.reply_text(f"📝 Your message: \"{user_message}\"")
                response = await self.process_text_message(update, context, user_message)
                await update.message.reply_text(response)
            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ I could not understand the voice note. Please try again.")
            except sr.RequestError as e:
                logger.error("Error from Google voice recognition service: " + str(e))
                await update.message.reply_text("⚠️ There was an error with the voice recognition service.")
        except Exception as e:
            logger.error("Error handling voice message: " + str(e))
            await update.message.reply_text("⚠️ There was an error processing your voice note.")
        finally:
            try:
                if os.path.exists(oga_file_path):
                    os.remove(oga_file_path)
                if os.path.exists(wav_file_path):
                    os.remove(wav_file_path)
            except Exception as e:
                logger.error("Error deleting temporary files: " + str(e))

    async def voice_settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        pref = self.user_preferences.get(chat_id, {'voice_responses': False, 'voice_speed': 1.0})
        voice_status = "activated" if pref['voice_responses'] else "deactivated"
        help_text = (
            "🎙 *Voice Settings*\n\n"
            f"Current status: Voice responses {voice_status}\n"
            f"Current speed: {pref['voice_speed']}x\n\n"
            "*Available Commands:*\n"
            "- 'Activate voice' - To receive voice responses\n"
            "- 'Deactivate voice' - To receive text responses\n"
            "- 'Speed X.X' - To adjust the speed (between 0.5 and 2.0)\n\n"
            "You can also use these commands in any message."
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
            logger.info("Bot initialized successfully")
        except Exception as e:
            logger.error(f"Error in async_init: {e}")
            raise

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            chat_id = update.message.chat.id
            await update.message.reply_text("👋 Welcome! How can I help you today?")
            logger.info(f"/start command executed by chat_id: {chat_id}")
        except Exception as e:
            logger.error(f"Error in start_command: {e}")
            await update.message.reply_text("❌ An error occurred. Please try again.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            help_text = (
                "🤖 *Available Commands:*\n\n"
                "/start - Start or restart the bot\n"
                "/help - Show this help message\n"
                "/voice - Configure voice responses\n\n"
                "📝 *Features:*\n"
                "- Exercise queries\n"
                "- Personalized recommendations\n"
                "- Progress tracking\n"
                "- Resources and videos\n"
                "- Voice notes (send or receive voice messages)\n\n"
                "✨ Simply type your question or send a voice note."
            )
            await update.message.reply_text(help_text, parse_mode='Markdown')
            logger.info(f"/help command executed by chat_id: {update.message.chat.id}")
        except Exception as e:
            logger.error(f"Error in help_command: {e}")
            await update.message.reply_text("❌ Error displaying help. Please try again.")

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await self.handle_message(update, context)
        except Exception as e:
            logger.error(f"Error in route_message: {e}")
            await update.message.reply_text(
                "❌ An error occurred processing your message. Please try again."
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
                raise ValueError("The assistant's response is empty")
            self.save_conversation(chat_id, "user", user_message)
            self.save_conversation(chat_id, "assistant", response)
            await update.message.reply_text(response)
        except asyncio.TimeoutError:
            logger.error(f"⏳ Timeout processing message for {chat_id}")
            await update.message.reply_text("⏳ The operation is taking too long. Please try again later.")
        except openai.OpenAIError as e:
            logger.error(f"❌ Error with OpenAI: {e}")
            await update.message.reply_text("❌ There was a problem with OpenAI.")
        except Exception as e:
            logger.error(f"⚠️ Unexpected error: {e}")
            await update.message.reply_text("⚠️ An unexpected error occurred. Please try again later.")

    async def text_to_speech(self, text, speed=1.0):
        """Converts text to speech with speed adjustment."""
        try:
            temp_dir = os.path.join(os.getcwd(), 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            temp_filename = f"voice_{int(time.time())}.mp3"
            temp_path = os.path.join(temp_dir, temp_filename)
            tts = gTTS(text=text, lang='en')
            tts.save(temp_path)
            if speed != 1.3:
                song = AudioSegment.from_mp3(temp_path)
                new_song = song.speedup(playback_speed=speed)
                new_song.export(temp_path, format="mp3")
            return temp_path
        except Exception as e:
            print(f"Error in text_to_speech: {e}")
            return None

try:
    bot = CoachBot()
except Exception as e:
    logger.error("Critical error initializing the bot: " + str(e))
    raise

@app.on_event("startup")
async def startup_event():
    try:
        await bot.async_init()
        logger.info("Application started successfully")
    except Exception as e:
        logger.error("❌ Error starting the application: " + str(e))

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot.telegram_app.bot)
        await bot.telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error("❌ Error processing webhook: " + str(e))
        return {"status": "error", "message": str(e)}
