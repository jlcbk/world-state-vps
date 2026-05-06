import world_state_collector as c


def test_extract_full_text_from_sec_body_div():
    html = '''<html><body><div class="clearfix text-formatted usa-prose field field--name-body field--type-text-with-summary field--label-hidden field__item"><p>First paragraph.</p><p>Second paragraph.</p></div></body></html>'''
    detail = c.extract_article_body(html, "sec_press")
    assert detail["full_text"] == "First paragraph. Second paragraph."
    assert "field--name-body" in detail["body_html"]


def test_extract_full_text_from_fed_article():
    html = '''<html><body><div id="article"><div class="heading"><h3 class="title">Fed title</h3></div><div class="col-xs-12 col-sm-8 col-md-8"><p>Policy paragraph.</p><p>Second policy paragraph.</p></div></div><div id="lastUpdate">Last Update</div></body></html>'''
    detail = c.extract_article_body(html, "fed_press")
    assert "Policy paragraph." in detail["full_text"]
    assert "Second policy paragraph." in detail["full_text"]


def test_parse_ofac_recent_actions_list():
    html = '''<div class="search-result views-row"><div><div><a href="/recent-actions/20260505">Issuance of Venezuela-related General License</a></div></div><div><div>May 05, 2026 - <a href="/recent-actions/general-licenses">General Licenses</a></div></div></div>'''
    rows = c.parse_ofac_recent_actions(html, "https://ofac.treasury.gov/recent-actions", max_items=10)
    assert rows[0]["title"] == "Issuance of Venezuela-related General License"
    assert rows[0]["url"] == "https://ofac.treasury.gov/recent-actions/20260505"
    assert rows[0]["category"] == "General Licenses"
    assert rows[0]["published_at"] is not None


def test_parse_whitehouse_briefings_list():
    html = '''<a href="https://www.whitehouse.gov/briefings-statements/2026/05/example/">Example Statement</a><time datetime="2026-05-05T15:39:57-04:00">May 5, 2026</time>'''
    rows = c.parse_whitehouse_briefings(html, "https://www.whitehouse.gov/briefings-statements/", max_items=10)
    assert rows[0]["title"] == "Example Statement"
    assert rows[0]["url"].endswith("/example/")
