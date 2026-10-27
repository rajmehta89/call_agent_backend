"""What the AI is told for each kind of store question a caller asks.

A small fake store in Shopify's own data shapes (the same GraphQL nodes the real store returns) runs through the real
parsing (_normalize_product / _normalize_order), search, verification and wording code. Each test is one kind of
question: products, sizes and specs, prices and sales, stock, order status in every state, who may hear about an order,
policies, questions that need no store data, and failures.
"""

import pytest

import shopify_service as shopify_module
from brain_service import brain_service
from shopify_service import shopify_service as shop

ALL_TOOLS = {"search_shopify_products": True, "check_inventory": True, "get_order": True, "get_customer_orders": True, "get_store_policy": True}


# ---------------------------------------------------------------------------------------------------- fake catalogue
def product_node(title, low, high=None, *, kind="", vendor="Regio", tags=(), inventory=50, options=(), variants=(), html="", description=None, reviews=None, collections=()):
    return {
        "id": f"gid://shopify/Product/{abs(hash(title)) % 10**9}", "title": title, "handle": title.lower().replace(" ", "-"), "status": "ACTIVE",
        "productType": kind, "vendor": vendor, "tags": list(tags), "onlineStoreUrl": None, "totalInventory": inventory,
        "description": description if description is not None else shopify_module.re.sub(r"<[^>]+>", " ", html),
        "descriptionHtml": html, "options": [{"name": name, "values": list(values)} for name, values in options],
        "collections": {"nodes": [{"title": item} for item in collections]},
        "reviews": {"value": reviews} if reviews else None,
        "priceRangeV2": {"minVariantPrice": {"amount": str(low), "currencyCode": "AUD"}, "maxVariantPrice": {"amount": str(high if high is not None else low), "currencyCode": "AUD"}},
        "variants": {"nodes": list(variants) or [{"title": "Default Title", "price": str(low), "compareAtPrice": None, "sku": "", "availableForSale": inventory > 0, "inventoryQuantity": inventory}]},
    }


def variant(title, price, quantity, was=None):
    return {"title": title, "price": str(price), "compareAtPrice": str(was) if was else None, "sku": "", "availableForSale": quantity > 0, "inventoryQuantity": quantity}


SPEC_HTML = ("<p>Upgrade your bathroom with a premium mixer.</p><p>Specifications<br>\nBrand: Regio<br>\nSeries: Lydia<br>\n"
             "Product: Wall Mixer with Diverter<br>\nMaterial: Premium Solid Brass<br>\nCartridge: Ceramic Disc Cartridge<br>\n"
             "Warranty: 15 Year Warranty<br>\nLabour Warranty: 1 Year Labour Warranty</p>")
CATALOGUE = [
    product_node("Lydia Series Wall Mixer with Diverter", "159.21", "166.60", kind="Wall Mixer", tags=["tapware"], inventory=500, html=SPEC_HTML,
                 options=[("Color", ["Chrome", "Black"])], collections=["Lydia Series"],
                 variants=[variant("Chrome", "159.21", 300, was="261.00"), variant("Black", "166.60", 0)]),
    product_node("Freestanding Oval Bathtub", "699", "899", kind="Freestanding Bathtubs", inventory=55, options=[("Color", ["White", "Black"]), ("Size", ["1500", "1700"])],
                 html="<p>Classic oval shape.</p><p>Available Sizes 1500 mm and 1700 mm.</p>",
                 variants=[variant("White / 1500", "799", 16), variant("Black / 1700", "699", 4)]),
    product_node("Regio Capri Matt Porcelain Tile 600 by 1200", "0", kind="Porcelain Tiles", inventory=0,
                 html="<p>Specifications<br>\nSize: 600 x 1200mm<br>\nMaterial: Porcelain</p>"),
    product_node("Regio Cacey Series Short Basin Mixer", "191", "249", kind="Short Basin Mixer", inventory=500, options=[("Color", ["Chrome", "Black"])],
                 html="<p>Specifications<br>\nProduct: Wall Mixer with Spout<br>\nMaterial: Solid Brass<br>\nWarranty: 15 Year Warranty</p>"),
    product_node("Above Counter Basin Round Matte Black 360x360x120mm", "130", kind="Basins", tags=["black", "basin"], inventory=29,
                 html="<p>Specifications<br>\nSize: 360 x 360 x 120mm<br>\nCapacity: 8.3L</p>", reviews="<div data-average-rating='4.75' data-number-of-reviews='12'></div>"),
    product_node("Handcrafted Black Kitchen Sink", "604", kind="Kitchen Sinks", tags=["black", "sink"], inventory=8, html="<p>Specifications<br>\nSize: 800 x 450 x 250mm</p>"),
]
CATALOGUE_WORDS = frozenset({"mixer", "diverter", "bathtub", "oval", "freestanding", "tile", "porcelain", "basin", "sink", "kitchen", "lydia", "cacey"})


