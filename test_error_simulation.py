#!/usr/bin/env python3
"""
Test script to simulate the error that occurs in the voice bot
"""

import asyncio
from qa_engine import RealEstateQA
from ai_services import AIServices

async def test_error_simulation():
    """Simulate the exact error path from websocket_server.py"""
    print("🧪 Testing error simulation...")
    
    try:
        # Initialize services (same as in websocket_server.py)
        ai_services = AIServices()
        bot = RealEstateQA(ai_services)
        history = []
        
        print("✅ Services initialized")
        
        # Test the get_response method (same as in websocket_server.py)
        user_text = "Hello, can you tell me about your projects?"
        print(f"🤖 Testing with user input: {user_text}")
        
        # This is the exact call that fails in websocket_server.py
        reply = await asyncio.get_event_loop().run_in_executor(
            None, 
            bot.get_response, 
            user_text, 
            history
        )
        
        print(f"✅ Response generated: {reply}")
        return True
        
    except Exception as e:
        print(f"❌ Error occurred: {e}")
        print(f"❌ Error type: {type(e).__name__}")
        print(f"❌ Full error details: {str(e)}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_error_simulation())
    if success:
        print("\n🎉 No errors detected!")
    else:
        print("\n💥 Error detected - this is what's happening in the voice bot!") 