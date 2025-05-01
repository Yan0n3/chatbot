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
    TurnContext
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

INTERES_ALIASES = {
    "ia": "inteligencia artificial",
    "ai": "inteligencia artificial",
    "nube": "cloud",
    "mercadeo": "marketing",
    # puedes extender esta lista
}

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
            logger.error(f"Error en Cosmos DB: {repr(e)}")

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
            credential = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
            self.graph_client = GraphClient(credential=credential)
            self.graph_available = True
            logger.info("MS Graph configurado correctamente")
        except Exception as e:
            logger.error(f"Error en MS Graph: {repr(e)}")

    def _setup_openai(self):
        AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
        AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
        AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
        self.AZURE_DEPLOYMENT_NAME = os.environ.get("AZURE_DEPLOYMENT_NAME", "gpt-4.1")
        if AZURE_OPENAI_KEY and AZURE_OPENAI_ENDPOINT:
            try:
                self.ai_client = AzureOpenAI(
                    api_key=AZURE_OPENAI_KEY,
                    azure_endpoint=AZURE_OPENAI_ENDPOINT,
                    api_version=AZURE_OPENAI_API_VERSION,
                )
                self.openai_available = True
                logger.info("Azure OpenAI configurado correctamente")
            except Exception as e:
                logger.error(f"Error en OpenAI: {repr(e)}")
        else:
            logger.warning("Credenciales de OpenAI no configuradas")

