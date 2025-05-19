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
