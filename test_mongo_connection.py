#!/usr/bin/env python3
"""
Test MongoDB connection and basic operations
"""

from mongo_client import mongo_client
from datetime import datetime

def test_mongo_connection():
    """Test MongoDB connection and basic operations"""
    print("🧪 Testing MongoDB Connection...")
    
    try:
        # Test connection
        if not mongo_client.is_connected():
            print("❌ MongoDB connection failed")
            return False
        
        print("✅ MongoDB connected successfully")
        
        # Test database stats
        stats = mongo_client.get_database_stats()
        print(f"📊 Database stats: {stats}")
        
        # Test leads collection
        print("\n📝 Testing leads collection...")
        
        # Insert a test lead
        test_lead = {
            "name": "Test Lead",
            "phone": "+1234567890",
            "email": "test@example.com",
            "company": "Test Company",
            "notes": "Test lead for MongoDB",
            "status": "new",
            "call_attempts": 0,
            "last_call": None,
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        
        result = mongo_client.leads.insert_one(test_lead)
        print(f"✅ Test lead inserted with ID: {result.inserted_id}")
        
        # Find the test lead
        found_lead = mongo_client.leads.find_one({"phone": "+1234567890"})
        if found_lead:
            print(f"✅ Test lead found: {found_lead['name']}")
        else:
            print("❌ Test lead not found")
        
        # Test calls collection
        print("\n📞 Testing calls collection...")
        
        # Insert a test call
        test_call = {
            "phone_number": "+1234567890",
            "lead_id": str(result.inserted_id),
            "call_date": datetime.now(),
            "status": "completed",
            "duration": 120,
            "transcription": [
                {
                    "type": "user",
                    "content": "Hello, I'm interested in your services",
                    "timestamp": datetime.now().isoformat()
                }
            ],
            "ai_responses": [
                {
                    "type": "bot",
                    "content": "Thank you for your interest! How can I help you?",
                    "timestamp": datetime.now().isoformat()
                }
            ],
            "call_summary": "Test call with user inquiry",
            "sentiment": "positive",
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        
        call_result = mongo_client.calls.insert_one(test_call)
        print(f"✅ Test call inserted with ID: {call_result.inserted_id}")
        
        # Find the test call
        found_call = mongo_client.calls.find_one({"phone_number": "+1234567890"})
        if found_call:
            print(f"✅ Test call found: {found_call['call_summary']}")
        else:
            print("❌ Test call not found")
        
        # Clean up test data
        mongo_client.leads.delete_one({"_id": result.inserted_id})
        mongo_client.calls.delete_one({"_id": call_result.inserted_id})
        print("🧹 Test data cleaned up")
        
        print("\n🎉 All MongoDB tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ MongoDB test failed: {e}")
        return False

if __name__ == "__main__":
    success = test_mongo_connection()
    if success:
        print("\n✅ MongoDB is ready for use!")
    else:
        print("\n💥 MongoDB setup needs attention!") 