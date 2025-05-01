import os
import json
import asyncio
import logging
import traceback
from flask import Flask, request, Response
from botbuilder.core import (
    BotFrameworkAdapterSettings, 
    BotFrameworkAdapter, 
    TurnContext,
    ConversationState,
    MemoryStorage
)
from botbuilder.schema import Activity, ActivityTypes
from openai import AzureOpenAI
from azure.cosmos import CosmosClient, PartitionKey, exceptions as cosmos_exceptions

# Configuración inicial de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("AzureBot")

class ServiceManager:
    """Clase para gestionar los servicios externos"""
    
    def __init__(self):
        self.cosmos_available = False
        self.graph_available = False
        self.openai_available = False
        
        # Inicializar servicios
        self._setup_cosmos()
        self._setup_graph()
        self._setup_openai()
    
    def _setup_cosmos(self):
        """Configurar Cosmos DB"""
        try:
            COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT")
            COSMOS_KEY = os.environ.get("COSMOS_KEY")
            
            if not (COSMOS_ENDPOINT and COSMOS_KEY):
                logger.warning("Credenciales de Cosmos DB no configuradas")
                return
                
            self.cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
            self.database = self.cosmos_client.get_database_client("smart-buddy")
            
            # Crear contenedores si no existen
            self.database.create_container_if_not_exists(
                id="Eventos",
                partition_key=PartitionKey(path="/sala")
            )
            self.database.create_container_if_not_exists(
                id="UserStates",
                partition_key=PartitionKey(path="/user_id")
            )
            
            # Obtener referencias a los contenedores
            self.event_container = self.database.get_container_client("Eventos")
            self.user_state_container = self.database.get_container_client("UserStates")
            
            self.cosmos_available = True
            logger.info("Contenedores de Cosmos DB verificados/creados correctamente")
        except ImportError:
            logger.warning("Módulo azure.cosmos no disponible")
        except Exception as e:
            logger.error(f"Error en configuración de Cosmos DB: {e}")
    
    def _setup_graph(self):
        """Configurar Microsoft Graph"""
        try:
            from azure.identity import ClientSecretCredential
            from msgraph.core import GraphClient
            
            TENANT_ID = os.environ.get("TENANT_ID")
            CLIENT_ID = os.environ.get("CLIENT_ID")
            CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
            
            if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
                logger.warning("Credenciales de MS Graph no configuradas")
                return
                
            credential = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
            self.graph_client = GraphClient(credential=credential)
            self.graph_available = True
            logger.info("MS Graph configurado correctamente")
        except ImportError:
            logger.warning("Módulo msgraph no disponible")
        except Exception as e:
            logger.error(f"Error en configuración de MS Graph: {e}")
    
    def _setup_openai(self):
        """Configurar Azure OpenAI"""
        AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
        AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
        AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
        self.AZURE_DEPLOYMENT_NAME = os.environ.get("AZURE_DEPLOYMENT_NAME", "gpt-4.1")
        
        if not (AZURE_OPENAI_KEY and AZURE_OPENAI_ENDPOINT):
            logger.warning("Credenciales de Azure OpenAI no configuradas")
            return
            
        try:
            self.ai_client = AzureOpenAI(
                api_key=AZURE_OPENAI_KEY,
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
                api_version=AZURE_OPENAI_API_VERSION,
            )
            self.openai_available = True
            logger.info("Azure OpenAI configurado correctamente")
        except Exception as e:
            logger.error(f"Error en Azure OpenAI: {e}")

