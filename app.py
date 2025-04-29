from flask import Flask, request, jsonify
from openai import AzureOpenAI
import os

app = Flask(__name__)

# Configurar cliente Azure OpenAI
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_KEY"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
)

DEPLOYMENT_NAME = os.getenv("AZURE_DEPLOYMENT_NAME", "gpt-4.1")

@app.route("/api/messages", methods=["POST"])
def chat():
    try:
        data = request.json

        # Solo procesar mensajes de texto
        if data.get("type") != "message" or "text" not in data:
            return jsonify({"type": "message", "text": "No puedo procesar este tipo de mensaje."})

        user_input = data["text"]

        response = client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": "Eres un asistente útil."},
                {"role": "user", "content": user_input}
            ],
            max_tokens=800,
            temperature=1.0,
            top_p=1.0,
            frequency_penalty=0.0,
            presence_penalty=0.0
        )

        reply = response.choices[0].message.content

        return jsonify({
            "type": "message",
            "text": reply
        })

    except Exception as e:
        return jsonify({
            "type": "message",
            "text": f"Ocurrió un error: {str(e)}"
        }), 500

@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
