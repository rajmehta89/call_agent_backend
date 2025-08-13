# """
# Flask API for Leads Management
# Provides endpoints for managing leads, CSV upload, and calling functionality
# """
#
# import csv
# import io
# import json
# import os
# from datetime import datetime
# from flask import Flask, request, jsonify
# from flask_cors import CORS
# from dotenv import load_dotenv
# import uuid
# from piopiy import RestClient, Action
#
# # Load environment variables
# load_dotenv()
#
# app = Flask(__name__)
#
# # Enable CORS for all routes and origins
# CORS(app)
#
# # File to store leads data
# LEADS_FILE = "leads_data.json"
#
# # Piopiy Configuration
# APP_ID = os.getenv("APP_ID")
# APP_SECRET = os.getenv("APP_SECRET")
# CALLER_ID = os.getenv("CALLER_ID")
# WEBSOCKET_URL = os.getenv("WEBSOCKET_URL")
#
#
# def clean_phone_number(phone_str):
#     """Removes '+' and spaces, then converts to integer."""
#     if isinstance(phone_str, str):
#         return int(phone_str.replace('+', '').replace('-', '').replace(' ', ''))
#     return phone_str
#
#
# class OutboundCaller:
#     def __init__(self):
#         # Check if all required variables are present
#         if not all([APP_ID, APP_SECRET, CALLER_ID, WEBSOCKET_URL]):
#             print("⚠️ Warning: Piopiy credentials not configured. Call functionality will be simulated.")
#             self.client = None
#             return
#
#         self.client = RestClient(int(APP_ID), APP_SECRET)
#         print("📞 Outbound caller initialized.")
#         print(f"   - Caller ID: {CALLER_ID}")
#         print(f"   - WebSocket URL: {WEBSOCKET_URL}")
#
#     def make_call(self, customer_number_str):
#         """Initiates an outbound call and connects it to the WebSocket voice agent."""
#         if not self.client:
#             print(f"[SIMULATED] Would call {customer_number_str}")
#             return {"status": "simulated", "message": "Piopiy not configured"}
#
#         try:
#             customer_number = clean_phone_number(customer_number_str)
#             piopiy_number = clean_phone_number(CALLER_ID)
#
#             action = Action()
#             action.stream(
#                 WEBSOCKET_URL,
#                 {
#                     "listen_mode": "callee",
#                     "stream_on_answer": True
#                 }
#             )
#
#             print(f"📲 Placing call to {customer_number_str}...")
#
#             response = self.client.voice.call(
#                 to=customer_number,
#                 piopiy_no=piopiy_number,
#                 to_or_array_pcmo=action.PCMO(),
#                 options={'record': True}
#             )
#
#             print("✅ Call initiated successfully!")
#             print("   - Response from Piopiy:", response)
#             return response
#
#         except Exception as e:
#             print(f"❌ Failed to make call: {e}")
#             return {"error": str(e)}
#
#
# # Initialize the outbound caller
# outbound_caller = OutboundCaller()
#
#
# def load_leads():
#     """Load leads from JSON file"""
#     try:
#         if os.path.exists(LEADS_FILE):
#             with open(LEADS_FILE, 'r', encoding='utf-8') as f:
#                 return json.load(f)
#         return []
#     except Exception as e:
#         print(f"Error loading leads: {e}")
#         return []
#
#
# def save_leads(leads):
#     """Save leads to JSON file"""
#     try:
#         with open(LEADS_FILE, 'w', encoding='utf-8') as f:
#             json.dump(leads, f, indent=2, ensure_ascii=False)
#         return True
#     except Exception as e:
#         print(f"Error saving leads: {e}")
#         return False
#
#
# @app.route("/api/leads", methods=["GET"])
# def get_leads():
#     """Get all leads"""
#     try:
#         leads = load_leads()
#         return jsonify({"success": True, "data": leads, "count": len(leads)}), 200
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)}), 500
#
#
# @app.route("/api/leads", methods=["POST"])
# def add_lead():
#     """Add a new lead manually"""
#     try:
#         data = request.get_json()
#         required_fields = ["name", "phone"]
#         for field in required_fields:
#             if not data.get(field):
#                 return jsonify({"success": False, "error": f"Missing required field: {field}"}), 400
#
#         lead = {
#             "id": str(uuid.uuid4()),
#             "name": data["name"].strip(),
#             "phone": data["phone"].strip(),
#             "email": data.get("email", "").strip(),
#             "company": data.get("company", "").strip(),
#             "notes": data.get("notes", "").strip(),
#             "status": "new",
#             "call_attempts": 0,
#             "last_call": None,
#             "created_at": datetime.now().isoformat(),
#             "updated_at": datetime.now().isoformat()
#         }
#
#         leads = load_leads()
#         leads.append(lead)
#
#         if save_leads(leads):
#             return jsonify({"success": True, "message": "Lead added successfully", "data": lead}), 201
#         else:
#             return jsonify({"success": False, "error": "Failed to save lead"}), 500
#
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)}), 500
#
#
# @app.route("/api/leads/<lead_id>", methods=["PUT"])
# def update_lead(lead_id):
#     """Update an existing lead"""
#     try:
#         data = request.get_json()
#         leads = load_leads()
#         lead_index = next((i for i, lead in enumerate(leads) if lead["id"] == lead_id), None)
#
#         if lead_index is None:
#             return jsonify({"success": False, "error": "Lead not found"}), 404
#
#         updatable_fields = ["name", "phone", "email", "company", "notes", "status"]
#         for field in updatable_fields:
#             if field in data:
#                 leads[lead_index][field] = data[field]
#
#         leads[lead_index]["updated_at"] = datetime.now().isoformat()
#
#         if save_leads(leads):
#             return jsonify({"success": True, "message": "Lead updated successfully", "data": leads[lead_index]}), 200
#         else:
#             return jsonify({"success": False, "error": "Failed to update lead"}), 500
#
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)}), 500
#
#
# @app.route("/api/leads/<lead_id>", methods=["DELETE"])
# def delete_lead(lead_id):
#     """Delete a lead"""
#     try:
#         leads = load_leads()
#         original_count = len(leads)
#         leads = [lead for lead in leads if lead["id"] != lead_id]
#
#         if len(leads) == original_count:
#             return jsonify({"success": False, "error": "Lead not found"}), 404
#
#         if save_leads(leads):
#             return jsonify({"success": True, "message": "Lead deleted successfully"}), 200
#         else:
#             return jsonify({"success": False, "error": "Failed to delete lead"}), 500
#
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)}), 500
#
#
# @app.route("/api/leads/upload", methods=["POST"])
# def upload_leads_csv():
#     """Upload leads from CSV file"""
#     try:
#         if 'file' not in request.files:
#             return jsonify({"success": False, "error": "No file provided"}), 400
#
#         file = request.files['file']
#         if file.filename == '':
#             return jsonify({"success": False, "error": "No file selected"}), 400
#
#         if not file.filename.lower().endswith('.csv'):
#             return jsonify({"success": False, "error": "File must be a CSV"}), 400
#
#         stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
#         csv_reader = csv.DictReader(stream)
#
#         new_leads, errors = [], []
#
#         for row_num, row in enumerate(csv_reader, start=2):
#             try:
#                 name, phone = row.get('name', '').strip(), row.get('phone', '').strip()
#                 if not name or not phone:
#                     errors.append(f"Row {row_num}: Missing name or phone")
#                     continue
#
#                 lead = {
#                     "id": str(uuid.uuid4()),
#                     "name": name,
#                     "phone": phone,
#                     "email": row.get('email', '').strip(),
#                     "company": row.get('company', '').strip(),
#                     "notes": row.get('notes', '').strip(),
#                     "status": "new",
#                     "call_attempts": 0,
#                     "last_call": None,
#                     "created_at": datetime.now().isoformat(),
#                     "updated_at": datetime.now().isoformat()
#                 }
#                 new_leads.append(lead)
#
#             except Exception as e:
#                 errors.append(f"Row {row_num}: {str(e)}")
#
#         if not new_leads:
#             return jsonify({"success": False, "error": "No valid leads found in CSV", "errors": errors}), 400
#
#         existing_leads = load_leads()
#         all_leads = existing_leads + new_leads
#
#         if save_leads(all_leads):
#             return jsonify({
#                 "success": True,
#                 "message": f"Successfully imported {len(new_leads)} leads",
#                 "imported_count": len(new_leads),
#                 "total_count": len(all_leads),
#                 "errors": errors
#             }), 200
#         else:
#             return jsonify({"success": False, "error": "Failed to save leads"}), 500
#
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)}), 500
#
#
# @app.route("/api/leads/<lead_id>/call", methods=["POST"])
# def call_lead(lead_id):
#     """Initiate a call to a specific lead using Piopiy"""
#     try:
#         leads = load_leads()
#         lead_index = next((i for i, lead in enumerate(leads) if lead["id"] == lead_id), None)
#
#         if lead_index is None:
#             return jsonify({"success": False, "error": "Lead not found"}), 404
#
#         lead = leads[lead_index]
#         print(f"📞 Initiating call to {lead['name']} at {lead['phone']}")
#         call_response = outbound_caller.make_call(lead["phone"])
#
#         if call_response:
#             if "error" in call_response:
#                 call_status, success = f"Call failed: {call_response['error']}", False
#             elif call_response.get("status") == "simulated":
#                 call_status, success = "Call simulated (Piopiy not configured)", True
#             else:
#                 call_status, success = "Call initiated successfully via Piopiy", True
#         else:
#             call_status, success = "Call failed - no response", False
#
#         if success:
#             leads[lead_index]["call_attempts"] += 1
#             leads[lead_index]["last_call"] = datetime.now().isoformat()
#             leads[lead_index]["status"] = "called"
#             leads[lead_index]["updated_at"] = datetime.now().isoformat()
#             save_leads(leads)
#
#         return jsonify({
#             "success": success,
#             "message": f"Call {'initiated' if success else 'failed'} to {lead['name']}",
#             "data": {
#                 "lead": leads[lead_index],
#                 "call_status": call_status,
#                 "phone_number": lead["phone"],
#                 "piopiy_response": call_response
#             }
#         }), 200 if success else 500
#
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)}), 500
#
#
# @app.route("/api/leads/stats", methods=["GET"])
# def get_leads_stats():
#     """Get leads statistics"""
#     try:
#         leads = load_leads()
#         stats = {
#             "total": len(leads),
#             "new": len([l for l in leads if l["status"] == "new"]),
#             "called": len([l for l in leads if l["status"] == "called"]),
#             "contacted": len([l for l in leads if l["status"] == "contacted"]),
#             "converted": len([l for l in leads if l["status"] == "converted"]),
#             "total_calls": sum(l["call_attempts"] for l in leads)
#         }
#         return jsonify({"success": True, "data": stats}), 200
#
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)}), 500
#
#
# @app.route("/api/health", methods=["GET"])
# def health_check():
#     """Health check endpoint"""
#     return jsonify({
#         "success": True,
#         "message": "Leads API is running",
#         "timestamp": datetime.now().isoformat()
#     }), 200
#
#
# if __name__ == "__main__":
#     print("🚀 Leads Management API running on port 5002")
#     app.run(host="0.0.0.0", port=5002, debug=False)
