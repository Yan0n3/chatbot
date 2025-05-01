import os
import json
import asyncio
import logging
import traceback
import datetime
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

# Configuración logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("AzureBot")

class ServiceManager:
    def __init__(self):
        self.cosmos_available = False
        self.graph_available = False
        self.openai_available = False
        self._setup_cosmos()
        self._setup_graph()
        self._setup_openai()

    def _setup_cosmos(self):
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
            
            self.event_container = self.database.get_container_client("Eventos")
            self.user_state_container = self.database.get_container_client("UserStates")
            self.cosmos_available = True
            logger.info("Cosmos DB configurado correctamente")
        except Exception as e:
            logger.error(f"Error en Cosmos DB: {e}")

    def _setup_graph(self):
        try:
            from azure.identity import ClientSecretCredential
            from msgraph.core import GraphClient
            
            TENANT_ID = os.environ.get("TENANT_ID")
            CLIENT_ID = os.environ.get("CLIENT_ID")
            CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
            
            if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
                logger.warning("Credenciales de MS Graph no configuradas")
                return
            
            self.credential = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
            self.graph_client = GraphClient(credential=self.credential)
            self.graph_available = True
            logger.info("MS Graph configurado correctamente")
        except ImportError:
            logger.warning("Módulo msgraph no disponible")
        except Exception as e:
            logger.error(f"Error en MS Graph: {e}")

    def _setup_openai(self):
        try:
            AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
            AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
            AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
            self.AZURE_DEPLOYMENT_NAME = os.environ.get("AZURE_DEPLOYMENT_NAME", "gpt-4.1")
            
            if AZURE_OPENAI_KEY and AZURE_OPENAI_ENDPOINT:
                self.ai_client = AzureOpenAI(
                    api_key=AZURE_OPENAI_KEY,
                    azure_endpoint=AZURE_OPENAI_ENDPOINT,
                    api_version=AZURE_OPENAI_API_VERSION,
                )
                self.openai_available = True
                logger.info("Azure OpenAI configurado correctamente")
            else:
                logger.warning("Credenciales de OpenAI no configuradas")
        except Exception as e:
            logger.error(f"Error en OpenAI: {e}")

