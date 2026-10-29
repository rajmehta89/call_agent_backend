"""Trade outreach names the business's city when the market is known, and reads naturally when it is not."""

from email_templates import DEFAULT_EMAIL_TEMPLATES, _city, render_email_template

TEMPLATE = next(item for item in DEFAULT_EMAIL_TEMPLATES if item["id"] == "missed-calls-on-the-job")


def test_city_drops_the_state_code():
    assert _city("Canberra ACT") == "Canberra" and _city("Gold Coast QLD") == "Gold Coast"
    assert _city("Dubai") == "Dubai" and _city("") == ""


def test_rendered_email_names_the_city_or_leaves_it_out():
    body = render_email_template(TEMPLATE, {"company_name": "Ready Set Plumb", "location": "Sydney NSW"})["body"]
    assert "next business on Google in Sydney." in body and 'reply "yes"' in body
    body = render_email_template(TEMPLATE, {"company_name": "Ready Set Plumb"})["body"]
    assert "next business on Google." in body and "{{" not in body
