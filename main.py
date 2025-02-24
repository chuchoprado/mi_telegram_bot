import os
import asyncio
import httpx
import io
import sqlite3
import json
import logging
import openai
import time
import pytz
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, BackgroundTasks
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openai import AsyncOpenAI
import speech_recognition as sr
from contextlib import closing
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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
        self.db_path = 'bot_data.db'

        # Inicializar la aplicación de Telegram
        self.telegram_app = Application.builder().token(self.TELEGRAM_TOKEN).build()    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
        
        # Inicializar el scheduler
        self.scheduler = AsyncIOScheduler()        
        # Modificar estructura de base de datos para incluir preferencias de horario
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
                    username TEXT,
                    timezone TEXT DEFAULT 'America/Mexico_City',
                    reminder_time TEXT DEFAULT '07:30',
                    reminders_enabled INTEGER DEFAULT 1
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
            # Nueva tabla para mensajes programados
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS scheduled_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    message_type TEXT,
                    content TEXT,
                    sent_date DATE,
                    FOREIGN KEY (chat_id) REFERENCES users (chat_id)
                )
            ''')
            conn.commit()

    def setup_handlers(self):
        """Configura los manejadores de comandos y mensajes"""
        try:
            self.telegram_app.add_handler(CommandHandler("start", self.start_command))
            self.telegram_app.add_handler(CommandHandler("help", self.help_command))
            # Nuevos comandos para configurar recordatorios
            self.telegram_app.add_handler(CommandHandler("settime", self.set_reminder_time))
            self.telegram_app.add_handler(CommandHandler("timezone", self.set_timezone))
            self.telegram_app.add_handler(CommandHandler("enablereminders", self.enable_reminders))
            self.telegram_app.add_handler(CommandHandler("disablereminders", self.disable_reminders))
            
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

    async def set_reminder_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Establece la hora para enviar recordatorios diarios"""
        chat_id = update.message.chat.id
        
        if chat_id not in self.verified_users:
            await update.message.reply_text("⚠️ Por favor, verifica tu email primero.")
            return
            
        # Verificar si hay argumentos
        if not context.args or len(context.args) != 1:
            await update.message.reply_text(
                "⚠️ Por favor, especifica la hora en formato HH:MM (24h).\n"
                "Ejemplo: /settime 07:30"
            )
            return
            
        time_str = context.args[0]
        
        # Validar formato de hora
        try:
            hour, minute = map(int, time_str.split(':'))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError("Hora fuera de rango")
            
            # Guardar en base de datos
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET reminder_time = ? WHERE chat_id = ?",
                    (time_str, chat_id)
                )
                conn.commit()
                
            # Recalcular próximo recordatorio
            self.schedule_user_reminders(chat_id)
                
            await update.message.reply_text(
                f"✅ Tu hora de recordatorios ha sido configurada a las {time_str}."
            )
            
        except Exception as e:
            logger.error(f"Error configurando hora de recordatorio: {e}")
            await update.message.reply_text(
                "❌ Formato de hora inválido. Usa el formato HH:MM (ejemplo: 07:30)"
            )

    async def set_timezone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Establece la zona horaria del usuario"""
        chat_id = update.message.chat.id
        
        if chat_id not in self.verified_users:
            await update.message.reply_text("⚠️ Por favor, verifica tu email primero.")
            return
            
        # Verificar si hay argumentos
        if not context.args or len(context.args) != 1:
            await update.message.reply_text(
                "⚠️ Por favor, especifica tu zona horaria.\n"
                "Ejemplo: /timezone America/Mexico_City\n\n"
                "Zonas comunes:\n"
                "- America/Mexico_City\n"
                "- America/Bogota\n"
                "- America/New_York\n"
                "- Europe/Madrid"
            )
            return
            
        timezone_str = context.args[0]
        
        # Validar zona horaria
        try:
            pytz.timezone(timezone_str)
            
            # Guardar en base de datos
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET timezone = ? WHERE chat_id = ?",
                    (timezone_str, chat_id)
                )
                conn.commit()
                
            # Recalcular próximo recordatorio
            self.schedule_user_reminders(chat_id)
                
            await update.message.reply_text(
                f"✅ Tu zona horaria ha sido configurada como {timezone_str}."
            )
            
        except Exception as e:
            logger.error(f"Error configurando zona horaria: {e}")
            await update.message.reply_text(
                "❌ Zona horaria inválida. Intenta con un valor como 'America/Mexico_City'"
            )

    async def enable_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Habilita los recordatorios para el usuario"""
        chat_id = update.message.chat.id
        
        if chat_id not in self.verified_users:
            await update.message.reply_text("⚠️ Por favor, verifica tu email primero.")
            return
            
        # Guardar en base de datos
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET reminders_enabled = 1 WHERE chat_id = ?",
                (chat_id,)
            )
            conn.commit()
            
        # Programar recordatorios
        self.schedule_user_reminders(chat_id)
            
        await update.message.reply_text(
            "✅ Los recordatorios diarios han sido habilitados."
        )

    async def disable_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Deshabilita los recordatorios para el usuario"""
        chat_id = update.message.chat.id
        
        if chat_id not in self.verified_users:
            await update.message.reply_text("⚠️ Por favor, verifica tu email primero.")
            return
            
        # Guardar en base de datos
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET reminders_enabled = 0 WHERE chat_id = ?",
                (chat_id,)
            )
            conn.commit()
            
        # Cancelar recordatorios programados para este usuario
        job_id = f"reminder_{chat_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            
        await update.message.reply_text(
            "✅ Los recordatorios diarios han sido deshabilitados."
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /help"""
        try:
            help_text = (
                "🤖 *Comandos disponibles:*\n\n"
                "/start - Iniciar o reiniciar el bot\n"
                "/help - Mostrar este mensaje de ayuda\n"
                "/settime HH:MM - Configurar hora de recordatorio diario\n"
                "/timezone Zona - Configurar zona horaria (ej: America/Mexico_City)\n"
                "/enablereminders - Activar recordatorios diarios\n"
                "/disablereminders - Desactivar recordatorios diarios\n\n"
                "📝 *Funcionalidades:*\n"
                "- Consultas sobre ejercicios\n"
                "- Recomendaciones personalizadas\n"
                "- Recordatorios diarios\n"
                "- Seguimiento de progreso\n"
                "- Recursos y videos\n\n"
                "✨ Simplemente escribe tu pregunta y te responderé."
            )
            await update.message.reply_text(help_text, parse_mode='Markdown')
            logger.info(f"Comando /help ejecutado por chat_id: {update.message.chat.id}")
        except Exception as e:
            logger.error(f"Error en help_command: {e}")
            await update.message.reply_text("❌ Error mostrando la ayuda. Intenta de nuevo.")

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
            self.verified_users[chat_id] = user_email
            
            self.save_verified_user(chat_id, user_email, username)
            
            # Programar recordatorios para este usuario
            self.schedule_user_reminders(chat_id)
            
            await update.message.reply_text(
                "✅ Email validado. Ahora puedes hablar conmigo.\n\n"
                "💡 Usaré tu zona horaria predeterminada (America/Mexico_City) para "
                "enviarte recordatorios diarios a las 07:30 AM.\n\n"
                "Puedes cambiar esto con los comandos:\n"
                "/settime HH:MM - Para cambiar la hora\n"
                "/timezone Zona - Para cambiar la zona horaria\n"
                "/disablereminders - Para desactivar los recordatorios"
            )

        except Exception as e:
            logger.error(f"❌ Error verificando email para {chat_id}: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error verificando tu email.")

    

    def schedule_user_reminders(self, chat_id):
        """Programa los recordatorios diarios para un usuario"""
        try:
            # Obtener configuración del usuario
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT timezone, reminder_time, reminders_enabled FROM users WHERE chat_id = ?",
                    (chat_id,)
                )
                result = cursor.fetchone()
                
                if not result:
                    logger.error(f"Usuario {chat_id} no encontrado en la base de datos")
                    return
                    
                timezone_str, reminder_time, reminders_enabled = result
                
            if not reminders_enabled:
                logger.info(f"Recordatorios deshabilitados para {chat_id}")
                # Remover cualquier trabajo programado para este usuario
                job_id = f"reminder_{chat_id}"
                if self.scheduler.get_job(job_id):
                    self.scheduler.remove_job(job_id)
                return
                
            # Parsear hora de recordatorio
            hour, minute = map(int, reminder_time.split(':'))
            
            # Crear o actualizar el trabajo programado
            job_id = f"reminder_{chat_id}"
            
            # Remover trabajo existente si hay uno
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                
            # Crear nuevo trabajo con CronTrigger
            self.scheduler.add_job(
                self.send_daily_reminder,
                CronTrigger(hour=hour, minute=minute, timezone=timezone_str),
                id=job_id,
                kwargs={'chat_id': chat_id},
                replace_existing=True
            )
            
            logger.info(f"Recordatorio programado para {chat_id} a las {hour}:{minute} ({timezone_str})")
            
        except Exception as e:
            logger.error(f"Error programando recordatorio: {e}")

    async def send_daily_reminder(self, chat_id):
        """Envía un recordatorio diario personalizado"""
        try:
            # Verificar si el usuario existe y tiene recordatorios habilitados
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT email, username, reminders_enabled FROM users WHERE chat_id = ?",
                    (chat_id,)
                )
                result = cursor.fetchone()
                
                if not result or not result[2]:
                    logger.info(f"Omitiendo recordatorio para {chat_id} (deshabilitado o usuario no existe)")
                    return
                    
                email, username = result[0], result[1]
            
            # Obtener contexto del usuario (últimas conversaciones)
            user_context = self.get_user_context(chat_id)
            
            # Generar mensaje personalizado usando OpenAI
            thread_id = await self.get_or_create_thread(chat_id)
            
            # Enviar solicitud para generar mensaje contextual
            prompt = (
                f"Genera un mensaje motivacional personalizado para el usuario con email {email}. "
                f"El mensaje debe ser breve (máximo 3 párrafos), positivo y motivador. "
                f"Incluye alguna recomendación útil basada en sus últimas conversaciones. "
                f"Contexto de sus últimas interacciones: {user_context}"
            )
            
            await self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=prompt
            )

            run = await self.client.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=self.assistant_id
            )

            # Esperar la respuesta
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
                elif time.time() - start_time > 30:
                    raise TimeoutError("⏳ La generación tomó demasiado tiempo.")

                await asyncio.sleep(1)

            messages = await self.client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            )

            if not messages.data or not messages.data[0].content:
                raise ValueError("Respuesta vacía del asistente")

            personalized_message = messages.data[0].content[0].text.value.strip()
            
            # Enviar mensaje al usuario
            reminder_text = (
                f"🌟 *Buenos días {username or ''}!*\n\n"
                f"{personalized_message}\n\n"
                f"💪 ¿Listo para el día de hoy?"
            )
            
            await self.telegram_app.bot.send_message(
                chat_id=chat_id,
                text=reminder_text,
                parse_mode='Markdown'
            )
            
            # Guardar en la base de datos
            self.save_scheduled_message(chat_id, "daily_reminder", reminder_text)
            
            logger.info(f"Recordatorio diario enviado a {chat_id}")
            
        except Exception as e:
            logger.error(f"Error enviando recordatorio diario: {e}")

    def get_user_context(self, chat_id):
        """Obtiene el contexto reciente del usuario basado en conversaciones anteriores"""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT role, content FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT 10",
                    (chat_id,)
                )
                conversations = cursor.fetchall()
                
            if not conversations:
                return "No hay conversaciones previas."
                
            # Formatear el contexto
            context = []
            for role, content in conversations:
                context.append(f"{role}: {content[:100]}...")
                
            return " ".join(context)
            
        except Exception as e:
            logger.error(f"Error obteniendo contexto del usuario: {e}")
            return "Error al obtener contexto"

    def save_scheduled_message(self, chat_id, message_type, content):
        """Guarda un mensaje programado en la base de datos"""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO scheduled_messages (chat_id, message_type, content, sent_date) VALUES (?, ?, ?, date('now'))",
                    (chat_id, message_type, content)
                )
                conn.commit()
                
        except Exception as e:
            logger.error(f"Error guardando mensaje programado: {e}")

    async def async_init(self):
        """Inicialización asíncrona del bot"""
        try:
            await self.telegram_app.initialize()
            self.load_verified_users()
            
            # Iniciar el scheduler
            self.scheduler.start()
            
            # Cargar y programar todos los recordatorios
            self.load_all_reminders()
            
            if not self.started:
                self.started = True
                await self.telegram_app.start()
            logger.info("Bot inicializado correctamente")
        except Exception as e:
            logger.error(f"Error en async_init: {e}")
            raise

    def load_all_reminders(self):
        """Carga y programa los recordatorios para todos los usuarios"""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT chat_id FROM users WHERE reminders_enabled = 1"
                )
                users = cursor.fetchall()
                
            for (chat_id,) in users:
                self.schedule_user_reminders(chat_id)
                
            logger.info(f"Recordatorios cargados para {len(users)} usuarios")
            
        except Exception as e:
            logger.error(f"Error cargando recordatorios: {e}")

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
        try:
            thread_id = await self.get_or_create_thread(chat_id)

            if not thread_id:
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

            # Verificar consulta de productos
            if any(keyword in user_message.lower() for keyword in ['producto', 'comprar', 'precio', 'costo']):
                return await self.process_product_query(chat_id, user_message)

            # Usar asistente de OpenAI
            response = await self.send_message_to_assistant(chat_id, user_message)

            if not response.strip():
                logger.error("⚠️ OpenAI devolvió una respuesta vacía.")
                return "⚠️ No obtuve una respuesta válida del asistente. Intenta de nuevo."

            # Guardar conversación solo si hay respuesta válida
            self.save_conversation(chat_id, "user", user_message)
            self.save_conversation(chat_id, "assistant", response)

            return response

        except Exception as e:  # Se corrigió la indentación aquí
            logger.error(f"❌ Error en process_text_message: {e}", exc_info=True)
            return "⚠️ Ocurrió un error al procesar tu mensaje."
    
    async def process_product_query(self, chat_id: int, query: str) -> str:
        try:
            products = await self.fetch_products(query)
            if "error" in products:
                return "⚠️ Ocurrió un error al consultar los productos."

            product_list = "\n".join([f"- {p.get('titulo', 'Sin título')}: {p.get('descripcion', 'Sin descripción')} (link: {p.get('link', 'No disponible')})" for p in products.get("data", [])])
            if not product_list:
                return "⚠️ No se encontraron productos."

            return f"🔍 Productos recomendados:\n{product_list}"
        except Exception as e:
            logger.error(f"❌ Error procesando consulta de productos: {e}")
            return "⚠️ Ocurrió un error al procesar tu consulta de productos."

    async def fetch_products(self, query):
        url = "https://script.google.com/macros/s/AKfycbwUieYWmu5pTzHUBnSnyrLGo-SROiiNFvufWdn5qm7urOamB65cqQkbQrkj05Xf3N3N_g/exec"
        params = {"query": query}
        
        logger.info(f"Consultando Google Sheets con: {params}")

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, params=params, follow_redirects=True)

            if response.status_code != 200:
                raise Exception(f"Error en Google Sheets API: {response.status_code}")

            logger.info(f"Respuesta de Google Sheets: {response.text}")
            return response.json()

        except httpx.TimeoutException:
            logger.error("⏳ La API de Google Sheets tardó demasiado en responder.")
            return {"error": "⏳ La consulta tardó demasiado. Inténtalo más tarde."}

        except Exception as e:
            logger.error(f"❌ Error consultando Google Sheets: {e}")
            return {"error": "Error consultando Google Sheets"}

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
                "- Recursos y videos\n\n"
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

            if "producto" in user_message.lower():
                response = await self.process_product_query(chat_id, user_message)
            else:
                response = await self.process_text_message(update, context, user_message)

            if response is None or not response.strip():
                raise ValueError("La respuesta del asistente está vacía")

            await update.message.reply_text(response)

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
                await self.process_text_message(update, context, user_message)
            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ No pude entender la nota de voz. Intenta de nuevo.")
            except sr.RequestError as e:
                logger.error(f"Error en el servicio de reconocimiento de voz de Google: {e}")
                await update.message.reply_text("⚠️ Ocurrió un error con el servicio de reconocimiento de voz.")

        except Exception as e:
            logger.error(f"Error manejando mensaje de voz: {e}")
            await update.message.reply_text("⚠️ Ocurrió un error procesando la nota de voz.")

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
        
