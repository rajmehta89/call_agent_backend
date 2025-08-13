#!/usr/bin/env python3
"""
Test MongoDB Atlas connection with detailed error handling
"""

import os
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError, OperationFailure

load_dotenv()

def test_atlas_connection():
    """Test MongoDB Atlas connection with detailed error messages"""
    print("🧪 Testing MongoDB Atlas Connection...")
    
    # Get connection string
    mongo_uri = os.getenv("MONGO_URI")
    database_name = os.getenv("MONGO_DB", "ai_agent_assist")
    
    if not mongo_uri:
        print("❌ MONGO_URI not found in environment variables")
        print("Please add your MongoDB Atlas connection string to .env file")
        return False
    
    print(f"🔗 Connection string: {mongo_uri[:50]}...")
    print(f"📊 Database name: {database_name}")
    
    try:
        # Test connection with timeout
        print("🔄 Attempting to connect...")
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        
        # Test if we can reach the server
        client.admin.command('ping')
        print("✅ Successfully connected to MongoDB Atlas!")
        
        # Test database access
        db = client[database_name]
        
        # Test collections
        print("📝 Testing collections...")
        
        # Test leads collection
        leads_count = db.leads.count_documents({})
        print(f"✅ Leads collection accessible: {leads_count} documents")
        
        # Test calls collection
        calls_count = db.calls.count_documents({})
        print(f"✅ Calls collection accessible: {calls_count} documents")
        
        # Test inserting a document
        test_doc = {"test": "connection", "timestamp": "2024-01-01"}
        result = db.leads.insert_one(test_doc)
        print(f"✅ Write test successful: {result.inserted_id}")
        
        # Clean up test document
        db.leads.delete_one({"_id": result.inserted_id})
        print("🧹 Test document cleaned up")
        
        client.close()
        print("\n🎉 MongoDB Atlas connection test passed!")
        return True
        
    except ConnectionFailure as e:
        print(f"❌ Connection failed: {e}")
        print("💡 Possible solutions:")
        print("   1. Check your internet connection")
        print("   2. Verify the connection string is correct")
        print("   3. Make sure your IP is whitelisted in MongoDB Atlas")
        return False
        
    except ServerSelectionTimeoutError as e:
        print(f"❌ Server selection timeout: {e}")
        print("💡 Possible solutions:")
        print("   1. Check your internet connection")
        print("   2. Verify the cluster is running in MongoDB Atlas")
        print("   3. Check if the connection string is correct")
        return False
        
    except OperationFailure as e:
        print(f"❌ Authentication failed: {e}")
        print("💡 Possible solutions:")
        print("   1. Replace <db_password> with your actual password in .env")
        print("   2. Check if username and password are correct")
        print("   3. Verify the database user has proper permissions")
        return False
        
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        print("💡 Please check your MongoDB Atlas configuration")
        return False

if __name__ == "__main__":
    success = test_atlas_connection()
    if success:
        print("\n✅ MongoDB Atlas is ready for use!")
    else:
        print("\n💥 MongoDB Atlas setup needs attention!")
        print("\n📋 Checklist:")
        print("   ✅ MongoDB Atlas cluster is running")
        print("   ✅ Connection string is correct")
        print("   ✅ Username and password are correct")
        print("   ✅ IP address is whitelisted")
        print("   ✅ Database user has proper permissions") 