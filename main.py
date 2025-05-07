import os
import asyncio
import httpx
import sqlite3
import json
import logging
import openai
import time
import shutil
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from openai import AsyncOpenAI
import speech_recognition as sr
from contextlib import closing
from gtts import gTTS
from pydub import AudioSegment
import subprocess
import re
import datetime

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = FastAPI()

# ------------------------------------------------------------------ #
# NUEVO: función para limpiar emojis y emoticonos                     #
# ------------------------------------------------------------------ #
def clean_text(text: str) -> str:
    """Elimina emojis, emoticonos ASCII y referencias de fuente."""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticones
        "\U0001F300-\U0001F5FF"  # símbolos/pictogramas
        "\U0001F680-\U0001F6FF"  # transporte/mapas
        "\U0001F1E0-\U0001F1FF"  # banderas
        "]+", flags=re.UNICODE)
    text = emoji_pattern.sub('', text)
    emoticon_pattern = re.compile(r'(:\)|:\(|;\)|:-\)|:-\(|;D|:D|<3)')
    text = emoticon_pattern.sub('', text)
    text = re.sub(r'\【[\d:]+†source\】', '', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def remove_source_references(text: str) -> str:
    """Elimina las referencias de fuentes del texto generado por OpenAI"""
    return re.sub(r'\【[\d:]+†source\】', '', text)


def convertOgaToWav(oga_path, wav_path):
    """Convierte archivos de audio de formato OGA a WAV usando ffmpeg"""
    try:
        subprocess.run(["ffmpeg", "-i", oga_path, wav_path], check=True, timeout=60)
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

        self.db_path = os.getenv("DB_PATH", "bot_data.db")
        logger.info(f"📂 Base de datos en → {os.path.abspath(self.db_path)}")
        self.user_preferences = {}
        self.user_threads = {}
        self.user_sent_voice = set()
        self.temp_dir = 'temp_files'

        # Crear directorio temporal si no existe
        os.makedirs(self.temp_dir, exist_ok=True)

        self._init_db()
        self._load_user_preferences()
        self._load_user_threads()
        self.setup_handlers()

    # ------------------------------------------------------------------ #
    #  ACTUALIZADO: método único que crea TODAS las tablas si no existen  #
    # ------------------------------------------------------------------ #
    def _init_db(self):
        """
        Inicializa la base de datos SQLite.
        Crea las tablas necesarias *solo* si todavía no existen para
        conservar todos los datos incluso tras reinicios del proceso.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()

            # Tabla de conversaciones resumidas
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id        INTEGER  PRIMARY KEY AUTOINCREMENT,
                    chat_id   INTEGER,
                    role      TEXT,
                    content   TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Preferencias del usuario
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_preferences (
                    chat_id        INTEGER PRIMARY KEY,
                    voice_responses BOOLEAN DEFAULT 0,
                    voice_speed     FLOAT   DEFAULT 1.0,
                    voice_language  TEXT    DEFAULT 'es',
                    voice_gender    TEXT    DEFAULT 'female'
                )
            ''')

            # Persistencia de hilos de OpenAI
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_threads (
                    chat_id    INTEGER PRIMARY KEY,
                    thread_id  TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # ---------- NUEVAS TABLAS para historial completo ---------- #
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT,
                    first_name  TEXT,
                    last_name   TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    message_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     INTEGER,
                    user_id     INTEGER,
                    content     TEXT,
                    timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_bot      BOOLEAN,
                    FOREIGN KEY (chat_id) REFERENCES user_threads(chat_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS context (
                    context_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id      INTEGER,
                    thread_id    TEXT,
                    context_data TEXT,
                    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (chat_id) REFERENCES user_threads(chat_id)
                )
            ''')

            conn.commit()

    # ------------------------------------------------------------------ #
    #            CARGA DE PREFS Y THREADS DESDE LA BASE DE DATOS         #
    # ------------------------------------------------------------------ #
    def _load_user_preferences(self):
        """Cargar preferencias de usuarios desde la base de datos"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT chat_id, voice_responses, voice_speed, voice_language, voice_gender FROM user_preferences')
            for chat_id, vr, vs, vl, vg in cursor.fetchall():
                self.user_preferences[chat_id] = {
                    'voice_responses': bool(vr),
                    'voice_speed': vs,
                    'voice_language': vl,
                    'voice_gender': vg
                }

    def _load_user_threads(self):
        """Cargar threads de OpenAI desde la base de datos para mantener el contexto"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT chat_id, thread_id FROM user_threads')
            for chat_id, thread_id in cursor.fetchall():
                self.user_threads[chat_id] = thread_id

    # ------------------------------------------------------------------ #
    #                        CONFIGURACIÓN DE HANDLERS                   #
    # ------------------------------------------------------------------ #
    def setup_handlers(self):
        """Configurar handlers para los diferentes tipos de mensajes y comandos"""
        self.telegram_app.add_handler(CommandHandler("start", self.start_command))
        self.telegram_app.add_handler(CommandHandler("voice", self.voice_settings_command))
        self.telegram_app.add_handler(CommandHandler("reset", self.reset_context_command))
        self.telegram_app.add_handler(CommandHandler("help", self.help_command))
        self.telegram_app.add_handler(CallbackQueryHandler(self.handle_button_press))
        self.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.route_message))
        self.telegram_app.add_handler(MessageHandler(filters.VOICE, self.handle_voice_message))

        # Programar limpieza de archivos temporales cada 6 h
        self.telegram_app.job_queue.run_repeating(self.cleanup_temp_files, interval=21600)

    async def async_init(self):
        """Inicialización asíncrona del bot"""
        await self.telegram_app.initialize()
        asyncio.create_task(self.handle_queue())
        logger.info("Bot inicializado correctamente")

    # ------------------------------------------------------------------ #
    #                  COLA PARA EVITAR SOBRECARGA DE OPENAI             #
    # ------------------------------------------------------------------ #
    async def handle_queue(self):
        """Procesar mensajes en cola para evitar sobrecarga"""
        while True:
            chat_id, update, context, message = await self.task_queue.get()
            try:
                await update.message.chat.send_action(action=ChatAction.TYPING)
                response = await self.get_openai_response(chat_id, message)
                await self.send_response(update, chat_id, response)
                self.save_conversation(chat_id, "user", message)
                self.save_conversation(chat_id, "assistant", response)
            except Exception as e:
                logger.error(f"Error procesando mensaje en la cola: {e}")
                await update.message.reply_text("❌ Error procesando tu mensaje. Por favor, intenta nuevamente.")
            finally:
                self.task_queue.task_done()

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enrutar mensajes de texto a la cola de procesamiento"""
        chat_id = update.message.chat.id
        message = clean_text(update.message.text.strip())
        await self.task_queue.put((chat_id, update, context, message))

    # ------------------------------------------------------------------ #
    #                MANEJO DE MENSAJES DE VOZ (ASR + TTS)               #
    # ------------------------------------------------------------------ #
    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejar mensajes de voz: reconocer, procesar y activar respuestas de voz"""
        chat_id = update.message.chat.id
        voice_file = await update.message.voice.get_file()

        # Rutas temporales únicas
        timestamp = int(time.time())
        oga_file = f"{self.temp_dir}/voice_{chat_id}_{timestamp}.oga"
        wav_file = f"{self.temp_dir}/voice_{chat_id}_{timestamp}.wav"

        # Descargar y convertir
        await voice_file.download_to_drive(oga_file)
        await update.message.chat.send_action(action=ChatAction.TYPING)

        if convertOgaToWav(oga_file, wav_file):
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_file) as source:
                audio = recognizer.record(source)
            try:
                language = self.user_preferences.get(chat_id, {}).get('voice_language', 'es')
                if language == 'auto':
                    user_text = recognizer.recognize_google(audio)
                else:
                    user_text = recognizer.recognize_google(audio, language=f"{language}-{language.upper()}")

                user_text = clean_text(user_text)  # ← NUEVO
                self.user_sent_voice.add(chat_id)

                if chat_id not in self.user_preferences:
                    self.save_user_preferences(chat_id, True, 1.0, language, 'female')
                else:
                    p = self.user_preferences[chat_id]
                    self.save_user_preferences(chat_id, True, p['voice_speed'], p['voice_language'], p['voice_gender'])

                await self.task_queue.put((chat_id, update, context, user_text))

            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ No pude entender la nota de voz. ¿Puedes intentar de nuevo?")
            except sr.RequestError as e:
                await update.message.reply_text(f"⚠️ Error en el servicio de reconocimiento de voz: {e}")
            except Exception as e:
                logger.error(f"Error procesando voz: {e}")
                await update.message.reply_text("⚠️ Ocurrió un error al procesar tu nota de voz.")
        else:
            await update.message.reply_text("⚠️ Error procesando audio. Verifica que el formato sea compatible.")

        # Limpiar temporales
        for f in (oga_file, wav_file):
            try:
                os.remove(f)
            except Exception as e:
                logger.error(f"Error eliminando temp: {e}")

    # ------------------------------------------------------------------ #
    #                COMUNICACIÓN CON OPENAI  (Threads API)              #
    # ------------------------------------------------------------------ #
    async def get_openai_response(self, chat_id, message):
        """Obtener respuesta de OpenAI manteniendo contexto usando threads"""
        try:
            thread_id = await self.get_or_create_thread(chat_id)

            await self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=message
            )

            run = await self.client.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=self.ASSISTANT_ID
            )

            max_wait, waited = 300, 0
            while True:
                status = await self.client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
                if status.status == 'completed':
                    break
                elif status.status == 'requires_action':
                    await self.client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                    return "⚠️ Solicité una función no disponible. Reformula tu pregunta."
                elif status.status in ['failed', 'cancelled', 'expired']:
                    raise Exception(f"Run fallido: {status.status}")
                await asyncio.sleep(1)
                waited += 1
                if waited > max_wait:
                    await self.client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                    raise Exception("La respuesta tardó demasiado. Intenta más tarde.")

            messages = await self.client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)
            if messages.data and messages.data[0].content:
                c = messages.data[0].content[0]
                if hasattr(c, 'text'):
                    return remove_source_references(c.text.value)
                return "⚠️ Respuesta en formato no compatible."
            return "⚠️ No obtuve respuesta del asistente."

        except Exception as e:
            logger.error(f"❌ Error en get_openai_response: {e}")
            return "⚠️ Problema procesando tu mensaje. Intenta en unos momentos."

    async def get_or_create_thread(self, chat_id):
        """Devuelve thread existente o crea uno nuevo"""
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]

        thread = await self.client.beta.threads.create()
        thread_id = thread.id
        self.user_threads[chat_id] = thread_id

        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO user_threads (chat_id, thread_id) VALUES (?, ?)',
                (chat_id, thread_id)
            )
            conn.commit()
        return thread_id

    # ------------------------------------------------------------------ #
    #                  RESPUESTA (TEXTO o VOZ) AL USUARIO                #
    # ------------------------------------------------------------------ #
    async def send_response(self, update, chat_id, text):
        """Enviar respuesta como texto o voz"""
        pref = self.user_preferences.get(chat_id, {
            "voice_responses": False,
            "voice_speed": 1.0,
            "voice_language": "es",
            "voice_gender": "female"
        })

        send_voice = pref["voice_responses"] and chat_id in self.user_sent_voice

        if send_voice:
            path = await self.text_to_speech(clean_text(text), pref)  # ← limpieza antes de TTS
            if path:
                with open(path, "rb") as audio:
                    await update.message.reply_voice(voice=audio)
                try:
                    os.remove(path)
                except Exception:
                    pass
            else:
                await update.message.reply_text(text)
        else:
            await update.message.reply_text(text)

    async def text_to_speech(self, text, preferences):
        """Convertir texto a voz con ajustes personalizados"""
        try:
            language = preferences.get('voice_language', 'es')
            speed = preferences.get('voice_speed', 1.0)
            supported_langs = ['es', 'en', 'fr', 'de', 'pt', 'it']
            if language not in supported_langs:
                language = 'es'

            temp_path = f"{self.temp_dir}/tts_{int(time.time())}.mp3"
            gTTS(text=text, lang=language, slow=False).save(temp_path)

            if speed != 1.0:
                audio = AudioSegment.from_mp3(temp_path)
                if speed > 1.0:
                    audio = audio.speedup(playback_speed=speed)
                else:
                    modifier = 1.0 / speed if speed > 0 else 1.0
                    audio = audio._spawn(audio.raw_data, overrides={
                        "frame_rate": int(audio.frame_rate * modifier)
                    }).set_frame_rate(audio.frame_rate)
                audio.export(temp_path, format="mp3")

            return temp_path
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return None

    # ------------------------------------------------------------------ #
    #                       PERSISTENCIA  (INSERTs)                      #
    # ------------------------------------------------------------------ #
    def save_conversation(self, chat_id, role, content):
        """Guardar conversación resumida (rol + contenido)"""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.cursor().execute(
                    'INSERT INTO conversations (chat_id, role, content, timestamp) VALUES (?, ?, ?, datetime("now"))',
                    (chat_id, role, content)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error guardando conversación: {e}")

    def save_user_preferences(self, chat_id, vr, vs, vl, vg):
        """Guardar preferencias de usuario"""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.cursor().execute(
                    'INSERT OR REPLACE INTO user_preferences (chat_id, voice_responses, voice_speed, voice_language, voice_gender) VALUES (?, ?, ?, ?, ?)',
                    (chat_id, vr, vs, vl, vg)
                )
                conn.commit()
            self.user_preferences[chat_id] = {
                'voice_responses': vr,
                'voice_speed': vs,
                'voice_language': vl,
                'voice_gender': vg
            }
        except Exception as e:
            logger.error(f"Error guardando preferencias: {e}")

    # ------------------------------------------------------------------ #
    #                        COMANDOS DEL BOT                            #
    # ------------------------------------------------------------------ #
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 ¡Hola! Soy tu Coach MeditaHub. Envíame texto o nota de voz. "
            "Comandos: /voice, /reset, /help"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🔍 *Guía rápida:*\n"
            "• /start – iniciar\n"
            "• /voice – configuración de voz\n"
            "• /reset – reiniciar contexto\n"
            "• /help – esta ayuda",
            parse_mode=ParseMode.MARKDOWN
        )

    async def voice_settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        p = self.user_preferences.get(chat_id, {
            "voice_responses": False, "voice_speed": 1.0, "voice_language": "es", "voice_gender": "female"
        })
        kb = [
            [InlineKeyboardButton("Activar voz" if not p['voice_responses'] else "Desactivar voz",
                                  callback_data=f"voice_toggle_{int(not p['voice_responses'])}")],
            [InlineKeyboardButton("Más lento", callback_data="voice_speed_down"),
             InlineKeyboardButton("Más rápido", callback_data="voice_speed_up")],
            [InlineKeyboardButton("🇪🇸", callback_data="voice_lang_es"),
             InlineKeyboardButton("🇬🇧", callback_data="voice_lang_en")]
        ]
        await update.message.reply_text(
            f"Voz {'✅' if p['voice_responses'] else '❌'} | Vel {p['voice_speed']}x | Idioma {p['voice_language']}",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    async def reset_context_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.message.chat.id
        if cid in self.user_threads:
            del self.user_threads[cid]
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.cursor().execute('DELETE FROM user_threads WHERE chat_id = ?', (cid,))
                conn.commit()
        await update.message.reply_text("🔄 Contexto reiniciado.")

    async def handle_button_press(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        cid = q.message.chat.id
        data = q.data
        p = self.user_preferences.get(cid, {
            'voice_responses': False, 'voice_speed': 1.0, 'voice_language': 'es', 'voice_gender': 'female'
        })

        if data.startswith("voice_toggle_"):
            p['voice_responses'] = not p['voice_responses']
        elif data == "voice_speed_up":
            p['voice_speed'] = min(p['voice_speed'] + 0.1, 2.0)
        elif data == "voice_speed_down":
            p['voice_speed'] = max(p['voice_speed'] - 0.1, 0.5)
        elif data.startswith("voice_lang_"):
            p['voice_language'] = data.split('_')[-1]

        self.save_user_preferences(cid, p['voice_responses'], p['voice_speed'], p['voice_language'], p['voice_gender'])
        await q.edit_message_text(f"Voz {'✅' if p['voice_responses'] else '❌'} | Vel {p['voice_speed']}x | Idioma {p['voice_language']}")

    # ------------------------------------------------------------------ #
    #                       LIMPIEZA DE ARCHIVOS TEMP                    #
    # ------------------------------------------------------------------ #
    async def cleanup_temp_files(self, context):
        now = time.time()
        removed = 0
        for fname in os.listdir(self.temp_dir):
            fpath = os.path.join(self.temp_dir, fname)
            try:
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 3600:
                    os.remove(fpath)
                    removed += 1
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
        if removed:
            logger.info(f"Limpieza temp: {removed} archivos")

# ================================ #
# Código de arranque de la API     #
# ================================ #
bot = CoachBot()

@app.on_event("startup")
async def startup_event():
    await bot.async_init()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot.telegram_app.bot)
        await bot.telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
