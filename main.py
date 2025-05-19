import os
import asyncio
import httpx
import sqlite3
import json
import hashlib
import logging
import openai
import time
import shutil
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
from openai import AsyncOpenAI
import speech_recognition as sr
from contextlib import closing
from gtts import gTTS, gTTSError
from pydub import AudioSegment
from functools import lru_cache
from pydub import AudioSegment
import subprocess
import re
import datetime

# ──────────────────── LOGGING ──────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = FastAPI()

# ──────────────────── NUEVO: LIMPIAR EMOJIS ──────────────────────
def clean_text(text: str) -> str:
    """
    Elimina:
    • Emojis Unicode
    • Emoticonos ASCII
    • Referencias de fuente  【..】
    • Símbolos de formato Markdown (* _ ~ ` # > - •)
    """
    # Emojis
    emoji_re = re.compile(
        "["                 # rangos unicode
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "]+", flags=re.UNICODE)
    text = emoji_re.sub("", text)

    # Emoticonos ASCII comunes
    emot_re = re.compile(r'(:\)|:\(|;\)|:-\)|:-\(|;D|:D|<3)')
    text = emot_re.sub("", text)

    # Referencias de fuente OpenAI
    text = re.sub(r'\【[\d:]+†source\】', '', text)

    # Símbolos de formato / listas / viñetas
    text = re.sub(r'[*_~`>#•\-]+', ' ', text)

    # Colapsar espacios
    return re.sub(r'\s{2,}', ' ', text).strip()

def remove_source_references(text: str) -> str:
    return re.sub(r'\【[\d:]+†source\】', '', text)

def convertOgaToWav(oga_path: str, wav_path: str) -> bool:
    """Convierte OGA→WAV usando ffmpeg"""
    try:
        subprocess.run(["ffmpeg", "-i", oga_path, wav_path],
                       check=True, timeout=60)
        return True
    except Exception as e:
        logger.error(f"Error al convertir audio: {e}")
        return False

# ────────────────────── CLASE PRINCIPAL ─────────────────────────
class CoachBot:
    class TTSManager:
    def __init__(self, temp_dir="temp_files", max_retries=3, cache_size=100):
        """
        Gestor de texto a voz con caché y manejo de límites de tasa
        
        Args:
            temp_dir: Directorio para archivos temporales
            max_retries: Número máximo de reintentos ante errores
            cache_size: Tamaño de la caché LRU para resultados
        """
        self.temp_dir = temp_dir
        self.max_retries = max_retries
        self.last_request_time = 0
        self.min_request_interval = 1.0  # Segundos entre solicitudes
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # Usamos caché LRU decorador en el método interno
        self._cached_tts = lru_cache(maxsize=cache_size)(self._generate_tts)
    
    async def text_to_speech(self, text, preferences):
        """
        Convierte texto a voz con manejo de errores y caché
        
        Args:
            text: Texto a convertir
            preferences: Diccionario con preferencias de voz
            
        Returns:
            Ruta al archivo de audio o None si hay error
        """
        lang = preferences.get("voice_language", "es")
        speed = preferences.get("voice_speed", 1.0)
        
        # Generamos un hash único para esta combinación de texto/idioma
        text_hash = hashlib.md5(f"{text}:{lang}".encode()).hexdigest()
        
        try:
            # Respeta límite de tasa
            await self._rate_limit()
            
            # Usa el método cacheado (el LRU cache solo funciona en métodos síncronos)
            base_path = self._cached_tts(text, lang, text_hash)
            
            if not base_path:
                return None
                
            # Aplicamos ajuste de velocidad si es necesario
            if speed != 1.0:
                return await self._adjust_speed(base_path, speed)
            return base_path
            
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return None
    
    def _generate_tts(self, text, lang, text_hash):
        """
        Genera el archivo TTS con manejo de reintentos
        """
        output_path = f"{self.temp_dir}/tts_{text_hash}.mp3"
        
        # Si ya existe el archivo, lo devolvemos directamente
        if os.path.exists(output_path):
            return output_path
        
        # Sistema de reintento con espera exponencial
        for attempt in range(self.max_retries):
            try:
                # Actualizamos timestamp de última solicitud
                self.last_request_time = time.time()
                
                # Generamos el audio
                gTTS(text=text, lang=lang, slow=False).save(output_path)
                return output_path
                
            except gTTSError as e:
                # Si es un error 429, esperamos más tiempo antes de reintentar
                if "429" in str(e):
                    wait_time = (2 ** attempt) + 1  # Espera exponencial: 1s, 3s, 7s...
                    logger.warning(f"gTTS rate limit hit. Esperando {wait_time}s antes de reintentar.")
                    time.sleep(wait_time)
                else:
                    # Otro tipo de error, registramos y fallamos
                    logger.error(f"Error gTTS: {e}")
                    return None
                    
            except Exception as e:
                logger.error(f"Error inesperado en TTS: {e}")
                return None
                
        # Si llegamos aquí, agotamos los reintentos
        logger.error(f"Se agotaron los reintentos para generar TTS")
        return None
        
    async def _rate_limit(self):
        """
        Implementa limitación de tasa simple para evitar errores 429
        """
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            await asyncio.sleep(self.min_request_interval - elapsed)
    
    async def _adjust_speed(self, mp3_path, speed):
        """
        Ajusta la velocidad del audio
        """
        try:
            output_path = f"{mp3_path.rsplit('.', 1)[0]}_speed{speed}.mp3"
            
            # Si ya existe este ajuste, devolverlo
            if os.path.exists(output_path):
                return output_path
                
            audio = AudioSegment.from_mp3(mp3_path)
            
            if speed > 1.0:
                audio = audio.speedup(playback_speed=speed)
            else:
                factor = 1.0 / speed
                audio = audio._spawn(
                    audio.raw_data,
                    overrides={"frame_rate": int(audio.frame_rate * factor)}
                ).set_frame_rate(audio.frame_rate)
                
            audio.export(output_path, format="mp3")
            return output_path
            
        except Exception as e:
            logger.error(f"Error ajustando velocidad: {e}")
            return mp3_path  # Devolvemos el original si hay error