def fake_products(query="", limit=25):
    """Shopify search, simplified: plain words must all match (title, type, tags); "(title:*a* OR title:*b*)" matches any."""
    query = query.replace(" status:active", "").strip().lower()
    nodes = [shop._normalize_product(node) for node in CATALOGUE]
    haystack = lambda product: " ".join([product["title"], product.get("product_type") or "", " ".join(product.get("tags") or [])]).lower()
    if query.startswith("("):
        words = shopify_module.re.findall(r"title:\*([a-z0-9]+)\*", query)
        return [product for product in nodes if any(word in product["title"].lower() for word in words)][:limit]
    words = query.split()
    return [product for product in nodes if all(word in haystack(product) for word in words)][:limit]


# ---------------------------------------------------------------------------------------------------- fake orders
def order_node(name, *, created="2026-09-18T02:00:00Z", financial="PAID", fulfillment="FULFILLED", items=(("Pull Out Kitchen Mixer", 1, 0),),
               total="297.21", email="buyer@example.com", billing_phone="0412 345 678", cancelled=None, cancel_reason=None, refunds=(), shipments=(), return_status="NO_RETURN"):
    return {
        "name": name, "createdAt": created, "cancelledAt": cancelled, "cancelReason": cancel_reason, "returnStatus": return_status,
        "displayFinancialStatus": financial, "displayFulfillmentStatus": fulfillment, "email": email, "phone": None,
        "totalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "AUD"}}, "customer": {"phone": None, "email": None},
        "shippingAddress": None, "billingAddress": {"phone": billing_phone},
        "refunds": [{"createdAt": at, "totalRefundedSet": {"shopMoney": {"amount": amount, "currencyCode": "AUD"}}} for at, amount in refunds],
        "lineItems": {"nodes": [{"title": title, "quantity": quantity, "unfulfilledQuantity": unfulfilled} for title, quantity, unfulfilled in items]},
        "fulfillments": list(shipments),
    }


def shipment(display, *, shipped="2026-09-18T05:00:00Z", delivered=None, estimated=None, latest=None, tracking=("Australia Post", "33GJU350793501361810103")):
    return {"status": "SUCCESS", "displayStatus": display, "createdAt": shipped, "inTransitAt": None, "deliveredAt": delivered, "estimatedDeliveryAt": estimated,
            "trackingInfo": [{"company": tracking[0], "number": tracking[1], "url": None}] if tracking else [],
            "events": {"nodes": [{"status": latest[0], "happenedAt": latest[1], "message": None, "city": latest[2]}] if latest else []}}


ORDERS = {
    "DFO001110": order_node("DFO001110", shipments=[shipment("DELIVERED", delivered="2026-09-23T03:00:00Z", latest=("DELIVERED", "2026-09-23T03:00:00Z", "Melbourne"))]),
    "DFO001120": order_node("DFO001120", shipments=[shipment("OUT_FOR_DELIVERY", latest=("OUT_FOR_DELIVERY", "2026-09-20T22:00:00Z", "Campbellfield"))]),
    "DFO001121": order_node("DFO001121", shipments=[shipment("IN_TRANSIT", estimated="2026-09-25T00:00:00Z", latest=("IN_TRANSIT", "2026-09-19T10:00:00Z", "Sydney"))]),
    "DFO001122": order_node("DFO001122", shipments=[shipment("ATTEMPTED_DELIVERY", latest=("ATTEMPTED_DELIVERY", "2026-09-21T01:00:00Z", None))]),
    "DFO001113": order_node("DFO001113", fulfillment="UNFULFILLED", items=(("Black Granite Sink", 1, 1),)),
    "DFO001117": order_node("DFO001117", fulfillment="UNFULFILLED", cancelled="2026-09-19T00:00:00Z", cancel_reason="CUSTOMER",
                            refunds=[("2026-09-19T01:00:00Z", "498.70")], items=(("90cm Canopy Rangehood", 1, 1),), total="498.70"),
    "DFO001116": order_node("DFO001116", financial="REFUNDED", fulfillment="UNFULFILLED", refunds=[("2026-09-19T01:00:00Z", "494.90")], items=(("90cm Rangehood", 1, 1),)),
    "DFO001130": order_node("DFO001130", fulfillment="PARTIALLY_FULFILLED", items=(("Vanity Unit", 1, 0), ("Mirror", 1, 1)), shipments=[shipment("IN_TRANSIT")]),
    "DFO001131": order_node("DFO001131", shipments=[shipment("MARKED_AS_FULFILLED", tracking=None)], email="other@example.com", billing_phone="0499 111 222"),
    "DFO001132": order_node("DFO001132", return_status="RETURN_REQUESTED", shipments=[shipment("DELIVERED", delivered="2026-09-23T03:00:00Z")]),
}
CALLER = "+61412345678"  # the billing phone on the orders above, as caller ID shows it


