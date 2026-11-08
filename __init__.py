"""B站音源点歌插件。

工作流程：
1. 用户在聊天里点歌，入口 ``play_song`` 收到歌名；
2. 用 requests 调 B 站搜索接口，取排名第一的视频；
3. 用 yt-dlp 解析该视频，拿到最佳音频流的直链；
4. 下载音频到插件本地 static 目录；
5. 按 N.E.K.O 播放器协议发送 ``music_allowlist_add`` 与
   ``music_play_url``，让前端播放本地音频。

说明：
- N.E.K.O 若是 PyInstaller / Nuitka 冻结应用，没有独立 python.exe；
  本插件会把 requests / yt-dlp 装进自己的 ``_vendor``，并用 sys.path
  优先加载，不依赖主程序的 Python 环境。
- B 站音频 CDN 会校验 Referer，且部分音频域名（bilivideo.cn）不在
  N.E.K.O 代理白名单里。因此这里先把音频下载到插件自己的 static，
  再交给前端播放；这样不依赖 B 站直链、也不依赖代理白名单。
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import inspect
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 依赖注入：让插件优先从自己的目录加载第三方库
# ---------------------------------------------------------------------------
#
# N.E.K.O 可能是 PyInstaller / Nuitka 打包的冻结应用，没有独立 python.exe，
# 不能直接对主程序 pip install。这里把插件目录和 _vendor 目录加入 sys.path：
#   - 插件目录：放 bootstrap_deps.py 等自己的模块；
#   - _vendor：放 pip --target 安装的 requests / yt-dlp 及其依赖。
#
# _vendor 插到 sys.path 最前面，确保同名库优先用插件自带的版本，
# 而不是依赖冻结应用里是否有、或是什么版本。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_VENDOR_DIR = os.path.join(_PLUGIN_DIR, "_vendor")

if _PLUGIN_DIR not in sys.path:
    sys.path.append(_PLUGIN_DIR)
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

# 这个模块本身只用标准库，不会在缺 requests 时 import 失败。
try:
    try:
        from .bootstrap_deps import (
            dependencies_present as _dependencies_present,
            ensure_dependencies as _ensure_dependencies,
        )
    except (ImportError, ValueError):
        # 插件也可能被当成普通目录加载，此时没有包上下文，退回绝对导入。
        from bootstrap_deps import (
            dependencies_present as _dependencies_present,
            ensure_dependencies as _ensure_dependencies,
        )
except Exception as _bootstrap_exc:  # pragma: no cover - 只在文件损坏时发生
    _ensure_dependencies = None
    _dependencies_present = None
    _BOOTSTRAP_IMPORT_ERROR = _bootstrap_exc
else:
    _BOOTSTRAP_IMPORT_ERROR = None

# 加载阶段绝对不要 import requests / yt_dlp：它们可能让插件进程错过
# 宿主约 1 秒的启动就绪时限，表现为 start_plugin failed 且没有任何日志。
# 这里只查包是否存在；真正导入和安装放在第一次点歌时。
if _dependencies_present is not None:
    try:
        _DEP_STATUS = _dependencies_present()
    except Exception as _dep_exc:  # pragma: no cover
        _DEP_STATUS = {"ok": False, "message": str(_dep_exc)}
else:
    _DEP_STATUS = {
        "ok": False,
        "message": f"无法导入 bootstrap_deps: {_BOOTSTRAP_IMPORT_ERROR}",
    }

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
)


# ---------------------------------------------------------------------------
# 可调整的常量
# ---------------------------------------------------------------------------

# B 站视频搜索接口。search_type=video 表示只搜视频。
BILI_SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
BILI_REFERER = "https://www.bilibili.com/"
BILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 搜索请求超时：(连接超时, 读取超时)，单位秒。
SEARCH_TIMEOUT = (6, 15)

# yt-dlp Python API / 命令行的超时。
YTDLP_SOCKET_TIMEOUT = 20
YTDLP_CLI_TIMEOUT = 90

# B 站搜索结果里偶尔会混入非视频条目，只认 BV 号。
_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# 数据结构与内部异常
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Track:
    """B 站搜索结果里的一首歌（实际上是视频）。"""

    bvid: str
    title: str
    artist: str
    cover: str
    page_url: str
    duration: int = 0
    duration_text: str = ""


@dataclass(slots=True)
class AudioStream:
    """yt-dlp 解析出来的音频流。"""

    url: str
    ext: str = ""
    protocol: str = ""
    duration: int = 0
    title: str = ""
    artist: str = ""


class JukeboxError(Exception):
    """带用户可读中文提示的插件内部异常。

    ``user_message`` 会原样返回给聊天模型/用户；``detail`` 只写日志，
    避免把 yt-dlp 或 requests 的原始堆栈暴露到聊天里。
    """

    def __init__(self, user_message: str, *, detail: str = "") -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail


# ---------------------------------------------------------------------------
# 纯工具函数
# ---------------------------------------------------------------------------


def _clean_text(raw: Any) -> str:
    """去掉搜索结果标题里的 <em> 高亮标签和 HTML 实体。"""
    if raw is None:
        return ""
    text = html.unescape(str(raw))
    text = _HTML_TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_bvid(raw: Any) -> str:
    """从 bvid、arcurl 等字段里提取 BV 号；提取不到返回空字符串。"""
    if raw is None:
        return ""
    match = _BVID_RE.search(str(raw))
    return match.group(0) if match else ""


def _format_duration(seconds: Any) -> str:
    """把秒数格式化成 3:05 / 1:03:05；无效值返回空字符串。"""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _normalize_cover(raw: Any) -> str:
    """B 站图片字段常以 // 开头，这里统一成 https。"""
    if not raw:
        return ""
    cover = str(raw).strip()
    if cover.startswith("//"):
        return "https:" + cover
    if cover.startswith("http://"):
        return "https://" + cover[len("http://"):]
    return cover


