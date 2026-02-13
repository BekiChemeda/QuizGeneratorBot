import re
import os
import glob
import time
import shutil
import traceback
from typing import Tuple, Optional, Iterable
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
# Transcript functions — compatible with youtube-transcript-api v0.x and v1.x
# -----------------------

def _try_import_transcript_api():
    """Import transcript API components with compatibility for both old and new versions."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, None, None, None

    # Try importing error classes (location varies by version)
    TranscriptsDisabled = None
    NoTranscriptFound = None
    try:
        from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
    except ImportError:
        try:
            from youtube_transcript_api import TranscriptsDisabled, NoTranscriptFound
        except ImportError:
            pass

    # Try importing TextFormatter (may not exist in newer versions)
    TextFormatter = None
    try:
        from youtube_transcript_api.formatters import TextFormatter
    except ImportError:
        pass

    return YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, TextFormatter


def _format_transcript(fetched, text_formatter_cls=None) -> str:
    """
    Convert fetched transcript data to plain text.
    Works with both old-style list-of-dicts and new FetchedTranscript objects.
    """
    # Try TextFormatter first if available
    if text_formatter_cls is not None:
        try:
            formatter = text_formatter_cls()
            result = formatter.format_transcript(fetched)
            if result and result.strip():
                return result.strip()
        except Exception:
            pass

    # Manual extraction — handles list of dicts or FetchedTranscript (iterable of snippet objects)
    parts = []
    try:
        for item in fetched:
            if isinstance(item, dict):
                text = item.get("text", "")
            else:
                text = getattr(item, "text", str(item))
            if text:
                parts.append(text.strip())
    except Exception:
        pass

    return " ".join(parts)


def clean_transcript(text: str) -> str:
    """Minimal cleaning: normalize whitespace and preserve paragraph breaks."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join([ln for ln in lines if ln]).strip()


