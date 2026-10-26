"""The decisions made about each call turn, by meaning (see meaning.py), replacing the keyword lists:

- CALLER_TURN: what the caller just did (answered yes/no, said goodbye, gave a detail, asked a question, looked for
  something, raised a problem or a complaint, asked to be called back, asked us to explain, or told us something).
  Decides which model answers (voice_turns.choose_voice_model) and which short acknowledgement fits (bridge_kind).
- AGENT_ASKED: what our last reply asked the caller for (their name, phone number, email, a time, an order number,
  or something else), so the detail they give next is understood as that detail.
- PLACE_QUESTION: whether the caller asks if something covers a place ("can you get it out to Geelong?").
- DIMENSION: which single measurement a caller asks for ("how far across is it?" -> width).
"""

from meaning import Classifier

CALLER_TURN = Classifier("caller turn", {
    "acknowledge": ["Yes.", "Yeah, okay.", "Sure.", "No.", "No, that's fine.", "Okay, cool.", "Alright.", "That's correct.", "Yep, sounds good.",
                    "Mm-hmm.", "Right.", "Perfect.", "Yes, please.", "No worries.", "Go ahead."],
    "closing": ["Thanks, bye.", "That's all, thank you.", "Okay, thanks so much, goodbye.", "Cheers, see you.", "Nothing else, thanks.",
                "Thank you for your help.", "Have a good day.", "Bye now."],
    "detail": ["It's John Smith.", "My name is Priya.", "My number is 0412 345 678.", "It's zero four one two, three four five.",
               "My email is john at gmail dot com.", "J O H N at gmail.", "Tomorrow at 3pm works.", "Friday morning is best.",
               "I'm in Brunswick.", "Postcode 3056.", "My order number is DFO001114.", "It's 42 Smith Street.", "Raj.", "Mehta, M E H T A."],
    "question": ["What are your opening hours?", "How much is delivery?", "Do you have it in stock?", "What does that cost?",
                 "Where are you located?", "Is it available in black?", "How long does shipping take?", "What's the warranty?",
                 "Can I pick it up?", "Do you sell tiles?", "What is your company?", "How does it work?",
                 "What finishes does it come in?", "What colours are available?", "What styles do you have?", "Which brands do you carry?",
                 "So what is your business about?", "What do you guys do?", "What is your company for?",
                 # Asking for the business's own details or coverage is a question, not a request for someone to act.
                 "What's your phone number?", "What's your email address?", "Do you deliver to my area?",
                 "Do you deliver out to my town?", "Can you deliver to my suburb?"],
    "search": ["I'm looking for a basin for a small bathroom.", "What would you recommend for a family bathroom?", "I need something for my kitchen.",
               "Which tap would suit a vessel basin?", "I want a modern freestanding bath.", "What options do you have for a powder room?",
               "Something that goes with matte black fittings.", "I'm after a new vanity."],
    "compound": ["I need a vanity and a basin and also some tiles for the floor and the walls.",
                 "Please explain the prices, compare the sizes and tell me about delivery.",
                 "I have several questions about the finish, fitting and stock."],
    "problem": ["I'm not sure which one to choose.", "I'm having trouble with the installation.", "It's not working properly.",
                "I can't work out how to fit it.", "I'm confused about the sizes.", "The website isn't letting me check out."],
    "sensitive": ["It arrived broken.", "The basin is cracked.", "It's leaking everywhere.", "I want a refund, this is ridiculous.",
                  "I'm really unhappy with this.", "My order never arrived.", "This is urgent, water is flooding my bathroom.",
                  "You sent me the wrong item.", "I want to make a complaint."],
    "request": ["Can someone call me back?", "I'd like a quote.", "Can I speak to a person?", "Please get the team to contact me.",
                "Can you book a time for me?", "I'd like to arrange a delivery.", "Can I talk to someone about a big order?",
                "Could the team ring me about a product?", "Can someone call me about the vanity unit?", "I'd like a quote for a product.",
                "Can I get a callback about some tiles?", "Have someone phone me about the bath please."],
    "clarify": ["What do you mean?", "Sorry, can you say that again?", "I didn't catch that.", "Can you repeat that?",
                "What does that mean?", "Pardon?", "I don't understand.", "Come again?"],
    "statement": ["I'm renovating my bathroom.", "We just bought a new house.", "I already have the tiles.", "I'm a builder.",
                  "The room is quite small.", "We're doing the kitchen next month.", "My partner likes black fittings.", "I saw it on your website."],
})