class SmartBuddyBot:
    def __init__(self, services):
        self.services = services

    async def get_user_state(self, user_id: str) -> dict:
        if not self.services.cosmos_available:
            return {}
            
        try:
            # Usar solo user_id como identificador
            item = await asyncio.to_thread(
                self.services.user_state_container.read_item,
                item=user_id,
                partition_key=user_id
            )
            return item.get('state', {})
        except cosmos_exceptions.CosmosHttpResponseError as e:
            if e.status_code == 404:
                return {}
            raise

    async def save_user_state(self, user_id: str, state: dict):
        if not self.services.cosmos_available:
            return
            
        document = {
            'id': user_id,
            'user_id': user_id,
            'state': state,
            'last_updated': datetime.datetime.utcnow().isoformat()
        }
        
        # Guardar con reintentos
        for _ in range(3):
            try:
                await asyncio.to_thread(
                    self.services.user_state_container.upsert_item,
                    document
                )
                return
            except Exception as e:
                logger.error(f"Error guardando estado: {e}")
                await asyncio.sleep(1)

    async def process_message(self, turn_context: TurnContext):
        if turn_context.activity.type != ActivityTypes.message:
            return

        user_id = turn_context.activity.from_property.id
        user_text = (turn_context.activity.text or "").strip().lower()

        # Obtener estado del usuario
        user_state = await self.get_user_state(user_id)
        
        # Flujo de intereses
        if not user_state.get("intereses"):
            if user_state.get("estado") != "esperando_intereses":
                await self.save_user_state(user_id, {"estado": "esperando_intereses"})
            await turn_context.send_activity("¡Hola! ¿Qué tipo de eventos te interesan? (Ej: IA, Marketing, Cloud)")
            return

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
            await turn_context.send_activity(f"¡Genial! Registré tus intereses: {', '.join(intereses)}. ¿Quieres una recomendación?")
            return

        # Flujo de recomendación
        if "recomienda" in user_text:
            if not self.services.cosmos_available:
                await turn_context.send_activity("Servicio de eventos no disponible.")
                return

            query = "SELECT * FROM Eventos e WHERE ARRAY_CONTAINS(@intereses, e.temas)"
            params = [{"name": "@intereses", "value": user_state["intereses"]}]
            
            try:
                eventos = list(self.services.event_container.query_items(
                    query=query,
                    parameters=params,
                    enable_cross_partition_query=True
                ))
                if eventos:
                    evento = eventos[0]
                    new_state = user_state | {
                        "evento_pendiente": evento["id"],
                        "evento_pendiente_sala": evento["sala"]
                    }
                    await self.save_user_state(user_id, new_state)
                    await turn_context.send_activity(
                        f"Evento: {evento['nombre']} en {evento['sala']} a las {evento['hora']}. ¿Agendar? (sí/no)"
                    )
                else:
                    await turn_context.send_activity("No hay eventos disponibles.")
            except Exception as e:
                logger.error(f"Error buscando eventos: {e}")
                await turn_context.send_activity("No pude buscar eventos.")

        # Flujo de confirmación
        elif "evento_pendiente" in user_state:
            if user_text in ("sí", "si"):
                evento_id = user_state["evento_pendiente"]
                sala = user_state["evento_pendiente_sala"]
                
                try:
                    evento = await asyncio.to_thread(
                        self.services.event_container.read_item,
                        item=evento_id,
                        partition_key=sala
                    )
                    
                    if self.services.graph_available:
                        new_event = {
                            "subject": evento["nombre"],
                            "start": {"dateTime": evento["hora"], "timeZone": "UTC"},
                            "end": {"dateTime": evento.get("hora_fin", evento["hora"]), "timeZone": "UTC"},
                            "location": {"displayName": evento["sala"]}
                        }
                        await self.services.graph_client.post(
                            "/me/calendar/events",
                            data=json.dumps(new_event),
                            headers={"Content-Type": "application/json"}
                        )
                        await turn_context.send_activity("¡Evento agendado!")
                    else:
                        await turn_context.send_activity("Evento registrado. Integración de calendario no disponible.")
                except Exception as e:
                    logger.error(f"Error agendando: {e}")
                    await turn_context.send_activity("No pude agendar el evento.")
                    
                # Limpiar estado
                new_state = user_state.copy()
                new_state.pop("evento_pendiente", None)
                new_state.pop("evento_pendiente_sala", None)
                await self.save_user_state(user_id, new_state)

            elif user_text in ("no", "nop"):
                new_state = user_state.copy()
                new_state.pop("evento_pendiente", None)
                new_state.pop("evento_pendiente_sala", None)
                await self.save_user_state(user_id, new_state)
                await turn_context.send_activity("Evento no agendado.")

        # Respuesta por defecto
        else:
            if self.services.openai_available:
                try:
                    response = self.services.ai_client.chat.completions.create(
                        model=self.services.AZURE_DEPLOYMENT_NAME,
                        messages=[
                            {"role": "system", "content": "Eres un asistente útil."},
                            {"role": "user", "content": user_text}
                        ],
                        max_tokens=800
                    )
                    await turn_context.send_activity(response.choices[0].message.content)
                except Exception as e:
                    logger.error(f"Error en OpenAI: {e}")
                    await turn_context.send_activity("No pude procesar tu solicitud.")
            else:
                await turn_context.send_activity("Estoy en modo limitado.")

# Configuración Flask
app = Flask(__name__)
PORT = int(os.environ.get("PORT", 3978))

# Configurar Bot Framework
APP_ID = os.environ.get("MicrosoftAppId", "")
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")
settings = BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD)
adapter = BotFrameworkAdapter(settings)

# Inicializar servicios
services = ServiceManager()
bot = SmartBuddyBot(services)

# Manejador de errores
async def on_error(context: TurnContext, error: Exception):
    logger.error(f"[on_turn_error] {error}")
    logger.error(traceback.format_exc())
    await context.send_activity("Lo siento, ha ocurrido un error.")

adapter.on_turn_error = on_error

@app.route("/api/messages", methods=["POST"])
def messages():
    if "application/json" not in request.headers.get("Content-Type", ""):
        return Response(status=415)
        
    body = request.json
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")
    
    async def call_bot():
        await adapter.process_activity(activity, auth_header, bot.process_message)
    
    try:
        asyncio.run(call_bot())
    except Exception as e:
        logger.error(f"Error procesando actividad: {e}")
        return Response(status=500)
    
    return Response(status=200)

@app.route("/", methods=["GET"])
def health_check():
    return json.dumps({
        "status": "running",
        "cosmos_db": "available" if services.cosmos_available else "unavailable",
        "msgraph": "available" if services.graph_available else "unavailable",
        "openai": "available" if services.openai_available else "unavailable"
    }), 200

if __name__ == "__main__":
    try:
        app.run(host='0.0.0.0', port=PORT)
    except Exception as ex:
        logger.error(f"Error al iniciar servidor: {ex}")
