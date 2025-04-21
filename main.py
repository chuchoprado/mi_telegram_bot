# COACHBOT CON FUNCIONES COMPLETAS Y MEJORAS EN RESPUESTA A NOTAS DE VOZ
# INCLUYE: manejo de cola, respuesta en voz si se recibe voz, sin mostrar texto "Has dicho" ni "procesando"
# MEJORAS: persistencia de threads, personalización avanzada de voz, manejo mejorado de errores
# ACTUALIZADO: _init_db() crea también users, messages y context para persistir todo el historial.

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


def remove_source_references(text: str) -> str:
    """Elimina las referencias de fuentes del texto generado por OpenAI"""
    return re.sub(r'\【[\d:]+†source\】', '', text)


def convertOgaToWav(oga_path, wav_path):
    """Convierte archivos de audio de formato OGA a WAV usando ffmpeg"""
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

        self.db_path = os.getenv("DB_PATH", "bot_data.db")
        logger.info(f"📂 Base de datos en → {os.path.abspath(self.db_path)}")
        self.user_preferences = {}
        self.user_threads = {}
        self.user_sent_voice = set()
        self.temp_dir = 'temp_files'

        # Crear directorio temporal si no existe
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)

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
        conservar todos los datos (usuarios, hilos, mensajes, contexto,
        preferencias…) incluso tras reinicios del proceso o despliegues.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()

            # Tabla de conversaciones resumidas (ya existente)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id        INTEGER  PRIMARY KEY AUTOINCREMENT,
                    chat_id   INTEGER,
                    role      TEXT,
                    content   TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Preferencias del usuario (ya existente)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_preferences (
                    chat_id        INTEGER PRIMARY KEY,
                    voice_responses BOOLEAN DEFAULT 0,
                    voice_speed     FLOAT   DEFAULT 1.0,
                    voice_language  TEXT    DEFAULT 'es',
                    voice_gender    TEXT    DEFAULT 'female'
                )
            ''')

            # Persistencia de hilos de OpenAI (ya existente)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_threads (
                    chat_id    INTEGER PRIMARY KEY,
                    thread_id  TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # ---------- NUEVAS TABLAS para historial completo ---------- #
            # Usuarios únicos
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT,
                    first_name  TEXT,
                    last_name   TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Mensajes detallados (útil para analíticas)
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

            # Contexto JSON por chat o thread
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
            rows = cursor.fetchall()
            for row in rows:
                chat_id, voice_responses, voice_speed, voice_language, voice_gender = row
                self.user_preferences[chat_id] = {
                    'voice_responses': bool(voice_responses),
                    'voice_speed': voice_speed,
                    'voice_language': voice_language,
                    'voice_gender': voice_gender
                }

    def _load_user_threads(self):
        """Cargar threads de OpenAI desde la base de datos para mantener el contexto"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT chat_id, thread_id FROM user_threads')
            rows = cursor.fetchall()
            for chat_id, thread_id in rows:
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

        # Programar limpieza de archivos temporales cada 6 horas
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
        message = update.message.text.strip()

        await self.task_queue.put((chat_id, update, context, message))

    # ------------------------------------------------------------------ #
    #                MANEJO DE MENSAJES DE VOZ (ASR + TTS)               #
    # ------------------------------------------------------------------ #
    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejar mensajes de voz: reconocer, procesar y activar respuestas de voz"""
        chat_id = update.message.chat.id
        voice_file = await update.message.voice.get_file()

        # Generar rutas temporales únicas para archivos de audio
        timestamp = int(time.time())
        oga_file = f"{self.temp_dir}/voice_{chat_id}_{timestamp}.oga"
        wav_file = f"{self.temp_dir}/voice_{chat_id}_{timestamp}.wav"

        # Descargar y convertir el archivo de voz
        await voice_file.download_to_drive(oga_file)

        await update.message.chat.send_action(action=ChatAction.TYPING)

        if convertOgaToWav(oga_file, wav_file):
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_file) as source:
                audio = recognizer.record(source)
            try:
                # Detectar automáticamente el idioma o usar preferencia del usuario
                language = self.user_preferences.get(chat_id, {}).get('voice_language', 'es')
                if language == 'auto':
                    user_text = recognizer.recognize_google(audio)
                else:
                    user_text = recognizer.recognize_google(audio, language=f"{language}-{language.upper()}")

                # Activar respuestas por voz para este usuario
                self.user_sent_voice.add(chat_id)
                if chat_id not in self.user_preferences:
                    self.save_user_preferences(chat_id, True, 1.0, language, 'female')
                else:
                    self.user_preferences[chat_id]['voice_responses'] = True
                    self.save_user_preferences(
                        chat_id,
                        True,
                        self.user_preferences[chat_id].get('voice_speed', 1.0),
                        self.user_preferences[chat_id].get('voice_language', language),
                        self.user_preferences[chat_id].get('voice_gender', 'female')
                    )

                # Enviar a procesar
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

        # Limpiar archivos temporales
        try:
            if os.path.exists(oga_file):
                os.remove(oga_file)
            if os.path.exists(wav_file):
                os.remove(wav_file)
        except Exception as e:
            logger.error(f"Error eliminando archivos temporales: {e}")

    # ------------------------------------------------------------------ #
    #                COMUNICACIÓN CON OPENAI  (Threads API)              #
    # ------------------------------------------------------------------ #
    async def get_openai_response(self, chat_id, message):
        """Obtener respuesta de OpenAI manteniendo contexto usando threads"""
        try:
            thread_id = await self.get_or_create_thread(chat_id)

            # Crear mensaje en el thread
            await self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=message
            )

            # Iniciar ejecución del asistente
            run = await self.client.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=self.ASSISTANT_ID
            )

            # Configuración de tiempos de espera
            max_wait_time = 300  # 5 minutos
            check_interval = 1
            total_waited = 0

            # Monitorear estado de la ejecución
            while True:
                run_status = await self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run.id
                )

                if run_status.status == 'completed':
                    break
                elif run_status.status == 'requires_action':
                    # Manejar funciones si el asistente las requiere
                    logger.info("El asistente requiere acciones, pero no hay funciones implementadas")
                    await self.client.beta.threads.runs.cancel(
                        thread_id=thread_id,
                        run_id=run.id
                    )
                    return "⚠️ Solicité una función no disponible. Por favor reformula tu pregunta."
                elif run_status.status in ['failed', 'cancelled', 'expired']:
                    logger.error(f"❌ Run fallido: {run_status.status} - {getattr(run_status, 'last_error', 'Sin detalles')}")
                    raise Exception(f"Falló la ejecución con estado: {run_status.status}")

                await asyncio.sleep(check_interval)
                total_waited += check_interval

                if total_waited > max_wait_time:
                    # Cancelar si toma demasiado tiempo
                    await self.client.beta.threads.runs.cancel(
                        thread_id=thread_id,
                        run_id=run.id
                    )
                    logger.error(f"⚠️ Tiempo de espera excedido para OpenAI en thread {thread_id}")
                    raise Exception("La respuesta del asistente tardó demasiado. Intenta nuevamente más tarde.")

            # Obtener el último mensaje (respuesta del asistente)
            messages = await self.client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            )

            # Manejar diferentes tipos de contenido (texto, imágenes, etc.)
            if messages.data and messages.data[0].content:
                main_content = messages.data[0].content[0]
                if hasattr(main_content, 'text'):
                    return remove_source_references(main_content.text.value)
                else:
                    return "⚠️ Recibí una respuesta en formato no compatible."
            else:
                return "⚠️ No obtuve respuesta del asistente. Intenta nuevamente."

        except Exception as e:
            logger.error(f"❌ Error en get_openai_response: {e}")
            return "⚠️ Hubo un problema procesando tu mensaje. Por favor, intenta nuevamente en unos momentos."

    async def get_or_create_thread(self, chat_id):
        """Obtener thread existente o crear uno nuevo, guardando en DB para persistencia"""
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]

        # Crear nuevo thread
        thread = await self.client.beta.threads.create()
        thread_id = thread.id
        self.user_threads[chat_id] = thread_id

        # Guardar en base de datos para persistencia
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
        """Enviar respuesta: como texto o como nota de voz según las preferencias"""
        pref = self.user_preferences.get(chat_id, {
            "voice_responses": False,
            "voice_speed": 1.0,
            "voice_language": "es",
            "voice_gender": "female"
        })

        # Decidir si enviar voz basado en preferencias y si el usuario envió voz
        send_voice = pref["voice_responses"] and chat_id in self.user_sent_voice

        if send_voice:
            path = await self.text_to_speech(text, pref)
            if path:
                with open(path, "rb") as audio:
                    await update.message.reply_voice(voice=audio)
                try:
                    os.remove(path)
                except Exception:
                    pass
            else:
                # Falló TTS, enviar texto
                await update.message.reply_text(text)
        else:
            # Enviar solo texto
            await update.message.reply_text(text)

    async def text_to_speech(self, text, preferences):
        """Convertir texto a voz con ajustes personalizados"""
        try:
            # Ajustar idioma según preferencias
            language = preferences.get('voice_language', 'es')
            speed = preferences.get('voice_speed', 1.0)

            # Validar idioma
            supported_langs = ['es', 'en', 'fr', 'de', 'pt', 'it']
            if language not in supported_langs:
                language = 'es'  # Default a español si no es compatible

            # Crear archivo temporal único
            timestamp = int(time.time())
            temp_path = f"{self.temp_dir}/tts_{timestamp}.mp3"

            # Generar archivo de voz
            tts = gTTS(text=text, lang=language, slow=False)
            tts.save(temp_path)

            # Ajustar velocidad si es diferente de 1.0
            if speed != 1.0:
                audio = AudioSegment.from_mp3(temp_path)
                # Ajustar velocidad manteniendo el tono (mejor calidad)
                if speed > 1.0:
                    audio = audio.speedup(playback_speed=speed)
                else:
                    # Para ralentizar (speed < 1.0), usamos otro enfoque
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
        """Guardar conversación resumida (rol + contenido) con timestamp"""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT INTO conversations (chat_id, role, content, timestamp) VALUES (?, ?, ?, datetime("now"))',
                    (chat_id, role, content)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error guardando conversación: {e}")

    def save_user_preferences(self, chat_id, voice_responses, voice_speed, voice_language, voice_gender):
        """Guardar preferencias de usuario en la base de datos"""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR REPLACE INTO user_preferences (chat_id, voice_responses, voice_speed, voice_language, voice_gender) VALUES (?, ?, ?, ?, ?)',
                    (chat_id, voice_responses, voice_speed, voice_language, voice_gender)
                )
                conn.commit()

                # Actualizar también en memoria
                self.user_preferences[chat_id] = {
                    'voice_responses': voice_responses,
                    'voice_speed': voice_speed,
                    'voice_language': voice_language,
                    'voice_gender': voice_gender
                }
        except Exception as e:
            logger.error(f"Error guardando preferencias: {e}")

    # ------------------------------------------------------------------ #
    #                        COMANDOS DEL BOT                            #
    # ------------------------------------------------------------------ #
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejar comando /start"""
        chat_id = update.message.chat.id
        welcome_message = (
            "👋 ¡Hola! Soy tu Coach MeditaHub. Puedes enviarme:\n\n"
            "• Mensajes de texto\n"
            "• Notas de voz (responderé con voz también)\n\n"
            "Comandos disponibles:\n"
            "/voice - Configurar opciones de voz\n"
            "/reset - Reiniciar contexto de la conversación\n"
            "/help - Ver ayuda e instrucciones"
        )
        await update.message.reply_text(welcome_message)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostrar ayuda e instrucciones de uso"""
        help_text = (
            "🔍 *Guía de uso de Coach MeditaHub:*\n\n"
            "*Comandos disponibles:*\n"
            "• /start - Iniciar o reiniciar el bot\n"
            "• /voice - Configurar opciones de voz\n"
            "• /reset - Borrar el contexto de la conversación\n"
            "• /help - Mostrar esta ayuda\n\n"
            "*Características:*\n"
            "• Puedes enviar mensajes de texto o notas de voz\n"
            "• El bot recuerda el contexto de tus conversaciones\n"
            "• Si envías una nota de voz, el bot responderá con voz\n"
            "• Puedes personalizar la velocidad e idioma de las respuestas de voz\n\n"
            "*Consejos:*\n"
            "• Sé específico en tus preguntas para obtener mejores respuestas\n"
            "• Si el bot no entiende tu nota de voz, intenta hablar más claramente\n"
            "• Usa /reset si quieres comenzar una conversación nueva"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

    async def voice_settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostrar y permitir cambiar configuración de voz con botones"""
        chat_id = update.message.chat.id
        pref = self.user_preferences.get(chat_id, {
            "voice_responses": False,
            "voice_speed": 1.0,
            "voice_language": "es",
            "voice_gender": "female"
        })

        # Crear teclado inline para controlar ajustes
        keyboard = [
            [
                InlineKeyboardButton("🔈 Activar voz" if not pref["voice_responses"] else "🔇 Desactivar voz",
                                     callback_data=f"voice_toggle_{1 if not pref['voice_responses'] else 0}")
            ],
            [
                InlineKeyboardButton("⏪ Más lento", callback_data="voice_speed_down"),
                InlineKeyboardButton("⏩ Más rápido", callback_data="voice_speed_up")
            ],
            [
                InlineKeyboardButton("🇪🇸 Español", callback_data="voice_lang_es"),
                InlineKeyboardButton("🇬🇧 English", callback_data="voice_lang_en")
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        # Mostrar configuración actual
        msg = (
            f"🎙 *Configuración de voz:*\n\n"
            f"• Estado: {'✅ Activada' if pref['voice_responses'] else '❌ Desactivada'}\n"
            f"• Velocidad: {pref['voice_speed']}x\n"
            f"• Idioma: {pref['voice_language'].upper()}\n\n"
            f"Usa los botones para ajustar la configuración:"
        )

        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    async def reset_context_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reiniciar contexto de la conversación (crear nuevo thread)"""
        chat_id = update.message.chat.id

        # Eliminar thread de la memoria y base de datos
        if chat_id in self.user_threads:
            del self.user_threads[chat_id]

            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM user_threads WHERE chat_id = ?', (chat_id,))
                conn.commit()

        await update.message.reply_text("🔄 El contexto de la conversación ha sido reiniciado. Estamos empezando desde cero.")

        # Crear nuevo thread inmediatamente
        thread_id = await self.get_or_create_thread(chat_id)
        logger.info(f"Nuevo thread creado para chat_id {chat_id}: {thread_id}")

    # ------------------------------------------------------------------ #
    #                      INLINE BUTTONS / CALLBACKS                    #
    # ------------------------------------------------------------------ #
    async def handle_button_press(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejar botones de configuración"""
        query = update.callback_query
        await query.answer()

        chat_id = query.message.chat_id
        data = query.data
        pref = self.user_preferences.get(chat_id, {
            "voice_responses": False,
            "voice_speed": 1.0,
            "voice_language": "es",
            "voice_gender": "female"
        })

        # Procesar diferentes acciones
        if data.startswith("voice_toggle_"):
            value = int(data.split("_")[-1])
            pref["voice_responses"] = bool(value)
            update_msg = f"🎙 Respuestas de voz {'activadas' if value else 'desactivadas'}"

        elif data == "voice_speed_up":
            current = pref["voice_speed"]
            if current < 2.0:  # Límite máximo
                pref["voice_speed"] = round(current + 0.1, 1)
            update_msg = f"🎙 Velocidad ajustada a {pref['voice_speed']}x"

        elif data == "voice_speed_down":
            current = pref["voice_speed"]
            if current > 0.5:  # Límite mínimo
                pref["voice_speed"] = round(current - 0.1, 1)
            update_msg = f"🎙 Velocidad ajustada a {pref['voice_speed']}x"

        elif data.startswith("voice_lang_"):
            lang = data.split("_")[-1]
            pref["voice_language"] = lang
            lang_names = {"es": "Español", "en": "English", "fr": "Français",
                          "de": "Deutsch", "pt": "Português", "it": "Italiano"}
            update_msg = f"🎙 Idioma cambiado a {lang_names.get(lang, lang.upper())}"

        else:
            update_msg = "⚠️ Opción no reconocida"

        # Guardar cambios
        self.save_user_preferences(
            chat_id,
            pref["voice_responses"],
            pref["voice_speed"],
            pref["voice_language"],
            pref["voice_gender"]
        )

        # Actualizar mensaje con configuración actual
        keyboard = [
            [
                InlineKeyboardButton("🔈 Activar voz" if not pref["voice_responses"] else "🔇 Desactivar voz",
                                     callback_data=f"voice_toggle_{1 if not pref['voice_responses'] else 0}")
            ],
            [
                InlineKeyboardButton("⏪ Más lento", callback_data="voice_speed_down"),
                InlineKeyboardButton("⏩ Más rápido", callback_data="voice_speed_up")
            ],
            [
                InlineKeyboardButton("🇪🇸 Español", callback_data="voice_lang_es"),
                InlineKeyboardButton("🇬🇧 English", callback_data="voice_lang_en")
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = (
            f"🎙 *Configuración de voz:*\n\n"
            f"• Estado: {'✅ Activada' if pref['voice_responses'] else '❌ Desactivada'}\n"
            f"• Velocidad: {pref['voice_speed']}x\n"
            f"• Idioma: {pref['voice_language'].upper()}\n\n"
            f"Usa los botones para ajustar la configuración:"
        )

        await query.edit_message_text(
            text=msg,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

        # Enviar notificación de cambio
        await context.bot.send_message(
            chat_id=chat_id,
            text=update_msg
        )

    # ------------------------------------------------------------------ #
    #                       LIMPIEZA DE ARCHIVOS TEMP                    #
    # ------------------------------------------------------------------ #
    async def cleanup_temp_files(self, context):
        """Limpiar archivos temporales periódicamente"""
        try:
            now = time.time()
            count = 0

            for filename in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, filename)

                # Si el archivo tiene más de 1 hora, eliminarlo
                if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > 3600:
                    os.remove(file_path)
                    count += 1

            logger.info(f"Limpieza completada: {count} archivos temporales eliminados")

            # Limpiar registros antiguos de la base de datos (mayores a 30 días)
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM conversations WHERE timestamp < datetime("now", "-30 day")')
                deleted_rows = cursor.rowcount
                conn.commit()
                if deleted_rows > 0:
                    logger.info(f"Limpieza de base de datos: {deleted_rows} registros antiguos eliminados")

        except Exception as e:
            logger.error(f"Error durante la limpieza de archivos temporales: {e}")


# ================================ #
# Código de arranque de la API     #
# ================================ #

bot = CoachBot()


@app.on_event("startup")
async def startup_event():
    """Evento de inicio de la aplicación FastAPI"""
    await bot.async_init()


@app.post("/webhook")
async def webhook(request: Request):
    """Endpoint para recibir actualizaciones de Telegram"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot.telegram_app.bot)
        await bot.telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    # Para ejecutar directamente con uvicorn
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