def _random_buvid3() -> str:
    """生成一个随机 buvid3，降低 B 站把请求判定为无 Cookie 爬虫的概率。"""
    return str(uuid.uuid4()).upper() + "infoc"


def _bili_headers(cookie_header: str = "") -> dict[str, str]:
    """构造 B 站搜索请求头，带 UA、Referer 和基础 Cookie。"""
    cookie_parts: list[str] = []
    if cookie_header.strip():
        cookie_parts.append(cookie_header.strip().rstrip(";"))
    cookie_parts.append(f"buvid3={_random_buvid3()}")
    cookie_parts.append(f"b_nut={int(time.time())}")

    return {
        "User-Agent": BILI_USER_AGENT,
        "Referer": BILI_REFERER,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": "; ".join(cookie_parts),
    }


def _read_cookie_header(path: Any) -> str:
    """从 cookies 文件里读出一行 Cookie 请求头。

    同时兼容两种格式：
    1. 浏览器导出的 Netscape cookies.txt（制表符分隔）；
    2. 直接写成 ``SESSDATA=...; bili_jct=...`` 的纯文本。
    """
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    # Netscape 格式：domain, include_subdomains, path, secure, expiry, name, value
    if "\t" in text:
        cookies: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and "bilibili.com" in parts[0]:
                cookies.append(f"{parts[5]}={parts[6]}")
        if cookies:
            return "; ".join(cookies)

    # 纯文本格式：取第一行看起来像 Cookie 的内容。
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            return line
    return ""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _submission_rejection(receipt: object) -> str | None:
    """判断 SDK 是否同步拒绝了消息提交。

    与旧插件保持一致：只有明确 ``submitted is False`` 才算失败；
    老版本 SDK 同步成功时可能返回 None，不能把 None 当失败。
    """
    if not isinstance(receipt, Mapping):
        return None
    if receipt.get("submitted") is not False:
        return None
    reason = receipt.get("reason")
    return str(reason) if isinstance(reason, str) and reason else "rejected"


# ---------------------------------------------------------------------------
# 搜索与解析
# ---------------------------------------------------------------------------