class SmartBuddyBot:
    """Clase principal del Bot"""
    
    def __init__(self, services):
        self.services = services
    
    async def get_user_state(self, user_id: str) -> dict:
        """Obtener el estado del usuario desde Cosmos DB"""
        if not self.services.cosmos_available:
            logger.warning(f"Cosmos DB no disponible para obtener estado de usuario {user_id}")
            return {}
            
        try:
            item = await asyncio.to_thread(
                self.services.user_state_container.read_item,
                item=user_id,
                partition_key=user_id
            )
            return item.get('state', {})
        except cosmos_exceptions.CosmosHttpResponseError as e:
            if e.status_code == 404:
                logger.info(f"Estado no encontrado para usuario {user_id}, creando nuevo")
                return {}
            logger.error(f"Error al leer estado de usuario {user_id}: {e}")
            raise
    
    async def save_user_state(self, user_id: str, state: dict):
        """Guardar el estado del usuario en Cosmos DB"""
        if not self.services.cosmos_available:
            logger.warning(f"Cosmos DB no disponible para guardar estado de usuario {user_id}")
            return
            
        try:
            await asyncio.to_thread(
                self.services.user_state_container.upsert_item,
                {
                    'id': user_id,
                    'user_id': user_id,
                    'state': state
                }
            )
            logger.info(f"Estado guardado para usuario {user_id}")
        except Exception as e:
            logger.error(f"Error guardando estado para usuario {user_id}: {e}")
    
    async def recomendar_eventos(self, user_id: str, user_state: dict, turn_context: TurnContext):
        """Recomendar eventos basados en intereses del usuario"""
        if not self.services.cosmos_available:
            await turn_context.send_activity("Servicio de eventos no disponible en este momento.")
            return
        
        intereses = user_state.get("intereses", [])
        if not intereses:
            await turn_context.send_activity("No tienes intereses registrados. Por favor, dime qué te interesa.")
            await self.save_user_state(user_id, {"estado": "esperando_intereses"})
            return
            
        query = "SELECT * FROM Eventos e WHERE ARRAY_CONTAINS(@intereses, e.temas)"
        params = [{"name": "@intereses", "value": intereses}]
        
        try:
            eventos = list(self.services.event_container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True
            ))
            
            if not eventos:
                await turn_context.send_activity("No encontré eventos que coincidan con tus intereses.")
                return
                
            evento = eventos[0]  # Tomar el primer evento encontrado
            
            # Actualizar estado con evento pendiente
            new_state = user_state.copy()
            new_state.update({
                "evento_pendiente": evento["id"],
                "evento_pendiente_sala": evento["sala"]
            })
            await self.save_user_state(user_id, new_state)
            
            # Enviar recomendación
            await turn_context.send_activity(
                f"Evento: {evento['nombre']} en {evento['sala']} a las {evento['hora']}. ¿Quieres agendarlo? (sí/no)"
            )
        except cosmos_exceptions.CosmosHttpResponseError as e:
            logger.error(f"Error buscando eventos: {e}")
            await turn_context.send_activity("No pude buscar eventos en este momento.")
    
    async def agendar_evento(self, user_id: str, user_state: dict, turn_context: TurnContext):
        """Agendar un evento pendiente"""
        if not self.services.cosmos_available:
            await turn_context.send_activity("No puedo acceder a la base de datos en este momento.")
            # Limpiar evento pendiente
            new_state = user_state.copy()
            new_state.pop("evento_pendiente", None)
            await self.save_user_state(user_id, new_state)
            return
            
        evento_id = user_state.get("evento_pendiente")
        sala = user_state.get("evento_pendiente_sala")
        
        if not evento_id or not sala:
            await turn_context.send_activity("No hay un evento pendiente para agendar.")
            return
            
        try:
            # Obtener detalles del evento
            evento = await asyncio.to_thread(
                self.services.event_container.read_item,
                item=evento_id,
                partition_key=sala
            )
            
            # Integración con calendario si está disponible
            if self.services.graph_available:
                # Crear evento en el calendario
                new_event = {
                    "subject": evento["nombre"],
                    "start": {"dateTime": evento["hora"], "timeZone": "UTC"},
                    "end": {"dateTime": evento.get("hora_fin", evento["hora"]), "timeZone": "UTC"},
                    "location": {"displayName": evento["sala"]},
                    "body": {
                        "contentType": "text",
                        "content": evento.get("descripcion", "Evento sin descripción")
                    }
                }
                
                try:
                    response = await asyncio.to_thread(
                        self.services.graph_client.post,
                        "/me/events",
                        json=new_event
                    )
                    await turn_context.send_activity("¡Evento agendado en tu calendario!")
                except Exception as e:
                    logger.error(f"Error en MS Graph al agendar evento: {e}")
                    await turn_context.send_activity("No pude agendar el evento en tu calendario.")
            else:
                await turn_context.send_activity(
                    f"Evento '{evento['nombre']}' registrado. Nota: La integración con el calendario no está disponible."
                )
        except cosmos_exceptions.CosmosHttpResponseError as e:
            logger.error(f"Error al leer evento {evento_id}: {e}")
            await turn_context.send_activity("No pude recuperar los detalles del evento.")
        
        # Limpiar evento pendiente
        new_state = user_state.copy()
        new_state.pop("evento_pendiente", None)
        new_state.pop("evento_pendiente_sala", None)
        await self.save_user_state(user_id, new_state)
    
    async def procesar_respuesta_ai(self, user_text: str, turn_context: TurnContext):
        """Procesar mensaje usando Azure OpenAI"""
        if not self.services.openai_available:
            await turn_context.send_activity(
                "Estoy en modo limitado y no puedo procesar consultas generales en este momento."
            )
            return
            
        system_message = """
        Eres un asistente útil que ayuda con información sobre eventos. 
        Tus respuestas deben ser concisas y amigables.
        Si te preguntan sobre eventos, sugiere usar el comando "recomienda eventos".
        """
            
        try:
            response = self.services.ai_client.chat.completions.create(
                model=self.services.AZURE_DEPLOYMENT_NAME,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_text}
                ],
                max_tokens=800,
                temperature=0.7
            )
            bot_reply = response.choices[0].message.content
            await turn_context.send_activity(bot_reply)
        except Exception as e:
            logger.error(f"Error en Azure OpenAI: {e}")
            await turn_context.send_activity("No pude procesar tu solicitud en este momento.")
    
    async def process_message(self, turn_context: TurnContext):
        """Procesar mensajes entrantes"""
        # Ignorar actividades que no sean mensajes
        if turn_context.activity.type != ActivityTypes.message:
            logger.info(f"Actividad ignorada: {turn_context.activity.type}")
            return
        
        # Extraer información del usuario y mensaje
        user_id = turn_context.activity.from_property.id
        channel_id = turn_context.activity.channel_id
        conversation_id = turn_context.activity.conversation.id
        
        logger.info(f"Mensaje recibido - Usuario: {user_id}, Canal: {channel_id}, Conversación: {conversation_id}")
        
        user_text = (turn_context.activity.text or "").strip().lower()
        logger.info(f"Texto recibido: '{user_text}'")
        
        # Obtener estado del usuario
        user_state = await self.get_user_state(user_id)
        logger.info(f"Estado del usuario: {user_state}")
        
        # Flujo de primera vez (sin intereses)
        if not user_state.get("intereses"):
            await turn_context.send_activity("¡Hola! ¿Qué tipo de eventos te interesan? (Ej: IA, Marketing, Cloud)")
            await self.save_user_state(user_id, {"estado": "esperando_intereses"})
            return
        
        # Flujo de captura de intereses
        if user_state.get("estado") == "esperando_intereses":
            intereses = [i.strip() for i in user_text.split(",") if i.strip()]
            if not intereses:
                await turn_context.send_activity("No entendí tus intereses. Por favor, sepáralos por comas (Ej: IA, Marketing, Cloud)")
                return
                
            new_state = {
                "intereses": intereses,
                "estado": "listo"
            }
            await self.save_user_state(user_id, new_state)
            await turn_context.send_activity(f"¡Genial! Registré tus intereses: {', '.join(intereses)}. Ahora puedo recomendarte eventos.")
            return
        
        # Flujo de confirmación de evento pendiente
        if "evento_pendiente" in user_state:
            if user_text in ("sí", "si", "yes", "claro", "por supuesto"):
                await self.agendar_evento(user_id, user_state, turn_context)
                return
            elif user_text in ("no", "nop", "nope"):
                # Limpiar evento pendiente
                new_state = user_state.copy()
                new_state.pop("evento_pendiente", None)
                new_state.pop("evento_pendiente_sala", None)
                await self.save_user_state(user_id, new_state)
                await turn_context.send_activity("Evento no agendado. ¿Puedo ayudarte con algo más?")
                return
        
        # Comandos específicos 
        if "recomienda" in user_text and "evento" in user_text:
            await self.recomendar_eventos(user_id, user_state, turn_context)
            return
        
        if "mis intereses" in user_text:
            intereses = user_state.get("intereses", [])
            if intereses:
                await turn_context.send_activity(f"Tus intereses actuales son: {', '.join(intereses)}")
            else:
                await turn_context.send_activity("No tienes intereses registrados.")
            return
            
        if "cambiar intereses" in user_text:
            await turn_context.send_activity("Por favor, dime tus nuevos intereses separados por comas (Ej: IA, Marketing, Cloud)")
            new_state = user_state.copy()
            new_state["estado"] = "esperando_intereses"
            await self.save_user_state(user_id, new_state)
            return
        
        # Procesar con Azure OpenAI para otras consultas
        await self.procesar_respuesta_ai(user_text, turn_context)

