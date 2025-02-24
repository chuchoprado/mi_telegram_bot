import os
import asyncio
import httpx
import io
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

class CoachBot:
    def __init__(self):
        # Validar variables de entorno críticas
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

        # Inicializar cliente AsyncOpenAI
        self.client = AsyncOpenAI(api_key=required_env_vars['OPENAI_API_KEY'])

        self.sheets_service = None
        self.started = False
        self.verified_users = {}
        self.conversation_history = {}
        self.user_threads = {}
        self.pending_requests = set()  # Conjunto para rastrear solicitudes en curso
        self.db_path = 'bot_data.db'

        # Inicializar la aplicación de Telegram
        self.telegram_app = Application.builder().token(self.TELEGRAM_TOKEN).build()

        self._init_db()
        self.setup_handlers()
        self._init_sheets()

    def _init_db(self):
        """Inicializar la base de datos y crear las tablas necesarias."""
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
        """Obtiene un thread existente o crea uno nuevo en OpenAI Assistant."""
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
        """
        Envía un mensaje al asistente de OpenAI y espera su respuesta.

        Args:
            chat_id (int): ID del chat de Telegram
            user_message (str): Mensaje del usuario

        Returns:
            str: Respuesta del asistente
        """
        # Si ya hay una solicitud en curso para este usuario, rechazar esta
        if chat_id in self.pending_requests:
            return "⏳ Ya estoy procesando tu solicitud anterior. Por favor espera."
        
        # Marcar como pendiente
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
                elif time.time() - start_time > 60:  # Timeout after 60 seconds
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
            # Siempre eliminar de pendientes cuando termine
            if chat_id in self.pending_requests:
                self.pending_requests.remove(chat_id)

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str) -> str:
        """Procesa los mensajes de texto recibidos."""
        try:
            chat_id = update.message.chat.id

            if not user_message.strip():
                return "⚠️ No se recibió un mensaje válido."

            await context.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING
            )

            # Verificar si es una consulta de productos
            product_keywords = ['producto', 'productos', 'comprar', 'precio', 'costo', 'tienda', 'venta']
            if any(keyword in user_message.lower() for keyword in product_keywords):
                response = await self.process_product_query(chat_id, user_message)
                # Guardar conversación
                self.save_conversation(chat_id, "user", user_message)
                self.save_conversation(chat_id, "assistant", response)
                return response

            # Usar asistente de OpenAI para otras consultas
            response = await self.send_message_to_assistant(chat_id, user_message)

            if not response.strip():
                logger.error("⚠️ OpenAI devolvió una respuesta vacía.")
                return "⚠️ No obtuve una respuesta válida del asistente. Intenta de nuevo."

            # Guardar conversación solo si hay respuesta válida
            self.save_conversation(chat_id, "user", user_message)
            self.save_conversation(chat_id, "assistant", response)

            return response

        except Exception as e:
            logger.error(f"❌ Error en process_text_message: {e}", exc_info=True)
            return "⚠️ Ocurrió un error al procesar tu mensaje."
    
    async def process_product_query(self, chat_id: int, query: str) -> str:
        """Procesa consultas relacionadas con productos."""
        try:
            # Notificar al usuario que estamos buscando productos
            logger.info(f"Procesando consulta de productos para {chat_id}: {query}")
            
            products = await self.fetch_products(query)
            
            if not products or not isinstance(products, dict):
                logger.error(f"Respuesta inválida del API de productos: {products}")
                return "⚠️ No se pudieron recuperar productos en este momento."
                
            if "error" in products:
                logger.error(f"Error desde API de productos: {products['error']}")
                return f"⚠️ {products['error']}"

            product_data = products.get("data", [])
            if not product_data:
                return "📦 No encontré productos que coincidan con tu consulta. ¿Puedes ser más específico?"

            # Limitar a máximo 5 productos para no sobrecargar la respuesta
            product_data = product_data[:5]
            
            product_list = []
            for p in product_data:
                title = p.get('titulo', 'Sin título')
                desc = p.get('descripcion', 'Sin descripción')
                link = p.get('link', 'No disponible')
                
                # Truncar descripciones largas
                if len(desc) > 100:
                    desc = desc[:97] + "..."
                    
                product_list.append(f"- *{title}*: {desc}\n  🔗 [Ver producto]({link})")
            
            formatted_products = "\n\n".join(product_list)
            return f"🔍 *Productos recomendados:*\n\n{formatted_products}\n\n¿Necesitas más información sobre alguno de estos productos?"
            
        except Exception as e:
            logger.error(f"❌ Error procesando consulta de productos: {e}", exc_info=True)
            return "⚠️ Ocurrió un error al buscar productos. Por favor, intenta más tarde."

    async def fetch_products(self, query):
        """Obtiene productos desde la API de Google Sheets."""
        url = "https://script.google.com/macros/s/AKfycbwUieYWmu5pTzHUBnSnyrLGo-SROiiNFvufWdn5qm7urOamB65cqQkbQrkj05Xf3N3N_g/exec"
        params = {"query": query}
        
        logger.info(f"Consultando Google Sheets con: {params}")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:  # Aumentar timeout a 15 segundos
                response = await client.get(url, params=params, follow_redirects=True)

            # Verificar respuesta HTTP correcta
            if response.status_code != 200:
                logger.error(f"Error en API de Google Sheets: {response.status_code}, {response.text}")
                return {"error": f"Error del servidor ({response.status_code})"}

            # Intentar parsear JSON con manejo de errores
            try:
                result = response.json()
                logger.info(f"JSON recibido correctamente de la API")
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

    def setup_handlers(self):
        """Configura los manejadores de comandos y mensajes"""
        try:
            self.telegram_app.add_handler(CommandHandler("start", self.start_command))
            self.telegram_app.add_handler(CommandHandler("help", self.help_command))
            self.telegram_app.add_handler(MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self.route_message
            ))
            self.telegram_app.add_handler(MessageHandler(
                filters.VOICE,
                self.handle_voice_message
            ))
            logger.info("Handlers configurados correctamente")
        except Exception as e:
            logger.error(f"Error en setup_handlers: {e}")
            raise

    def load_verified_users(self):
        """Carga usuarios validados desde la base de datos."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT chat_id, email FROM users')
            rows = cursor.fetchall()
            for chat_id, email in rows:
                self.verified_users[chat_id] = email

    def save_verified_user(self, chat_id, email, username):
        """Guarda un usuario validado en la base de datos."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO users (chat_id, email, username)
                VALUES (?, ?, ?)
            ''', (chat_id, email, username))
            conn.commit()

    def save_conversation(self, chat_id, role, content):
        """Guarda un mensaje de conversación en la base de datos."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO conversations (chat_id, role, content)
                VALUES (?, ?, ?)
            ''', (chat_id, role, content))
            conn.commit()

    def _init_sheets(self):
        """Inicializa la conexión con Google Sheets"""
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
        """Inicialización asíncrona del bot"""
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
        """Maneja el comando /start"""
        try:
            chat_id = update.message.chat.id
            if chat_id in self.verified_users:
                await update.message.reply_text(
                    "👋 ¡Bienvenido de nuevo! ¿En qué puedo ayudarte hoy?"
                )
            else:
                await update.message.reply_text(
                    "👋 ¡Hola! Por favor, proporciona tu email para comenzar.\n\n"
                    "📧 Debe ser un email autorizado para usar el servicio."
                )
            logger.info(f"Comando /start ejecutado por chat_id: {chat_id}")
        except Exception as e:
            logger.error(f"Error en start_command: {e}")
            await update.message.reply_text("❌ Ocurrió un error. Por favor, intenta de nuevo.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /help"""
        try:
            help_text = (
                "🤖 *Comandos disponibles:*\n\n"
                "/start - Iniciar o reiniciar el bot\n"
                "/help - Mostrar este mensaje de ayuda\n\n"
                "📝 *Funcionalidades:*\n"
                "- Consultas sobre ejercicios\n"
                "- Recomendaciones personalizadas\n"
                "- Seguimiento de progreso\n"
                "- Recursos y videos\n"
                "- Consultas de productos\n\n"
                "✨ Simplemente escribe tu pregunta y te responderé."
            )
            await update.message.reply_text(help_text, parse_mode='Markdown')
            logger.info(f"Comando /help ejecutado por chat_id: {update.message.chat.id}")
        except Exception as e:
            logger.error(f"Error en help_command: {e}")
            await update.message.reply_text("❌ Error mostrando la ayuda. Intenta de nuevo.")

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enruta los mensajes según el estado de verificación del usuario"""
        try:
            chat_id = update.message.chat.id
            if chat_id in self.verified_users:
                await self.handle_message(update, context)
            else:
                await self.verify_email(update, context)
        except Exception as e:
            logger.error(f"Error en route_message: {e}")
            await update.message.reply_text(
                "❌ Ocurrió un error procesando tu mensaje. Por favor, intenta de nuevo."
            )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes recibidos después de la verificación"""
        try:
            chat_id = update.message.chat.id
            user_message = update.message.text.strip()
            if not user_message:
                return

            # Procesar mensaje con timeout para evitar bloqueos
            response = await asyncio.wait_for(
                self.process_text_message(update, context, user_message),
                timeout=60.0  # Timeout general de 60 segundos
            )

            if response is None or not response.strip():
                raise ValueError("La respuesta del asistente está vacía")

            # Enviar la respuesta con el formato adecuado para URLs si hay enlaces
            if "🔗 [Ver producto]" in response:
                await update.message.reply_text(response, parse_mode='Markdown', disable_web_page_preview=True)
            else:
                await update.message.reply_text(response)

        except asyncio.TimeoutError:
            logger.error(f"⏳ Timeout procesando mensaje de {chat_id}")
            await update.message.reply_text("⏳ La operación está tomando demasiado tiempo. Por favor, inténtalo más tarde.")

        except openai.OpenAIError as e:
            logger.error(f"❌ Error en OpenAI: {e}")
            await update.message.reply_text("❌ Hubo un problema con OpenAI.")

        except Exception as e:
            logger.error(f"⚠️ Error inesperado: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error inesperado. Inténtalo más tarde.")

    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes de voz"""
        try:
            chat_id = update.message.chat.id
            voice_file = await update.message.voice.get_file()
            voice_file_path = f"{chat_id}_voice_note.ogg"
            await voice_file.download(voice_file_path)

            recognizer = sr.Recognizer()
            with sr.AudioFile(voice_file_path) as source:
                audio = recognizer.record(source)

            try:
                user_message = recognizer.recognize_google(audio, language='es-ES')
                logger.info(f"Transcripción de voz: {user_message}")
                
                # Informar al usuario que su voz ha sido transcrita
                await update.message.reply_text(f"📝 Tu mensaje: \"{user_message}\"")
                
                # Procesar el mensaje transcrito
                response = await self.process_text_message(update, context, user_message)
                await update.message.reply_text(response)
                
            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ No pude entender la nota de voz. Intenta de nuevo.")
            except sr.RequestError as e:
                logger.error(f"Error en el servicio de reconocimiento de voz de Google: {e}")
                await update.message.reply_text("⚠️ Ocurrió un error con el servicio de reconocimiento de voz.")

        except Exception as e:
            logger.error(f"Error manejando mensaje de voz: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error procesando la nota de voz.")
        finally:
            # Limpiar archivo temporal
            try:
                if os.path.exists(voice_file_path):
                    os.remove(voice_file_path)
            except Exception as e:
                logger.error(f"Error eliminando archivo temporal: {e}")

    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verifica el email del usuario"""
        chat_id = update.message.chat.id
        user_email = update.message.text.strip().lower()
        username = update.message.from_user.username or "Unknown"

        if not '@' in user_email or not '.' in user_email:
            await update.message.reply_text("❌ Por favor, proporciona un email válido.")
            return

        try:
            if not await self.is_user_whitelisted(user_email):
                await update.message.reply_text(
                    "❌ Tu email no está en la lista autorizada. Contacta a soporte."
                )
                return

            thread_id = await self.get_or_create_thread(chat_id)
            self.user_threads[chat_id] = thread_id
            self.verified_users[chat_id] = user_email  # Actualizar en memoria también

            self.save_verified_user(chat_id, user_email, username)
            await update.message.reply_text("✅ Email validado. Ahora puedes hablar conmigo.")

        except Exception as e:
            logger.error(f"❌ Error verificando email para {chat_id}: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error verificando tu email.")

    async def is_user_whitelisted(self, email: str) -> bool:
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.SPREADSHEET_ID,
                range='Usuarios!A:A'
            ).execute()

            values = result.get('values', [])
            whitelist = [email[0].lower() for email in values if email]

            return email.lower() in whitelist

        except Exception as e:
            logger.error(f"Error verificando whitelist: {e}")
            return False

# Instanciar el bot
try:
    bot = CoachBot()
except Exception as e:
    logger.error(f"Error crítico inicializando el bot: {e}")
    raise

@app.on_event("startup")
async def startup_event():
    """Evento de inicio de la aplicación"""
    try:
        await bot.async_init()
        logger.info("Aplicación iniciada correctamente")
    except Exception as e:
        logger.error(f"❌ Error al iniciar la aplicación: {e}")

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook de Telegram"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot.telegram_app.bot)
        await bot.telegram_app.update_queue.put(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ Error procesando webhook: {e}")
        return {"status": "error", "message": str(e)}
        