def _search_bilibili(query: str, cookie_header: str = "") -> Track:
    """调用 B 站搜索接口，返回排名第一的视频。"""
    keyword = _clean_text(query)
    if not keyword:
        raise JukeboxError("喵？要听什么歌呀，歌名还没告诉我呢～")

    # 延迟导入：路径注入已经完成，这里会优先命中 _vendor 里的 requests。
    # 这样即使依赖没装好，插件本身也还能加载，入口可以返回友好提示。
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:
        raise JukeboxError(
            "喵…点歌依赖还没装好，先运行插件目录里的 "
            "python bootstrap_deps.py --target-python 3.11 吧。",
            detail=f"requests import failed: {exc}",
        ) from exc

    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": 1,
        "page_size": 20,
        "order": "totalrank",
        "duration": 0,
        "tids": 0,
        "from_source": "webtop_search",
    }

    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )

    try:
        with requests.Session() as session:
            session.headers.update(_bili_headers(cookie_header))
            session.mount("https://", HTTPAdapter(max_retries=retry))
            response = session.get(
                BILI_SEARCH_API,
                params=params,
                timeout=SEARCH_TIMEOUT,
            )
    except requests.Timeout as exc:
        raise JukeboxError(
            "呜…B站搜索超时了喵，网络是不是不太好？",
            detail=f"search timeout: {exc}",
        ) from exc
    except requests.RequestException as exc:
        raise JukeboxError(
            "呜…连不上B站喵，检查一下网络或代理吧。",
            detail=f"search request failed: {exc}",
        ) from exc

    if response.status_code == 412:
        raise JukeboxError(
            "B站把搜索请求当成机器人拦住了喵…过一会儿再试，"
            "或者在插件 data 目录放一份 bilibili_cookies.txt 吧。",
            detail="search http 412",
        )
    if response.status_code >= 400:
        raise JukeboxError(
            "B站搜索接口暂时不可用喵，稍后再试试吧。",
            detail=f"search http {response.status_code}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise JukeboxError(
            "B站返回了看不懂的内容喵…可能是被风控了，稍后再试吧。",
            detail=f"invalid search json: {exc}",
        ) from exc

    if not isinstance(payload, dict):
        raise JukeboxError("B站返回了看不懂的内容喵…稍后再试吧。")

    code = payload.get("code")
    if code != 0:
        if code in (-412, -352):
            raise JukeboxError(
                "B站触发风控了喵…过一会儿再点，或者放一份 cookies 吧。",
                detail=f"search api code {code}: {payload.get('message')}",
            )
        raise JukeboxError(
            "B站搜索失败了喵，稍后再试试吧。",
            detail=f"search api code {code}: {payload.get('message')}",
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise JukeboxError("呜呜，B站上没找到这首歌喵~")

    result = data.get("result")
    # 不同接口版本可能返回 list，也可能返回 {"video": [...]}。
    if isinstance(result, dict):
        result = result.get("video") or []
    if not isinstance(result, list):
        result = []

    for item in result:
        if not isinstance(item, dict):
            continue
        # 只接受视频结果；有的条目是专栏/用户。
        if item.get("type") not in (None, "", "video"):
            continue

        bvid = _extract_bvid(item.get("bvid") or item.get("arcurl") or "")
        if not bvid:
            continue

        title = _clean_text(item.get("title")) or "B站视频"
        artist = _clean_text(item.get("author")) or "未知UP主"
        cover = _normalize_cover(item.get("pic"))
        duration_text = _clean_text(item.get("duration"))

        return Track(
            bvid=bvid,
            title=title,
            artist=artist,
            cover=cover,
            page_url=f"https://www.bilibili.com/video/{bvid}",
            duration_text=duration_text,
        )

    raise JukeboxError("呜呜，B站上没找到这首歌喵~")


def _pick_best_audio(info: dict[str, Any]) -> AudioStream | None:
    """从 yt-dlp 的 info 中挑一个最适合播放的音频流。

    B 站通常是 DASH 音视频分离，优先纯音频（vcodec=none），
    再按码率/体积排序。m3u8 在普通 <audio> 标签里兼容性差，跳过。
    """
    if not isinstance(info, dict):
        return None

    candidates: list[dict[str, Any]] = []
    requested = info.get("requested_downloads")
    if isinstance(requested, list):
        candidates.extend(item for item in requested if isinstance(item, dict))
    formats = info.get("formats")
    if isinstance(formats, list):
        candidates.extend(item for item in formats if isinstance(item, dict))
    candidates.append(info)

    best: dict[str, Any] | None = None
    best_score = float("-inf")

    for index, fmt in enumerate(candidates):
        url = fmt.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue

        acodec = str(fmt.get("acodec") or "").lower()
        vcodec = str(fmt.get("vcodec") or "").lower()
        if acodec in ("", "none"):
            continue

        protocol = str(fmt.get("protocol") or "").lower()
        if "m3u8" in protocol or url.split("?", 1)[0].lower().endswith(".m3u8"):
            continue

        audio_only = vcodec in ("", "none")
        abr = _as_float(fmt.get("abr") or fmt.get("tbr"))
        size = _as_float(fmt.get("filesize") or fmt.get("filesize_approx"))
        score = (200.0 if audio_only else 0.0) + min(abr, 1000.0) + min(size / 1e9, 5.0)
        # yt-dlp 已选中的 requested_downloads 排在最前，给一点小加成。
        score += max(0.0, 5.0 - index * 0.01)

        if score > best_score:
            best = fmt
            best_score = score

    # 兜底：极少数 extractor 只给出 info["url"]，没有 formats 明细。
    if best is None:
        fallback_url = info.get("url")
        if isinstance(fallback_url, str) and fallback_url.startswith(("http://", "https://")):
            best = info

    if best is None:
        return None

    return AudioStream(
        url=str(best["url"]),
        ext=str(best.get("ext") or ""),
        protocol=str(best.get("protocol") or ""),
        duration=int(_as_float(best.get("duration"))),
        title=_clean_text(best.get("track") or best.get("title")),
        artist=_clean_text(best.get("artist") or best.get("creator") or best.get("uploader")),
    )


def _extract_with_python(page_url: str, cookie_file: Any) -> AudioStream:
    """用 yt-dlp 的 Python API 解析音频直链。"""
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise JukeboxError(
            "喵…没装 yt-dlp，先在 N.E.K.O 的 Python 环境里运行 "
            "pip install yt-dlp 吧。",
            detail=f"yt_dlp import failed: {exc}",
        ) from exc

    options: dict[str, Any] = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": 2,
        "extractor_retries": 2,
        "http_headers": {
            "User-Agent": BILI_USER_AGENT,
            "Referer": BILI_REFERER,
        },
    }
    if cookie_file is not None:
        options["cookiefile"] = str(cookie_file)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(page_url, download=False)
    except Exception as exc:  # yt-dlp 的 DownloadError 等
        raise JukeboxError(
            "喵…这首歌虽然找到了，但B站没给出可用的音频流"
            "（可能被版权、地区或登录限制）。",
            detail=f"yt-dlp python failed: {type(exc).__name__}: {exc}",
        ) from exc

    if not isinstance(info, dict):
        raise JukeboxError(
            "喵…yt-dlp 没解析出这首歌的音频信息。",
            detail="yt-dlp returned no info",
        )

    # 有的地址会被解析成 playlist，取第一个 entry。
    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
        if entries:
            info = entries[0]

    stream = _pick_best_audio(info)
    if stream is None:
        raise JukeboxError(
            "喵…这首歌找到了，但B站没有返回能播放的音频流。",
            detail="no audio format in yt-dlp info",
        )

    # 搜索结果的标题通常更干净；yt-dlp 的标题作为兜底。
    if not stream.title:
        stream.title = _clean_text(info.get("title"))
    if not stream.artist:
        stream.artist = _clean_text(
            info.get("artist") or info.get("creator") or info.get("uploader")
        )
    if not stream.duration:
        stream.duration = int(_as_float(info.get("duration")))
    return stream


