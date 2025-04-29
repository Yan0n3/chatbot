from flask import Flask, request, jsonify
import openai
import os

app = Flask(__name__)

# Configurar claves desde variables de entorno
openai.api_type = "azure"
openai.api_base = os.getenv("AZURE_OPENAI_ENDPOINT")
openai.api_version = "2023-03-15-preview"
openai.api_key = os.getenv("AZURE_OPENAI_KEY")
DEPLOYMENT_NAME = os.getenv("AZURE_DEPLOYMENT_NAME")

@app.route("/api/messages", methods=["POST"])
def get_response():
    try:
        data = request.json
        user_msg = data.get("text", "")

        response = openai.ChatCompletion.create(
            engine=DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": "Eres un asistente útil."},
                {"role": "user", "content": user_msg}
            ]
        )

        reply = response["choices"][0]["message"]["content"]
        return jsonify({"text": reply})
    except Exception as e:
        return jsonify({"text": f"Ocurrió un error: {str(e)}"}), 500

@app.route("/", methods=["GET"])
def health():
    return "Bot is running!", 200

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))  # Render define PORT automáticamente
    app.run(host="0.0.0.0", port=port)
