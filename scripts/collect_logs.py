#!/usr/bin/env python3
"""Collect only known log directories, never traverse Windows profile junctions."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
work = root / "work"
if work.is_dir():
    for run in work.iterdir():
        if not run.is_dir() or run.is_symlink():
            continue
        candidates = [run / "docker-build.log"]
        logs = run / "logs"
        if logs.is_dir() and not logs.is_symlink():
            candidates.extend(logs.glob("*.log"))
        for log in candidates:
            if log.is_file() and not log.is_symlink():
                out = root / "diagnostics" / run.name / log.name
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(log, out)