AGENT_ASKED = Classifier("agent asked", {
    "name": ["May I have your name?", "What's your name, please?", "Who am I speaking with?", "Could I get your full name?", "Can you spell your surname?"],
    "phone": ["What's the best number to reach you on?", "Can I get your phone number?", "Could you confirm your mobile number?",
              "What number should the team call?", "Can you say the number again slowly?"],
    "email": ["What's your email address?", "Could you spell the email for me?", "What email should we send it to?", "What email should the team use?"],
    # The email on an order verifies the caller for that order: it is looked up in the store (main model).
    "order_email": ["What's the email on the order?", "What email did you use when you ordered?", "Which email is the order under?",
                    "Can you confirm the email address used for the purchase?"],
    "time": ["What day suits you?", "What time works best?", "When would you like the callback?", "Is morning or afternoon better?"],
    "order": ["What's your order number?", "Do you have the order number handy?", "Which order is this about?"],
    "address": ["What's your suburb?", "What's the delivery postcode?", "What's the address for delivery?"],
    "other": ["Are you looking for a particular style?", "Would you like me to have the team call you?", "We have basins from $40 to $145.",
              "Is there anything else I can help with?", "Does that help?", "Would you like details on the colours?", "Thanks for calling.",
              "It comes in 1500 and 1700 mm sizes.", "Hello, how can I help you today?"],
}, min_score=0.6)

# Place names are replaced by "that town" (here and in what the caller said) so the decision is about what is asked,
# not about which place is named.
PLACE_QUESTION = Classifier("place question", {
    "place": ["Do you deliver to that town?", "Can you ship to that town?", "Do you install in that town?", "Is there a store in that town?", "Do you have a showroom in that town?",
              "Can you get it out to that town?", "Do you service that town?", "Can I pick up from somewhere near that town?",
              "Do you cover that town?", "Will it reach me in that town?"],
    "other": ["How much is delivery?", "How long does shipping take?", "Do you have it in stock?", "What's your phone number?",
              "Where is your store?", "I'm calling from India.", "It's an Indian number.", "My name is Raj.", "Can someone call me back?",
              "What sizes does it come in?", "I live in that town and I like your products.", "We're based in that town and renovating.",
              "I'm in that town and need a new vanity.", "I live in that town and I want a new bath.", "My sister in that town recommended you."],
}, min_score=0.6)

DIMENSION = Classifier("dimension", {
    "width": ["How wide is it?", "What's the width?", "How far across is it?", "How wide is the bath?", "Is it wide enough for my space?",
              "What's it measure side to side?"],
    "depth": ["How deep is it?", "What's the depth?", "How far does it stick out from the wall?", "How deep is the basin bowl?",
              "How far is it from the front to the back?", "How much does it project out?"],
    "height": ["How tall is it?", "What's the height?", "How high does it stand?", "How high is the vanity?", "How tall is the mixer?",
               "How far is it from the floor to the top?"],
    "length": ["How long is it?", "What's the length?", "How long is the bathtub end to end?", "What's the length of the bath?",
               "How long is the tub?"],
    "none": ["What sizes does it come in?", "What are the dimensions?", "What are the measurements?", "How much is it?",
             "Does it come in black?", "Is it in stock?", "How big is it overall?", "How big is it?", "How large is it?",
             "What size is it?", "Is it a big one?"],
}, min_score=0.6, close_call=0.02)  # "wide" and "long" are near-synonyms to the small model: only a near-tie is two

CONTACT = Classifier("contact detail", {
    "phone": ["My number is 0412 345 678.", "You can reach me on 0412 345 678.", "Call me on zero four one two, three four five, six seven eight.",
              "Ring me on my mobile, 0400 111 222.", "It's an Indian number, nine eight seven six five four three two one zero.",
              "My phone is plus nine one eight eight six six.", "The best number is 0398765432."],
    "email": ["My email is john at gmail dot com.", "It's raj underscore m at outlook dot com.", "Send it to info at mycompany dot com dot au."],
    "other": ["My order number is DFO001114.", "It's 400 by 400 by 120.", "My postcode is 3061.", "I paid 1299 dollars.", "Order 1114.",
              "The street number is 42.", "I need two of them.", "It's the 1500 mm one."],
}, min_score=0.6)

