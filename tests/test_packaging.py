"""The version is written in four places, and three of them are not tested.

The release workflow already refuses a git tag that disagrees with main.VERSION,
but it only ever sees a tagged build. Nothing looked at the Dockerfile's own ARG
default, which is what a local `docker build .` and the CI image job use - so a
bump to main.VERSION left an image running 1.0.0 code and reporting 0.9.1 from
/healthz, the exact failure the version field exists to prevent, with no tag
involved for the workflow to catch.

The compose example and the README quick start are here for the same reason from
the other direction: they are the first two files anyone copies from, and an
image reference in either one that no release ever published is a quick start
that fails on docker pull.
"""

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def only(pattern, text, what):
    found = re.findall(pattern, text, re.M)
    assert len(found) == 1, f"expected exactly one {what}, found {found}"
    return found[0].strip()


class Version(unittest.TestCase):
    def setUp(self):
        # Read out of the source rather than imported, so this stays a test of
        # what the file SAYS. main.VERSION is now a plain constant and no
        # environment variable can move it - which is the fix for a container
        # that reported 1.0.0 while running 1.0.1 - but reading the literal is
        # still what catches a bump that edited only one of the four places.
        self.version = only(r'^VERSION = "([^"]+)"$', read("app/main.py"),
                            "VERSION constant in main.py")

    def test_nothing_outside_the_image_can_move_the_version(self):
        # The regression this guards: Container Station rebuilds a container
        # from the environment it recorded at create, so an explicit
        # TRANSCODEARR_VERSION outlives every image update and /healthz reports
        # the build the container was FIRST made from, forever.
        # Matched on the code, not the word: the comment above the constant
        # explains this regression by name and must stay sayable.
        self.assertNotRegex(read("app/main.py"), r'environ\S*\(\s*"TRANSCODEARR_VERSION"',
                            "main.py reads the version from the environment again - an env var is the "
                            "one part of a container that survives being rebuilt on a newer image")
        # re.M or the anchor only ever tries the first line of the Dockerfile,
        # and the assertion passes without looking at the ENV it is about.
        self.assertNotRegex(read("Dockerfile"), re.compile(r"^ENV TRANSCODEARR_VERSION", re.M),
                            "the image sets TRANSCODEARR_VERSION again, which is what froze /healthz")

    def test_the_dockerfile_default_builds_an_image_that_reports_the_truth(self):
        arg = only(r"^ARG VERSION=(.+)$", read("Dockerfile"), "ARG VERSION")
        self.assertEqual(arg, self.version,
                         "Dockerfile ARG VERSION and main.VERSION disagree, so an untagged "
                         "build reports a version it is not running")

    def publishable(self):
        """Every tag a release of this version actually pushes.

        The four the metadata-action matrix produces in .github/workflows/
        release.yml: latest, and the three semver widths. Anything else in a
        file people copy from is a docker pull that answers "not found".
        """
        major, minor, _patch = self.version.split(".")
        return {"latest", self.version, f"{major}.{minor}", major}

    def test_the_example_compose_pulls_a_tag_that_will_exist(self):
        tag = only(r"image: ghcr\.io/managearr/transcodearr:(\S+)",
                   read("docker-compose.example.yml"), "image reference")
        self.assertIn(tag, self.publishable())

    def test_the_readme_pulls_tags_that_will_exist(self):
        pulls = set(re.findall(r"ghcr\.io/managearr/transcodearr:(\S+)", read("README.md")))
        self.assertTrue(pulls, "the README names no image to pull at all")
        self.assertEqual(set(), pulls - self.publishable())

    def test_the_changelog_leads_with_this_version(self):
        heading = re.search(r"^## \[([^\]]+)\]", read("CHANGELOG.md"), re.M)
        self.assertEqual(heading.group(1), self.version,
                         "the newest CHANGELOG section is not the version this builds")


if __name__ == "__main__":
    unittest.main()