class SmartBuddyBot:
    def __init__(self, services):
        self.services = services

    async def get_user_state(self, user_id: str) -> dict:
        if not self.services.cosmos_available:
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
                return {}
            raise

    async def save_user_state(self, user_id: str, state: dict):
        if not self.services.cosmos_available:
            return
        document = {
            'id': user_id,
            'user_id': user_id,
            'state': state,
            'last_updated': str(datetime.datetime.utcnow())
        }
        await asyncio.to_thread(
            self.services.user_state_container.upsert_item,
            document
        )

    async def recomendar_eventos(self, user_id: str, user_state: dict, turn_context: TurnContext):
        if not self.services.cosmos_available:
            await turn_context.send_activity("Servicio de eventos no disponible.")
            return

        intereses = [i.lower() for i in user_state.get("intereses", [])]
        if not intereses:
            await turn_context.send_activity("No tienes intereses registrados.")
            return

        query_conditions = " OR ".join([f"ARRAY_CONTAINS(e.temas, @interes_{idx})" 
                                      for idx in range(len(intereses))])
        query = f"SELECT * FROM Eventos e WHERE {query_conditions}"
        params = [{"name": f"@interes_{idx}", "value": interes} 
                 for idx, interes in enumerate(intereses)]

        try:
            eventos = list(self.services.event_container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True
            ))

            if not eventos:
                await turn_context.send_activity("No hay eventos que coincidan con tus intereses.")
                return

            eventos.sort(key=lambda x: (-x.get('popularidad', 0), x['hora']))

            mensaje = "Eventos recomendados:\n"
            for evento in eventos[:3]:
                mensaje += (
                    f"- **{evento['nombre']}**\n"
                    f"  Sala: {evento['sala']}\n"
                    f"  Hora: {evento['hora']}\n"
                    f"  Popularidad: {evento.get('popularidad', 0)}%\n"
                    f"  Descripción: {evento.get('descripcion', 'Sin descripción')}\n"
                    "  ¿Agendar? (sí/no)\n\n"
                )

            new_state = user_state.copy()
            new_state["eventos_pendientes"] = [e["id"] for e in eventos[:3]]
            await self.save_user_state(user_id, new_state)

            await turn_context.send_activity(mensaje)
        except Exception as e:
            logger.error(f"Error recomendando eventos: {repr(e)}")
            await turn_context.send_activity("No pude buscar eventos en este momento.")

    async def agendar_evento(self, user_id: str, user_state: dict, turn_context: TurnContext):
        evento_id = user_state.get("eventos_pendientes", [None])[0]
        if not evento_id:
            await turn_context.send_activity("No hay eventos pendientes para agendar.")
            return

        try:
            evento = await asyncio.to_thread(
                self.services.event_container.read_item,
                item=evento_id,
                partition_key=evento_id.split("_")[0]
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
                    json=new_event
                )
                await turn_context.send_activity("¡Evento agendado!")
            else:
                await turn_context.send_activity(f"Evento '{evento['nombre']}' registrado.")
        except Exception as e:
            logger.error(f"Error agendando evento: {repr(e)}")
            await turn_context.send_activity("No pude agendar el evento.")
        finally:
            new_state = user_state.copy()
            new_state.pop("eventos_pendientes", None)
            await self.save_user_state(user_id, new_state)

    async def process_message(self, turn_context: TurnContext):
        if turn_context.activity.type != ActivityTypes.message:
            return

        user_id = turn_context.activity.from_property.id
        user_text = (turn_context.activity.text or "").strip().lower()

        user_state = await self.get_user_state(user_id)
        logger.info(f"Estado del usuario: {user_state}")

        if not user_state.get("intereses"):
            if user_state.get("estado") != "esperando_intereses":
                await self.save_user_state(user_id, {"estado": "esperando_intereses"})
            await turn_context.send_activity("¡Hola! ¿Qué eventos te interesan? (Separa con comas: IA, Cloud, Marketing)")
            return

        if user_state.get("estado") == "esperando_intereses":
            if "," not in user_text:
                await turn_context.send_activity("Por favor, separa tus intereses con comas. Ej: 'IA, Cloud, Marketing'")
                return
            intereses = [i.strip() for i in user_text.split(",") if i.strip()]
            new_state = {
                "intereses": intereses,
                "estado": "listo"
            }
            await self.save_user_state(user_id, new_state)
            await turn_context.send_activity(f"¡Genial! Ahora puedo recomendarte eventos sobre: {', '.join(intereses)}. ¿Quieres una recomendación?")
            return

        if "eventos_pendientes" in user_state and user_text in ("sí", "si"):
            await self.agendar_evento(user_id, user_state, turn_context)
            return

        if "recomienda" in user_text:
            await self.recomendar_eventos(user_id, user_state, turn_context)
            return

        user_text_tokens = user_text.split()
        user_text_explicit = " ".join([INTERES_ALIASES.get(token, token) for token in user_text_tokens])
        intereses_usuario = [i.lower() for i in user_state.get("intereses", [])]

        if any(interes in user_text_explicit for interes in intereses_usuario):
            await self.recomendar_eventos(user_id, user_state, turn_context)
            return

        if self.services.openai_available:
            try:
                response = self.services.ai_client.chat.completions.create(
                    model=self.services.AZURE_DEPLOYMENT_NAME,
                    messages=[
                        {"role": "system", "content": "Eres un asistente de eventos."},
                        {"role": "user", "content": user_text}
                    ],
                    max_tokens=800
                )
                await turn_context.send_activity(response.choices[0].message.content)
            except Exception as e:
                logger.error(f"Error en OpenAI: {repr(e)}")
                await turn_context.send_activity("No pude procesar tu solicitud.")
        else:
            await turn_context.send_activity("Estoy en modo limitado.")

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 3978))
settings = BotFrameworkAdapterSettings(
    os.environ.get("MicrosoftAppId", ""), 
    os.environ.get("MicrosoftAppPassword", "")
)
adapter = BotFrameworkAdapter(settings)
services = ServiceManager()
bot = SmartBuddyBot(services)

async def on_error(context: TurnContext, error: Exception):
    logger.error(f"[on_turn_error] {repr(error)}")
    traceback.print_exc()
    await context.send_activity("Lo siento, ocurrió un error. El equipo técnico fue notificado.")

adapter.on_turn_error = on_error

@app.route("/api/messages", methods=["POST"])
def messages():
    if "application/json" not in request.headers.get("Content-Type", ""):
        return Response(status=415)

    activity = Activity().from_dict(request.json)
    auth_header = request.headers.get("Authorization", "")

    async def call_bot():
        await adapter.process_activity(activity, auth_header, bot.process_message)

    try:
        asyncio.run(call_bot())
    except Exception as e:
        logger.error(f"Error procesando actividad: {repr(e)}")
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
        logger.error(f"Error al iniciar servidor: {repr(ex)}")