def normalized(name):
    order = shop._normalize_order(ORDERS[name])
    order["_emails"] = sorted(order["_emails"])
    return order


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(shop, "store_domain", "demo.myshopify.com")
    monkeypatch.setattr(shop, "static_token", "shpat_test")
    monkeypatch.setattr(shop, "client_id", "")
    monkeypatch.setattr(shop, "client_secret", "")
    monkeypatch.setattr(shop, "_scopes", ["read_customers", "read_inventory", "read_legal_policies", "read_orders", "read_products"])
    monkeypatch.setattr(shop, "products", fake_products)
    monkeypatch.setattr(shop, "catalog_words", lambda: CATALOGUE_WORDS)
    monkeypatch.setattr(shop, "catalog_index", lambda: shop._index[0])  # no background build reaching the network
    monkeypatch.setattr(shop, "_orders", lambda search, limit: [normalized(name) for name in ORDERS if not search or search.lstrip("name:#") == name][:limit])
    monkeypatch.setattr(shop, "_caller_orders", {})
    monkeypatch.setattr(shop, "_caller_lookups", {})
    monkeypatch.setattr(shop, "prefetch_orders_for_phone", lambda phone: None)
    monkeypatch.setattr(shop, "graphql", lambda query, variables=None: {"customers": {"nodes": []}})
    monkeypatch.setattr(shop, "policies", lambda: [{"type": "REFUND_POLICY", "title": "Refund policy", "text": "Change-of-mind returns within 30 days, unused and in original packaging."},
                                                   {"type": "SHIPPING_POLICY", "title": "Shipping", "text": "Delivery Australia wide; allow up to 30 days."}])
    tools = dict(ALL_TOOLS)
    monkeypatch.setattr(brain_service, "tools", lambda: tools)
    return tools


def ask(question, phone=None, earlier=""):
    return brain_service._shopify_context(question, phone, f"{earlier} {question}".strip())


# ---------------------------------------------------------------------------------------------------- products
@pytest.mark.parametrize("question, product", [
    ("is the lydia wall mixer with diverter in stock?", "Lydia Series Wall Mixer with Diverter"),
    ("do you have black sinks", "Handcrafted Black Kitchen Sink"),
    ("got any freestanding baths?", "Freestanding Oval Bathtub"),           # plural and no buying words: the catalogue names it
    ("I need a matte black basin", "Above Counter Basin Round Matte Black"),
    ("how much is the oval bathtub", "Freestanding Oval Bathtub"),
])
def test_product_questions_find_what_the_caller_means(store, question, product):
    text, tool = ask(question)
    assert tool == "search_shopify_products"
    assert text.split("\n- ")[1].startswith(product)  # the closest match comes first


def test_price_range_sale_price_and_stock_per_option(store):
    text, _ = ask("how much is the lydia wall mixer with diverter")
    assert "price 159.21-166.60 AUD" in text
    assert "on sale (was 261.00, now 159.21 AUD)" in text
    assert "Chrome 159.21 (300 left)" in text and "Black 166.60 (sold out)" in text


def test_sizes_finishes_and_options_are_given(store):
    text, _ = ask("what sizes does the freestanding oval bathtub come in")
    assert "Size: 1500, 1700" in text and "Color: White, Black" in text and "1500 mm" in text


def test_specifications_and_warranty_are_given(store):
    text, _ = ask("what's the warranty on the lydia wall mixer with diverter")
    assert "Warranty: 15 Year Warranty" in text and "Labour Warranty: 1 Year Labour Warranty" in text and "Material: Premium Solid Brass" in text


def test_measurements_from_the_title_and_specs(store):
    text, _ = ask("what are the measurements of the matte black basin")
    assert "360x360x120mm" in text and "Capacity: 8.3L" in text


def test_a_detail_that_is_not_listed_is_never_estimated(store):
    text, _ = ask("how wide is the freestanding oval bathtub")
    assert "never estimate it" in text and "offer to have the team confirm" in text


