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
import string
import subprocess  # Nuevo: para llamar a ffmpeg

# Función para extraer palabras clave de la consulta de productos
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

# Función para convertir archivos OGA a WAV usando ffmpeg
def convertOgaToWav(oga_path, wav_path):
    try:
        subprocess.run(["ffmpeg", "-i", oga_path, wav_path], check=True)
        return True
    except Exception as e:
        logger.error("Error converting audio file: " + str(e))
        return False

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
        """
        if chat_id in self.pending_requests:
            return "⏳ Ya estoy procesando tu solicitud anterior. Por favor espera."
        self.pending_requests.add(chat_id)
        try:
            thread_id = await self.get_or_create_thread(chat_id)
            if (!thread_id) {
                self.pending_requests.remove(chat_id);
                return "❌ No se pudo establecer conexión con el asistente.";
            }
            await self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=user_message
            )
            run = await self.client.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=self.assistant_id
            )
            start_time = time.time();
            while (true) {
                var run_status = await self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run.id
                );
                if (run_status.status == 'completed') {
                    break;
                } else if (run_status.status in ['failed', 'cancelled', 'expired']) {
                    throw new Exception("Run failed with status: " + run_status.status);
                } else if (time.time() - start_time > 60) {
                    throw new TimeoutError("La consulta al asistente tomó demasiado tiempo.");
                }
                await asyncio.sleep(1);
            }
            var messages = await self.client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            );
            if (!messages.data || !messages.data[0].content) {
                self.pending_requests.remove(chat_id);
                return "⚠️ La respuesta del asistente está vacía. Inténtalo más tarde.";
            }
            var assistant_message = messages.data[0].content[0].text.value;
            self.conversation_history.setdefault(chat_id, []).append({
                "role": "assistant",
                "content": assistant_message
            });
            return assistant_message;
        } catch (e) {
            logger.error(f"❌ Error procesando mensaje: {e}");
            return "⚠️ Ocurrió un error al procesar tu mensaje.";
        } finally {
            if (chat_id in self.pending_requests):
                self.pending_requests.remove(chat_id);
        }

    async def process_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str) -> str:
        """Procesa los mensajes de texto recibidos."""
        try:
            var chat_id = update.message.chat.id;
            if (!user_message.trim()) {
                return "⚠️ No se recibió un mensaje válido.";
            }
            await context.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING
            );
            // Usar la consulta filtrada para determinar si es una consulta de productos
            var filtered_query = extract_product_keywords(user_message);
            var product_keywords = ['producto', 'productos', 'comprar', 'precio', 'costo', 'tienda', 'venta', 
                                    'suplemento', 'meditacion', 'vitaminas', 'vitamina', 'suplementos', 
                                    'libro', 'libros', 'ebook', 'ebooks', 'amazon', 'meditacion'];
            if (product_keywords.some(function(keyword) {
                return filtered_query.toLowerCase().indexOf(keyword) !== -1;
            })) {
                var response = await this.process_product_query(chat_id, user_message);
                this.save_conversation(chat_id, "user", user_message);
                this.save_conversation(chat_id, "assistant", response);
                return response;
            }
            var response = await this.send_message_to_assistant(chat_id, user_message);
            if (!response.trim()) {
                logger.error("⚠️ OpenAI devolvió una respuesta vacía.");
                return "⚠️ No obtuve una respuesta válida del asistente. Intenta de nuevo.";
            }
            this.save_conversation(chat_id, "user", user_message);
            this.save_conversation(chat_id, "assistant", response);
            return response;
        } catch (e) {
            logger.error("❌ Error en process_text_message: " + e, e);
            return "⚠️ Ocurrió un error al procesar tu mensaje.";
        }
    async def process_product_query(self, chat_id: int, query: str) -> str:
        """Procesa consultas relacionadas con productos."""
        try:
            logger.info("Procesando consulta de productos para " + chat_id + ": " + query);
            var filtered_query = extract_product_keywords(query);
            logger.info("Consulta filtrada: " + filtered_query);
            var products = await this.fetch_products(filtered_query);
            if (!products || typeof(products) !== "object") {
                logger.error("Respuesta inválida del API de productos: " + products);
                return "⚠️ No se pudieron recuperar productos en este momento.";
            }
            if (products.error) {
                logger.error("Error desde API de productos: " + products.error);
                return "⚠️ " + products.error;
            }
            var product_data = products.data || [];
            if (!product_data.length) {
                return "📦 No encontré productos que coincidan con tu consulta. ¿Puedes ser más específico?";
            }
            product_data = product_data.slice(0, 5);
            var product_list = [];
            product_data.forEach(function(p) {
                var title = p.titulo || p.fuente || "Sin título";
                var desc = p.descripcion || "Sin descripción";
                var link = p.link || "No disponible";
                if (desc.length > 100) {
                    desc = desc.substring(0, 97) + "...";
                }
                product_list.push("- *" + title + "*: " + desc + "\n  🔗 [Ver producto](" + link + ")");
            });
            var formatted_products = product_list.join("\n\n");
            return "🔍 *Productos recomendados:*\n\n" + formatted_products + "\n\n¿Necesitas más información sobre alguno de estos productos?";
        } catch (e) {
            logger.error("❌ Error procesando consulta de productos: " + e, e);
            return "⚠️ Ocurrió un error al buscar productos. Por favor, intenta más tarde.";
        }
    async def fetch_products(self, query):
        """Obtiene productos desde la API de Google Sheets."""
        var url = "https://script.google.com/macros/s/AKfycbzb1VZCKQgMCtOyHeC8QX_0lS0qHzue3HNeNf9YqdT7gP3EgXfoFuO-SQ8igHvZ5As0_A/exec";
        var params = { query: query };
        logger.info("Consultando Google Sheets con: " + JSON.stringify(params));
        try {
            var client = new httpx.AsyncClient({ timeout: 15.0 });
            var response = await client.get(url, { params: params, follow_redirects: true });
            if (response.status_code !== 200) {
                logger.error("Error en API de Google Sheets: " + response.status_code + ", " + response.text);
                return { error: "Error del servidor (" + response.status_code + ")" };
            }
            try {
                var result = response.json();
                logger.info("JSON recibido correctamente de la API");
                return result;
            } catch (e) {
                logger.error("Error decodificando JSON: " + e + ", respuesta: " + response.text.substring(0, 200));
                return { error: "Formato de respuesta inválido" };
            }
        } catch (e) {
            if (e instanceof httpx.TimeoutException) {
                logger.error("⏳ La API de Google Sheets tardó demasiado en responder.");
                return { error: "⏳ Tiempo de espera agotado. Inténtalo más tarde." };
            } else if (e instanceof httpx.RequestError) {
                logger.error("❌ Error de conexión a Google Sheets: " + e);
                return { error: "Error de conexión a la base de datos de productos" };
            } else {
                logger.error("❌ Error inesperado consultando Google Sheets: " + e);
                return { error: "Error inesperado consultando productos" };
            }
        }
    def searchProducts(self, data, query, start, limit):
        var results = [];
        var count = 0;
        var queryWords = query.split(/\s+/);
        for (var i = start; i < data.length; i++) {
            if (!data[i] || data[i].length < 6) continue;
            var categoria = data[i][0] ? normalizeText(data[i][0]) : "";
            var etiquetas = data[i][1] ? normalizeText(data[i][1].replace(/#/g, "")) : "";
            var titulo = data[i][2] ? normalizeText(data[i][2]) : "";
            var link = data[i][3] ? data[i][3].trim() : "";
            var description = data[i][4] ? data[i][4].trim() : "";
            var autor = data[i][5] ? normalizeText(data[i][5]) : "desconocido";
            var match = queryWords.some(function(word) {
                return categoria.indexOf(word) !== -1 ||
                       etiquetas.indexOf(word) !== -1 ||
                       titulo.indexOf(word) !== -1 ||
                       autor.indexOf(word) !== -1;
            });
            if (match && link !== "") {
                results.push({ link: link, descripcion: description, fuente: autor });
                count++;
            }
            if (count >= limit) break;
        }
        return results;
    def setup_handlers(self):
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
            logger.error("Error en setup_handlers: " + e)
            raise
    def load_verified_users(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            var cursor = conn.cursor();
            cursor.execute("SELECT chat_id, email FROM users");
            var rows = cursor.fetchall();
            for (var i = 0; i < rows.length; i++) {
                var chat_id = rows[i][0];
                var email = rows[i][1];
                self.verified_users[chat_id] = email;
            }
    def save_verified_user(self, chat_id, email, username):
        with closing(sqlite3.connect(self.db_path)) as conn:
            var cursor = conn.cursor();
            cursor.execute('''
                INSERT OR REPLACE INTO users (chat_id, email, username)
                VALUES (?, ?, ?)
            ''', [chat_id, email, username]);
            conn.commit();
    def save_conversation(self, chat_id, role, content):
        with closing(sqlite3.connect(self.db_path)) as conn:
            var cursor = conn.cursor();
            cursor.execute('''
                INSERT INTO conversations (chat_id, role, content)
                VALUES (?, ?, ?)
            ''', [chat_id, role, content]);
            conn.commit();
    def _init_sheets(self):
        try:
            if (!os.path.exists(self.credentials_path)) {
                logger.error("Archivo de credenciales no encontrado en: " + self.credentials_path);
                return False;
            }
            var credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                ["https://www.googleapis.com/auth/spreadsheets.readonly"]
            );
            self.sheets_service = build("sheets", "v4", { credentials: credentials });
            try {
                self.sheets_service.spreadsheets().get({
                    spreadsheetId: self.SPREADSHEET_ID
                }).execute();
                logger.info("Conexión con Google Sheets inicializada correctamente.");
                return True;
            } catch (e) {
                logger.error("Error accediendo al spreadsheet: " + e);
                return False;
            }
        } catch (e) {
            logger.error("Error inicializando Google Sheets: " + e);
            return False;
        }
    async def async_init(self):
        try:
            await self.telegram_app.initialize();
            self.load_verified_users();
            if (!self.started) {
                self.started = True;
                await self.telegram_app.start();
            }
            logger.info("Bot inicializado correctamente");
        } catch (e) {
            logger.error("Error en async_init: " + e);
            throw e;
        }
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            var chat_id = update.message.chat.id;
            if (chat_id in self.verified_users) {
                await update.message.reply_text("👋 ¡Bienvenido de nuevo! ¿En qué puedo ayudarte hoy?");
            } else {
                await update.message.reply_text("👋 ¡Hola! Por favor, proporciona tu email para comenzar.\n\n📧 Debe ser un email autorizado para usar el servicio.");
            }
            logger.info("Comando /start ejecutado por chat_id: " + chat_id);
        } catch (e) {
            logger.error("Error en start_command: " + e);
            await update.message.reply_text("❌ Ocurrió un error. Por favor, intenta de nuevo.");
        }
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            var help_text = "🤖 *Comandos disponibles:*\n\n" +
                "/start - Iniciar o reiniciar el bot\n" +
                "/help - Mostrar este mensaje de ayuda\n\n" +
                "📝 *Funcionalidades:*\n" +
                "- Consultas sobre ejercicios\n" +
                "- Recomendaciones personalizadas\n" +
                "- Seguimiento de progreso\n" +
                "- Recursos y videos\n" +
                "- Consultas de productos\n\n" +
                "✨ Simplemente escribe tu pregunta y te responderé.";
            await update.message.reply_text(help_text, { parse_mode: "Markdown" });
            logger.info("Comando /help ejecutado por chat_id: " + update.message.chat.id);
        } catch (e) {
            logger.error("Error en help_command: " + e);
            await update.message.reply_text("❌ Error mostrando la ayuda. Intenta de nuevo.");
        }
    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            var chat_id = update.message.chat.id;
            if (chat_id in self.verified_users) {
                await self.handle_message(update, context);
            } else {
                await self.verify_email(update, context);
            }
        } catch (e) {
            logger.error("Error en route_message: " + e);
            await update.message.reply_text("❌ Ocurrió un error procesando tu mensaje. Por favor, intenta de nuevo.");
        }
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            var chat_id = update.message.chat.id;
            var user_message = update.message.text.trim();
            if (!user_message) return;
            var response = await asyncio.wait_for(
                this.process_text_message(update, context, user_message),
                60.0
            );
            if (!response.trim()) throw new Error("La respuesta del asistente está vacía");
            if (response.indexOf("🔗 [Ver producto]") !== -1) {
                await update.message.reply_text(response, { parse_mode: "Markdown", disable_web_page_preview: true });
            } else {
                await update.message.reply_text(response);
            }
        } catch (e) {
            if (e instanceof asyncio.TimeoutError) {
                logger.error("⏳ Timeout procesando mensaje de " + chat_id);
                await update.message.reply_text("⏳ La operación está tomando demasiado tiempo. Por favor, inténtalo más tarde.");
            } else if (e instanceof openai.OpenAIError) {
                logger.error("❌ Error en OpenAI: " + e);
                await update.message.reply_text("❌ Hubo un problema con OpenAI.");
            } else {
                logger.error("⚠️ Error inesperado: " + e);
                await update.message.reply_text("⚠️ Ocurrió un error inesperado. Inténtalo más tarde.");
            }
        }
    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            var chat_id = update.message.chat.id;
            var voice_file = await update.message.voice.get_file();
            var oga_file_path = chat_id + "_voice_note.oga";
            await voice_file.download_to_drive(oga_file_path);
            var wav_file_path = chat_id + "_voice_note.wav";
            if (!convertOgaToWav(oga_file_path, wav_file_path)) {
                await update.message.reply_text("⚠️ No se pudo procesar el archivo de audio.");
                return;
            }
            var recognizer = new sr.Recognizer();
            with (new sr.AudioFile(wav_file_path)) as source:
                var audio = recognizer.record(source);
            try {
                var user_message = recognizer.recognize_google(audio, { language: "es-ES" });
                logger.info("Transcripción de voz: " + user_message);
                await update.message.reply_text("📝 Tu mensaje: \"" + user_message + "\"");
                var response = await this.process_text_message(update, context, user_message);
                await update.message.reply_text(response);
            } catch (e) {
                if (e instanceof sr.UnknownValueError) {
                    await update.message.reply_text("⚠️ No pude entender la nota de voz. Intenta de nuevo.");
                } else if (e instanceof sr.RequestError) {
                    logger.error("Error en el servicio de reconocimiento de voz de Google: " + e);
                    await update.message.reply_text("⚠️ Ocurrió un error con el servicio de reconocimiento de voz.");
                }
            }
        } catch (e) {
            logger.error("Error manejando mensaje de voz: " + e);
            await update.message.reply_text("⚠️ Ocurrió un error procesando la nota de voz.");
        } finally {
            try {
                if (os.path.exists(oga_file_path)) {
                    os.remove(oga_file_path);
                }
                if (os.path.exists(wav_file_path)) {
                    os.remove(wav_file_path);
                }
            } catch (e) {
                logger.error("Error eliminando archivos temporales: " + e);
            }
        }
    async def verify_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        var chat_id = update.message.chat.id;
        var user_email = update.message.text.trim().toLowerCase();
        var username = update.message.from_user.username || "Unknown";
        if (user_email.indexOf("@") === -1 || user_email.indexOf(".") === -1) {
            await update.message.reply_text("❌ Por favor, proporciona un email válido.");
            return;
        }
        try {
            if (!(await this.is_user_whitelisted(user_email))) {
                await update.message.reply_text("❌ Tu email no está en la lista autorizada. Contacta a soporte.");
                return;
            }
            var thread_id = await this.get_or_create_thread(chat_id);
            this.user_threads[chat_id] = thread_id;
            this.verified_users[chat_id] = user_email;
            this.save_verified_user(chat_id, user_email, username);
            await update.message.reply_text("✅ Email validado. Ahora puedes hablar conmigo.");
        } catch (e) {
            logger.error("❌ Error verificando email para " + chat_id + ": " + e);
            await update.message.reply_text("⚠️ Ocurrió un error verificando tu email.");
        }
    async def is_user_whitelisted(self, email: string) -> boolean:
        try {
            var result = this.sheets_service.spreadsheets().values().get({
                spreadsheetId: this.SPREADSHEET_ID,
                range: "Usuarios!A:A"
            }).execute();
            var values = result.values || [];
            var whitelist = values.map(function(email) { return email[0].toLowerCase(); });
            return whitelist.indexOf(email.toLowerCase()) !== -1;
        } catch (e) {
            logger.error("Error verificando whitelist: " + e);
            return false;
        }

# Instanciar el bot
try {
    var bot = new CoachBot();
} catch (e) {
    logger.error("Error crítico inicializando el bot: " + e);
    throw e;
}

@app.on_event("startup")
async function startup_event() {
    try {
        await bot.async_init();
        logger.info("Aplicación iniciada correctamente");
    } catch (e) {
        logger.error("❌ Error al iniciar la aplicación: " + e);
    }
}

@app.post("/webhook")
async function webhook(request: Request) {
    try {
        var data = await request.json();
        var update = Update.de_json(data, bot.telegram_app.bot);
        await bot.telegram_app.update_queue.put(update);
        return { status: "ok" };
    } catch (e) {
        logger.error("❌ Error procesando webhook: " + e);
        return { status: "error", message: e.toString() };
    }
}
