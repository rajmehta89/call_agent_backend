#!/usr/bin/env python3
"""
Test script to check Groq API configuration
"""

import os
from dotenv import load_dotenv
from groq import Groq

def test_groq_config():
    """Test Groq API configuration"""
    print("🧪 Testing Groq API Configuration...")
    
    # Load environment variables
    load_dotenv()
    
    # Check if .env file exists
    if not os.path.exists('.env'):
        print("❌ No .env file found!")
        print("   Please create a .env file with your GROQ_API_KEY")
        return False
    
    # Get API key
    api_key = os.getenv("GROQ_API_KEY")
    
    if not api_key:
        print("❌ GROQ_API_KEY not found in environment variables")
        return False
    
    if api_key == "your_groq_api_key_here":
        print("❌ GROQ_API_KEY is still using placeholder value")
        print("   Please replace 'your_groq_api_key_here' with your actual Groq API key")
        return False
    
    print(f"✅ GROQ_API_KEY found (length: {len(api_key)})")
    print(f"✅ API Key starts with: {api_key[:10]}...")
    
    # Test Groq client initialization
    try:
        client = Groq(api_key=api_key)
        print("✅ Groq client initialized successfully")
    except Exception as e:
        print(f"❌ Failed to initialize Groq client: {e}")
        return False
    
    # Test a simple API call
    try:
        print("🤖 Testing API call...")
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": "Hello, this is a test"}],
            model="llama-3.3-70b-versatile",
            max_tokens=10,
            temperature=0.1
        )
        
        if response.choices and response.choices[0].message.content:
            print("✅ API call successful!")
            print(f"✅ Response: {response.choices[0].message.content}")
            return True
        else:
            print("❌ API call returned empty response")
            return False
            
    except Exception as e:
        print(f"❌ API call failed: {e}")
        print(f"❌ Error type: {type(e).__name__}")
        
        # Check for specific error types
        if "authentication" in str(e).lower() or "api_key" in str(e).lower():
            print("❌ This looks like an authentication/API key issue")
        elif "quota" in str(e).lower() or "limit" in str(e).lower():
            print("❌ This looks like a quota/rate limit issue")
        elif "model" in str(e).lower():
            print("❌ This looks like a model availability issue")
        elif "network" in str(e).lower() or "connection" in str(e).lower():
            print("❌ This looks like a network connectivity issue")
        
        return False

if __name__ == "__main__":
    success = test_groq_config()
    if success:
        print("\n🎉 Groq API is working correctly!")
    else:
        print("\n💥 Groq API configuration needs to be fixed!")
        print("\nTo fix this:")
        print("1. Get your Groq API key from https://console.groq.com/")
        print("2. Add it to your .env file: GROQ_API_KEY=your_actual_key_here")
        print("3. Run this test again") 