# Ahora modificamos la clase CoachBot para usar este gestor TTS

# En CoachBot.__init__, añadir:
# self.tts_manager = TTSManager(temp_dir=self.temp_dir)

# Reemplazar el método text_to_speech con:
async def text_to_speech(self, txt: str, pref: dict) -> str | None:
    """
    Convierte texto a voz usando el gestor TTS con caché y límites
    """
    return await self.tts_manager.text_to_speech(txt, pref)
    def __init__(self):
        self.TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        self.ASSISTANT_ID = os.getenv("ASSISTANT_ID")

        if not all([self.TELEGRAM_TOKEN, self.OPENAI_API_KEY, self.ASSISTANT_ID]):
            raise EnvironmentError("Faltan variables de entorno necesarias")

        self.client = AsyncOpenAI(api_key=self.OPENAI_API_KEY)
        self.telegram_app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self.task_queue = asyncio.Queue()

        self.db_path = os.getenv("DB_PATH", "bot_data.db")
        logger.info(f"📂 Base de datos en → {os.path.abspath(self.db_path)}")

        self.user_preferences: dict[int, dict] = {}
        self.user_threads: dict[int, str] = {}
        self.user_sent_voice: set[int] = set()
        self.temp_dir = "temp_files"
        os.makedirs(self.temp_dir, exist_ok=True)

        self._init_db()
        self._load_user_preferences()
        self._load_user_threads()
        self.setup_handlers()

    # ─────────────── BDD ───────────────
    def _init_db(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.cursor()
            cur.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_preferences (
                chat_id INTEGER PRIMARY KEY,
                voice_responses BOOLEAN DEFAULT 0,
                voice_speed FLOAT DEFAULT 1.0,
                voice_language TEXT DEFAULT 'es',
                voice_gender TEXT DEFAULT 'female'
            );
            CREATE TABLE IF NOT EXISTS user_threads (
                chat_id INTEGER PRIMARY KEY,
                thread_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_bot BOOLEAN,
                FOREIGN KEY (chat_id) REFERENCES user_threads(chat_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS context (
                context_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                thread_id TEXT,
                context_data TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES user_threads(chat_id)
            );
            """)
            conn.commit()

    def _load_user_preferences(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.cursor()
            for row in cur.execute(
                "SELECT chat_id, voice_responses, voice_speed, voice_language, voice_gender FROM user_preferences"
            ):
                cid, vr, vs, vl, vg = row
                self.user_preferences[cid] = {
                    "voice_responses": bool(vr),
                    "voice_speed": vs,
                    "voice_language": vl,
                    "voice_gender": vg,
                }

    def _load_user_threads(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.cursor()
            for cid, tid in cur.execute("SELECT chat_id, thread_id FROM user_threads"):
                self.user_threads[cid] = tid

    # ───────────── HANDLERS ─────────────
    def setup_handlers(self):
        tp = self.telegram_app
        tp.add_handler(CommandHandler("start", self.start_command))
        tp.add_handler(CommandHandler("voice", self.voice_settings_command))
        tp.add_handler(CommandHandler("reset", self.reset_context_command))
        tp.add_handler(CommandHandler("help", self.help_command))
        tp.add_handler(CallbackQueryHandler(self.handle_button_press))
        tp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.route_message))
        tp.add_handler(MessageHandler(filters.VOICE, self.handle_voice_message))
        tp.job_queue.run_repeating(self.cleanup_temp_files, interval=21600)

    async def async_init(self):
        await self.telegram_app.initialize()
        asyncio.create_task(self.handle_queue())
        logger.info("Bot inicializado correctamente")

    # ───────────────────── COLA ──────────────────────
    async def handle_queue(self):
        while True:
            chat_id, update, context, msg = await self.task_queue.get()
            try:
                await update.message.chat.send_action(ChatAction.TYPING)
                resp = await self.get_openai_response(chat_id, msg)
                await self.send_response(update, chat_id, resp)
                self.save_conversation(chat_id, "user", msg)
                self.save_conversation(chat_id, "assistant", resp)
            except Exception as e:
                logger.error(f"Error cola: {e}")
                await update.message.reply_text("❌ Error procesando tu mensaje.")
            finally:
                self.task_queue.task_done()

    # ──────────── ROUTING TEXTO ────────────
    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.message.chat.id
        msg = clean_text(update.message.text.strip())
        await self.task_queue.put((cid, update, context, msg))

    # ──────────── MANEJO VOZ ────────────
    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.message.chat.id
        voice_file = await update.message.voice.get_file()
        ts = int(time.time())
        oga = f"{self.temp_dir}/voice_{cid}_{ts}.oga"
        wav = f"{self.temp_dir}/voice_{cid}_{ts}.wav"
        await voice_file.download_to_drive(oga)
        await update.message.chat.send_action(ChatAction.TYPING)

        if convertOgaToWav(oga, wav):
            r = sr.Recognizer()
            with sr.AudioFile(wav) as src:
                audio = r.record(src)
            try:
                lang = self.user_preferences.get(cid, {}).get("voice_language", "es")
                user_text = (r.recognize_google(audio)
                             if lang == "auto"
                             else r.recognize_google(audio, language=f"{lang}-{lang.upper()}"))
                user_text = clean_text(user_text)
                self.user_sent_voice.add(cid)

                # Activar preferencias si no existían
                if cid not in self.user_preferences:
                    self.save_user_preferences(cid, True, 1.0, lang, "female")
                else:
                    p = self.user_preferences[cid]
                    self.save_user_preferences(cid, True, p["voice_speed"], p["voice_language"], p["voice_gender"])

                await self.task_queue.put((cid, update, context, user_text))
            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ No pude entender la nota de voz.")
            except Exception as e:
                logger.error(f"Voz error: {e}")
                await update.message.reply_text("⚠️ Error procesando tu voz.")
        else:
            await update.message.reply_text("⚠️ Error convirtiendo audio.")

        # limpiar temp
        for f in (oga, wav):
            try:
                os.remove(f)
            except Exception:
                pass

    # ──────────── OPENAI THREADS ────────────
    async def get_openai_response(self, chat_id: int, message: str) -> str:
        try:
            thread_id = await self.get_or_create_thread(chat_id)
            await self.client.beta.threads.messages.create(
                thread_id=thread_id, role="user", content=message
            )
            run = await self.client.beta.threads.runs.create(
                thread_id=thread_id, assistant_id=self.ASSISTANT_ID
            )

            timeout, waited = 300, 0
            while True:
                status = await self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id, run_id=run.id
                )
                if status.status == "completed":
                    break
                if status.status == "requires_action":
                    await self.client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                    return "⚠️ El asistente pidió una acción no disponible."
                if status.status in {"failed", "cancelled", "expired"}:
                    raise RuntimeError(f"Run {status.status}")
                await asyncio.sleep(1)
                waited += 1
                if waited >= timeout:
                    await self.client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                    raise TimeoutError("Tiempo de espera excedido")

            msgs = await self.client.beta.threads.messages.list(
                thread_id=thread_id, order="desc", limit=1
            )
            if msgs.data and msgs.data[0].content:
                return remove_source_references(msgs.data[0].content[0].text.value)
            return "⚠️ Sin respuesta del asistente."
        except Exception as e:
            logger.error(f"get_openai_response: {e}")
            return "⚠️ Problema procesando tu mensaje."

    async def get_or_create_thread(self, chat_id: int) -> str:
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]
        thread = await self.client.beta.threads.create()
        tid = thread.id
        self.user_threads[chat_id] = tid
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT OR REPLACE INTO user_threads VALUES (?,?)", (chat_id, tid))
            conn.commit()
        return tid

    # ──────────── RESPUESTA AL USUARIO ────────────
    async def send_response(self, update: Update, cid: int, text: str):
        pref = self.user_preferences.get(cid, {
            "voice_responses": False, "voice_speed": 1.0,
            "voice_language": "es", "voice_gender": "female"
        })
        if pref["voice_responses"] and cid in self.user_sent_voice:
            path = await self.text_to_speech(clean_text(text), pref)
            if path:
                with open(path, "rb") as a:
                    await update.message.reply_voice(voice=a)
                try:
                    os.remove(path)
                except Exception:
                    pass
            else:
                await update.message.reply_text(text)
        else:
            await update.message.reply_text(text)

    async def text_to_speech(self, txt: str, pref: dict) -> str | None:
        try:
            lang = pref.get("voice_language", "es")
            speed = pref.get("voice_speed", 1.0)
            tmp = f"{self.temp_dir}/tts_{int(time.time())}.mp3"
            gTTS(text=txt, lang=lang, slow=False).save(tmp)

            if speed != 1.0:
                audio = AudioSegment.from_mp3(tmp)
                if speed > 1.0:
                    audio = audio.speedup(playback_speed=speed)
                else:
                    factor = 1.0 / speed
                    audio = audio._spawn(
                        audio.raw_data,
                        overrides={"frame_rate": int(audio.frame_rate * factor)}
                    ).set_frame_rate(audio.frame_rate)
                audio.export(tmp, format="mp3")
            return tmp
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return None

    # ──────────── PERSISTENCIA SIMPLE ────────────
    def save_conversation(self, cid: int, role: str, content: str):
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    "INSERT INTO conversations (chat_id, role, content) VALUES (?,?,?)",
                    (cid, role, content)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Save conv error: {e}")

    def save_user_preferences(self, cid: int, vr: bool, vs: float, vl: str, vg: str):
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO user_preferences VALUES (?,?,?,?,?)",
                    (cid, vr, vs, vl, vg)
                )
                conn.commit()
            self.user_preferences[cid] = {
                "voice_responses": vr, "voice_speed": vs,
                "voice_language": vl, "voice_gender": vg
            }
        except Exception as e:
            logger.error(f"Prefs error: {e}")

    # ──────────── COMANDOS ─────────────
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 ¡Hola! Soy tu Coach MeditaHub.\n"
            "Envíame texto o nota de voz.\n"
            "Comandos: /voice, /reset, /help"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "*Guía rápida:*\n"
            "• /start – iniciar\n• /voice – voz\n• /reset – nuevo contexto",
            parse_mode=ParseMode.MARKDOWN
        )

    async def voice_settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.message.chat.id
        p = self.user_preferences.get(cid, {
            "voice_responses": False, "voice_speed": 1.0,
            "voice_language": "es", "voice_gender": "female"
        })
        kb = [
            [InlineKeyboardButton(
                "Activar voz" if not p["voice_responses"] else "Desactivar voz",
                callback_data=f"voice_toggle_{int(not p['voice_responses'])}"
            )],
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
                conn.execute("DELETE FROM user_threads WHERE chat_id=?", (cid,))
                conn.commit()
        await update.message.reply_text("🔄 Contexto reiniciado.")

    async def handle_button_press(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        cid = q.message.chat.id
        data = q.data
        p = self.user_preferences.get(cid, {
            "voice_responses": False, "voice_speed": 1.0,
            "voice_language": "es", "voice_gender": "female"
        })
        if data.startswith("voice_toggle_"):
            p["voice_responses"] = not p["voice_responses"]
        elif data == "voice_speed_up":
            p["voice_speed"] = min(p["voice_speed"] + 0.1, 2.0)
        elif data == "voice_speed_down":
            p["voice_speed"] = max(p["voice_speed"] - 0.1, 0.5)
        elif data.startswith("voice_lang_"):
            p["voice_language"] = data.split("_")[-1]
        self.save_user_preferences(
            cid, p["voice_responses"], p["voice_speed"],
            p["voice_language"], p["voice_gender"]
        )
        await q.edit_message_text(
            f"Voz {'✅' if p['voice_responses'] else '❌'} | Vel {p['voice_speed']}x | Idioma {p['voice_language']}"
        )

    # ──────────── LIMPIEZA TEMP ────────────
    async def cleanup_temp_files(self, context):
        now = time.time()
        count = 0
        for fname in os.listdir(self.temp_dir):
            fpath = os.path.join(self.temp_dir, fname)
            try:
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 3600:
                    os.remove(fpath)
                    count += 1
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
        if count:
            logger.info(f"Limpieza: {count} archivos temp")

# ─────────────── FASTAPI ARRANQUE ─────────────────
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
# En el método __init__ de CoachBot, añade:
self.tts_manager = TTSManager(temp_dir=self.temp_dir)

# Reemplaza tu método actual text_to_speech por:
async def text_to_speech(self, txt: str, pref: dict) -> str | None:
    """
    Convierte texto a voz usando el gestor TTS con caché y límites
    """
    return await self.tts_manager.text_to_speech(txt, pref)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