def get_transcript_with_fallback(video_id: str, preferred_languages: Optional[Iterable[str]] = None) -> Optional[str]:
    """
    Attempts to fetch transcript using multiple strategies:
      1. Direct fetch() with preferred languages
      2. List transcripts and find manual/generated English
      3. Try translating first available to English
      4. Use any available transcript as last resort
    Returns cleaned transcript text or None.
    """
    YTApi, TranscriptsDisabled, NoTranscriptFound, TextFormatterCls = _try_import_transcript_api()
    if YTApi is None:
        print("youtube-transcript-api not installed")
        return None

    api = YTApi()
    if preferred_languages is None:
        preferred_languages = ["en"]

    # Build error classes tuple for catching
    transcript_errors = tuple(filter(None, [TranscriptsDisabled, NoTranscriptFound, Exception]))

    # Strategy 1: Direct fetch
    try:
        fetched = api.fetch(video_id, languages=list(preferred_languages))
        raw = _format_transcript(fetched, TextFormatterCls)
        if raw and len(raw.strip()) > 50:
            return clean_transcript(raw)
    except Exception as e:
        print(f"Transcript fetch attempt 1 failed: {e}")

    # Strategy 2: List and find manual/generated transcripts  
    try:
        transcript_list = api.list(video_id)
    except Exception as e:
        print(f"Transcript list failed: {e}")
        return None

    # Try finding English transcript (manual)
    try:
        t = transcript_list.find_transcript(["en"])
        data = t.fetch()
        raw = _format_transcript(data, TextFormatterCls)
        if raw and len(raw.strip()) > 50:
            return clean_transcript(raw)
    except Exception:
        pass

    # Try auto-generated English
    try:
        if hasattr(transcript_list, "find_generated_transcript"):
            t = transcript_list.find_generated_transcript(["en"])
            data = t.fetch()
            raw = _format_transcript(data, TextFormatterCls)
            if raw and len(raw.strip()) > 50:
                return clean_transcript(raw)
    except Exception:
        pass

    # Strategy 3: Translate first available transcript to English
    try:
        available = list(transcript_list)
        if available:
            first = available[0]
            if getattr(first, "is_translatable", False):
                translated = first.translate("en")
                data = translated.fetch()
                raw = _format_transcript(data, TextFormatterCls)
                if raw and len(raw.strip()) > 50:
                    return clean_transcript(raw)

            # Strategy 4: Just use the first available transcript (any language)
            data = first.fetch()
            raw = _format_transcript(data, TextFormatterCls)
            if raw and len(raw.strip()) > 50:
                return clean_transcript(raw)
    except Exception as e:
        print(f"Transcript fallback failed: {e}")

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
    """Try a single format; returns True on success, None on failure."""
    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": headers,
        "retries": 3,
        "socket_timeout": 30,
    }
    if has_ffmpeg:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            return True
    except DownloadError as de:
        print(f"Download format '{fmt}' failed: {de}")
        return None
    except Exception as e:
        print(f"Download format '{fmt}' error: {e}")
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

    format_candidates = [
        "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "bestaudio/best",
        "worstaudio",  # very small audio as last resort
        "best",
    ]

    for fmt in format_candidates:
        ok = _attempt_download_with_format(url, fmt, outtmpl, has_ffmpeg, headers)
        if ok:
            files = [f for f in glob.glob(f"{TEMP_AUDIO_PREFIX}*") if not f.endswith(".part") and ".part-" not in f]
            if not files:
                cleanup_temp_files()
                continue

            files.sort(key=os.path.getmtime, reverse=True)
            fp = files[0]

            try:
                size_bytes = os.path.getsize(fp)
            except Exception:
                cleanup_temp_files()
                continue

            if size_bytes > max_audio_mb * 1024 * 1024:
                cleanup_temp_files()
                continue

            if size_bytes == 0:
                cleanup_temp_files()
                continue

            try:
                with open(fp, "rb") as fh:
                    data = fh.read()
            except Exception:
                cleanup_temp_files()
                continue
            finally:
                cleanup_temp_files()

            if has_ffmpeg:
                return data, "audio/mp3"
            else:
                ext = os.path.splitext(fp)[1].lower()
                mime_map = {".webm": "audio/webm", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".opus": "audio/opus", ".mp3": "audio/mp3", ".ogg": "audio/ogg"}
                return data, mime_map.get(ext, "audio/octet-stream")

    print("Audio download failed for all format fallbacks.")
    cleanup_temp_files()
    return None, None


# -----------------------
# Public function (keeps same return signature)
# -----------------------
def get_youtube_transcript(url: str, max_audio_mb: int = DEFAULT_MAX_AUDIO_MB) -> Tuple[Optional[str], Optional[bytes], Optional[str], Optional[str], Optional[str]]:
    """
    Main convenience function:
      returns (transcript_text, audio_bytes, mime_type, title, description)

    Tries transcript first, falls back to audio download if not available.
    """
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError("Invalid YouTube URL — could not extract video ID. Please send a full YouTube link (e.g. https://www.youtube.com/watch?v=...)")

    # Get metadata (best-effort, non-blocking)
    title, description = get_video_metadata(url)

    # Try to get transcript
    try:
        transcript_text = get_transcript_with_fallback(video_id, preferred_languages=["en"])
        if transcript_text and len(transcript_text.strip()) > 50:
            return transcript_text, None, None, title, description
    except Exception as e:
        print(f"Transcript extraction error: {e}")

    # Transcript not available → download audio
    print("Transcript not available — attempting audio download...")
    try:
        audio_data, mime = download_audio(url, max_audio_mb=max_audio_mb, headers=HTTP_HEADERS)
        if audio_data and len(audio_data) > 0:
            return None, audio_data, mime, title, description
    except Exception as e:
        print(f"Audio download error: {e}")

    # Both failed
    raise RuntimeError(
        "Could not retrieve transcript or download audio.\n"
        "Possible reasons:\n"
        "• Video has no captions/subtitles\n"
        "• Video is age-restricted or region-locked\n"
        "• Video is too long or unavailable\n"
        "Try a different video or a shorter one."
    )
