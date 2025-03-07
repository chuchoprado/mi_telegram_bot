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
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openai import AsyncOpenAI
import speech_recognition as sr
from contextlib import closing
import string
import subprocess

def extract_product_keywords(query: str) -> str:
    """
    Extrae palabras clave relevantes eliminando saludos, agradecimientos, puntuación y palabras comunes
    que no aportan a la búsqueda de productos.
    """
    stopwords = {
        "hola", "podrias", "recomendarme", "recomiendes", "por", "favor", "un", "una",
        "que", "me", "ayude", "a", "dame", "los", "las", "el", "la", "de", "en", "con",
        "puedes", "puedo", "ok", "ayudarme", "recomendandome", "y", "necesito", "gracias", "adicional"
    }
    translator = str.maketrans('', '', string.punctuation)
    cleaned_query = query.translate(translator)
    words = cleaned_query.split()
    keywords = [word for word in words if word.lower() not in stopwords]
    return " ".join(keywords)

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
        self.credentials_path = '/etc/secrets/credentials.json'
        self.client = AsyncOpenAI(api_key=required_env_vars['OPENAI_API_KEY'])
        self.sheets_service = None
        self.started = False
        self.verified_users = {}
        self.conversation_history = {}
        self.user_threads = {}
        self.pending_requests = set()
        self.db_path = 'bot_data.db'
        self.telegram_app = Application.builder().token(self.TELEGRAM_TOKEN).build()
        self._init_db()
        self.setup_handlers()
        self._init_sheets()

    def _init_db(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    username TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    role TEXT,
                    content TEXT,
                    FOREIGN KEY (chat_id) REFERENCES users (chat_id)
                )
            ''')
            conn.commit()

    async def get_or_create_thread(self, chat_id):
        if chat_id in self.user_threads:
            return self.user_threads[chat_id]
        try:
            thread = await self.client.beta.threads.create()
            self.user_threads[chat_id] = thread.id
            return thread.id
        except Exception as e:
            logger.error(f"❌ Error creando thread para {chat_id}: {e}")
            return None

    async def send_message_to_assistant(self, chat_id: int, user_message: str) -> str:
        if chat_id in self.pending_requests:
            return "⏳ Ya estoy procesando tu solicitud anterior. Por favor espera."
        self.pending_requests.add(chat_id)
        try:
            thread_id = await self.get_or_create_thread(chat_id)
            if not thread_id:
                self.pending_requests.remove(chat_id)
                return "❌ No se pudo establecer conexión con el asistente."
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
                    raise Exception(f"Run failed with status: {run_status.status}")
                elif time.time() - start_time > 60:
                    raise TimeoutError("La consulta al asistente tomó demasiado tiempo.")
                await asyncio.sleep(1)
            messages = await self.client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            )
            if not messages.data or not messages.data[0].content:
                self.pending_requests.remove(chat_id)
                return "⚠️ La respuesta del asistente está vacía. Inténtalo más tarde."
            assistant_message = messages.data[0].content[0].text.value
            self.conversation_history.setdefault(chat_id, []).append({
                "role": "assistant",
                "content": assistant_message
            })
            return assistant_message
        except Exception as e:
            logger.error(f"❌ Error procesando mensaje: {e}")
            return "⚠️ Ocurrió un error al procesar tu mensaje."
        finally:
            if chat_id in self.pending_requests:
                self.pending_requests.remove(chat_id)

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str) -> str:
        try:
            chat_id = update.message.chat.id
            if not user_message.strip():
                return "⚠️ No se recibió un mensaje válido."
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            filtered_query = extract_product_keywords(user_message)
            product_keywords = ['producto', 'productos', 'comprar', 'precio', 'costo', 'tienda', 'venta',
                                'suplemento', 'meditacion', 'vitaminas', 'vitamina', 'suplementos',
                                'libro', 'libros', 'ebook', 'ebooks', 'amazon']
            if any(keyword in filtered_query.lower() for keyword in product_keywords):
                response = await self.process_product_query(chat_id, user_message)
                self.save_conversation(chat_id, "user", user_message)
                self.save_conversation(chat_id, "assistant", response)
                return response
            response = await self.send_message_to_assistant(chat_id, user_message)
            if not response.strip():
                logger.error("⚠️ OpenAI devolvió una respuesta vacía.")
                return "⚠️ No obtuve una respuesta válida del asistente. Intenta de nuevo."
            self.save_conversation(chat_id, "user", user_message)
            self.save_conversation(chat_id, "assistant", response)
            return response
        except Exception as e:
            logger.error(f"❌ Error en process_text_message: {e}", exc_info=True)
            return "⚠️ Ocurrió un error al procesar tu mensaje."

    async def process_product_query(self, chat_id: int, query: str) -> str:
        try:
            logger.info(f"Procesando consulta de productos para {chat_id}: {query}")
            filtered_query = extract_product_keywords(query)
            logger.info(f"Consulta filtrada: {filtered_query}")
            products = await self.fetch_products(filtered_query)
            if not products or not isinstance(products, dict):
                logger.error(f"Respuesta inválida del API de productos: {products}")
                return "⚠️ No se pudieron recuperar productos en este momento."
            if "error" in products:
                logger.error(f"Error desde API de productos: {products['error']}")
                return f"⚠️ {products['error']}"
            product_data = products.get("data", [])
            if not product_data:
                return "📦 No encontré productos que coincidan con tu consulta. ¿Puedes ser más específico?"
            product_data = product_data[:5]
            product_list = []
            for p in product_data:
                title = p.get('titulo') or p.get('fuente', 'Sin título')
                desc = p.get('descripcion', 'Sin descripción')
                link = p.get('link', 'No disponible')
                if len(desc) > 100:
                    desc = desc[:97] + "..."
                product_list.append(f"- *{title}*: {desc}\n  🔗 [Ver producto]({link})")
            formatted_products = "\n\n".join(product_list)
            return f"🔍 *Productos recomendados:*\n\n{formatted_products}\n\n¿Necesitas más información sobre alguno de estos productos?"
        except Exception as e:
            logger.error(f"❌ Error procesando consulta de productos: {e}", exc_info=True)
            return "⚠️ Ocurrió un error al buscar productos. Por favor, intenta más tarde."

    async def fetch_products(self, query):
        url = "https://script.google.com/macros/s/AKfycbzb1VZCKQgMCtOyHeC8QX_0lS0qHzue3HNeNf9YqdT7gP3EgXfoFuO-SQ8igHvZ5As0_A/exec"
        params = {"query": query}
        logger.info(f"Consultando Google Sheets con: {params}")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, params=params, follow_redirects=True)
            if response.status_code != 200:
                logger.error(f"Error en API de Google Sheets: {response.status_code}, {response.text}")
                return {"error": f"Error del servidor ({response.status_code})"}
            try:
                result = response.json()
                logger.info("JSON recibido correctamente de la API")
                return result
            except json.JSONDecodeError as e:
                logger.error(f"Error decodificando JSON: {e}, respuesta: {response.text[:200]}")
                return {"error": "Formato de respuesta inválido"}
        except httpx.TimeoutException:
            logger.error("⏳ La API de Google Sheets tardó demasiado en responder.")
            return {"error": "⏳ Tiempo de espera agotado. Inténtalo más tarde."}
        except httpx.RequestError as e:
            logger.error(f"❌ Error de conexión a Google Sheets: {e}")
            return {"error": "Error de conexión a la base de datos de productos"}
        except Exception as e:
            logger.error(f"❌ Error inesperado consultando Google Sheets: {e}")
            return {"error": "Error inesperado consultando productos"}

    def searchProducts(self, data, query, start, limit):
        results = []
        count = 0
        queryWords = query.split()
        for i in range(start, len(data)):
            if not data[i] or len(data[i]) < 6:
                continue
            categoria = normalizeText(data[i][0]) if data[i][0] else ""
            etiquetas = normalizeText(data[i][1].replace("#", "")) if data[i][1] else ""
            titulo = normalizeText(data[i][2]) if data[i][2] else ""
            link = data[i][3].strip() if data[i][3] else ""
            description = data[i][4].strip() if data[i][4] else ""
            autor = normalizeText(data[i][5]) if data[i][5] else "desconocido"
            match = any(word in categoria or word in etiquetas or word in titulo or word in autor for word in queryWords)
            if match and link != "":
                results.append({"link": link, "descripcion": description, "fuente": autor})
                count += 1
            if count >= limit:
                break
        return results

    def setup_handlers(self):
        try:
            self.telegram_app.add_handler(CommandHandler("start", self.start_command))
            self.telegram_app.add_handler(CommandHandler("help", self.help_command))
            self.telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.route_message))
            self.telegram_app.add_handler(MessageHandler(filters.VOICE, self.handle_voice_message))
            logger.info("Handlers configurados correctamente")
        except Exception as e:
            logger.error(f"Error en setup_handlers: {e}")
            raise

    def load_verified_users(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT chat_id, email FROM users')
            rows = cursor.fetchall()
            for chat_id, email in rows:
                self.verified_users[chat_id] = email

    def save_verified_user(self, chat_id, email, username):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO users (chat_id, email, username)
                VALUES (?, ?, ?)
            ''', (chat_id, email, username))
            conn.commit()

    def save_conversation(self, chat_id, role, content):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO conversations (chat_id, role, content)
                VALUES (?, ?, ?)
            ''', (chat_id, role, content))
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
            try:
                self.sheets_service.spreadsheets().get(
                    spreadsheetId=self.SPREADSHEET_ID
                ).execute()
                logger.info("Conexión con Google Sheets inicializada correctamente.")
                return True
            except Exception as e:
                logger.error(f"Error accediendo al spreadsheet: {e}")
                return False
        except Exception as e:
            logger.error(f"Error inicializando Google Sheets: {e}")
            return False

    async def async_init(self):
        try:
            await self.telegram_app.initialize()
            self.load_verified_users()
            if not self.started:
                self.started = True
                await self.telegram_app.start()
            logger.info("Bot inicializado correctamente")
        except Exception as e:
            logger.error(f"Error en async_init: {e}")
            raise

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            chat_id = update.message.chat.id
            if chat_id in self.verified_users:
                await update.message.reply_text("👋 ¡Bienvenido de nuevo! ¿En qué puedo ayudarte hoy?")
            else:
                await update.message.reply_text("👋 ¡Hola! Por favor, proporciona tu email para comenzar.\n\n📧 Debe ser un email autorizado para usar el servicio.")
            logger.info("Comando /start ejecutado por chat_id: " + str(chat_id))
        except Exception as e:
            logger.error("Error en start_command: " + str(e))
            await update.message.reply_text("❌ Ocurrió un error. Por favor, intenta de nuevo.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            help_text = ("🤖 *Comandos disponibles:*\n\n"
                         "/start - Iniciar o reiniciar el bot\n"
                         "/help - Mostrar este mensaje de ayuda\n\n"
                         "📝 *Funcionalidades:*\n"
                         "- Consultas sobre ejercicios\n"
                         "- Recomendaciones personalizadas\n"
                         "- Seguimiento de progreso\n"
                         "- Recursos y videos\n"
                         "- Consultas de productos\n\n"
                         "✨ Simplemente escribe tu pregunta y te responderé.")
            await update.message.reply_text(help_text, parse_mode='Markdown')
            logger.info("Comando /help ejecutado por chat_id: " + str(update.message.chat.id))
        except Exception as e:
            logger.error("Error en help_command: " + str(e))
            await update.message.reply_text("❌ Error mostrando la ayuda. Intenta de nuevo.")

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            chat_id = update.message.chat.id
            if chat_id in self.verified_users:
                await self.handle_message(update, context)
            else:
                await self.verify_email(update, context)
        except Exception as e:
            logger.error("Error en route_message: " + str(e))
            await update.message.reply_text("❌ Ocurrió un error procesando tu mensaje. Por favor, intenta de nuevo.")

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
            if not response.strip():
                raise Exception("La respuesta del asistente está vacía")
            if "🔗 [Ver producto]" in response:
                await update.message.reply_text(response, parse_mode='Markdown', disable_web_page_preview=True)
            else:
                await update.message.reply_text(response)
        except asyncio.TimeoutError:
            logger.error("⏳ Timeout procesando mensaje de " + str(chat_id))
            await update.message.reply_text("⏳ La operación está tomando demasiado tiempo. Por favor, inténtalo más tarde.")
        except openai.OpenAIError as e:
            logger.error("❌ Error en OpenAI: " + str(e))
            await update.message.reply_text("❌ Hubo un problema con OpenAI.")
        except Exception as e:
            logger.error("⚠️ Error inesperado: " + str(e))
            await update.message.reply_text("⚠️ Ocurrió un error inesperado. Inténtalo más tarde.")

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
                user_message = recognizer.recognize_google(audio, language='es-ES')
                logger.info("Transcripción de voz: " + user_message)
                await update.message.reply_text(f"📝 Tu mensaje: \"{user_message}\"")
                response = await self.process_text_message(update, context, user_message)
                await update.message.reply_text(response)
            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ No pude entender la nota de voz. Intenta de nuevo.")
            except sr.RequestError as e:
                logger.error("Error en el servicio de reconocimiento de voz de Google: " + str(e))
                await update.message.reply_text("⚠️ Ocurrió un error con el servicio de reconocimiento de voz.")
        except Exception as e:
            logger.error("Error manejando mensaje de voz: " + str(e))
            await update.message.reply_text("⚠️ Ocurrió un error procesando la nota de voz.")
        finally:
            try:
                if os.path.exists(oga_file_path):
                    os.remove(oga_file_path)
                if os.path.exists(wav_file_path):
                    os.remove(wav_file_path)
            except Exception as e:
                logger.error("Error eliminando archivos temporales: " + str(e))

    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat.id
        user_email = update.message.text.strip().lower()
        username = update.message.from_user.username or "Unknown"
        if "@" not in user_email or "." not in user_email:
            await update.message.reply_text("❌ Por favor, proporciona un email válido.")
            return
        try:
            if not (await self.is_user_whitelisted(user_email)):
                await update.message.reply_text("❌ Tu email no está en la lista autorizada. Contacta a soporte.")
                return
            thread_id = await self.get_or_create_thread(chat_id)
            self.user_threads[chat_id] = thread_id
            self.verified_users[chat_id] = user_email
            self.save_verified_user(chat_id, user_email, username)
            await update.message.reply_text("✅ Email validado. Ahora puedes hablar conmigo.")
        except Exception as e:
            logger.error("❌ Error verificando email para " + str(chat_id) + ": " + str(e))
            await update.message.reply_text("⚠️ Ocurrió un error verificando tu email.")

    async def is_user_whitelisted(self, email: str) -> bool:
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.SPREADSHEET_ID,
                range='Usuarios!A:A'
            ).execute()
            values = result.get('values', [])
            whitelist = [row[0].lower() for row in values if row]
            return email.lower() in whitelist
        except Exception as e:
            logger.error("Error verificando whitelist: " + str(e))
            return False

try:
    bot = CoachBot()
except Exception as e:
    logger.error("Error crítico inicializando el bot: " + str(e))
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
        await bot.telegram_app.update_queue.put(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error("❌ Error procesando webhook: " + str(e))
        return {"status": "error", "message": str(e)}