# Crear aplicación Flask
app = Flask(__name__)
PORT = int(os.environ.get("PORT", 3978))

# Configurar Bot Framework
APP_ID = os.environ.get("MicrosoftAppId", "")
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")

logger.info("Configurando Bot Framework con ID: %s", APP_ID if APP_ID else "No configurado")

settings = BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD)
adapter = BotFrameworkAdapter(settings)

# Storage para estado de la conversación
memory = MemoryStorage()
conversation_state = ConversationState(memory)

# Inicializar servicios y bot
services = ServiceManager()
bot = SmartBuddyBot(services)

# Manejador de errores
async def on_error(context: TurnContext, error: Exception):
    logger.error(f"[on_turn_error] {error}")
    logger.error(traceback.format_exc())
    
    # Guardar conversación para depuración
    if hasattr(context, 'activity') and context.activity:
        logger.error(f"Error en actividad: {context.activity.type}")
        if hasattr(context.activity, 'from_property') and context.activity.from_property:
            logger.error(f"Usuario: {context.activity.from_property.id}")
        if hasattr(context.activity, 'text'):
            logger.error(f"Texto: {context.activity.text}")
    
    # Notificar al usuario
    await context.send_activity("Lo siento, ha ocurrido un error. El equipo técnico ha sido notificado.")

