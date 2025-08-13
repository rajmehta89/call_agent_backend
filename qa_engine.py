from agent_config import agent_config
from typing import Any, Dict, List
import json

class DynamicQA:
    def __init__(self, ai_services):
        self.ai_services = ai_services
        self.agent_config = agent_config

    def get_knowledge_base(self) -> Dict[str, Any]:
        """Get knowledge base - user-defined if enabled, otherwise empty"""
        # Check if user has enabled custom knowledge base
        if self.agent_config.get_knowledge_base_enabled():
            user_kb = self.agent_config.get_knowledge_base()
            if user_kb:  # Only use if not empty
                return user_kb
        
        # If knowledge base is disabled or empty, return empty dict
        # This means the agent will only use the system prompt without additional knowledge
        return {}

    def _get_real_estate_kb(self) -> Dict[str, Any]:
        """Real estate knowledge base"""
        return {
            "developer": "Vansh Real Estate Developers",
            "about": (
                "Vansh Real Estate Developers is a trusted Mumbai-based company with over 15 years of experience, "
                "offering both luxury and affordable housing solutions across the city. We pride ourselves on transparency, timely delivery, and customer satisfaction."
            ),
            "contact_details": {
                "phone": "+91-90000-55555",
                "email": "contact@vanshdevelopers.com",
                "website": "www.vanshdevelopers.com",
                "address": "101, Vansh Tower, Bandra East, Mumbai 400051"
            },
            "projects": {
                "Dream Housing Complex": {
                    "location": "Kurla",
                    "units": ["2BHK", "3BHK"],
                    "possession": "October 2025",
                    "amenities": ["Swimming Pool", "Playground", "Jogging Track", "24x7 Security"],
                    "status": "Under Construction",
                    "offer": "Free Club Membership for bookings before August 2025"
                },
                "MyHome Complex": {
                    "location": "Andheri",
                    "units": ["1BHK", "2BHK"],
                    "possession": "March 2025",
                    "amenities": ["Clubhouse", "Gymnasium", "Power Backup"],
                    "status": "Ready to Move"
                },
                "Luxury Living": {
                    "location": "Bandra",
                    "units": ["3BHK", "4BHK Penthouse"],
                    "possession": "December 2026",
                    "amenities": ["Infinity Pool", "Sky Deck", "Kids' Play Area", "Banquet Hall"],
                    "status": "Pre-Launch"
                }
            },
            "general_amenities": ["Playground", "Clubhouse", "Jogging Track"],
            "possession": "Earliest in 6 months (for MyHome Complex)",
            "payment_method": "20:20:60 (20% on booking, 20% during construction, 60% on possession)",
            "financing_options": [
                "Home loan tie-ups with HDFC, SBI, ICICI",
                "EMI holiday up to possession phase"
            ],
            "site_visit": "Site visits available Monday to Saturday on prior appointment.",
            "customer_service_hours": "Monday to Friday, 10am-6pm",
            "rera_id": "MAHARERA-P123456789"
        }

    def _get_customer_service_kb(self) -> Dict[str, Any]:
        """Customer service knowledge base"""
        return {
            "company": "Your Company Name",
            "about": "We are committed to providing excellent customer service and support.",
            "contact_details": {
                "phone": "+1-800-CUSTOMER",
                "email": "support@yourcompany.com",
                "website": "www.yourcompany.com"
            },
            "support_hours": "Monday to Friday, 9am-6pm EST",
            "common_issues": [
                "Account access problems",
                "Billing inquiries",
                "Technical support",
                "Product information"
            ],
            "escalation_process": "If I cannot resolve your issue, I will escalate to a human agent.",
            "service_commitment": "We aim to resolve all issues within 24 hours."
        }

    def _get_sales_kb(self) -> Dict[str, Any]:
        """Sales knowledge base"""
        return {
            "company": "Your Company Name",
            "about": "We offer high-quality products and services to meet your needs.",
            "contact_details": {
                "phone": "+1-800-SALES",
                "email": "sales@yourcompany.com",
                "website": "www.yourcompany.com"
            },
            "products": [
                "Product A - Premium solution",
                "Product B - Standard solution", 
                "Product C - Basic solution"
            ],
            "pricing": "Contact us for custom pricing based on your needs",
            "benefits": [
                "24/7 support",
                "30-day money-back guarantee",
                "Free consultation"
            ],
            "next_steps": "I can help you understand our products and guide you toward the best solution."
        }

    def _get_appointment_kb(self) -> Dict[str, Any]:
        """Appointment scheduling knowledge base"""
        return {
            "company": "Your Company Name",
            "about": "We provide professional services with flexible scheduling options.",
            "contact_details": {
                "phone": "+1-800-APPOINT",
                "email": "appointments@yourcompany.com",
                "website": "www.yourcompany.com"
            },
            "available_times": [
                "Monday-Friday: 9am-5pm",
                "Saturday: 10am-2pm",
                "Sunday: Closed"
            ],
            "services": [
                "Consultation (30 min)",
                "Full Service (1 hour)",
                "Follow-up (15 min)"
            ],
            "cancellation_policy": "24-hour notice required for cancellations",
            "confirmation": "All appointments will be confirmed via email and SMS"
        }

    def _get_generic_kb(self) -> Dict[str, Any]:
        """Generic knowledge base for custom prompts"""
        return {
            "company": "Your Company",
            "about": "We provide professional services to meet your needs.",
            "contact_details": {
                "phone": "+1-800-COMPANY",
                "email": "info@yourcompany.com",
                "website": "www.yourcompany.com"
            },
            "services": "Please ask about our specific services and I'll be happy to help."
        }

    def build_system_prompt(self) -> str:
        """Build system prompt with dynamic knowledge base"""
        # Get dynamic system prompt from configuration
        base_prompt = self.agent_config.get_system_prompt()
        
        # Add concise response instruction
        concise_instruction = "\n\nIMPORTANT: Keep your responses SHORT and CONCISE. Aim for 1-2 sentences maximum. Be direct and to the point. Avoid lengthy explanations unless specifically asked for more details."
        
        # Get appropriate knowledge base
        kb = self.get_knowledge_base()
        
        # If no knowledge base, just return the base prompt with concise instruction
        if not kb:
            return base_prompt + concise_instruction
        
        # Build the complete prompt with knowledge base
        prompt = f"{base_prompt}{concise_instruction}\n\nHere is what you know:\n"
        for section, value in kb.items():
            prompt += f"\n{section.replace('_', ' ').capitalize()}:\n"
            if isinstance(value, dict):
                for k, v in value.items():
                    prompt += f" - {k}: {v}\n"
            elif isinstance(value, list):
                prompt += " - " + ", ".join(str(i) for i in value) + "\n"
            else:
                prompt += f" - {value}\n"
        
        return prompt

    def get_response(self, user_input: str, conversation_history=None) -> str:
        """Calls LLM with up-to-date context. Returns the assistant's reply."""
        try:
            # Check if Groq client is properly initialized
            if not hasattr(self.ai_services, 'groq_client') or self.ai_services.groq_client is None:
                print("❌ Groq client is not initialized")
                return "I'm sorry, the AI service is not properly configured."
            
            # Check if API key is set
            if not self.ai_services.config.GROQ_API_KEY or self.ai_services.config.GROQ_API_KEY == "your_groq_api_key_here":
                print("❌ GROQ_API_KEY is not set or is using placeholder value")
                return "I'm sorry, the AI service API key is not configured."
            
            system_prompt = self.build_system_prompt()
            print(f"System Prompt: {system_prompt[:200]}...")  # Print first 200 chars for debugging
            if conversation_history is None:
                conversation_history = []
            # optional: keep recent history if wanted
            messages = [
                {'role': 'system', 'content': system_prompt},
                *conversation_history[-4:],  # last exchanges if you want context
                {'role': 'user', 'content': user_input}
            ]
            
            print(f"🤖 Attempting to call Groq API...")
            print(f"🤖 API Key present: {'Yes' if self.ai_services.config.GROQ_API_KEY else 'No'}")
            print(f"🤖 API Key length: {len(self.ai_services.config.GROQ_API_KEY) if self.ai_services.config.GROQ_API_KEY else 0}")
            
            # You may need to change `.chat.completions.create` call to match your ai_services object
            response = self.ai_services.groq_client.chat.completions.create(
                messages=messages,
                model="llama-3.3-70b-versatile",
                max_tokens=80,  # Reduced for shorter responses
                temperature=0.7,
                top_p=0.9,
                stream=False
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"❌ LLM error: {e}")
            print(f"❌ Error type: {type(e).__name__}")
            print(f"❌ Full error details: {str(e)}")
            return self._dynamic_fallback(user_input)

    def _dynamic_fallback(self, user_input: str) -> str:
        """Dynamic fallback based on current knowledge base"""
        kb = self.get_knowledge_base()
        if not kb:
            return "I'm here to help! Please ask me any questions."
        available = ", ".join([k.replace('_', ' ') for k in kb.keys()])
        return f"I can help you with: {available}. What would you like to know?"

    def is_exit_intent(self, user_input: str) -> bool:
        """Check if user wants to exit"""
        user_input = user_input.lower()
        exit_keywords = [
            "bye", "goodbye", "see you", "exit", "quit", 
            "stop", "end", "finish", "done", "thank you",
            "thanks", "that's all", "no more", "nothing else",
            "i'm done", "gotta go", "have to go", "talk later"
        ]
        return any(keyword in user_input for keyword in exit_keywords)

    def should_transfer_to_agent(self, user_input: str) -> bool:
        """Check if user wants to talk to an agent"""
        user_input = user_input.lower()
        
        transfer_keywords = [
            "agent", "human", "person", "representative", 
            "talk to someone", "speak to agent", "connect agent",
            "real person", "customer service", "help me",
            "not satisfied", "complaint", "issue"
        ]
        
        return any(keyword in user_input for keyword in transfer_keywords)
    
    def get_greeting_message(self) -> str:
        """Get dynamic greeting message"""
        return self.agent_config.get_greeting_message()
    
    def get_exit_message(self) -> str:
        """Get dynamic exit message"""
        return self.agent_config.get_exit_message()

    def analyze_conversation_interest(self, transcription: List[Dict], ai_responses: List[Dict]) -> Dict[str, Any]:
        """
        Analyze the entire conversation to determine user interest level using LLM
        
        Args:
            transcription: List of user messages with timestamps
            ai_responses: List of AI responses with timestamps
            
        Returns:
            Dict with:
            - interest_status: "interested" | "not_interested" | "neutral"
            - confidence: float (0.0-1.0)
            - reasoning: str explaining the decision
            - key_indicators: List[str] of specific phrases/behaviors
        """
        try:
            # Check if LLM service is available
            if not hasattr(self.ai_services, 'groq_client') or self.ai_services.groq_client is None:
                print("❌ Groq client not available for interest analysis")
                return self._fallback_interest_analysis(transcription, ai_responses)
            
            # Check if API key is set
            if not self.ai_services.config.GROQ_API_KEY or self.ai_services.config.GROQ_API_KEY == "your_groq_api_key_here":
                print("❌ GROQ_API_KEY not configured for interest analysis")
                return self._fallback_interest_analysis(transcription, ai_responses)
            
            # Build conversation text for analysis
            conversation_text = self._format_conversation_for_analysis(transcription, ai_responses)
            
            # If conversation is too short, return neutral
            if len(transcription) < 2:
                return {
                    "interest_status": "neutral",
                    "confidence": 0.5,
                    "reasoning": "Conversation too short to determine interest level",
                    "key_indicators": []
                }
            
            # Create analysis prompt
            analysis_prompt = self._build_interest_analysis_prompt(conversation_text)
            
            # Call LLM for analysis
            messages = [
                {'role': 'system', 'content': analysis_prompt},
                {'role': 'user', 'content': f"Please analyze this conversation:\n\n{conversation_text}"}
            ]
            
            print("🔍 Analyzing conversation interest with LLM...")
            
            response = self.ai_services.groq_client.chat.completions.create(
                messages=messages,
                model="llama-3.3-70b-versatile",
                max_tokens=300,
                temperature=0.3,  # Lower temperature for more consistent analysis
                top_p=0.9,
                stream=False
            )
            
            # Parse LLM response
            analysis_result = self._parse_interest_analysis_response(response.choices[0].message.content.strip())
            
            print(f"🎯 Interest Analysis Result: {analysis_result['interest_status']} ({analysis_result['confidence']:.2f} confidence)")
            print(f"📝 Reasoning: {analysis_result['reasoning']}")
            
            return analysis_result
            
        except Exception as e:
            print(f"❌ Error in interest analysis: {e}")
            return self._fallback_interest_analysis(transcription, ai_responses)

    def _format_conversation_for_analysis(self, transcription: List[Dict], ai_responses: List[Dict]) -> str:
        """Format conversation into readable text for LLM analysis"""
        conversation = []
        
        # Combine and sort all messages by timestamp
        all_messages = []
        
        for msg in transcription:
            all_messages.append({
                "timestamp": msg.get("timestamp", ""),
                "speaker": "User",
                "content": msg.get("content", "")
            })
        
        for msg in ai_responses:
            all_messages.append({
                "timestamp": msg.get("timestamp", ""),
                "speaker": "Bot",
                "content": msg.get("content", "")
            })
        
        # Sort by timestamp if available
        try:
            all_messages.sort(key=lambda x: x["timestamp"])
        except:
            pass  # If timestamp sorting fails, keep original order
        
        # Format conversation
        formatted_lines = []
        for msg in all_messages:
            if msg["content"].strip():
                formatted_lines.append(f"{msg['speaker']}: {msg['content'].strip()}")
        
        return "\n".join(formatted_lines)

    def _build_interest_analysis_prompt(self, conversation_text: str) -> str:
        """Build the prompt for interest analysis"""
        return """You are an expert conversation analyst specializing in real estate sales. 
Your task is to analyze conversations between potential customers and real estate agents to determine the customer's level of interest.

Please analyze the conversation and provide your assessment in the following JSON format:
{
    "interest_status": "interested" | "not_interested" | "neutral",
    "confidence": 0.85,
    "reasoning": "Brief explanation of why you chose this status",
    "key_indicators": ["specific phrase 1", "specific phrase 2", "behavior 1"]
}

Interest Level Guidelines:
- INTERESTED: Customer asks specific questions about properties, pricing, amenities, site visits, payment plans, possession dates, or shows clear intent to purchase/visit
- NOT_INTERESTED: Customer explicitly says no, shows disinterest, asks to be removed from calls, or gives clear negative responses
- NEUTRAL: Customer engages but doesn't show clear positive or negative intent, asks general questions only, or conversation is too brief

Key indicators to look for:
POSITIVE: Questions about price, unit sizes, amenities, possession dates, payment plans, site visits, location details, RERA info, documentation, loan assistance
NEGATIVE: "Not interested", "Don't call again", "Not looking to buy", "Can't afford", explicit refusal
NEUTRAL: Basic acknowledgments, general questions, no clear commitment either way

Focus on:
1. Quality and specificity of questions asked
2. Requests for additional information or next steps  
3. Engagement level and conversation length
4. Explicit positive or negative statements
5. Intent to take action (visit, call back, etc.)

Be conservative - only mark as "interested" if there are clear positive indicators. When in doubt, choose "neutral"."""

    def _parse_interest_analysis_response(self, llm_response: str) -> Dict[str, Any]:
        """Parse the LLM response into structured data"""
        try:
            # Try to extract JSON from the response
            start_idx = llm_response.find('{')
            end_idx = llm_response.rfind('}') + 1
            
            if start_idx != -1 and end_idx != -1:
                json_str = llm_response[start_idx:end_idx]
                parsed = json.loads(json_str)
                
                # Validate required fields
                if all(key in parsed for key in ["interest_status", "confidence", "reasoning", "key_indicators"]):
                    # Ensure confidence is between 0 and 1
                    confidence = max(0.0, min(1.0, float(parsed["confidence"])))
                    
                    # Validate interest_status
                    valid_statuses = ["interested", "not_interested", "neutral"]
                    status = parsed["interest_status"].lower()
                    if status not in valid_statuses:
                        status = "neutral"
                    
                    return {
                        "interest_status": status,
                        "confidence": confidence,
                        "reasoning": str(parsed["reasoning"])[:500],  # Limit reasoning length
                        "key_indicators": parsed["key_indicators"][:10] if isinstance(parsed["key_indicators"], list) else []
                    }
        except Exception as e:
            print(f"⚠️ Failed to parse LLM interest analysis response: {e}")
        
        # Fallback parsing for non-JSON responses
        return self._fallback_parse_response(llm_response)

    def _fallback_parse_response(self, llm_response: str) -> Dict[str, Any]:
        """Fallback parser for when JSON parsing fails"""
        response_lower = llm_response.lower()
        
        # Simple keyword-based analysis as fallback
        if any(word in response_lower for word in ["interested", "positive", "wants to", "asking about"]):
            status = "interested"
            confidence = 0.6
        elif any(word in response_lower for word in ["not interested", "negative", "refused", "declined"]):
            status = "not_interested"
            confidence = 0.6
        else:
            status = "neutral"
            confidence = 0.5
        
        return {
            "interest_status": status,
            "confidence": confidence,
            "reasoning": "Fallback analysis - LLM response could not be parsed properly",
            "key_indicators": []
        }

    def _fallback_interest_analysis(self, transcription: List[Dict], ai_responses: List[Dict]) -> Dict[str, Any]:
        """Fallback interest analysis when LLM is not available"""
        # Simple rule-based fallback
        user_messages = [msg.get("content", "").lower() for msg in transcription]
        combined_text = " ".join(user_messages)
        
        # Positive indicators
        positive_keywords = [
            "price", "cost", "visit", "site", "when", "how much", "payment", 
            "loan", "emi", "possession", "ready", "interested", "yes", "ok", 
            "sure", "tell me", "what about", "can i", "booking"
        ]
        
        # Negative indicators
        negative_keywords = [
            "not interested", "no", "don't call", "remove", "stop", "never",
            "can't afford", "not looking", "not buying", "not now"
        ]
        
        positive_score = sum(1 for keyword in positive_keywords if keyword in combined_text)
        negative_score = sum(1 for keyword in negative_keywords if keyword in combined_text)
        
        if positive_score > negative_score and positive_score >= 2:
            return {
                "interest_status": "interested",
                "confidence": 0.7,
                "reasoning": f"Fallback analysis detected {positive_score} positive indicators",
                "key_indicators": [kw for kw in positive_keywords if kw in combined_text][:5]
            }
        elif negative_score > positive_score and negative_score >= 1:
            return {
                "interest_status": "not_interested", 
                "confidence": 0.7,
                "reasoning": f"Fallback analysis detected {negative_score} negative indicators",
                "key_indicators": [kw for kw in negative_keywords if kw in combined_text][:5]
            }
        else:
            return {
                "interest_status": "neutral",
                "confidence": 0.5,
                "reasoning": "Fallback analysis - no clear interest indicators detected",
                "key_indicators": []
            }

# Keep the old class name for backward compatibility
RealEstateQA = DynamicQA