# Shopify's own policy types: the same in every store, so these examples fit any store's policies.
POLICY_KIND = Classifier("policy kind", {
    "REFUND_POLICY": ["Can I return it?", "Can I send it back if I don't like it?", "Do I get my money back?", "Can I exchange it?",
                      "What if it arrives faulty?", "How do refunds work?", "Is there a change of mind return?"],
    "SHIPPING_POLICY": ["How long does delivery take?", "How long until it arrives?", "Do you charge for postage?", "How much is shipping?",
                        "Do you deliver to my area?", "Can I pick it up instead?", "Who do you use for delivery?"],
    "PRIVACY_POLICY": ["What do you do with my personal information?", "Do you share my data?", "How do you store my details?",
                       "Can you delete my information?"],
    "TERMS_OF_SERVICE": ["What are your terms and conditions?", "What are the rules for buying from you?", "What are the terms of sale?",
                         "Is there a contract when I order?"],
    "CONTACT_INFORMATION": ["How can I contact you?", "What's your business address?", "Who owns the business?"],
}, min_score=0.5)

# How much product detail a question needs: an overview of several products, or everything about one of them.
PRODUCT_DETAIL = Classifier("product detail", {
    "overview": ["What basins do you have?", "How much are they?", "What's the price range?", "Do you have any in stock?",
                 "What options are there?", "Can you list what you have with prices?", "Do you sell freestanding baths?",
                 "Which ones are cheapest?", "What colours do they come in?", "Have you got anything in black?",
                 "What sizes are available?", "I'm looking for a vanity."],
    "detail": ["What's the warranty on that one?", "What is it made of?", "How deep is it?", "What are the exact dimensions?",
               "Tell me more about that one.", "What are the specifications?", "How is it installed?", "What does it come with?",
               "Is the cartridge ceramic?", "Does it need a special waste?", "What's the difference between those two?",
               "Is it suitable for a small bathroom?", "How do I clean it?"],
}, min_score=0.5)

# Does the caller want to talk to a person RIGHT NOW (live call forwarding), rather than a callback later or a question
# about people? Read on kind_of_turn_text, so product words never decide it.
PERSON_REQUEST = Classifier("person request", {
    "person": ["Can I speak to a person?", "I want to talk to a real person.", "Can you put me through to someone?",
               "Is there someone I can talk to?", "Can you transfer me to your team?", "I'd rather talk to a human.",
               "Let me speak to a manager.", "Connect me with someone from sales.", "Can I talk to a staff member now?",
               "Put me on to a real person please.", "I need to speak with someone, not a machine.", "Get me an actual human.",
               "Is anyone there I can talk to?", "Can I be transferred?", "I'd like to talk to someone in customer service."],
    "callback": ["Can someone call me back?", "Have the team ring me tomorrow.", "Could someone phone me later about it?",
                 "Ask them to give me a call when they can.", "I'd like a callback.", "Can the team contact me next week?"],
    "other": ["Are you a real person?", "Am I talking to a robot?", "Can someone install it for me?", "Do you have people who measure up?",
              "I need to ask my partner first.", "How many staff do you have?", "Who owns the business?", "What's your phone number?",
              "Can I pick it up in person?", "Is the showroom staffed on weekends?", "Do your installers come to the house?",
              "What are your opening hours?", "How much is the product?", "I'll talk to my builder and get back to you.",
              "Can a plumber fit it?", "Thanks, that's all I needed."],
}, min_score=0.6, close_call=0.04)

