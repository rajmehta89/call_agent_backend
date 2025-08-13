#!/usr/bin/env python3
"""
Quick test to verify real estate agent is working
"""

import asyncio
from qa_engine import RealEstateQA
from ai_services import AIServices

async def quick_test():
    """Quick test of real estate agent"""
    print("🧪 Quick Real Estate Agent Test...")
    
    try:
        ai_services = AIServices()
        bot = RealEstateQA(ai_services)
        
        # Test a simple question
        question = "What projects do you have?"
        print(f"🤖 Question: {question}")
        
        reply = await asyncio.get_event_loop().run_in_executor(
            None, 
            bot.get_response, 
            question, 
            []
        )
        
        print(f"✅ Answer: {reply}")
        
        # Test another question
        question2 = "Tell me about Dream Housing Complex"
        print(f"\n🤖 Question: {question2}")
        
        reply2 = await asyncio.get_event_loop().run_in_executor(
            None, 
            bot.get_response, 
            question2, 
            []
        )
        
        print(f"✅ Answer: {reply2}")
        
        print("\n🎉 Real estate agent is working correctly!")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(quick_test())
    if success:
        print("\n✅ Test passed!")
    else:
        print("\n💥 Test failed!") 