adapter.on_turn_error = on_error

@app.route("/api/messages", methods=["POST"])
def messages():
    """Endpoint principal para mensajes del Bot Framework"""
    # Verificar Content-Type
    if "application/json" not in request.headers.get("Content-Type", ""):
        logger.warning("Content-Type incorrecto")
        return Response(status=415)
    
    # Deserializar actividad
    body = request.json
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")
    
    logger.info(f"Actividad recibida: {activity.type}")
    
    # Procesar actividad
    async def call_bot():
        await adapter.process_activity(activity, auth_header, bot.process_message)
    
    try:
        asyncio.run(call_bot())
    except Exception as e:
        logger.error(f"Error procesando actividad: {e}")
        logger.error(traceback.format_exc())
        return Response(status=500)
    
    return Response(status=200)

@app.route("/", methods=["GET"])
def health_check():
    """Endpoint de verificación de estado"""
    status = {
        "status": "running",
        "cosmos_db": "available" if services.cosmos_available else "unavailable",
        "msgraph": "available" if services.graph_available else "unavailable",
        "openai": "available" if services.openai_available else "unavailable",
        "bot_framework": "configured" if APP_ID and APP_PASSWORD else "not_configured"
    }
    
    return json.dumps(status), 200, {'Content-Type': 'application/json'}

if __name__ == "__main__":
    try:
        logger.info(f"Iniciando servidor en el puerto {PORT}")
        app.run(host='0.0.0.0', port=PORT)
    except Exception as ex:
        logger.error(f"Error al iniciar servidor: {ex}")
        logger.error(traceback.format_exc())
