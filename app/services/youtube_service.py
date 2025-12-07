import re
import os
import glob
import time
import shutil
from typing import Tuple, Optional, Iterable
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound, RequestBlocked
import yt_dlp
from yt_dlp.utils import DownloadError

# -----------------------
# Configuration / constants
# -----------------------
DEFAULT_MAX_AUDIO_MB = 20
TEMP_AUDIO_PREFIX = "temp_audio"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


# -----------------------
# Helpers
# -----------------------
def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video id from url or return None."""
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def cleanup_temp_files(pattern: str = f"{TEMP_AUDIO_PREFIX}*", max_retries: int = 3):
    """Remove temp audio files; retry briefly if locked."""
    for fp in glob.glob(pattern):
        for attempt in range(max_retries):
            try:
                os.remove(fp)
                break
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(0.3)


# -----------------------
# Transcript functions (uses youtube-transcript-api properly)
# -----------------------
def clean_transcript(text: str) -> str:
    """Minimal cleaning: normalize whitespace and preserve paragraph breaks."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join([ln for ln in lines if ln]).strip()


def fetch_transcript_object(video_id: str, api: Optional[YouTubeTranscriptApi] = None,
                            preferred_languages: Optional[Iterable[str]] = None):
    """
    Fetch transcript object(s) using youtube-transcript-api.
    Returns a FetchedTranscript-like object (or raises).
    - preferred_languages: list of language codes in preference order (defaults to ['en'])
    - api: optional YouTubeTranscriptApi instance (useful for proxy_config or custom http session)
    """
    if api is None:
        api = YouTubeTranscriptApi()

    if preferred_languages is None:
        preferred_languages = ["en"]

    # youtube-transcript-api provides .fetch and .list (and .list_transcripts / .list depending on version)
    # the public API: YouTubeTranscriptApi().fetch(video_id, languages=[...])
    return api.fetch(video_id, languages=list(preferred_languages))


def get_transcript_with_fallback(video_id: str, api: Optional[YouTubeTranscriptApi] = None,
                                 preferred_languages: Optional[Iterable[str]] = None) -> Optional[str]:
    """
    Attempts steps (in order):
      1. fetch() with preferred_languages (defaults to ['en'])
      2. list() transcripts and:
         - try find_transcript(['en'])
         - try find_generated_transcript(['en'])
         - try translating first available transcript to English
    Returns cleaned text or None.
    """
    if api is None:
        api = YouTubeTranscriptApi()

    if preferred_languages is None:
        preferred_languages = ["en"]

    # 1) Try direct fetch with preferred languages (convenient single-call)
    try:
        fetched = api.fetch(video_id, languages=list(preferred_languages))
        # fetched is iterable FetchedTranscript
        formatter = TextFormatter()
        raw = formatter.format_transcript(fetched)
        return clean_transcript(raw)
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    except RequestBlocked:
        # Let caller decide about proxies; surface None but include message
        print("RequestBlocked: YouTube may be blocking requests from this IP. Consider using residential rotating proxies.")
        return None
    except Exception:
        # If fetch() failed fall back to listing and manual selection
        pass

    # 2) Fallback: list transcripts and pick best option
    try:
        transcript_list = api.list(video_id)  # returns TranscriptList
    except Exception:
        # listing failed — surface None
        return None

    # Prefer manual 'en'
    try:
        t = transcript_list.find_transcript(['en'])
        data = t.fetch()
        raw = TextFormatter().format_transcript(data)
        return clean_transcript(raw)
    except NoTranscriptFound:
        pass
    except Exception:
        pass

    # Try auto-generated english
    try:
        t = transcript_list.find_generated_transcript(['en'])
        data = t.fetch()
        raw = TextFormatter().format_transcript(data)
        return clean_transcript(raw)
    except NoTranscriptFound:
        pass
    except Exception:
        pass

    # Try translating the first available transcript to English (if translatable)
    try:
        available = list(transcript_list)
        if not available:
            return None
        first = available[0]
        if getattr(first, "is_translatable", False):
            translated = first.translate("en")
            data = translated.fetch()
            raw = TextFormatter().format_transcript(data)
            return clean_transcript(raw)
        else:
            # If not translatable, just fetch the first available raw and return raw text (best-effort)
            data = first.fetch()
            raw = TextFormatter().format_transcript(data)
            return clean_transcript(raw)
    except Exception:
        return None


# -----------------------
# Metadata via yt-dlp (safe)
# -----------------------
def get_video_metadata(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (title, description) or (None, None) on failure."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("title", ""), info.get("description", "")
    except Exception:
        return None, None


# -----------------------
# Audio download (robust)
# -----------------------
def _attempt_download_with_format(url: str, fmt: str, outtmpl: str, has_ffmpeg: bool, headers: dict) -> Optional[str]:
    """Try a single format; returns downloaded file path or None."""
    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": headers,
        # set retries to handle transient network issues
        "retries": 2,
    }
    if has_ffmpeg:
        # convert to mp3 small bitrate
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # when a postprocessor runs, output file will be <outtmpl>.mp3 by default
            # yt-dlp doesn't always expose final filename; we search for files after download
            return True
    except DownloadError as de:
        # common download errors include 403, requested format not available, etc.
        # return None and let caller try next format
        return None
    except Exception:
        return None


