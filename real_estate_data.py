"""
Real Estate Knowledge Base
Contains all the information about properties, developer, and services
"""

REAL_ESTATE_INFO = {
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

# Additional context for better responses
developer_name = REAL_ESTATE_INFO.get("developer", "Our Real Estate Developers")
RESPONSE_TEMPLATES = {
    "greeting": "Hello! I'm here to help you with information about {developer_name} projects. How can I assist you?",
    "unknown": "I can only provide information about {developer_name} projects. Please ask about our properties, amenities, or payment plans.",
    "goodbye": "Thank you for your interest in {developer_name}. Have a great day!"
}