def test_a_zero_price_is_never_quoted(store):
    text, _ = ask("how much is the regio capri porcelain tile")
    assert "not listed online (offer a quote from the team)" in text and "price 0" not in text and "0.0 AUD" not in text
    assert "out of stock" in text and "Size: 600 x 1200mm" in text  # the other facts still come through


def test_a_description_copied_from_another_product_is_not_used(store):
    text, _ = ask("what's the warranty on the cacey short basin mixer")
    block = text.split("\n- ")[1]
    assert block.startswith("Regio Cacey Series Short Basin Mixer") and "price 191-249 AUD" in block
    assert "description is for a different product" in block and "Warranty:" not in block


def test_real_reviews_are_mentioned_and_empty_ones_are_not(store):
    assert "rated 4.8 out of 5 from 12 reviews" in ask("matte black basin")[0]
    assert "rated" not in ask("lydia wall mixer with diverter")[0]


def test_stock_counts_are_withheld_when_the_stock_switch_is_off(store):
    store["check_inventory"] = False
    text, _ = ask("is the lydia wall mixer with diverter available")
    assert "do not quote stock counts" in text and "300 left" not in text and "in stock" not in text


def test_no_matching_product_is_said_plainly(store):
    text, tool = ask("do you sell garden hoses")
    assert tool == "search_shopify_products" and "no product matched" in text


# ---------------------------------------------------------------------------------------------------- order status
@pytest.mark.parametrize("name, status", [
    ("DFO001110", "Delivered 23 Sep 2026"),
    ("DFO001120", "Out for delivery"),
    ("DFO001121", "On the way (shipped 18 Sep 2026)"),
    ("DFO001122", "Delivery was attempted (shipped 18 Sep 2026)"),
    ("DFO001113", "Not shipped yet"),
    ("DFO001117", "Cancelled 19 Sep 2026 at the customer's request and refunded"),
    ("DFO001116", "Not shipped yet (refunded)"),
    ("DFO001130", "On the way (shipped 18 Sep 2026), part of the order still to ship"),
    ("DFO001131", "Shipped 18 Sep 2026; no carrier updates yet"),
])
def test_every_order_state_in_plain_words(name, status):
    assert normalized(name)["status"] == status


def test_the_order_story_has_items_tracking_updates_refunds_and_returns():
    in_transit = shop.describe_order(normalized("DFO001121"))
    assert "1 x Pull Out Kitchen Mixer" in in_transit and "total 297.21 AUD" in in_transit
    assert "tracking Australia Post 33GJU350793501361810103" in in_transit
    assert "latest carrier update: in transit 19 Sep 2026 in Sydney" in in_transit and "estimated delivery 25 Sep 2026" in in_transit
    assert "Still to ship: 1 x Mirror" in shop.describe_order(normalized("DFO001130"))
    assert "Refund of 494.90 AUD issued 19 Sep 2026" in shop.describe_order(normalized("DFO001116"))
    assert "Return: return requested" in shop.describe_order(normalized("DFO001132"))


def test_order_details_never_include_addresses_payment_or_contact_details():
    story = shop.describe_order(normalized("DFO001110"))
    assert "buyer@example.com" not in story and "0412" not in story and "billing" not in story.lower()


# ---------------------------------------------------------------------------------------------------- who may hear about an order
def test_a_caller_on_the_order_hears_its_status(store):
    text, tool = ask("where is my order DFO001110?", phone=CALLER)
    assert tool == "get_order" and "Verified order" in text and "Delivered 23 Sep 2026" in text


def test_a_caller_not_on_the_order_hears_nothing_until_they_give_its_email(store):
    text, _ = ask("where is my order DFO001110?", phone="+61499999999")
    assert "not verified" in text and "Delivered" not in text and "Ask for the email" in text
    text, _ = ask("it's buyer at example dot com", phone="+61499999999", earlier="where is my order DFO001110?")
    assert "Verified order" in text and "Delivered 23 Sep 2026" in text


def test_the_order_number_can_be_said_in_any_form(store):
    for said in ("DFO001110", "dfo 001110", "order number 1110"):
        assert "Delivered 23 Sep 2026" in ask(f"where's my order {said}", phone=CALLER)[0], said


def test_an_unknown_order_number(store):
    text, _ = ask("where is my order DFO009999", phone=CALLER)
    assert "No order DFO009999 was found" in text


def test_caller_id_alone_finds_the_callers_orders(store):
    text, tool = ask("has my order shipped yet?", phone=CALLER)
    assert tool == "get_order" and "found by their caller ID" in text
    assert "DFO001110" in text and "DFO001131" not in text  # another customer's order is never included


