"""Publish completed course work from its source repo to the website and live server.

Standard-library only. Configuration lives outside Git in .git/course-publisher.json.
Run --preview to validate and show the snapshot without network or publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from html.parser import HTMLParser

CONTENT = "GESP_Python一级_全套32讲互动课件与教案"
ALLOWED_ASSETS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".woff", ".woff2"}


def run(args, cwd=None, capture=False):
    # Hooks inherit GIT_DIR/INDEX_FILE; never let those redirect website operations.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True,
                            stdout=subprocess.PIPE if capture else None,
                            encoding="utf-8", errors="replace")
    return result.stdout.strip() if capture else None


def source_file(root, relative):
    if not relative or "\\" in relative or Path(relative).is_absolute():
        raise ValueError(f"Invalid source path: {relative}")
    result = (root / relative).resolve()
    result.relative_to(root.resolve())
    if not result.is_file():
        raise ValueError(f"Missing source file: {relative}")
    return result


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and (name == "src" or (tag == "link" and name == "href")):
                self.urls.append(value)


def snapshot(source, destination):
    data = json.loads(source_file(source, "课程发布.json").read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1 or data.get("track") != "python" or not data.get("stages"):
        raise ValueError("Unsupported or empty curriculum manifest")
    files = {}
    keys, numbers = set(), set()

    def copy(relative, prefix):
        original = source_file(source, relative)
        output = destination / prefix / relative
        output.resolve().relative_to(destination.resolve())
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, output)
        files[output.relative_to(destination).as_posix()] = hashlib.sha256(output.read_bytes()).hexdigest()
        return relative

    for stage in data["stages"]:
        for item in [stage, *stage["lessons"]]:
            key = item["key"]
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,99}", key) or key in keys:
                raise ValueError(f"Invalid or repeated stable key: {key}")
            keys.add(key)
            if not item.get("title") or len(item["title"]) > 110:
                raise ValueError(f"Invalid title for {key}")
        for lesson in stage["lessons"]:
            number = lesson["number"]
            if type(number) is not int or number < 1 or number in numbers:
                raise ValueError("Lesson numbers must be positive and unique")
            numbers.add(number)
            if lesson.get("status") not in {"published", "planned"}:
                raise ValueError("Invalid lesson status")
            if lesson["status"] == "planned":
                if lesson.get("slide") or lesson.get("plan"):
                    raise ValueError("A planned lesson must not expose draft material")
                continue
            if not lesson.get("slide") or not lesson.get("plan") or not lesson.get("spec"):
                raise ValueError(f"Completed lesson requires slide, plan and specification: {lesson['key']}")
            slide = source_file(source, lesson["slide"])
            if slide.suffix.lower() != ".html":
                raise ValueError("A slide must be HTML")
            copy(lesson["slide"], "03_互动课件")
            for field in ["plan", "spec"]:
                file = source_file(source, lesson[field])
                if file.suffix.lower() != ".md" or (field == "plan" and "教案" not in file.name):
                    raise ValueError(f"Invalid {field} file")
                copy(lesson[field], "02_课次研发")
            parser = AssetParser()
            html = slide.read_text(encoding="utf-8-sig")
            parser.feed(html)
            parser.urls.extend(re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", html))
            for url in parser.urls:
                parsed = urlsplit(url)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                if parsed.path.startswith("/"):
                    raise ValueError(f"Local asset must be relative: {url}")
                asset = (slide.parent / unquote(parsed.path)).resolve()
                relative = asset.relative_to(source.resolve()).as_posix()
                if asset.suffix.lower() not in ALLOWED_ASSETS:
                    raise ValueError(f"Unsupported local slide asset: {url}")
                copy(relative, "03_互动课件")
    for directory in data.get("asset_dirs", []):
        folder = (source / directory).resolve()
        folder.relative_to(source.resolve())
        if not folder.is_dir():
            raise ValueError(f"Missing asset folder: {directory}")
        for asset in folder.rglob("*"):
            if asset.is_file() and asset.suffix.lower() in ALLOWED_ASSETS:
                copy(asset.relative_to(source).as_posix(), "03_互动课件")
    for document in data.get("documents", []):
        if Path(document).suffix.lower() != ".md":
            raise ValueError("A course document must be Markdown")
        copy(document, "01_课程设计")
    data["files"] = dict(sorted(files.items()))
    data["source_revision"] = run(["git", "rev-parse", "HEAD"], source, True)
    # Source docs are published in their own folder, while slide paths stay relative.
    (destination / "课程发布.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    # Snapshot a completed, committed source version; drafts cannot enter a release.
    with tempfile.TemporaryDirectory(prefix="course-release-") as temp:
        staged = Path(temp) / CONTENT
        staged.mkdir()
        release = snapshot(source, staged)
        if args.preview:
            print(json.dumps({"lessons": len([x for s in release['stages'] for x in s['lessons']]),
                              "files": len(release['files']), "source_revision": release['source_revision']}, ensure_ascii=False))
            return
        if args.snapshot:
            args.snapshot.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged, args.snapshot, dirs_exist_ok=True)
            return
        git_dir = Path(run(["git", "rev-parse", "--absolute-git-dir"], source, True))
        config = json.loads((git_dir / "course-publisher.json").read_text(encoding="utf-8"))
        if run(["git", "branch", "--show-current"], source, True) != "main":
            raise ValueError("Automatic publication is only enabled on the source main branch")
        managed = ["课程发布.json", *release.get("documents", [])]
        managed.extend(p for s in release["stages"] for l in s["lessons"]
                       for p in [l.get("slide"), l.get("plan"), l.get("spec")] if p)
        managed.extend(release.get("asset_dirs", []))
        if run(["git", "status", "--porcelain", "--", *managed], source, True):
            raise ValueError("Complete and commit the course files before publication; draft changes remain local")
        run(["git", "push", "origin", "main"], source)
        cache = Path(config["cache"]).resolve()
        cache.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive lock covers Git push + live deployment, including source hooks.
        lock = git_dir / "course-publisher.lock"
        try:
            lock.mkdir()
        except FileExistsError:
            raise ValueError(f"Another publication is running; lock: {lock}")
        try:
            if not (cache / ".git").is_dir():
                run(["git", "clone", "--branch", "main", config["website_remote"], cache])
            if run(["git", "remote", "get-url", "origin"], cache, True) != config["website_remote"]:
                raise ValueError("Publisher cache has a different remote")
            if run(["git", "status", "--porcelain"], cache, True):
                raise ValueError("Publisher cache contains changes; refusing to overwrite them")
            run(["git", "pull", "--ff-only", "origin", "main"], cache)
            content = cache / CONTENT
            content.mkdir(exist_ok=True)
            # Delete only previously tracked course files absent from this release.
            published = {str(p.relative_to(staged)).replace("\\", "/") for p in staged.rglob("*") if p.is_file()}
            tracked = run(["git", "-c", "core.quotePath=false", "ls-files", "--", CONTENT], cache, True)
            for name in tracked.splitlines():
                path = cache / name
                relative = path.relative_to(content).as_posix()
                if relative not in published:
                    path.resolve().relative_to(content.resolve())
                    path.unlink(missing_ok=True)
            shutil.copytree(staged, content, dirs_exist_ok=True)
            run(["git", "add", "--", CONTENT], cache)
            if run(["git", "diff", "--cached", "--name-only"], cache, True):
                run(["git", "commit", "-m", f"content: publish curriculum {release['source_revision'][:12]}"], cache)
            run(["git", "push", "origin", "main"], cache)
            target = run(["git", "rev-parse", "HEAD"], cache, True)
            run(["ssh", "-i", config["ssh_key"], "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                 config["server"], f"bash {config['server_repo']}/deploy/publish-course.sh {target}"])
            local_site = Path(config["local_website"])
            if local_site.exists():
                # Fast-forward only; preserve unrelated in-progress development.
                run(["git", "fetch", "origin", "main"], local_site)
                run(["git", "merge", "--ff-only", target], local_site)
            state = {"website_revision": target, "source_revision": release["source_revision"], "status": "published"}
            (git_dir / "course-publisher-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
            print("Course publication completed: local website, GitHub and live site updated.")
        finally:
            lock.rmdir()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Course publication failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
