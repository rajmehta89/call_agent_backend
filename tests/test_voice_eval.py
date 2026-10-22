"""The truth checker used to grade spoken answers (voice_eval.judge)."""

from voice_eval import Case, judge, numbers_in, ungrounded_numbers

SOURCES = ("Lydia Series Wall Mixer: price 159.21-166.60 AUD; on sale (was 261.00, now 159.21 AUD); Warranty: 15 Year Warranty; "
           "Size: 1500, 1700; Order DFO001110, placed 18 Sep 2026: delivered 23 Sep 2026; total 297.21 AUD")


def test_numbers_are_read_in_every_written_form():
    assert numbers_in("$1,299.00 for the 15-year model") == ["1299", "15"]
    assert numbers_in("fifteen years, about one hundred") == ["15", "1"]
    assert numbers_in("order DFO001110") == ["1110"]


def test_numbers_from_the_data_are_grounded():
    assert ungrounded_numbers("It's $159.21 on sale, down from $261, with a fifteen year warranty.", SOURCES) == []
    assert ungrounded_numbers("It comes in 1500 and 1700 mm.", SOURCES) == []
    assert ungrounded_numbers("It was delivered on the 23rd of September.", SOURCES) == []
    assert ungrounded_numbers("About $159 for chrome.", SOURCES) == []  # a price said rounded is fine


def test_invented_numbers_are_caught():
    assert ungrounded_numbers("It's 750 mm wide and costs $199.", SOURCES) == ["199", "750"]
    assert ungrounded_numbers("It has a ten year warranty.", SOURCES) == ["10"]


def test_small_counting_words_are_not_facts():
    assert ungrounded_numbers("I can help with one or two things, or three.", SOURCES) == []


def test_a_true_answer_passes():
    case = Case("warranty", "what's the warranty?", "product", must=["15"], must_not=["lifetime"])
    assert judge(case, "The Lydia mixer has a 15 year warranty, plus one year on labour.", SOURCES)["ok"]


def test_each_kind_of_wrong_answer_fails_with_a_reason():
    case = Case("warranty", "what's the warranty?", "product", must=["15"], must_any=["warranty"], must_not=["lifetime"])
    assert "missing '15'" in judge(case, "It has a great warranty.", SOURCES)["problems"]
    assert any("said 'lifetime'" in problem for problem in judge(case, "A 15 year, lifetime warranty.", SOURCES)["problems"])
    assert any("numbers not in the data: 20" in problem for problem in judge(case, "A 15 year warranty, 20 on parts.", SOURCES)["problems"])
    assert any("none of" in problem for problem in judge(case, "Fifteen 15 years.", SOURCES)["problems"])
    assert "no answer" in judge(case, "  ", SOURCES)["problems"]


def test_a_phone_number_is_compared_by_its_digits():
    sources = "Phone: (04) 2221 3831"
    assert ungrounded_numbers("Call us on 0422 213 831.", sources) == []  # same digits, grouped differently
    assert ungrounded_numbers("Call us on 0422 221 383.", sources) == ["422221383"]  # a wrong number is caught


def test_a_price_range_is_not_mistaken_for_a_phone_number():
    assert ungrounded_numbers("It ranges from 131.40 to 159.30 AUD.", "price 131.40-159.30 AUD") == []