def _find_external_ytdlp() -> str | None:
    """找 PATH 里的 yt-dlp 可执行文件。"""
    return shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")


def _extract_with_cli(page_url: str, cookie_file: Any) -> AudioStream:
    """用 yt-dlp 命令行解析音频直链（Python API 不可用时的兜底）。"""
    if importlib.util.find_spec("yt_dlp") is not None:
        command = [sys.executable, "-m", "yt_dlp"]
    else:
        executable = _find_external_ytdlp()
        if not executable:
            raise JukeboxError(
                "喵…没装 yt-dlp，先在 N.E.K.O 的 Python 环境里运行 "
                "pip install yt-dlp 吧。",
                detail="yt-dlp executable not found",
            )
        command = [executable]

    command.extend([
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--no-progress",
        "-f", "bestaudio[ext=m4a]/bestaudio/best",
        "-g",
        page_url,
    ])
    if cookie_file is not None:
        command.extend(["--cookies", str(cookie_file)])

    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": YTDLP_CLI_TIMEOUT,
    }
    if os.name == "nt":
        # 避免在桌面端弹出一个黑色命令行窗口。
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise JukeboxError(
            "喵…解析B站音频超时了，网络有点慢，再试一次吧。",
            detail=f"yt-dlp cli timeout: {exc}",
        ) from exc
    except OSError as exc:
        raise JukeboxError(
            "喵…启动 yt-dlp 失败了，检查一下依赖安装吧。",
            detail=f"yt-dlp cli start failed: {exc}",
        ) from exc

    if completed.returncode != 0:
        raise JukeboxError(
            "喵…这首歌虽然找到了，但B站没给出可用的音频流"
            "（可能被版权、地区或登录限制）。",
            detail=f"yt-dlp cli exit {completed.returncode}: {completed.stderr[-800:]}",
        )

    urls = [
        line.strip()
        for line in (completed.stdout or "").splitlines()
        if line.strip().startswith(("http://", "https://"))
    ]
    if not urls:
        raise JukeboxError(
            "喵…yt-dlp 没输出音频地址，可能被B站风控了。",
            detail=f"yt-dlp cli stdout empty: {completed.stderr[-800:]}",
        )

    return AudioStream(url=urls[0], ext="")


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