def test_the_email_alone_finds_the_callers_orders(store, monkeypatch):
    monkeypatch.setattr(shop, "orders_for_email", lambda email, limit=5: [normalized("DFO001131")] if email == "other@example.com" else [])
    text, _ = ask("where is my order? my email is other at example dot com", phone="+61400000000")
    assert "Orders placed with the email the caller gave" in text and "DFO001131" in text


@pytest.mark.parametrize("question", [
    "the thing I got last week is leaking",
    "I want to send it back",
    "when is my stuff coming",
])
def test_any_wording_about_a_purchase_reaches_the_callers_orders(store, question):
    shop.orders_for_phone(CALLER)  # looked up when the call started
    text, tool = ask(question, phone=CALLER)
    assert tool in ("caller_orders", "get_order", "get_store_policy")
    if tool == "caller_orders":
        assert "DFO001110" in text and "do not bring them up yourself" in text


def test_a_caller_with_no_orders_is_asked_for_the_number(store):
    text, _ = ask("where's my order?", phone="+61400000000")
    assert "Ask for their order number, or the email used on the order" in text


def test_order_lookups_are_off_when_their_switch_is_off(store):
    store["get_order"] = False
    assert ask("where is my order DFO001110", phone=CALLER)[1] != "get_order"


# ---------------------------------------------------------------------------------------------------- policies and the rest
@pytest.mark.parametrize("question, expected", [
    ("what's your refund policy?", "Change-of-mind returns within 30 days"),
    ("how does shipping work?", "Delivery Australia wide"),
])
def test_policy_questions(store, question, expected):
    text, tool = ask(question)
    assert tool == "get_store_policy" and expected in text


@pytest.mark.parametrize("question", ["what time do you close?", "hello", "who am I speaking to?", "can I talk to someone about a renovation"])
def test_questions_that_need_no_store_data(store, question):
    assert ask(question) == ("", None)


def test_a_store_failure_is_said_honestly(store, monkeypatch):
    def broken(query="", limit=25):
        raise RuntimeError("Shopify is down")
    monkeypatch.setattr(shop, "products", broken)
    text, tool = ask("how much is the lydia wall mixer with diverter")
    assert tool == "shopify_error" and "could not check the store" in text


def test_no_store_connected_means_no_lookup(store, monkeypatch):
    monkeypatch.setattr(shop, "store_domain", "")
    assert ask("where is my order DFO001110", phone=CALLER) == ("", None)


def test_a_failed_customer_search_keeps_the_orders_found_by_phone(store, monkeypatch):
    def down(query, variables=None):
        raise RuntimeError("Shopify customers search is down")
    monkeypatch.setattr(shop, "graphql", down)
    assert [order["name"] for order in shop.orders_for_phone(CALLER)][:1] == ["DFO001110"]


def test_a_store_wide_warranty_question_goes_to_the_policies(store, monkeypatch):
    monkeypatch.setattr(shop, "policies", lambda: [{"type": "TERMS_OF_SERVICE", "title": "Warranty", "text": "All tapware carries a manufacturer warranty."}])
    text, tool = ask("what warranty do you give?")
    assert tool == "get_store_policy" and "manufacturer warranty" in text


def test_shopify_placeholders_never_reach_the_ai(store, monkeypatch):
    plain = product_node("Plain Basin 400x400mm", "99", kind="Default", options=[("Title", ["Default Title"])], html="<p>Size: 400 x 400mm</p>")
    monkeypatch.setattr(shop, "products", lambda query="", limit=25: [shop._normalize_product(plain)])
    text, _ = ask("plain basin")
    assert "Default Title" not in text and "type Default" not in text


def test_a_size_without_a_named_dimension_is_never_called_a_width(store):
    assert "never call it the width, depth or height" in ask("how wide is the freestanding oval bathtub")[0]


def test_catalogue_health_finds_a_title_size_that_differs_from_the_description():
    assert shopify_module._size_conflict("Round Basin 360x360x120mm", "SIZE: 355*355*120mm CAPACITY: 8.3L")
    assert not shopify_module._size_conflict("Round Basin 360x360x120mm", "Size 360 x 360 x 120 mm")
    assert not shopify_module._size_conflict("Round Basin", "SIZE: 355*355*120mm")  # no size in the title: nothing to compare


# ---------------------------------------------------------------------------------------------------- follow-ups (from a real call)
EARLIER = "Yeah. I want to buy a basin."


