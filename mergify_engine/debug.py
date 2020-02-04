# -*- encoding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import pprint

import github
import requests

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import rules
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.tasks.engine import actions_runner


def get_repositories_setuped(token, install_id):  # pragma: no cover
    repositories = []
    url = "https://api.%s/user/installations/%s/repositories" % (
        config.GITHUB_DOMAIN,
        install_id,
    )
    token = "token {}".format(token)
    session = requests.Session()
    while True:
        response = session.get(
            url,
            headers={
                "Authorization": token,
                "Accept": "application/vnd.github.machine-man-preview+json",
                "User-Agent": "PyGithub/Python",
            },
        )
        if response.status_code == 200:
            repositories.extend(response.json()["repositories"])
            if "next" in response.links:
                url = response.links["next"]["url"]
                continue
            else:
                return repositories
        elif response.status_code == 403:
            raise github.BadCredentialsException(
                status=response.status_code, data=response.text
            )
        elif response.status_code == 404:
            raise github.UnknownObjectException(
                status=response.status_code, data=response.text
            )
        raise github.GithubException(status=response.status_code, data=response.text)


def create_jwt():
    integration = github.GithubIntegration(config.INTEGRATION_ID, config.PRIVATE_KEY)
    return integration.create_jwt()


def report_sub(install_id, slug, sub, title):
    print(f"* {title} SUB DETAIL: {sub['subscription_reason']}")
    print(f"* {title} SUB NUMBER OF TOKENS: {len(sub['tokens'])}")

    try:
        for login, token in sub["tokens"].items():
            try:
                repos = get_repositories_setuped(token, install_id)
            except github.BadCredentialsException:
                print(f"* {title} SUB: token for {login} is invalid (BadCreds)")
            except github.GithubException as e:
                if e.status != 401:
                    raise
                print(f"* {title} SUB: token for {login} is invalid (401)")
            else:
                if any((r["full_name"] == slug) for r in repos):
                    print(
                        f"* {title} SUB: MERGIFY INSTALLED AND ENABLED ON THIS REPOSITORY"
                    )
                else:
                    print(
                        f"* {title} SUB: MERGIFY INSTALLED BUT DISABLED ON THIS REPOSITORY"
                    )
                break
        else:
            print(f"* {title} SUB: MERGIFY DOESN'T HAVE ANY VALID OAUTH TOKENS")
    except github.UnknownObjectException:
        print(f"* {title} SUB: MERGIFY SEEMS NOT INSTALLED")


def report(url):
    redis = utils.get_redis_for_cache()
    path = url.replace("https://github.com/", "")
    try:
        owner, repo, _, pull_number = path.split("/")
    except ValueError:
        print(f"Wrong URL: {url}")
        return
    slug = owner + "/" + repo

    integration = github.GithubIntegration(config.INTEGRATION_ID, config.PRIVATE_KEY)
    install_id = utils.get_installation_id(integration, owner, repo=repo)

    print("* INSTALLATION ID: %s" % install_id)

    cached_sub = sub_utils.get_subscription(redis, install_id)
    db_sub = sub_utils._retrieve_subscription_from_db(install_id)
    print(
        "* SUBSCRIBED (cache/db): %s / %s"
        % (cached_sub["subscription_active"], db_sub["subscription_active"])
    )
    report_sub(install_id, slug, cached_sub, "ENGINE-CACHE")
    report_sub(install_id, slug, db_sub, "DASHBOARD")

    installation_token = integration.get_access_token(install_id).token

    g = github.Github(
        installation_token, base_url="https://api.%s" % config.GITHUB_DOMAIN
    )
    r = g.get_repo(owner + "/" + repo)
    print("* REPOSITORY IS %s" % "PRIVATE" if r.private else "PUBLIC")

    print("* CONFIGURATION:")
    try:
        mergify_config_content = rules.get_mergify_config_content(r)
    except rules.NoRules:  # pragma: no cover
        print(".mergify.yml is missing")
    else:
        print(mergify_config_content.decode())

    try:
        mergify_config = rules.UserConfigurationSchema(mergify_config_content)
    except rules.InvalidRules as e:  # pragma: no cover
        print("configuration is invalid %s" % str(e))
    else:
        pull_request_rules_raw = mergify_config["pull_request_rules"].as_dict()
        pull_request_rules_raw["rules"].extend(actions_runner.MERGIFY_RULE["rules"])
        pull_request_rules = rules.PullRequestRules(**pull_request_rules_raw)

    try:
        p = r.get_pull(int(pull_number))
    except github.UnknownObjectException:
        print("Wrong pull request number")
        return g, None

    mp = mergify_pull.MergifyPull(g, p, install_id)
    print("* PULL REQUEST:")
    pprint.pprint(mp.to_dict(), width=160)
    try:
        print("is_behind: %s" % mp.is_behind())
    except github.GithubException as e:
        print("Unable to know if pull request branch is behind: %s" % e)

    print("mergeable_state: %s" % mp.g_pull.mergeable_state)

    print("* MERGIFY LAST CHECKS:")
    checks = list(check_api.get_checks(p))
    for c in checks:
        if c._rawData["app"]["id"] == config.INTEGRATION_ID:
            print("[%s]: %s | %s" % (c.name, c.conclusion, c.output.get("title")))
            print("> " + "\n> ".join(c.output.get("summary").split("\n")))

    print("* MERGIFY LIVE MATCHES:")
    match = pull_request_rules.get_pull_request_rule(mp)
    summary_title, summary = actions_runner.gen_summary("refresh", {}, mp, match)
    print("> %s" % summary_title)
    print(summary)

    return g, p


def main():
    parser = argparse.ArgumentParser(description="Debugger for mergify")
    parser.add_argument("url", help="Pull request url")
    args = parser.parse_args()
    report(args.url)
