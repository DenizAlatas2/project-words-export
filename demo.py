#!/usr/bin/env python3
"""Run an entirely synthetic glossary discovery and export demo."""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

tool = Path(__file__).with_name("project_words.py")
with tempfile.TemporaryDirectory(prefix="project-words-demo-") as tmp:
    base = Path(tmp)
    project = base / "sample-project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "FlowEngine.py").write_text("synthetic", encoding="utf-8")
    (project / "package.json").write_text(json.dumps({"name": "@demo/FlowEngine"}), encoding="utf-8")
    config = base / "config.json"
    config.write_text(json.dumps({"root": "sample-project", "files": ["src/FlowEngine.py"], "manifests": {"package.json": ["name"]}}), encoding="utf-8")
    glossary_path = base / "glossary.json"
    subprocess.run([sys.executable, str(tool), "discover", "--config", str(config), "--glossary", str(glossary_path)], check=True)
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    candidate = next(item for item in glossary["candidates"] if item["word"] == "Flow")
    # Synthetic illustration of a user copying and confirming one candidate.
    glossary["terms"] = [{"word": candidate["word"], "sources": candidate["sources"], "confirmed": True, "aliases": ["Flo"]}]
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cspell, wispr = base / "project-words.txt", base / "wispr.csv"
    subprocess.run([sys.executable, str(tool), "export", "--glossary", str(glossary_path), "--cspell", str(cspell), "--wispr-csv", str(wispr)], check=True)
    print("CSpell:", repr(cspell.read_text(encoding="utf-8")))
    print("Wispr rows:", list(csv.reader(wispr.open(encoding="utf-8", newline=""))))