def download_audio(url: str, max_audio_mb: int = DEFAULT_MAX_AUDIO_MB, headers: Optional[dict] = None) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Robust audio downloader:
     - tries multiple format fallback orders
     - sets headers to reduce 403s
     - converts to small mp3 if ffmpeg available
    Returns (bytes, mime) or (None, None)
    """
    if headers is None:
        headers = HTTP_HEADERS

    cleanup_temp_files()

    has_ffmpeg = is_ffmpeg_available()
    outtmpl = TEMP_AUDIO_PREFIX  # yt-dlp will append extension

    # Prefer formats in order — these capture many server combos
    format_candidates = [
        "bestaudio[ext=webm]/bestaudio/best",
        "bestaudio[ext=m4a]/bestaudio/best",
        "bestaudio/best",
        "best",  # final fallback
    ]

    # Try each format until one succeeds; limit retries to avoid long blocking
    for fmt in format_candidates:
        ok = _attempt_download_with_format(url, fmt, outtmpl, has_ffmpeg, headers)
        if ok:
            # find candidate files
            files = [f for f in glob.glob(f"{TEMP_AUDIO_PREFIX}*") if not f.endswith(".part") and ".part-" not in f]
            if not files:
                cleanup_temp_files()
                continue

            # pick most recent
            files.sort(key=os.path.getmtime, reverse=True)
            fp = files[0]

            # Ensure size under limit
            try:
                size_bytes = os.path.getsize(fp)
            except Exception:
                cleanup_temp_files()
                continue

            if size_bytes > max_audio_mb * 1024 * 1024:
                # too large; clean and try next format candidate
                cleanup_temp_files()
                continue

            # Read bytes
            try:
                with open(fp, "rb") as fh:
                    data = fh.read()
            finally:
                cleanup_temp_files()

            # Returned MIME: prefer audio/mp3 if ffmpeg conversion was done, otherwise guess by extension
            if has_ffmpeg:
                return data, "audio/mp3"
            else:
                # guess mime
                ext = os.path.splitext(fp)[1].lower()
                if ext in (".webm", ".m4a", ".mp4", ".opus"):
                    return data, f"audio/{ext.lstrip('.')}"
                return data, "audio/octet-stream"

    # All attempts failed
    # Provide a helpful message advising proxies or cookies for age-restricted content
    print("Audio download failed for all format fallbacks. Possible causes: 403/format not available/region restriction.")
    print("If you run this from a cloud IP, consider using rotating residential proxies. If the video is age-restricted, cookies may be required.")
    cleanup_temp_files()
    return None, None


# -----------------------
# Public function (keeps same return signature)
# -----------------------
def get_youtube_transcript(url: str, proxy_config: Optional[object] = None,
                           http_client: Optional[object] = None,
                           max_audio_mb: int = DEFAULT_MAX_AUDIO_MB) -> Tuple[Optional[str], Optional[bytes], Optional[str], Optional[str], Optional[str]]:
    """
    Main convenience function:
      returns (transcript_text, audio_bytes, mime_type, title, description)

    Optional args:
      proxy_config: pass a youtube_transcript_api proxy_config (e.g. WebshareProxyConfig) if you have one
      http_client: pass a requests.Session-like object for custom headers/cookies
      max_audio_mb: maximum audio size accepted (default 20)
    """
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError("Invalid YouTube URL")

    # metadata (best-effort)
    title, description = get_video_metadata(url)

    # create YouTubeTranscriptApi instance if proxy_config or http_client provided
    try:
        if proxy_config is not None or http_client is not None:
            # instantiate with provided options (constructor supports proxy_config and http_client)
            api = YouTubeTranscriptApi(proxy_config=proxy_config, http_client=http_client) if proxy_config or http_client else YouTubeTranscriptApi()
        else:
            api = YouTubeTranscriptApi()
    except Exception:
        api = YouTubeTranscriptApi()  # fallback to default

    # try to get transcript (preferred english)
    transcript_text = get_transcript_with_fallback(video_id, api=api, preferred_languages=["en"])
    if transcript_text:
        return transcript_text, None, None, title, description

    # transcript not available -> download audio
    print("Transcript not available or blocked — attempting audio download.")
    audio_data, mime = download_audio(url, max_audio_mb=max_audio_mb, headers=HTTP_HEADERS)

    if audio_data:
        return None, audio_data, mime, title, description

    # If we get here, both transcript and audio failed. Raise with helpful guidance.
    raise RuntimeError(
        "Could not retrieve transcript or download audio. Possible reasons: "
        "transcripts disabled, IP blocked, age-restriction, or video unavailable. "
        "Consider using rotating residential proxies (see youtube-transcript-api docs), "
        "supplying cookies, or running from a different IP."
    )