# Live handoff, later callback and unclear timing are separate meanings. This reads the full caller turn and, when
# available, the preceding team request; trimming to the last sentence can discard the request itself.
HANDOFF_ACTION = Classifier("handoff action", {
    "live": [
        "Please connect me to your team on this call.", "I want to speak with a person right now.",
        "Can you transfer me to someone in the team?", "Put me through to a human agent.",
        "I need to talk with your team if you can link them.", "Can I speak with your staff now?",
        "Can you arrange a call so I can talk with a person now?", "Could you connect us while I am on the line?",
        "No, I mean call now, not a callback later.", "Can you just help me to call now?",
        "Yeah, just forward it.", "Please forward this call to the team.", "Connect me now, please.",
        "Let me talk to the team on the phone.", "Can you get someone to pick up this call?",
        "Can somebody put me through to the sales desk?", "Please connect me with the team.",
        "I do not want a bot; give me a person.", "I want someone to pick up my call now.",
        "Can we transfer this call to the team?", "Can you arrange a call from your team now?",
        "Can we transfer our call?",
        "Please arrange a call with a human so I can speak with him now.",
        "Yes, please arrange that call now while I am here.",
        "Please send me through to somebody who can speak with me on the phone.",
        "Put me on the line with an available staff member.",
    ],
    "callback": [
        "Please have someone call me back later.", "Could the team ring me tomorrow?",
        "I want a callback when someone is free.", "Ask a staff member to phone me next week.",
        "Can you take my number so the team can call me?", "I cannot talk now; call me this afternoon.",
        "Have someone contact me after this call.", "Please arrange for the team to call me later.",
        "Can you arrange a call from your team tomorrow?", "Can you transfer my call tomorrow?",
        "Can your team call me back later?", "I would rather have the team call me another time.",
        "Could your people phone me once they have a moment?",
        "Please have the team get back to us this afternoon.",
    ],
    "unclear": [
        "Can you arrange a call from your team?", "I would like to speak with your team sometime.",
        "Can someone from the team talk to me?", "I need to discuss this with a person.",
        "Could I get in touch with someone?", "Can you arrange a call so we can discuss it?",
        "I would like somebody from your team to help me.",
        "I would like to talk with someone about a problem with my order.",
    ],
    "other": [
        "What does your team do?", "How many agents work there?", "Can your installers come to my house?",
        "What are the opening hours?", "How much is this product?", "Can I call your store number?",
        "What is this voice service for?", "What is this call assistant for?", "Can we check that?",
        "Could you check that for me?",
        "I will talk to my partner first.", "What is the price of the black basin?",
        "Do you have someone who can install a shower?", "Thank you, goodbye.",
        "Are you an AI?", "Am I talking to a bot?", "Yeah.", "Okay.", "Yes.",
        "Do not transfer me.", "I do not need anyone to call me.",
        "I paid for my order and it has not arrived. Where is it?",
        "My payment went through but I have not received the order. What happened?",
        "I need an update on my delivery.", "There is a problem with my purchase.",
        "Can you help with this product?", "Can you guide me through the product details?",
        "I want help choosing a bath.", "Can you tell me what you sell?",
        "My number is eight eight six six six three one four nine one.",
        "Eight eight six six six three one four nine one.", "Kitchen.",
    ],
}, min_score=0.56, close_call=0.035)


# These are meanings of whole utterances, not trigger words. Offers and replies
# are classified separately so agreement inherits the action actually offered.
HANDOFF_OFFER = Classifier("agent handoff offer", {
    "live": ["Would you like me to connect you with the team for full product details?",
             "Shall I put you through to a colleague?", "I can transfer you to someone now, if you like.",
             "Would speaking with a person on this call help?"],
    "callback": ["Would you like someone to call you back?", "Shall I arrange for the team to phone you later?",
                 "I can ask a colleague to contact you after this call. Would that help?"],
    "choice": ["Would you like me to connect you now or have someone call you back later?",
               "Shall I put you through, or would you prefer a callback?"],
    "other": ["Would you like the basin sizes and finishes?", "Anything else?",
              "The team will call you on this number.", "Your callback request is already with the team.",
              "What phone number should the team call?", "Would you like more information about delivery?"],
}, min_score=0.58, close_call=0.025)

