import importlib.util
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_build():
    spec = importlib.util.spec_from_file_location("build_addon", os.path.join(ROOT, "scripts", "build_addon.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_package_contents(tmp_path):
    out = tmp_path / "anki_explainer.ankiaddon"
    names = load_build().build(str(out))
    with zipfile.ZipFile(out) as zf:
        members = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
    assert set(names) == members
    for required in (
        "__init__.py", "manifest.json", "config.json", "config.md",
        "explainer/hooks.py", "explainer/controller.py",
        "explainer/context/extractor.py", "explainer/llm/openrouter.py",
        "explainer/ui/panel.py", "explainer/ui/settings_dialog.py",
        "web/panel.html", "web/panel.css", "web/panel.js", "web/bottombar.js",
        "web/vendor/marked.min.js", "web/vendor/purify.min.js",
        "web/vendor/LICENSE.marked.md", "web/vendor/LICENSE.dompurify.txt",
        "user_files/README.txt", "LICENSE",
    ):
        assert required in members, required
    assert not any("__pycache__" in m or m.endswith(".pyc") for m in members)
    assert "meta.json" not in members
    assert not any(m.startswith("user_files/") and m != "user_files/README.txt" for m in members)
    assert manifest["min_point_version"] == 250200 and manifest["package"] == "anki_explainer"


def test_web_exports_cover_assets():
    import re

    pattern = re.compile(r"web/.*\.(js|css|html|svg|png)")
    for name in ("web/panel.js", "web/panel.css", "web/panel.html", "web/vendor/marked.min.js", "web/bottombar.css"):
        assert pattern.fullmatch(name)


def test_default_config_matches_spec_appendix_a():
    with open(os.path.join(ROOT, "addon", "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["model"] == "~deepseek/deepseek-flash-latest"
    assert cfg["reasoning"]["hint"] == {"effort": "none"}
    assert cfg["reasoning"]["concise"] == {"effort": "minimal"}
    assert cfg["reasoning"]["followup"] == {"effort": "low"}
    assert cfg["reasoning"]["deep"] == {"effort": "medium"}
    assert cfg["output"]["max_tokens"] == {"hint": 300, "concise": 1200, "followup": 2500, "deep": 4000}
    assert cfg["output"]["temperature"] == 0.3
    assert cfg["shortcuts"]["explain"] == "X" and cfg["shortcuts"]["focus_input"] == "Shift+X"
    assert cfg["shortcuts"]["chips"] == ["Alt+1", "Alt+2", "Alt+3", "Alt+4", "Alt+5", "Alt+6"]
    assert cfg["behavior"]["panel_width"] == 380 and cfg["behavior"]["font_size"] == 15
    assert cfg["context"]["max_images"] == 4 and cfg["context"]["cache_retention_days"] == 90
    assert "api_key" not in json.dumps(cfg).lower()
