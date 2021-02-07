# Copyright © 2018–2020 Endless Mobile, Inc.
#
# Authors:
#       Joaquim Rocha <jrocha@endlessm.com>
#       Will Thompson <wjt@endlessm.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import datetime as dt
import glob
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import urllib.request
import urllib.parse
import copy
import io
import typing as t

from collections import OrderedDict
from ruamel.yaml import YAML
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)
from elftools.elf.elffile import ELFFile

from . import externaldata

import gi

gi.require_version("Json", "1.0")
from gi.repository import GLib, Json  # noqa: E402

log = logging.getLogger(__name__)

# With the default urllib User-Agent, dl.discordapp.net returns 403
USER_AGENT = (
    "flatpak-external-data-checker "
    "(+https://github.com/flathub/flatpak-external-data-checker)"
)
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT_SECONDS = 60


def _extract_timestamp(info):
    date_str = info["Last-Modified"] or info["Date"]
    if date_str:
        try:
            return dt.datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %Z")
        except ValueError:
            return dt.datetime.strptime(date_str, "%a, %d-%b-%Y %H:%M:%S %Z")
    else:
        return dt.datetime.now()  # what else can we do?


def strip_query(url):
    """Sanitizes the query string from the given URL, if any. Parameters whose
    names begin with an underscore are assumed to be tracking identifiers and
    are removed."""
    parts = urllib.parse.urlparse(url)
    if not parts.query:
        return url
    qsl = urllib.parse.parse_qsl(parts.query)
    qsl_stripped = [(k, v) for (k, v) in qsl if not k.startswith("_")]
    query_stripped = urllib.parse.urlencode(qsl_stripped)
    stripped = urllib.parse.urlunparse(parts._replace(query=query_stripped))
    log.debug("Normalised %s to %s", url, stripped)
    return stripped


def get_timestamp_from_url(url):
    request = urllib.request.Request(url, headers=HEADERS, method="HEAD")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return _extract_timestamp(response.info())


@retry(
    retry=retry_if_exception_type((ConnectionResetError, socket.timeout)),
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    before_sleep=before_sleep_log(log, logging.DEBUG),
)
def get_extra_data_info_from_url(url, follow_redirects=True):
    request = urllib.request.Request(url, headers=HEADERS)

    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        real_url = response.geturl()
        data = response.read()
        info = response.info()

    if "Content-Length" in info:
        size = int(info["Content-Length"])
    else:
        size = len(data)

    checksum = hashlib.sha256(data).hexdigest()
    external_file = externaldata.ExternalFile(
        strip_query(real_url if follow_redirects else url),
        checksum,
        size,
        None,
        _extract_timestamp(info),
    )

    return external_file, data


def clear_env(environ):
    new_env = copy.deepcopy(environ)
    for varname in new_env.keys():
        if any(i in varname.lower() for i in ["pass", "token", "secret", "auth"]):
            log.debug("Removing env %s", varname)
            new_env.pop(varname)
    return new_env


def wrap_in_bwrap(cmdline, bwrap_args=None):
    bwrap_cmd = ["bwrap", "--unshare-all"]
    for path in ("/usr", "/lib", "/lib64", "/bin", "/proc"):
        bwrap_cmd.extend(["--ro-bind", path, path])
    if bwrap_args is not None:
        bwrap_cmd.extend(bwrap_args)
    return bwrap_cmd + ["--"] + cmdline


def run_command(argv, cwd=None, env=None, bwrap=True, bwrap_args=None):
    if bwrap:
        command = wrap_in_bwrap(argv, bwrap_args)
    else:
        command = argv
    if env is None:
        env = os.environ
    p = subprocess.run(
        command, cwd=cwd, env=env, stderr=subprocess.PIPE, encoding="utf-8"
    )
    return p


def check_bwrap():
    try:
        p = run_command(["/bin/true"])
        if p.returncode == 0:
            return True
    except FileNotFoundError:
        pass

    logging.warning("bwrap is not available")
    return False


def git_ls_remote(url: str) -> t.Dict[str, str]:
    git_cmd = ["git", "ls-remote", "--exit-code", url]
    if check_bwrap():
        git_cmd = wrap_in_bwrap(
            git_cmd,
            bwrap_args=[
                # fmt: off
                "--share-net",
                "--dev", "/dev",
                "--ro-bind", "/etc/ssl", "/etc/ssl",
                "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
                # fmt: on
            ],
        )
    git_proc = subprocess.run(
        git_cmd,
        check=True,
        stdout=subprocess.PIPE,
        env=clear_env(os.environ),
        timeout=5,
    )
    git_stdout = git_proc.stdout.decode()

    return {r: c for c, r in (l.split() for l in git_stdout.splitlines())}


