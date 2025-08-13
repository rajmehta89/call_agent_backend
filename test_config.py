#!/usr/bin/env python3
"""
Test script for Agent Configuration System
Tests all configuration functionality
"""

import json
import requests
import time
from agent_config import AgentConfig

def test_agent_config_class():
    """Test the AgentConfig class functionality"""
    print("🧪 Testing AgentConfig class...")
    
    # Test initialization
    config = AgentConfig("test_config.json")
    
    # Test default values
    assert config.get_greeting_message(), "Greeting message should not be empty"
    assert config.get_exit_message(), "Exit message should not be empty"
    assert config.get_system_prompt(), "System prompt should not be empty"
    
    # Test updates
    test_greeting = "Test greeting message"
    assert config.set_greeting_message(test_greeting), "Should be able to set greeting"
    assert config.get_greeting_message() == test_greeting, "Greeting should be updated"
    
    test_exit = "Test exit message"
    assert config.set_exit_message(test_exit), "Should be able to set exit message"
    assert config.get_exit_message() == test_exit, "Exit message should be updated"
    
    test_prompt = "Test system prompt"
    assert config.set_system_prompt(test_prompt), "Should be able to set system prompt"
    assert config.get_system_prompt() == test_prompt, "System prompt should be updated"
    
    # Test reset
    assert config.reset_to_defaults(), "Should be able to reset to defaults"
    assert config.get_greeting_message() != test_greeting, "Should reset greeting"
    
    print("✅ AgentConfig class tests passed!")
    
    # Cleanup
    import os
    if os.path.exists("test_config.json"):
        os.remove("test_config.json")

def test_api_endpoints():
    """Test the API endpoints"""
    print("🧪 Testing API endpoints...")
    
    base_url = "http://localhost:5001"
    
    try:
        # Test health endpoint
        response = requests.get(f"{base_url}/api/health", timeout=5)
        if response.status_code == 200:
            print("✅ Health endpoint working")
        else:
            print(f"❌ Health endpoint failed: {response.status_code}")
            return False
        
        # Test get config
        response = requests.get(f"{base_url}/api/config", timeout=5)
        if response.status_code == 200:
            config_data = response.json()
            if config_data.get("success"):
                print("✅ Get config endpoint working")
                current_config = config_data["data"]
            else:
                print(f"❌ Get config failed: {config_data}")
                return False
        else:
            print(f"❌ Get config endpoint failed: {response.status_code}")
            return False
        
        # Test update config
        test_update = {
            "greeting_message": "Test API greeting",
            "exit_message": "Test API exit",
            "system_prompt": "Test API prompt"
        }
        
        response = requests.put(
            f"{base_url}/api/config",
            json=test_update,
            timeout=5
        )
        
        if response.status_code == 200:
            update_data = response.json()
            if update_data.get("success"):
                print("✅ Update config endpoint working")
            else:
                print(f"❌ Update config failed: {update_data}")
                return False
        else:
            print(f"❌ Update config endpoint failed: {response.status_code}")
            return False
        
        # Test individual endpoints
        individual_tests = [
            ("/api/config/greeting", {"message": "Test individual greeting"}),
            ("/api/config/exit", {"message": "Test individual exit"}),
            ("/api/config/prompt", {"prompt": "Test individual prompt"})
        ]
        
        for endpoint, data in individual_tests:
            response = requests.post(f"{base_url}{endpoint}", json=data, timeout=5)
            if response.status_code == 200:
                print(f"✅ {endpoint} working")
            else:
                print(f"❌ {endpoint} failed: {response.status_code}")
        
        # Test reset
        response = requests.post(f"{base_url}/api/config/reset", timeout=5)
        if response.status_code == 200:
            print("✅ Reset endpoint working")
        else:
            print(f"❌ Reset endpoint failed: {response.status_code}")
        
        print("✅ All API endpoint tests passed!")
        return True
        
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to API server. Make sure it's running on port 5001")
        return False
    except Exception as e:
        print(f"❌ API test error: {e}")
        return False

def test_qa_engine_integration():
    """Test QA engine integration"""
    print("🧪 Testing QA engine integration...")
    
    try:
        from qa_engine import RealEstateQA
        from ai_services import AIServices
        
        # Initialize services
        ai_services = AIServices()
        qa_engine = RealEstateQA(ai_services)
        
        # Test greeting and exit messages
        greeting = qa_engine.get_greeting_message()
        exit_msg = qa_engine.get_exit_message()
        
        assert greeting, "QA engine should return greeting message"
        assert exit_msg, "QA engine should return exit message"
        
        # Test system prompt building
        system_prompt = qa_engine.build_system_prompt()
        assert system_prompt, "QA engine should build system prompt"
        assert "real estate" in system_prompt.lower(), "System prompt should contain real estate context"
        
        print("✅ QA engine integration tests passed!")
        return True
        
    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("Make sure all dependencies are installed")
        return False
    except Exception as e:
        print(f"❌ QA engine test error: {e}")
        return False

def main():
    """Run all tests"""
    print("🎯 Starting Agent Configuration System Tests")
    print("=" * 50)
    
    tests = [
        test_agent_config_class,
        test_qa_engine_integration,
        test_api_endpoints,
    ]
    
    passed = 0
    total = len(tests)
    
    for test_func in tests:
        try:
            if test_func():
                passed += 1
            print()
        except Exception as e:
            print(f"❌ Test {test_func.__name__} failed with error: {e}")
            print()
    
    print("=" * 50)
    print(f"📊 Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! System is working correctly.")
        return True
    else:
        print("⚠️  Some tests failed. Check the output above for details.")
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1) 