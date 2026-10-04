"""手动检查公开 Release，校验完整目录包，退出后原地更新程序。"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path

import httpx

from .paths import is_frozen, resource

REPOSITORY = "https://github.com/LLL-svg-jpg/Online-Course-Assistant"
LATEST_URL = "https://api.github.com/repos/LLL-svg-jpg/Online-Course-Assistant/releases/latest"
EXE_NAME = "OnlineCourseAssistant.exe"
PROGRAM_ENTRIES = (EXE_NAME, "_internal", "README.md", "requirements.txt",
                   "安装依赖.bat", "config.example.toml", "THIRD_PARTY_NOTICES.txt")
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "OnlineCourseAssistant",
           "X-GitHub-Api-Version": "2022-11-28"}


def version_tuple(version: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("发布版本号不是可识别的正式版本。")
    return tuple(int(part) for part in version.split("."))


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    asset_url: str = ""
    size: int = 0
    sha256: str = ""

    def newer_than(self, current: str) -> bool:
        return version_tuple(self.version) > version_tuple(current)

    @property
    def installable(self) -> bool:
        return bool(self.asset_url and self.size > 0 and re.fullmatch(r"[0-9a-f]{64}", self.sha256))


def parse_release(data: dict) -> Release:
    version = str(data.get("tag_name", "")).removeprefix("v")
    version_tuple(version)
    if data.get("draft") or data.get("prerelease"):
        raise ValueError("不是已发布的正式版本。")
    page = REPOSITORY + "/releases/tag/v" + version
    name = f"OnlineCourseAssistant-v{version}-windows-x64.zip"
    for asset in data.get("assets", []):
        if asset.get("name") != name:
            continue
        url = REPOSITORY + "/releases/download/v" + version + "/" + name
        if asset.get("browser_download_url") != url:
            raise ValueError("更新附件不属于本项目的发布目录。")
        digest = str(asset.get("digest", ""))
        sha = digest.removeprefix("sha256:") if digest.startswith("sha256:") else ""
        return Release(version, page, url, int(asset.get("size", 0)), sha)
    return Release(version, page)


def check_latest() -> Release:
    with httpx.Client(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        response = client.get(LATEST_URL)
        response.raise_for_status()
        return parse_release(response.json())


def file_version(exe: Path) -> str:
    """读取 Windows PE 的版本资源，不执行下载的 EXE。"""
    library = ctypes.windll.version
    size = library.GetFileVersionInfoSizeW(str(exe), None)
    buffer = ctypes.create_string_buffer(size)
    value = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not size or not library.GetFileVersionInfoW(str(exe), 0, size, buffer) or not library.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
        raise ValueError("无法核对更新 EXE 的版本信息。")
    words = ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32))
    if length.value < 52 or words[0] != 0xFEEF04BD:
        raise ValueError("更新 EXE 的版本信息无效。")
    return ".".join(str(n) for n in (words[2] >> 16, words[2] & 65535,
                                    words[3] >> 16, words[3] & 65535))


def extract_package(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as zipped:
        seen = set()
        members = []
        for info in zipped.infolist():
            name = info.filename.rstrip("/")
            parts = name.split("/")
            bad_component = any(not p or p in (".", "..") or p.endswith((".", " "))
                                or re.search(r'[\\:<>"|?*\x00-\x1f]', p)
                                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
                                for p in parts)
            private = any(p.lower() in ("config.toml", "runtime", "answers.db", "cookies.json",
                                       "credentials.json") for p in parts[1:])
            if (parts[0] != "OnlineCourseAssistant" or bad_component or private
                    or (len(parts) > 1 and parts[1] not in PROGRAM_ENTRIES)
                    or stat.S_ISLNK(info.external_attr >> 16) or name.casefold() in seen):
                raise ValueError("更新包包含不允许的路径、个人数据或重复文件。")
            seen.add(name.casefold())
            members.append((info, parts))
        total = sum(info.file_size for info, _ in members)
        if total > 4 * 1024 ** 3 or total > shutil.disk_usage(destination.parent).free:
            raise ValueError("更新包展开大小过大，或磁盘剩余空间不足。")
        for info, parts in members:
            target = destination.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    package = destination / "OnlineCourseAssistant"
    if not (package / EXE_NAME).is_file() or not (package / "_internal/assets/app.ico").is_file():
        raise ValueError("更新包缺少 EXE 或完整依赖目录。")
    return package


def prepare_update(release: Release, application_dir: Path, progress=lambda text: None) -> Path:
    if not release.installable:
        raise ValueError("新版尚无可验证的 Windows x64 更新包。")
    application_dir = application_dir.resolve()
    if not (application_dir / EXE_NAME).is_file() or not (application_dir / "_internal").is_dir():
        raise ValueError("只支持在完整的 EXE 目录中更新。")
    update_dir = application_dir / "runtime/updates"
    for path in (application_dir / "runtime", update_dir):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0)
                                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
            raise ValueError("更新暂存目录不能是目录链接。")
    update_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"v{release.version}-", dir=update_dir))
    archive = stage / "package.zip"
    digest = hashlib.sha256()
    received = 0
    with httpx.Client(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        with client.stream("GET", release.asset_url) as response:
            response.raise_for_status()
            address = urlparse(str(response.url))
            if address.scheme != "https" or address.hostname not in (
                "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
            ):
                raise ValueError("更新下载被重定向到非 GitHub HTTPS 地址。")
            with archive.open("xb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    received += len(chunk)
                    if received > release.size:
                        raise ValueError("更新包大小与发布记录不一致。")
                    output.write(chunk)
                    digest.update(chunk)
                    progress(f"正在下载：{received / release.size:.0%}")
    if received != release.size or digest.hexdigest() != release.sha256:
        raise ValueError("更新包大小或 SHA-256 校验失败，旧版本未改动。")
    progress("正在校验并展开更新包…")
    package = extract_package(archive, stage / "unpacked")
    if file_version(package / EXE_NAME) != release.version + ".0":
        raise ValueError("更新 EXE 版本与发布标签不一致，旧版本未改动。")
    files = {}
    for path in package.rglob("*"):
        if path.is_file():
            files[path.relative_to(package).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    request = stage / "request.json"
    request.write_text(json.dumps({"application_dir": str(application_dir), "version": release.version,
                                   "parent_pid": os.getpid(), "files": files,
                                   "entries": [name for name in PROGRAM_ENTRIES if (package / name).exists()]},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(resource("assets", "update.ps1"), stage / "update.ps1")
    return request


def launch_update(request: Path) -> None:
    if os.name != "nt" or not is_frozen() or Path(sys.executable).name != EXE_NAME:
        raise RuntimeError("源码运行不能原地安装 EXE 更新，请使用 Windows 目录版。")
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                      "-File", str(request.parent / "update.ps1"), "-Request", str(request)],
                     cwd=request.parent, creationflags=subprocess.CREATE_NO_WINDOW)
