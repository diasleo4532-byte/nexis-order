from __future__ import annotations

from nexis.tools.web import (
    _BingParser,
    _clean_ddg_url,
    _decode_bing_url,
    _DuckDuckGoParser,
    _extract_text,
)


def test_duckduckgo_results_are_parsed():
    markup = """
    <div class="result results_links">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">
         Example <b>Page</b>
      </a>
      <a class="result__snippet">An example page about things.</a>
    </div>
    <div class="result results_links">
      <a rel="nofollow" class="result__a" href="https://second.example.org/">Second</a>
      <a class="result__snippet">Another result.</a>
    </div>
    """
    parser = _DuckDuckGoParser()
    parser.feed(markup)
    parser.close()

    assert parser.links[0][0] == "Example Page"
    assert parser.links[0][1] == "https://example.com/page"
    assert parser.links[1][1] == "https://second.example.org/"
    assert parser.snippets[0].startswith("An example page")


def test_clean_ddg_url_leaves_plain_urls_alone():
    assert _clean_ddg_url("https://example.com/a?b=1") == "https://example.com/a?b=1"


def test_bing_results_are_parsed_and_urls_decoded():
    markup = """
    <ol id="b_results">
      <li class="b_algo"><h2><a target="_blank"
        href="https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;u=a1aHR0cHM6Ly93d3cucHl0aG9uLm9yZy8&amp;ntb=1"
        h="ID=SERP,1.2">Welcome to <strong>Python.org</strong></a></h2>
        <div class="b_caption"><p class="b_lineclamp2">The mission of the Python
        Software Foundation…Read more</p></div>
      </li>
      <li class="b_algo"><h2><a href="https://second.example.org/">Second result</a></h2>
        <p class="b_lineclamp4">Another snippet.</p>
      </li>
      <li class="b_ad"><h2><a href="https://ad.example/">Advertisement</a></h2></li>
    </ol>
    """
    parser = _BingParser()
    parser.feed(markup)
    parser.close()

    assert len(parser.results) == 2
    first = parser.results[0]
    assert first["title"] == "Welcome to Python.org"
    assert first["url"] == "https://www.python.org/"
    assert "mission" in first["snippet"]
    assert not first["snippet"].endswith("Read more")
    assert parser.results[1]["url"] == "https://second.example.org/"


def test_decode_bing_url_passes_through_normal_urls():
    assert _decode_bing_url("https://example.com/x") == "https://example.com/x"


def test_extract_text_drops_scripts_and_reads_title():
    markup = """
    <html><head><title>Hello Title</title>
    <style>body { color: red }</style></head>
    <body><h1>Heading</h1><script>alert('x')</script><p>Some body text.</p></body></html>
    """
    title, text = _extract_text(markup)
    assert title == "Hello Title"
    assert "Heading" in text
    assert "Some body text." in text
    assert "alert" not in text
    assert "color: red" not in text
