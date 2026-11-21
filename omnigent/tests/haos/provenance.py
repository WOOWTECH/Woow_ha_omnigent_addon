"""Bind the existing PR image to its actual producer checkout and add-on tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


FILES = ("config.yaml", "Dockerfile", "build/artifact-pin.json")


def hashes(root):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in FILES}


def validate_source(receipt, head, tree, root):
    expected_head = os.environ["HAOS_HEAD_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", receipt.get("checkout_commit", "")):
        raise RuntimeError("invalid producer checkout")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt.get("image_id", "")):
        raise RuntimeError("invalid producer image ID")
    if not (receipt.get("schema") == 1 and head == expected_head == receipt.get("pr_head")
            and tree == receipt.get("addon_tree") and receipt.get("files") == hashes(root)):
        raise RuntimeError("producer add-on content differs from acceptance PR head")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    def command(argv):
        return subprocess.check_output(argv, text=True).strip()
    receipt = {
        "schema": 1,
        "pr_head": os.environ["HAOS_HEAD_SHA"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "checkout_commit": command(["git", "rev-parse", "HEAD"]),
        "addon_tree": command(["git", "rev-parse", "HEAD:omnigent"]),
        "files": hashes(Path("omnigent")),
        "image_id": command(["docker", "image", "inspect", "--format", "{{.Id}}", args.image]),
    }
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
