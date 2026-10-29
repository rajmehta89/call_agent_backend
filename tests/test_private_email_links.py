"""Outreach emails carry an HTML alternative so the website link is clickable in every mail app."""

from private_email_service import PrivateEmailService


def test_links_are_clickable_and_text_is_escaped():
    body = PrivateEmailService._html_body("Hi <team>,\n\nCheers,\nRaj Mehta\nhttps://buildwithraj.com")
    assert '<a href="https://buildwithraj.com">https://buildwithraj.com</a>' in body
    assert "&lt;team&gt;" in body and "<br>" in body


def test_sentence_punctuation_stays_outside_the_link():
    body = PrivateEmailService._html_body("See https://buildwithraj.com.")
    assert '<a href="https://buildwithraj.com">https://buildwithraj.com</a>.' in body
