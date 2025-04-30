from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes
from flask import Flask, request, Response
import os
import asyncio
import sys
import traceback
import logging

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("AzureBot")

# App y configuración
APP = Flask(__name__)
PORT = os.environ.get("PORT", 3978)

# IMPORTANTE: Configuración del ID y contraseña del bot
APP_ID = os.environ.get("MicrosoftAppId", "315d9eaa-47c1-40e6-8348-28397241e393") 
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")

# Log de verificación de credenciales
logger.info(f"Configurando bot con APP_ID: {APP_ID}")
logger.info(f"Password configurada: {'Sí' if APP_PASSWORD else 'No'}")

# CLAVE DE SOLUCIÓN: Configuración adecuada para Auth JWTs
SETTINGS = BotFrameworkAdapterSettings(
    app_id=APP_ID,
    app_password=APP_PASSWORD,
    auth_connect_timeout=5000,  # Aumentar timeout para auth
    auth_connect_retry_count=3,  # Intentos de reconexión
    channel_provider=None,  # Usar el channel provider default
    auth_configuration=None  # Usar la configuración de auth default
)

# Crear adaptador con la configuración
ADAPTER = BotFrameworkAdapter(SETTINGS)

# Manejo de errores
async def on_error(context: TurnContext, error: Exception):
    logger.error(f"\n [on_turn_error] No controlado: {error}")
    logger.error(traceback.format_exc())
    
    await context.send_activity("Lo siento, ha ocurrido un error.")

ADAPTER.on_turn_error = on_error

# SOLUCIÓN: Deshabilitar la autenticación si no hay credenciales
# Esto es útil para pruebas locales o si hay problemas con las credenciales
if not APP_ID or not APP_PASSWORD:
    logger.warning("¡ADVERTENCIA! Ejecutando sin autenticación. Esto solo debe hacerse en desarrollo.")
    
    # Monkey patch la función authenticate_request para bypass la autenticación
    # Solo para desarrollo y pruebas - no usar en producción sin credenciales
    from botframework.connector.auth import JwtTokenValidation
    
    async def authenticate_request_bypass(activity, auth_header, credentials, channel_service_url=None):
        return
    
    JwtTokenValidation.authenticate_request = authenticate_request_bypass

# Ruta para mensajes
@APP.route("/api/messages", methods=["POST"])
def messages():
    logger.info("==== NUEVA SOLICITUD RECIBIDA ====")
    
    # Imprimir los headers para depuración
    auth_header = request.headers.get("Authorization", "No Auth header found")
    logger.info(f"Auth Header: {auth_header[:30]}..." if len(auth_header) > 30 else auth_header)
    
    if "application/json" in request.headers.get("Content-Type", ""):
        body = request.json
        logger.info(f"Body recibido: {body}")
    else:
        logger.warning("Solicitud sin content-type application/json")
        return Response(status=415)
    
    async def process_activity():
        try:
            activity = Activity().deserialize(body)
            
            # Obtener el auth header para la autenticación
            auth_header = request.headers.get("Authorization", "")
            
            async def turn_call(turn_context):
                if turn_context.activity.type == ActivityTypes.message:
                    message_text = turn_context.activity.text
                    logger.info(f"Mensaje recibido: {message_text}")
                    await turn_context.send_activity(f"Recibí: '{message_text}'")
            
            # IMPORTANTE: Pasar el auth_header al process_activity
            await ADAPTER.process_activity(activity, auth_header, turn_call)
            logger.info("Actividad procesada correctamente")
            
        except PermissionError as pe:
            logger.error(f"Error de permisos: {str(pe)}")
            logger.info("Intentando continuar sin auth...")
            
            # Opción alternativa si hay problemas de autenticación
            activity = Activity().deserialize(body)
            
            async def turn_call(turn_context):
                if turn_context.activity.type == ActivityTypes.message:
                    await turn_context.send_activity(f"Recibí un mensaje (auth bypass)")
            
            # Usar un auth_header vacío o "" como bypass
            await ADAPTER.process_activity(activity, "", turn_call)
            
        except Exception as e:
            logger.error(f"Error procesando actividad: {str(e)}")
            logger.error(traceback.format_exc())
    
    # Ejecutar la actividad en un loop asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_activity())
    except Exception as e:
        logger.error(f"Error en el loop asyncio: {str(e)}")
    finally:
        loop.close()
    
    return Response(status=200)

# Endpoint de diagnóstico para verificar estado
@APP.route("/", methods=["GET"])
def ping():
    return "Bot running!"

# Iniciar el servidor
if __name__ == "__main__":
    try:
        APP.run(host='0.0.0.0', port=int(PORT))
    except Exception as ex:
        logger.error(f"Error al iniciar el servidor: {ex}")
