    from flask import Flask, request, jsonify
    from piopiy import Action
    from dotenv import load_dotenv
    import os

    load_dotenv()

    WEBSOCKET_URL = os.getenv("WEBSOCKET_URL")
    # The number for your human agent.
    AGENT_NUMBER = os.getenv("AGENT_NUMBER")
    # Your Piopiy number that will receive the call.
    PIOPIY_NUMBER = os.getenv("PIOPIY_NUMBER")
    app = Flask(__name__)
    @app.route("/python/inbound", methods=["POST"])
    def inbound_call():
        action = Action()
        action.stream(WEBSOCKET_URL,{ "listen_mode":"caller", "voice_quality": "8000", "stream_on_answer": True })
        action.call(AGENT_NUMBER, PIOPIY_NUMBER,{ "duration": 300, "timeout": 20, "loop": 2 })
        return jsonify(action.PCMO())
    if __name__ == "__main__":
        app.run(port=3001, debug=True)