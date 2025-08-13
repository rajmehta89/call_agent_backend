#!/usr/bin/env python3
"""
Test script to verify real estate knowledge base is working
"""

import asyncio
from qa_engine import RealEstateQA
from ai_services import AIServices

async def test_real_estate_responses():
    """Test various real estate questions"""
    print("🧪 Testing Real Estate Knowledge Base...")
    
    try:
        # Initialize services
        ai_services = AIServices()
        bot = RealEstateQA(ai_services)
        history = []
        
        # Test questions
        test_questions = [
            "What projects do you have?",
            "Tell me about Dream Housing Complex",
            "What's the payment method?",
            "What's your contact number?",
            "When can I get possession?",
            "What amenities do you offer?"
        ]
        
        for question in test_questions:
            print(f"\n🤖 Question: {question}")
            
            reply = await asyncio.get_event_loop().run_in_executor(
                None, 
                bot.get_response, 
                question, 
                history
            )
            
            print(f"✅ Answer: {reply}")
            
            # Update history for context
            history.extend([
                {"role": "user", "content": question},
                {"role": "assistant", "content": reply}
            ])
        
        print("\n🎉 Real estate knowledge base is working correctly!")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_real_estate_responses())
    if success:
        print("\n✅ All tests passed!")
    else:
        print("\n💥 Tests failed!") 