@pytest.mark.parametrize("follow_up", [
    "I need to know the pricing.",
    "Okay. So please list down all the business that you have. And also the pricing for that.",  # "basins" misheard as "business"
    "I need the smaller one.",
    "which one is cheapest?",
])
def test_a_follow_up_stays_on_the_product_being_discussed(store, follow_up):
    text, tool = ask(follow_up, earlier=EARLIER)
    assert tool == "search_shopify_products", follow_up
    assert "Above Counter Basin Round Matte Black" in text and "130" in text


def test_live_prices_replace_the_old_send_them_to_the_website_rule(store):
    text, _ = ask("how much are your basins")
    assert "replaces any instruction to send callers to the website for prices" in text


def test_a_broad_question_gets_examples_and_the_price_range(store):
    text, _ = ask("what basins do you have and what do they cost", earlier=EARLIER)
    assert "Prices across these" in text and "from " in text


def test_pricing_without_any_product_in_the_call_still_searches(store):
    text, tool = ask("what's the pricing on the oval bathtub")
    assert tool == "search_shopify_products" and "Freestanding Oval Bathtub" in text


# ---------------------------------------------------------------------------------------------------- the in-memory catalogue
def live_nodes(ids, price_change=None):
    """What Shopify returns for a live price/stock request: only those fields."""
    by_id = {node["id"]: node for node in CATALOGUE}
    nodes = []
    for product_id in ids:
        node = dict(by_id[product_id])
        if price_change and node["title"] == price_change[0]:
            node["priceRangeV2"] = {"minVariantPrice": {"amount": price_change[1], "currencyCode": "AUD"}, "maxVariantPrice": {"amount": price_change[1], "currencyCode": "AUD"}}
        nodes.append({key: node[key] for key in ("id", "totalInventory", "priceRangeV2", "variants")})
    return nodes


@pytest.fixture
def indexed(store, monkeypatch):
    monkeypatch.setattr(shop, "_index", ([shop._normalize_product(node) for node in CATALOGUE], __import__("time").time()))
    monkeypatch.setattr(shop, "_live_products", {})  # each test reads live prices afresh
    requests = []

    def graphql(query, variables=None):
        requests.append(query)
        if "nodes(ids:" in query:
            return {"nodes": live_nodes(variables["ids"], price_change=getattr(graphql, "price_change", None))}
        return {"customers": {"nodes": []}}

    monkeypatch.setattr(shop, "graphql", graphql)
    monkeypatch.setattr(shop, "products", lambda query="", limit=25: pytest.fail("the in-memory catalogue should answer, not a Shopify search"))
    return graphql, requests


def test_products_are_found_in_memory_with_one_live_price_request(indexed):
    graphql, requests = indexed
    found = shop.search_products("is the lydia wall mixer with diverter in stock", limit=5)
    assert found[0]["title"] == "Lydia Series Wall Mixer with Diverter"
    assert len(requests) == 1 and "nodes(ids:" in requests[0]  # one small request: prices and stock only


def test_live_prices_are_reused_briefly_for_a_follow_up(indexed, monkeypatch):
    # A follow-up about the same product re-read its prices each turn (0.4-0.9 s on production calls).
    _, requests = indexed
    shop.search_products("freestanding oval bathtub", limit=1)
    shop.search_products("tell me more about the freestanding oval bathtub", limit=1)
    assert sum("nodes(ids:" in query for query in requests) == 1
    monkeypatch.setenv("SHOPIFY_LIVE_SECONDS", "0")
    monkeypatch.setenv("SHOPIFY_LIVE_MAX_SECONDS", "0")  # too old to use: read live before answering
    shop.search_products("freestanding oval bathtub", limit=1)
    assert sum("nodes(ids:" in query for query in requests) == 2


def _wait_for_refresh():
    import time
    for _ in range(100):
        if not shop._live_refreshing:
            return
        time.sleep(0.02)
    raise AssertionError("background refresh did not finish")


def test_an_older_price_answers_at_once_and_refreshes_in_the_background(indexed, monkeypatch):
    graphql, requests = indexed
    shop.search_products("freestanding oval bathtub", limit=1)
    monkeypatch.setenv("SHOPIFY_LIVE_SECONDS", "0")  # older than "fresh", still usable
    graphql.price_change = ("Freestanding Oval Bathtub", "649")
    first = shop.search_products("freestanding oval bathtub", limit=1)
    assert first[0]["min_price"] != "649"  # answered at once from memory, no wait
    _wait_for_refresh()
    assert sum("nodes(ids:" in query for query in requests) == 2  # re-read in the background
    monkeypatch.setenv("SHOPIFY_LIVE_SECONDS", "60")
    assert shop.search_products("freestanding oval bathtub", limit=1)[0]["min_price"] == "649"  # next turn: new price


