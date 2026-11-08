"""把 requests / yt-dlp 安装到插件自己的 _vendor 目录。

这个模块只使用 Python 标准库，所以即使插件环境里没有 requests，
它也能被加载。N.E.K.O 如果是 PyInstaller / Nuitka 冻结应用，主程序
没有可用的 python.exe，就不能对它执行 pip；本模块会去系统里找
一个 Python（或 py 启动器），用它的 pip 把依赖装到：

    <插件目录>/_vendor

并用 ``--python-version`` / ``--platform`` / ``--abi`` 选择与
N.E.K.O 内置解释器版本匹配的 wheel，避免装成另一个 Python 版本的
二进制包。

也可以手动运行：

    python bootstrap_deps.py --target-python 3.11
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PLUGIN_DIR / "_vendor"
LOG_PATH = VENDOR_DIR / "install.log"

PACKAGES = (
    "requests>=2.28,<3",
    "yt-dlp>=2024.8.6",
)
REQUIRED_MODULES = ("requests", "yt_dlp")

# 同一进程里可能有多个入口同时检查依赖，避免重复 pip install。
_INSTALL_LOCK = threading.Lock()
_READY = False


def _ensure_import_paths() -> None:
    """把插件目录和 _vendor 放到 sys.path 前面。

    先插入 PLUGIN_DIR，再插入 VENDOR_DIR，最终 _vendor 在索引 0，
    所以同名第三方库优先使用插件自己的版本。
    """
    for path in (str(PLUGIN_DIR), str(VENDOR_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _append_log(message: str) -> None:
    """把安装过程写到 _vendor/install.log，方便用户和插件日志排查。"""
    try:
        VENDOR_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{timestamp}] {message}\n")
    except OSError:
        # 日志写不了不能影响安装本身。
        pass


def _run(
    command: list[str],
    *,
    timeout: int,
    log: bool = True,
) -> subprocess.CompletedProcess[str] | None:
    """运行子进程；失败时只记录，不把异常抛给插件加载流程。"""
    if log:
        _append_log("$ " + " ".join(command))
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if os.name == "nt":
        # 不弹黑色命令行窗口。
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        if log:
            _append_log(f"TIMEOUT after {timeout}s: {exc}")
        return None
    except OSError as exc:
        if log:
            _append_log(f"START FAILED: {exc}")
        return None

    if log:
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if output:
            _append_log(output[-6000:])
    return completed


def _can_import(module_name: str) -> tuple[bool, str]:
    """真正 import 一次，能发现“包在但 ABI/依赖坏了”的情况。"""
    try:
        importlib.import_module(module_name)
        return True, ""
    except Exception as exc:  # ImportError、DLL load failed 等
        return False, f"{type(exc).__name__}: {exc}"


def dependencies_present() -> dict[str, Any]:
    """只查包是否存在，不执行 import。

    插件进程启动时有较短的“就绪”时限，顶层 import requests / yt-dlp
    可能拖过时限导致 start_plugin failed。所以加载阶段只做这个轻量检查，
    真正的导入和安装留到第一次点歌时。
    """
    _ensure_import_paths()

    missing = [
        name for name in REQUIRED_MODULES
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        return {
            "ok": False,
            "message": f"缺少依赖: {', '.join(missing)}（首次点歌时会自动安装）",
            "missing": missing,
            "vendor": str(VENDOR_DIR),
            "log": str(LOG_PATH),
        }
    return {
        "ok": True,
        "message": "依赖包已找到",
        "vendor": str(VENDOR_DIR),
        "log": str(LOG_PATH),
    }


def dependencies_ready(force: bool = False) -> dict[str, Any]:
    """检查依赖是否可用。

    返回统一结构，插件入口只需要看 ``ok`` 和 ``message``。
    """
    global _READY
    _ensure_import_paths()

    if _READY and not force:
        return {
            "ok": True,
            "message": "依赖已就绪",
            "vendor": str(VENDOR_DIR),
            "log": str(LOG_PATH),
        }

    missing: list[str] = []
    errors: dict[str, str] = {}
    for module_name in REQUIRED_MODULES:
        ok, error = _can_import(module_name)
        if not ok:
            missing.append(module_name)
            errors[module_name] = error

    if missing:
        _READY = False
        detail = "；".join(f"{name}: {errors[name]}" for name in missing)
        return {
            "ok": False,
            "message": f"缺少依赖: {', '.join(missing)}（{detail}）",
            "missing": missing,
            "errors": errors,
            "vendor": str(VENDOR_DIR),
            "log": str(LOG_PATH),
        }

    _READY = True
    return {
        "ok": True,
        "message": "依赖已就绪",
        "vendor": str(VENDOR_DIR),
        "log": str(LOG_PATH),
    }


def _is_python_executable(path: str) -> bool:
    """避免把 N.E.K.O.exe 当成 Python 去执行 -m pip。"""
    name = Path(path).name.lower()
    return name.startswith("python") or name.startswith("pypy")


def _python_candidates() -> list[list[str]]:
    """列出可能可用的 Python 命令。"""
    candidates: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def add(command: list[str]) -> None:
        key = tuple(command)
        if command and key not in seen:
            seen.add(key)
            candidates.append(command)

    explicit = os.getenv("NEKO_BILI_PYTHON")
    if explicit:
        add([explicit])

    # 冻结应用里 sys.executable 是 N.E.K.O.exe，不能拿来跑 pip。
    if sys.executable and _is_python_executable(sys.executable):
        add([sys.executable])

    major, minor = sys.version_info[:2]
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            add([launcher, f"-{major}.{minor}"])
            add([launcher, f"-{major}"])

    for name in (f"python{major}.{minor}", "python3", "python"):
        executable = shutil.which(name)
        if executable:
            add([executable])

    return candidates


def _pip_works(python_prefix: list[str]) -> bool:
    """检查候选 Python 的 pip 是否可用，必要时尝试 ensurepip。"""
    completed = _run(
        [*python_prefix, "-m", "pip", "--version"],
        timeout=25,
        log=False,
    )
    if completed is not None and completed.returncode == 0:
        return True

    # 有些 Python 安装没把 pip 加到环境里，ensurepip 通常能补上。
    _append_log("pip 不可用，尝试 ensurepip: " + " ".join(python_prefix))
    completed = _run(
        [*python_prefix, "-m", "ensurepip", "--upgrade"],
        timeout=120,
        log=True,
    )
    if completed is None or completed.returncode != 0:
        return False

    completed = _run(
        [*python_prefix, "-m", "pip", "--version"],
        timeout=25,
        log=False,
    )
    return completed is not None and completed.returncode == 0


def _find_python() -> list[str] | None:
    """找一个能运行 pip 的 Python。"""
    for candidate in _python_candidates():
        if _pip_works(candidate):
            _append_log("使用 Python: " + " ".join(candidate))
            return candidate
    return None


def _normalize_target_version(value: str | None) -> tuple[int, int]:
    """把 '3.11' / '3.11.9' 归一化成 (3, 11)；非法值回退到当前版本。"""
    if value:
        parts = value.strip().split(".")
        try:
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass
    return sys.version_info.major, sys.version_info.minor


def _platform_args(target_version: tuple[int, int]) -> list[str]:
    """为 pip 指定目标解释器/平台，避免把 cp312 的 pyd 装给 3.11。"""
    if os.name != "nt":
        # 非 Windows 先不强行指定平台；通常 N.E.K.O 用户是 Windows。
        return []

    major, minor = target_version
    arch = "win_amd64" if sys.maxsize > 2 ** 32 else "win32"
    return [
        "--platform", arch,
        "--implementation", "cp",
        "--python-version", f"{major}.{minor}",
        "--abi", f"cp{major}{minor}",
    ]


def _pip_install(
    python_prefix: list[str],
    target_version: tuple[int, int],
) -> bool:
    """把依赖装进 _vendor。返回是否成功。"""
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    command = [
        *python_prefix,
        "-m", "pip", "install",
        "--upgrade",
        "--target", str(VENDOR_DIR),
        "--no-compile",                 # 不能给另一个 Python 版本留 cp312 pyc
        "--no-warn-script-location",
        "--disable-pip-version-check",
        "--only-binary=:all:",          # 只装 wheel，避免现场编译
    ]
    command.extend(_platform_args(target_version))
    command.extend(PACKAGES)

    _append_log(
        f"开始安装依赖到 {VENDOR_DIR}，目标 Python "
        f"{target_version[0]}.{target_version[1]}"
    )
    completed = _run(command, timeout=300, log=True)
    if completed is None:
        return False
    if completed.returncode != 0:
        _append_log(f"pip install 失败，退出码 {completed.returncode}")
        return False
    return True


def ensure_dependencies(
    auto_install: bool = False,
    *,
    force: bool = False,
    target_version: str | None = None,
) -> dict[str, Any]:
    """确保依赖在 _vendor 里可用。

    ``auto_install=False`` 只检查，不安装；插件加载阶段用这个模式，
    避免启动被 pip 阻塞。``auto_install=True`` 会在首次点歌时尝试安装。
    """
    status = dependencies_ready(force=force)
    if status["ok"]:
        return status

    if not auto_install:
        status["message"] = (
            status["message"]
            + "；可运行 python bootstrap_deps.py 手动安装"
        )
        return status

    with _INSTALL_LOCK:
        # 等锁期间另一个线程可能已经装好了。
        status = dependencies_ready(force=True)
        if status["ok"]:
            return status

        python_prefix = _find_python()
        if python_prefix is None:
            return {
                "ok": False,
                "message": (
                    "没找到可用的 Python 来执行 pip。请安装 Python 3.11，"
                    "然后在插件目录运行：python bootstrap_deps.py --target-python 3.11"
                ),
                "vendor": str(VENDOR_DIR),
                "log": str(LOG_PATH),
            }

        version = _normalize_target_version(
            target_version
            or f"{sys.version_info.major}.{sys.version_info.minor}"
        )
        _append_log(
            "目标 Python 版本: "
            f"{version[0]}.{version[1]}；当前运行版本: "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )

        if not _pip_install(python_prefix, version):
            return {
                "ok": False,
                "message": (
                    "pip 安装依赖失败，详情看 "
                    f"{LOG_PATH}。也可以手动运行："
                    "python bootstrap_deps.py --target-python "
                    f"{version[0]}.{version[1]}"
                ),
                "vendor": str(VENDOR_DIR),
                "log": str(LOG_PATH),
            }

        # 如果目标版本就是当前解释器，可以直接 import 验证；
        # 如果用户用 Python 3.12 给 N.E.K.O 3.11 装包，就不能在本进程
        # import cp311 的扩展模块，只能检查包目录是否落地。
        importlib.invalidate_caches()
        current_version = sys.version_info[:2]
        if current_version == version:
            status = dependencies_ready(force=True)
        else:
            packages_present = all(
                (VENDOR_DIR / name).is_dir()
                for name in ("requests", "yt_dlp")
            )
            status = {
                "ok": packages_present,
                "message": (
                    f"依赖已安装到 _vendor（目标 Python {version[0]}.{version[1]}）"
                    if packages_present
                    else "pip 返回成功，但 _vendor 里没有找到 requests / yt_dlp"
                ),
                "vendor": str(VENDOR_DIR),
                "log": str(LOG_PATH),
            }

        status["installed"] = True
        status["target_python"] = f"{version[0]}.{version[1]}"
        if status["ok"]:
            _append_log("依赖安装完成并通过检查")
        else:
            _append_log("安装后检查仍失败: " + status.get("message", ""))
        return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 requests / yt-dlp 安装到插件的 _vendor 目录"
    )
    parser.add_argument(
        "--target-python",
        default=None,
        help="N.E.K.O 内置的 Python 版本，例如 3.11；默认用当前解释器版本",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="指定用来执行 pip 的 Python 可执行文件路径",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使依赖已可用也重新安装",
    )
    args = parser.parse_args(argv)

    if args.python:
        os.environ["NEKO_BILI_PYTHON"] = args.python

    target = args.target_python or (
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    status = ensure_dependencies(
        auto_install=True,
        force=args.force,
        target_version=target,
    )
    print(status.get("message", ""))
    print(f"_vendor: {VENDOR_DIR}")
    print(f"日志: {LOG_PATH}")
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())