@neko_plugin
class BilibiliMusicPlugin(NekoPluginBase):
    """B 站音源点歌插件。"""

    name = "bilibili_music"

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = getattr(ctx, "logger", logging.getLogger(__name__))
        self._static_dir: Path | None = None
        self._audio_dir: Path | None = None
        if not _DEP_STATUS.get("ok"):
            self.logger.warning(
                "B站点歌依赖尚未就绪，首次点歌时会尝试安装到 %s：%s",
                _VENDOR_DIR,
                _DEP_STATUS.get("message"),
            )

    # -- 目标角色解析 ------------------------------------------------------

    def _resolve_target_lanlan(self, kwargs: dict[str, Any]) -> str | None:
        """解析这条点歌指令属于哪个角色。

        与旧插件保持一致：只认本次调用自带的 target_lanlan / _ctx，
        不读 ctx._current_lanlan，避免把歌放给另一个角色。
        """
        explicit = kwargs.get("target_lanlan")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()

        ctx_obj = kwargs.get("_ctx")
        if isinstance(ctx_obj, Mapping):
            lanlan_name = ctx_obj.get("lanlan_name")
            if isinstance(lanlan_name, str) and lanlan_name.strip():
                return lanlan_name.strip()
        return None

    # -- cookies -----------------------------------------------------------

    def _cookie_file(self) -> Any:
        """插件 data 目录下的可选 cookies 文件。"""
        try:
            path = self.data_path("bilibili_cookies.txt")
        except Exception:
            return None
        try:
            return path if path.exists() else None
        except OSError:
            return None

    def _cookie_header(self) -> str:
        return _read_cookie_header(self._cookie_file())

    # -- 网络 / yt-dlp（在线程里跑） ---------------------------------------

    def _search_sync(self, query: str) -> Track:
        return _search_bilibili(query, self._cookie_header())

    def _extract_sync(self, track: Track) -> AudioStream:
        """先走 Python API，失败再尝试命令行；两者都失败才报错。"""
        cookie_file = self._cookie_file()
        errors: list[str] = []

        if importlib.util.find_spec("yt_dlp") is not None:
            try:
                return _extract_with_python(track.page_url, cookie_file)
            except JukeboxError as exc:
                errors.append(exc.detail or exc.user_message)
                self.logger.warning("yt-dlp Python API 解析失败: %s", exc)
            except Exception as exc:  # 防御性兜底
                errors.append(f"{type(exc).__name__}: {exc}")
                self.logger.warning("yt-dlp Python API 异常: %s", exc)

        if importlib.util.find_spec("yt_dlp") is not None or _find_external_ytdlp():
            try:
                return _extract_with_cli(track.page_url, cookie_file)
            except JukeboxError as exc:
                errors.append(exc.detail or exc.user_message)
                self.logger.warning("yt-dlp 命令行解析失败: %s", exc)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                self.logger.warning("yt-dlp 命令行异常: %s", exc)

        if not errors:
            raise JukeboxError(
                "喵…没装 yt-dlp，先在 N.E.K.O 的 Python 环境里运行 "
                "pip install yt-dlp 吧。",
                detail="yt-dlp unavailable",
            )

        raise JukeboxError(
            "喵…这首歌虽然找到了，但B站没给出可用的音频流"
            "（可能被版权、地区或登录限制）。",
            detail=" | ".join(errors)[-1500:],
        )

    # -- 本地音频与播放推送 ------------------------------------------------

    def _resolve_public_origin(self) -> str:
        """解析 N.E.K.O 用户插件服务器的地址。

        静态文件地址必须是绝对 URL，播放器才能通过
        ``/plugin/bilibili_music/ui/audio/...`` 访问。
        """
        for name in (
            "NEKO_PLUGIN_SERVER_ORIGIN",
            "NEKO_USER_PLUGIN_SERVER_ORIGIN",
            "NEKO_SERVER_ORIGIN",
        ):
            value = str(os.getenv(name, "")).strip().rstrip("/")
            if value.startswith(("http://", "https://")):
                return value

        try:
            env_port = int(os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", "").strip())
            if 1 <= env_port <= 65535:
                return f"http://127.0.0.1:{env_port}"
        except Exception:
            pass

        try:
            from config import USER_PLUGIN_SERVER_PORT

            port = int(USER_PLUGIN_SERVER_PORT)
            if 1 <= port <= 65535:
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass

        return "http://127.0.0.1:48916"

    def _prepare_static_ui(self) -> None:
        """准备本地音频目录，并把 static 注册给插件服务器。"""
        static_dir = Path(self.plugin_dir) / "static"
        audio_dir = static_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        index_path = static_dir / "index.html"
        if not index_path.exists():
            index_path.write_text(
                "<!doctype html><meta charset='utf-8'><title>Bilibili Music</title>",
                encoding="utf-8",
            )

        register = getattr(self, "register_static_ui", None)
        if callable(register):
            try:
                register(
                    "static",
                    index_file="index.html",
                    cache_control="public, max-age=3600",
                )
            except TypeError:
                register("static")
            except Exception as exc:
                self.logger.warning("注册静态音频目录失败: %s", exc)

        self._static_dir = static_dir
        self._audio_dir = audio_dir

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        """插件启动时注册 static 目录；失败也让插件保持可加载。"""
        try:
            self._prepare_static_ui()
            return Ok({"status": "ready", "audio_dir": str(self._audio_dir)})
        except Exception as exc:
            self.logger.warning("初始化本地音频目录失败: %s", exc)
            return Ok({"status": "degraded", "message": str(exc)})

    def _download_audio_sync(self, track: Track, stream: AudioStream) -> Path:
        """把 B 站音频下载到插件 static/audio，绕开 CDN 的 Referer 限制。"""
        if self._audio_dir is None:
            self._prepare_static_ui()
        if self._audio_dir is None:
            raise JukeboxError("喵…本地音频目录没有准备好，暂时不能播放。")

        destination = self._audio_dir / f"{track.bvid}.m4a"
        try:
            if destination.is_file() and destination.stat().st_size > 1024:
                return destination
        except OSError:
            pass

        try:
            import requests
        except ImportError as exc:
            raise JukeboxError(
                "喵…点歌依赖还没装好，先运行 bootstrap_deps.py 吧。",
                detail=f"requests import failed: {exc}",
            ) from exc

        headers = {
            "User-Agent": BILI_USER_AGENT,
            "Referer": BILI_REFERER,
        }
        cookie_header = self._cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header

        temp_path = destination.with_suffix(".part")
        total = 0
        try:
            with requests.get(
                stream.url,
                headers=headers,
                stream=True,
                timeout=(10, 90),
            ) as response:
                self.logger.info(
                    "下载音频: host=%s status=%s type=%s length=%s",
                    urllib.parse.urlparse(stream.url).hostname,
                    response.status_code,
                    response.headers.get("content-type"),
                    response.headers.get("content-length"),
                )
                response.raise_for_status()
                with temp_path.open("wb") as handle:
                    for chunk in response.iter_content(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > 60 * 1024 * 1024:
                            raise JukeboxError("喵…这首歌的音频文件太大了，先不下了。")
                        handle.write(chunk)
                self.logger.info("音频下载完成: bytes=%s", total)

            if total <= 1024:
                raise JukeboxError("呜呜，B站没返回有效的音频文件喵~")
            os.replace(temp_path, destination)
        except requests.Timeout as exc:
            raise JukeboxError(
                "呜…下载B站音频超时了，网络有点慢，再试一次吧。",
                detail=f"audio download timeout: {exc}",
            ) from exc
        except requests.RequestException as exc:
            raise JukeboxError(
                "呜…B站音频下载失败了，可能被版权、地区或登录限制。",
                detail=f"audio download failed: {exc}",
            ) from exc
        except OSError as exc:
            raise JukeboxError(
                "呜…音频文件写不进插件目录，检查一下磁盘权限吧。",
                detail=f"audio write failed: {exc}",
            ) from exc
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        if total <= 1024 or not destination.is_file():
            raise JukeboxError("呜呜，B站没返回有效的音频文件喵~")

        return destination

    def _audio_url(self, filename: str) -> str:
        return (
            f"{self._resolve_public_origin()}/plugin/{self.plugin_id}"
            f"/ui/audio/{urllib.parse.quote(filename)}"
        )

    async def _send_message(self, **kwargs: Any) -> None:
        """统一处理 push_message 的同步/异步回执和拒绝。"""
        push = getattr(self.ctx, "push_message", None)
        if not callable(push):
            push = getattr(self, "push_message", None)
        if not callable(push):
            raise JukeboxError(
                "当前 N.E.K.O 版本没有 push_message 接口喵…点歌指令送不出去。",
                detail="push_message unavailable",
            )

        try:
            receipt = push(**kwargs)
            if inspect.isawaitable(receipt):
                receipt = await receipt
        except Exception as exc:
            raise JukeboxError(
                "呜…点歌指令送不出去喵，检查一下 N.E.K.O 的播放器接口吧。",
                detail=f"push_message raised: {type(exc).__name__}: {exc}",
            ) from exc

        rejection = _submission_rejection(receipt)
        if rejection:
            raise JukeboxError(
                "呜…点歌指令没能送到播放器喵，稍后再试一次吧。",
                detail=f"push_message rejected: {rejection}",
            )

    async def _push_play_action(
        self,
        track: Track,
        stream: AudioStream,
        target_lanlan: str,
    ) -> str:
        """下载音频到本地 static，并按 N.E.K.O 播放协议推送。

        N.E.K.O 前端实际监听的是 music_pusher 同款消息：
        - ``music_allowlist_add``：把本地 127.0.0.1 加进播放白名单；
        - ``music_play_url``：让播放器播放这个本地音频 URL。
        """
        audio_path = await asyncio.to_thread(
            self._download_audio_sync, track, stream
        )
        audio_url = self._audio_url(audio_path.name)

        duration = stream.duration or track.duration
        title = track.title or stream.title or "B站视频"
        artist = track.artist or stream.artist or "未知UP主"
        event_id = f"bili_music_{uuid.uuid4().hex[:12]}"

        await self._send_message(
            source="bilibili_music",
            message_type="music_allowlist_add",
            description="Allow local Bilibili audio",
            priority=7,
            metadata={
                "domains": ["127.0.0.1", "localhost", "::1"],
                "http_urls": [audio_url],
                "event_id": event_id,
            },
            target_lanlan=target_lanlan or None,
        )
        await self._send_message(
            source="bilibili_music",
            message_type="music_play_url",
            description=f"🎵 {title}",
            priority=9,
            metadata={
                "url": audio_url,
                "name": title,
                "artist": artist,
                "event_id": event_id,
                "duration": duration,
                "cover": track.cover,
                "bvid": track.bvid,
                "source": "bilibili",
            },
            target_lanlan=target_lanlan or None,
        )
        return audio_url

    # -- 插件入口 ----------------------------------------------------------

    @plugin_entry(
        id="play_song",
        name="B站点歌",
        description=(
            "当用户想听歌、点歌、播放某首歌曲时使用。"
            "插件会搜索B站，取排名第一的视频，提取最佳音频流并交给播放器。"
            "参数 query 是歌名或“歌名 歌手”。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要播放的歌曲名，建议写成“歌名 歌手”，例如“晴天 周杰伦”。",
                },
            },
            "required": ["query"],
        },
        llm_result_fields=["message", "title", "artist", "bvid", "duration"],
    )
    async def play_song(self, query: str = "", **kwargs: Any):
        """点歌入口：搜索 → 解析音频 → 推送播放。"""
        keyword = str(query or "").strip()
        if not keyword:
            return Err(SdkError("INVALID_ARGUMENT: 喵？要听哪首歌呀，歌名还没告诉我呢～"))

        target_lanlan = self._resolve_target_lanlan(kwargs)
        if not target_lanlan:
            return Err(SdkError(
                "FAILED_PRECONDITION: 不知道这首歌唱给谁听喵…请在聊天里点歌。"
            ))

        # 首次点歌前检查依赖；缺了就在 _vendor 里自动 pip install。
        # 安装可能耗时，放到线程池，避免卡住事件循环。
        if _ensure_dependencies is None:
            return Err(SdkError(
                "喵…依赖引导模块加载失败，请检查插件目录里的 "
                "bootstrap_deps.py 是否完整。"
            ))

        try:
            dep_status = await asyncio.to_thread(_ensure_dependencies, True)
        except Exception as exc:
            self.logger.exception("依赖检查/安装失败: %s", exc)
            return Err(SdkError(
                "喵…点歌依赖安装失败了，请到插件目录运行："
                "python bootstrap_deps.py --target-python 3.11"
            ))

        if not dep_status.get("ok"):
            self.logger.warning("依赖未就绪: %s", dep_status.get("message"))
            return Err(SdkError(
                "喵…点歌依赖还没准备好，请到插件目录运行："
                "python bootstrap_deps.py --target-python 3.11"
            ))

        try:
            # requests 和 yt-dlp 都是同步阻塞调用，放线程池，避免卡住插件事件循环。
            track = await asyncio.to_thread(self._search_sync, keyword)
            self.logger.info(
                "B站搜索命中: %s (%s) by %s",
                track.title, track.bvid, track.artist,
            )

            stream = await asyncio.to_thread(self._extract_sync, track)
            self.logger.info(
                "音频流解析成功: bvid=%s ext=%s protocol=%s",
                track.bvid, stream.ext, stream.protocol,
            )

            audio_url = await self._push_play_action(track, stream, target_lanlan)
        except JukeboxError as exc:
            self.logger.warning("点歌失败: %s | %s", exc.user_message, exc.detail)
            return Err(SdkError(exc.user_message))
        except Exception as exc:
            self.logger.exception("点歌时发生未预期异常: %s", exc)
            return Err(SdkError("呜…点歌的时候出了点小意外喵，再试一次好不好？"))

        duration_text = _format_duration(stream.duration or track.duration)
        return Ok({
            "message": f"找到啦～《{track.title}》正在送去播放喵♪",
            "title": track.title,
            "artist": track.artist,
            "bvid": track.bvid,
            "duration": duration_text,
            "audio_url": audio_url,
        })