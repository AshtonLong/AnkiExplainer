"""Run the in-Anki integration self-test.

Creates a throwaway Anki base folder, installs the add-on (copied from
``addon/``) plus the self-test driver, launches Anki, and prints the results.
Your real Anki profile is never touched.

    python scripts/run_anki_selftest.py            # uses the Python running this script
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "tests", "_artifacts")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    base = tempfile.mkdtemp(prefix="anki-explainer-selftest-")
    addons = os.path.join(base, "addons21")
    os.makedirs(addons)
    shutil.copytree(os.path.join(ROOT, "addon"), os.path.join(addons, "anki_explainer"),
                    ignore=shutil.ignore_patterns("__pycache__", "meta.json", "user_files"))
    os.makedirs(os.path.join(addons, "anki_explainer", "user_files"))
    shutil.copytree(os.path.join(ROOT, "tests", "integration", "selftest_addon"), os.path.join(addons, "zz_selftest"))
    # Empty the folder rather than delete it: sync clients (e.g. OneDrive) can
    # hold a handle on the directory itself, which makes rmdir fail on Windows.
    os.makedirs(ART, exist_ok=True)
    for name in os.listdir(ART):
        p = os.path.join(ART, name)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    env = dict(os.environ)
    env.update({
        "EXPLAINER_SELFTEST": "1",
        "EXPLAINER_SELFTEST_OUT": ART,
        "EXPLAINER_TESTS_DIR": os.path.join(ROOT, "tests"),
        "ANKI_SINGLE_INSTANCE_KEY": "anki-explainer-selftest",
    })
    # Skip the first-run language picker and Anki's own update prompt in the
    # throwaway profile (both would steal focus from the scripted session).
    launcher = (
        "import sys, aqt, aqt.profiles;"
        "aqt.profiles.ProfileManager.setDefaultLang = lambda self, idx: self.setLang('en_US');"
        "aqt.profiles.ProfileManager.check_for_updates = lambda self: False;"
        "sys.argv = ['anki', '-b', sys.argv[1]];"
        "aqt.run()"
    )
    cmd = [sys.executable, "-c", launcher, base]
    print("base folder:", base)
    proc = subprocess.run(cmd, env=env, timeout=600)
    path = os.path.join(ART, "selftest.json")
    if not os.path.exists(path):
        print("no results written; exit code", proc.returncode)
        return 1
    with open(path, encoding="utf-8") as f:
        results = json.load(f)
    failed = [r for r in results if not r["ok"]]
    for r in results:
        print(("PASS " if r["ok"] else "FAIL ") + r["name"] + ("" if r["ok"] else f"  -> {r['detail']}"))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed. Screenshots in {ART}")
    shutil.copy(os.path.join(base, "addons21", "anki_explainer", "user_files", "explainer.log"), ART) if os.path.exists(
        os.path.join(base, "addons21", "anki_explainer", "user_files", "explainer.log")) else None
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
