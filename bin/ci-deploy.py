#!/usr/bin/env python3

import sys
import os
import os.path
import pickle
import git
import requests
import shlex
import subprocess
import time


ASSET_FILES = [
    ".github/workflows/assets.yml",
    "public",
    "ui",
    "package.json",
    "yarn.lock",
]

ASSET_BUILDS_URL = "https://api.github.com/repos/ornicar/lila/actions/workflows/assets.yml/runs"


def hash_files(tree, files):
    return tuple(tree[path].hexsha for path in files)


def find_commits(commit, files, wanted_hash):
    try:
        if hash_files(commit.tree, files) != wanted_hash:
            return
    except KeyError:
        return

    yield commit.hexsha

    for parent in commit.parents:
        yield from find_commits(parent, files, wanted_hash)


def workflow_runs(session, repo):
    with open(os.path.join(repo.common_dir, "workflow_runs.pickle"), "ab+") as f:
        try:
            f.seek(0)
            data = pickle.load(f)
        except EOFError:
            print("Created workflow run database.")
            data = {}

        try:
            new = 0
            synced = False
            url = ASSET_BUILDS_URL

            while not synced:
                print("Fetching workflow runs ...")
                res = session.get(url)
                if res.status_code != 200:
                    print(f"Unexpected response: {res.status_code} {res.text}")
                    break

                for run in res.json()["workflow_runs"]:
                    if run["id"] in data and data[run["id"]]["status"] == "completed":
                        synced = True
                    else:
                        new += 1
                    data[run["id"]] = run

                if "next" not in res.links:
                    break
                url = res.links["next"]["url"]
        finally:
            f.seek(0)
            f.truncate()
            pickle.dump(data, f)
            print(f"Added/updated {new} workflow run(s).")

        return data


def find_workflow_run(runs, wanted_commits):
    found = None

    print("Matching workflow runs:")
    for run in runs.values():
        if run["head_commit"]["id"] not in wanted_commits:
            continue

        if run["status"] != "completed":
            print(f"- {run['html_url']} pending.")
        elif run["conclusion"] != "success":
            print(f"- {run['html_url']} failed.")
        else:
            print(f"- {run['html_url']} succeeded.")
            if found is None:
                found = run

    if found is None:
        raise RuntimeError("Did not find successful matching workflow run.")

    print(f"Selected {found['html_url']}.")
    return found


def artifact_url(session, run, name):
    for artifact in session.get(run["artifacts_url"]).json()["artifacts"]:
        if artifact["name"] == name:
            if artifact["expired"]:
                print("Artifact expired.")
            return artifact["archive_download_url"]

    raise RuntimeError(f"Did not find artifact {name}.")


def main():
    try:
        github_api_token = os.environ["GITHUB_API_TOKEN"]
    except KeyError:
        print("Need environment variable GITHUB_API_TOKEN. See https://github.com/settings/tokens/new. Scope public_repo.")
        return 128

    session = requests.Session()
    session.headers["Authorization"] = f"token {github_api_token}"

    repo = git.Repo(search_parent_directories=True)
    runs = workflow_runs(session, repo)

    try:
        wanted_hash = hash_files(repo.head.commit.tree, ASSET_FILES)
    except KeyError:
        print("Commit is missing asset file.")
        return 1

    wanted_commits = set(find_commits(repo.head.commit, ASSET_FILES, wanted_hash))
    print(f"Found {len(wanted_commits)} matching commits.")

    run = find_workflow_run(runs, wanted_commits)
    url = artifact_url(session, run, "lila-assets")

    print(f"Deploying {url} to khiaw ...")
    time.sleep(1)
    header = f"Authorization: {session.headers['Authorization']}"
    artifact_target = f"/home/lichess-artifacts/lila-assets-{run['id']:d}.zip"
    command = ";".join([
        f"mkdir -p /home/lichess-artifacts",
        f"mkdir -p /home/lichess-deploy",
        f"wget --header={shlex.quote(header)} -O {shlex.quote(artifact_target)} --no-clobber {shlex.quote(url)}",
        f"unzip -q -o {shlex.quote(artifact_target)} -d /home/lichess-artifacts/lila-assets-{run['id']:d}",
        f"cat /home/lichess-artifacts/lila-assets-{run['id']:d}/commit.txt",
        f"ln -f -s /home/lichess-artifacts/lila-assets-{run['id']:d}/public /home/lichess-deploy/public",
        "/bin/bash",
    ])
    return subprocess.call(["ssh", "-t", "root@khiaw.lichess.ovh", "tmux", "new-session", "-s", "lila-deploy", f"/bin/sh -c {shlex.quote(command)}"], stdout=sys.stdout, stdin=sys.stdin)


if __name__ == "__main__":
    sys.exit(main())