def extract_appimage_version(basename, data):
    """
    Saves 'data' to a temporary file with the given basename, executes it (in a sandbox)
    with --appimage-extract to unpack it, and scrapes the version number out of the
    first .desktop file it finds.
    """

    with tempfile.TemporaryDirectory() as tmpdir:
        appimage_path = os.path.join(tmpdir, basename)

        with io.BytesIO(data) as appimg_io:
            header = ELFFile(appimg_io).header
            offset = header["e_shoff"] + header["e_shnum"] * header["e_shentsize"]
            appimg_io.seek(offset)

            log.info("Writing %s to %s with offset %i", basename, appimage_path, offset)

            with open(appimage_path, "wb") as fp:
                fp.write(appimg_io.read())

        bwrap = check_bwrap()
        bwrap_args = [
            "--bind",
            tmpdir,
            tmpdir,
            "--die-with-parent",
            "--new-session",
        ]

        args = ["unsquashfs", "-no-progress", appimage_path]

        log.debug("$ %s", " ".join(args))
        p = run_command(
            args,
            cwd=tmpdir,
            env=clear_env(os.environ),
            bwrap=bwrap,
            bwrap_args=bwrap_args,
        )

        if p.returncode != 0:
            log.error("AppImage extraction failed\n%s", p.stderr)
            p.check_returncode()

        for desktop in glob.glob(os.path.join(tmpdir, "squashfs-root", "*.desktop")):
            kf = GLib.KeyFile()
            kf.load_from_file(desktop, GLib.KeyFileFlags.NONE)
            return kf.get_string(GLib.KEY_FILE_DESKTOP_GROUP, "X-AppImage-Version")


_GITHUB_URL_PATTERN = re.compile(
    r"""
        ^git@github.com:
        (?P<org_repo>[^/]+/[^/]+?)
        (?:\.git)?$
    """,
    re.VERBOSE,
)


def parse_github_url(url):
    """
    Parses the organization/repo part out of a git remote URL.
    """
    if url.startswith("https:"):
        o = urllib.parse.urlparse(url)
        return o.path[1:]

    m = _GITHUB_URL_PATTERN.match(url)
    if m:
        return m.group("org_repo")
    else:
        raise ValueError(f"{url!r} doesn't look like a Git URL")


def read_json_manifest(manifest_path):
    """Read manifest from 'manifest_path', which may contain C-style
    comments or multi-line strings (accepted by json-glib and hence
    flatpak-builder, but not Python's json module)."""

    # Round-trip through json-glib to get rid of comments, multi-line
    # strings, and any other invalid JSON
    parser = Json.Parser()
    parser.load_from_file(manifest_path)
    root = parser.get_root()
    clean_manifest = Json.to_string(root, False)

    return json.loads(clean_manifest, object_pairs_hook=OrderedDict)


_yaml = YAML()
# ruamel preserves some formatting (such as comments and blank lines) but
# not the indentation of the source file. These settings match the style
# recommended at <https://github.com/flathub/flathub/wiki/YAML-Style-Guide>.
_yaml.indent(mapping=2, sequence=4, offset=2)


def read_yaml_manifest(manifest_path):
    """Read a YAML manifest from 'manifest_path'."""
    with open(manifest_path, "r") as f:
        return _yaml.load(f)


def read_manifest(manifest_path):
    """Reads a JSON or YAML manifest from 'manifest_path'."""
    _, ext = os.path.splitext(manifest_path)
    if ext in (".yaml", ".yml"):
        return read_yaml_manifest(manifest_path)
    else:
        return read_json_manifest(manifest_path)


def dump_manifest(contents, manifest_path):
    """Writes back 'contents' to 'manifest_path'.

    For YAML, we make a best-effort attempt to preserve
    formatting; for JSON, we use the canonical 4-space indentation."""
    _, ext = os.path.splitext(manifest_path)
    with open(manifest_path, "w", encoding="utf-8") as fp:
        if ext in (".yaml", ".yml"):
            _yaml.dump(contents, fp)
        else:
            json.dump(obj=contents, fp=fp, indent=4)


def init_logging(level=logging.DEBUG):
    logging.basicConfig(
        level=level, format="+ %(asctime)s %(levelname)7s %(name)s: %(message)s"
    )
