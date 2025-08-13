import os
import csv
import json
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from twilio.rest import Client
from werkzeug.utils import secure_filename

# Load environment variables
load_dotenv()

# Flask app setup
app = Flask(__name__)
CORS(app)  # Allow all origins

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Piopiy Configuration (optional)
PIOPIY_API_KEY = os.getenv("PIOPIY_API_KEY")

# Upload folder
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Data storage
DATA_FILE = "leads.json"
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump([], f)

# Twilio Client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_sms(to, message):
    """Send SMS via Twilio or Piopiy"""
    if twilio_client:
        try:
            twilio_client.messages.create(
                body=message,
                from_=TWILIO_PHONE_NUMBER,
                to=to
            )
            return True, "SMS sent via Twilio"
        except Exception as e:
            return False, str(e)
    elif PIOPIY_API_KEY:
        try:
            import requests
            resp = requests.get(
                "https://sms.piopiy.com/api/send",
                params={
                    "key": PIOPIY_API_KEY,
                    "phone": to,
                    "message": message
                }
            )
            return True, resp.text
        except Exception as e:
            return False, str(e)
    else:
        print(f"Simulated SMS to {to}: {message}")
        return True, "Simulated"


def save_lead(name, phone, project):
    """Save lead to local JSON"""
    with open(DATA_FILE, "r+") as f:
        leads = json.load(f)
        leads.append({
            "name": name,
            "phone": phone,
            "project": project,
            "timestamp": datetime.now().isoformat()
        })
        f.seek(0)
        json.dump(leads, f, indent=2)


@app.route("/upload_csv", methods=["POST"])
def upload_csv():
    """Upload and process CSV file"""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    with open(filepath, newline='', encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            name = row.get("name")
            phone = row.get("phone")
            project = row.get("project")
            if phone:
                send_sms(phone, f"Hello {name}, thanks for showing interest in {project}")
                save_lead(name, phone, project)

    return jsonify({"message": "CSV processed successfully"})


@app.route("/leads", methods=["GET"])
def get_leads():
    """Get all leads"""
    with open(DATA_FILE, "r") as f:
        leads = json.load(f)
    return jsonify(leads)


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "running", "message": "Lead Management API"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
