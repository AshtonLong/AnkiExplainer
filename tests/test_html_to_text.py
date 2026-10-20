from explainer.context.html_to_text import html_to_text, truncate_middle


def t(html):
    return html_to_text(html).text


def test_scripts_styles_hidden_removed():
    html = "<style>.x{}</style><script>alert(1)</script>Hi<div style='display:none'>secret</div><span hidden>no</span> there"
    assert t(html) == "Hi there"


def test_inline_formatting():
    assert t("<b>bold</b> and <i>it</i> and <strong>s</strong>") == "**bold** and *it* and **s**"
    assert t("H<sub>2</sub>O and x<sup>10</sup>") == "H_2O and x^{10}"


def test_breaks_and_blocks():
    assert t("a<br>b<div>c</div><p>d</p>e") == "a\nb\nc\nd\ne"


def test_lists():
    out = t("<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul><ol><li>x</li><li>y</li></ol>")
    assert out.splitlines() == ["- one", "- two", "  - nested", "1. x", "2. y"]


def test_table_to_markdown():
    out = t("<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2|3</td></tr></table>")
    assert out.splitlines() == ["| a | b |", "|---|---|", r"| 1 | 2\|3 |"]


def test_code_fenced_and_inline():
    res = html_to_text('<pre><code class="language-py">x = 1\n  y = 2</code></pre> use <code>foo()</code>')
    assert "```py\nx = 1\n  y = 2\n```" in res.text
    assert "`foo()`" in res.text
    assert res.has_code


def test_mathjax_kept_verbatim():
    res = html_to_text(r"Solve \(x^2 &lt; 4\) and \[\int_0^1 x\,dx\]")
    assert r"\(x^2 < 4\)" in res.text
    assert r"\[\int_0^1 x\,dx\]" in res.text
    assert res.has_math


def test_images_collected_with_markers():
    res = html_to_text('a <img src="one.png"> b <img src="two.jpg" alt="Heart">')
    assert res.text == "a [image 1] b [image 2: Heart]"
    assert [i.src for i in res.images] == ["one.png", "two.jpg"]


def test_entities_and_whitespace():
    assert t("a&nbsp;&nbsp;b \n\n  c &amp; d") == "a b c & d"


def test_controls_dropped():
    assert t("<div>Q</div><button>Toggle Masks</button><canvas></canvas>") == "Q"


def test_truncate_middle():
    text = "a" * 5000 + "b" * 5000
    out, truncated = truncate_middle(text, 8000)
    assert truncated
    assert out.startswith("a" * 4000) and out.endswith("b" * 4000)
    assert "2000 characters truncated" in out


def test_cap_applied():
    res = html_to_text("x" * 9000)
    assert res.truncated and len(res.text) < 8100
