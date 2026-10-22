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