HANDOFF_REPLY = Classifier("caller response to handoff offer", {
    "accept": ["Yeah. Yeah. Please.", "Yes, please.", "Go ahead.", "That would be helpful.",
               "I'd appreciate that.", "Sounds good to me.", "OK.", "Thanks.", "That works for me."],
    "decline": ["No, thanks.", "Please don't do that.", "I would rather stay with you.",
                "No need, I have what I need.", "Don't transfer me.", "I do not want a callback."],
    "live": ["Now, please.", "Put me through while I am here.", "I'd prefer to stay on the line.",
             "No, can you transfer me now?"],
    "callback": ["Later, please.", "Tomorrow would suit me better.", "Someone call me later.",
                 "Have them get back to me when they can."],
    "other": ["What is the price of the basin?", "My number is 0412345678.", "I need the dimensions.",
              "Can you tell me about delivery?", "I have a question about my order.",
              "Can you help with this product?", "Please explain the options.", "Kitchen."],
}, min_score=0.7, close_call=0.04)

# A short "yes" can acknowledge a callback confirmation, but it must not
# inherit stale callback state after the agent has moved on to another topic.
CALLBACK_ACK_CONTEXT = Classifier("callback confirmation context", {
    "confirmed": ["I've noted your callback request. The team will call you on this number.",
                  "Your callback request is already with the team.",
                  "I've asked the team to call you back after this call.",
                  "The team will get back to you about your callback request.",
                  "A live transfer isn't available right now. I've noted your callback request."],
    "other": ["Are you looking for a specific style or size?", "Would you like a particular basin size?",
              "We have a range of basins in different sizes and finishes.", "What number should the team call?",
              "Can I get your phone number?", "Would you like me to connect you now?",
              "Here are the product options.", "Is there anything else I can help you with?"],
}, min_score=0.58, close_call=0.03)


PAUSE_TURN = Classifier("caller pause", {
    "pause": ["Sorry. Sorry.", "Wait a second.", "Please stop.", "Hold on a moment.",
              "Hang on please.", "Excuse me.", "Let me finish.", "Sorry.", "Wait, please.", "Stop."],
    "other": ["Sorry, can you explain the delivery options?", "I didn't catch that, could you repeat it?",
              "Can you stop the delivery?", "I am waiting for my order.", "Yes, please.",
              "What is the price?", "Someone call me later.", "Please transfer me."],
}, min_score=0.75, close_call=0.04)

AGENT_FAREWELL = Classifier("agent farewell", {
    "farewell": ["Thank you for calling. Goodbye.", "Have a great day, Raj. Goodbye.",
                 "Take care and enjoy your evening.", "Bye now."],
    "other": ["Is there anything else I can help with?", "Thanks. Would you like more information?",
              "The team will call you on this number.", "Here are the prices.", "Hello, how can I help?"],
}, min_score=0.6)

WAIT_QUESTION = Classifier("complete question for delayed reply", {
    "complete": ["What is your Home DFO business for?", "Can you tell me what your company does?",
                 "What are the product prices and delivery options?", "Could you explain how this works?"],
    "other": ["Could you please tell me about", "Can you check the", "I would like to ask you for",
              "My email is john at gmail dot com.", "What email should the team use?",
              "My order arrived broken and I want a refund.", "Yes, please."],
}, min_score=0.6)


TURN_COMPLETION = Classifier("caller sentence completion", {
    "unfinished": ["Could you arrange a call to your", "Could you please tell me about",
                   "I am looking for", "What are the popular", "I need to know whether",
                   "I was wondering if", "Can you check the", "My email is", "Can you tell me about the.",
                   "Yes. So what", "Yes, so what?", "Yes, so what is", "So what is", "Could you tell me what", "Do you have a",
                   "I just wanted to know", "أريد أن أعرف إذا", "هل لديكم", "مجھے جاننا ہے کہ", "کیا آپ کے پاس"],
    "complete": ["What is it for?", "Who is it by?", "What are your services like?",
                 "I'm looking for a round basin.", "Kitchen.", "Someone call me later.",
                 "Yes, please.", "My number is eight eight six six six three one four nine one.",
                 "I want to know about your business.", "Can you help with this product?",
                 "Do you sell kitchen sinks?", "Yes.", "Yeah.", "Okay.", "No, thanks.",
                 "What do you sell?", "Do you have a basin?", "أريد معرفة السعر.", "نعم.", "تمام.",
                 "مجھے قیمت جاننی ہے۔", "جی ہاں۔", "کیا آپ کے پاس بیسن ہے؟"],
}, min_score=0.62)