def test_after_a_restart_prices_are_read_live_first(indexed, monkeypatch):
    graphql, requests = indexed
    shop.search_products("freestanding oval bathtub", limit=1)
    monkeypatch.setattr(shop, "_live_products", {})  # what a restart leaves: nothing remembered
    graphql.price_change = ("Freestanding Oval Bathtub", "649")
    assert shop.search_products("freestanding oval bathtub", limit=1)[0]["min_price"] == "649"
    assert sum("nodes(ids:" in query for query in requests) == 2


def test_a_failed_background_refresh_keeps_the_remembered_price(indexed, monkeypatch):
    graphql, requests = indexed
    shop.search_products("freestanding oval bathtub", limit=1)
    monkeypatch.setenv("SHOPIFY_LIVE_SECONDS", "0")
    monkeypatch.setattr(shop, "graphql", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Shopify down")))
    assert shop.search_products("freestanding oval bathtub", limit=1)  # still answered
    _wait_for_refresh()
    assert shop.search_products("freestanding oval bathtub", limit=1)  # and again, within the usable age


def test_prices_and_stock_are_live_even_from_memory(indexed):
    graphql, _ = indexed
    graphql.price_change = ("Freestanding Oval Bathtub", "649")
    found = shop.search_products("freestanding oval bathtub", limit=1)
    assert found[0]["min_price"] == "649"  # today's price, not the one remembered


def test_the_memory_search_reads_descriptions_and_specs(indexed):
    found = shop.search_products("mixer with a ceramic disc cartridge", limit=3)
    assert found[0]["title"] == "Lydia Series Wall Mixer with Diverter"  # "ceramic disc cartridge" is only in its specifications


def test_a_named_kind_of_product_keeps_the_search_to_that_kind(indexed):
    titles = [product["title"] for product in shop.search_products("black sink", limit=5)]
    assert titles == ["Handcrafted Black Kitchen Sink"]  # the black basin is black, but it is not a sink


def test_without_the_memory_copy_the_shopify_search_is_used(store):
    assert shop.search_products("freestanding oval bathtub", limit=1)[0]["title"] == "Freestanding Oval Bathtub"


def test_what_a_product_is_comes_from_its_title(store):
    assert shop._product_is("Lydia Series Wall Mixer with Diverter") == "mixer"
    assert shop._product_is("Above Counter Basin Round Matte Black 360x360x120mm") == "basin"
    assert shop._product_is("Regio Cacey Series Short Basin Mixer") == "mixer"


def test_asking_for_a_kind_of_product_lists_that_kind_first(indexed):
    titles = [product["title"] for product in shop.search_products("basin list down all pricing", limit=5)]
    assert titles[0] == "Above Counter Basin Round Matte Black 360x360x120mm"  # a basin before the basin mixer


def test_an_unlisted_dimension_is_named_for_the_ai(store):
    text, _ = ask("how wide is the freestanding oval bathtub")
    assert "The caller asked for the width. No width is written for these products" in text
    assert "The caller asked for" not in ask("what are the measurements of the matte black basin")[0]  # no single dimension asked
    assert "No height" not in ask("how tall is the matte black basin")[0]  # 360x360x120mm gives every dimension


def test_an_unnamed_purchase_is_matched_to_the_latest_order(store):
    shop.orders_for_phone(CALLER)
    text, tool = ask("the thing I got is leaking", phone=CALLER)
    assert tool == "get_order" and "DFO001110" in text and "name that item and confirm it" in text


def test_dimension_questions_are_read_from_how_the_caller_asks():
    from shopify_service import _dimension_asked, _states_dimension
    assert _dimension_asked("How wide is the Freestanding Oval Bathtub with High Back Rest?") == "width"
    assert _dimension_asked("what's the depth of it") == "depth"
    assert _dimension_asked("how wide and how deep is it") == ""
    assert _dimension_asked("do you have the high back rest bath") == ""
    assert not _states_dimension({"title": "Oval Bath", "description": "A wide range of finishes."}, "width")
    assert _states_dimension({"title": "Oval Bath", "description": "Width: 750 mm"}, "width")
    assert _states_dimension({"title": "Oval Bath", "description": "It is 750mm wide and 580mm high"}, "width")


def test_the_meaning_and_words_split_is_a_setting(monkeypatch):
    monkeypatch.delenv("STORE_SEARCH_MEANING_PERCENT", raising=False)
    assert shop.meaning_share == shop.MEANING_PERCENT_DEFAULT / 100
    monkeypatch.setenv("STORE_SEARCH_MEANING_PERCENT", "30")
    assert shop.meaning_share == 0.3
    monkeypatch.setenv("STORE_SEARCH_MEANING_PERCENT", "250")
    assert shop.meaning_share == 1.0


@pytest.mark.parametrize("question, earlier, kinds", [
    ("I need to know the pricing.", "Yeah, I want to buy a basin.", ["product"]),
    ("what would those cost me", "", ["product"]),
    ("the thing I got is leaking", "", ["order"]),
    ("has it gone out yet", "Where is my order DFO001110?", ["order"]),
    ("do you ship to Perth", "", ["policy"]),
    ("what time do you open tomorrow", "", []),
    ("What services do you offer?", "", []),
])
def test_what_the_caller_wants_is_understood_by_meaning(question, earlier, kinds):
    import store_intent
    got = store_intent.wanted(question, earlier)
    assert got[:len(kinds)] == kinds if kinds else got == []


@pytest.mark.parametrize("question, earlier", [
    ("I'm interested in round Beijing. So if you guys have it, that would be fine.", ""),
    ("So you don't have the round basin, actually?", ""),
    ("What are your price ranges for all kinds of busy?", "I'm interested in round Beijing. So you don't have the round basin, actually?"),
])
def test_product_questions_survive_misheard_names_and_follow_up_turns(store, question, earlier):
    import store_intent
    assert store_intent.wanted(question, earlier)[:1] == ["product"]


def test_misheard_product_follow_up_still_searches_recent_basin_and_prices(store, monkeypatch):
    question = "What are your price ranges for all kinds of busy?"
    earlier = "I'm interested in round Beijing. So you don't have the round basin, actually?"
    text, tool = ask(question, earlier=earlier)
    assert tool == "search_shopify_products"
    assert "Above Counter Basin Round Matte Black" in text
    assert "AUD" in text
    import store_intent
    assert store_intent.clearly_wanted(question, earlier) is True


def test_round_product_interest_and_clarification_return_live_catalogue_facts(store):
    first, first_tool = ask("I'm interested in round Beijing. So if you guys have it, that would be fine.")
    clarification, clarification_tool = ask("So you don't have the round basin, actually?")
    assert first_tool == clarification_tool == "search_shopify_products"
    assert "Above Counter Basin Round Matte Black" in first
    assert "Above Counter Basin Round Matte Black" in clarification
    assert "AUD" in clarification


def test_a_general_question_gets_an_overview_and_a_detail_question_gets_the_full_product(store, monkeypatch):
    monkeypatch.setattr(shop, "_wants_detail", lambda question: "warranty" in question)  # decided by meaning in the app
    overview, _ = ask("how much is the lydia wall mixer with diverter")
    detail, _ = ask("what's the warranty on the lydia wall mixer with diverter")
    assert "About it:" not in overview and "an overview" in overview          # no long description for a general question
    assert "Each option: Chrome 159.21" in overview                           # but every price the caller may ask about
    assert "Warranty: 15 Year Warranty" in detail and "Labour Warranty" in detail and "About it:" in detail
    assert len(overview) < len(detail)


@pytest.mark.parametrize("question, check", [
    # From a real call: "who won the football?" half-matched an order question and the agent said "let me check".
    ("Okay. So who won the football last night?", False),
    ("What is the weather like in Melbourne today?", False),
    ("Can you tell me a joke?", False),
    ("How much is the Lydia wall mixer?", True),
    ("How long does delivery usually take?", True),
    ("So where is my order?", True),
])
def test_let_me_check_is_only_for_clear_store_questions(question, check):
    import store_intent
    assert store_intent.clearly_wanted(question) is check
    if not check:
        assert store_intent.wanted(question) == []  # and off-topic chat does not reach the store at all


def test_a_product_removed_from_the_store_is_not_offered_from_memory(indexed, monkeypatch):
    assert shop.search_products("freestanding oval bathtub", limit=1)
    monkeypatch.setattr(shop, "graphql", lambda query, variables=None: {"nodes": [None for _ in variables["ids"]]})
    monkeypatch.setenv("SHOPIFY_LIVE_SECONDS", "0")
    shop.search_products("freestanding oval bathtub", limit=1)  # answered from memory, refreshed in the background
    _wait_for_refresh()
    titles = [product["title"] for product in shop.search_products("freestanding oval bathtub", limit=5)]
    assert "Freestanding Oval Bathtub" not in titles  # the refresh saw it was gone
