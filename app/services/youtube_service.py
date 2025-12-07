import re
import os
import glob
from typing import Tuple, Optional
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
import yt_dlp

def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from URL."""
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def download_audio(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Download audio from YouTube video using yt-dlp."""
    filename_base = "temp_audio"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': filename_base,
        'noplaylist': True,
        'quiet': True,
        'max_filesize': 20 * 1024 * 1024, # Limit to 20MB to match Telegram/Gemini practical limits for quick processing
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            
        # Find the downloaded file (extension varies)
        files = glob.glob(f"{filename_base}*")
        if not files:
            return None, None
            
        file_path = files[0]
        with open(file_path, "rb") as f:
            data = f.read()
            
        # Cleanup
        try:
            os.remove(file_path)
        except:
            pass
            
        return data, "audio/mp3" # Generic MIME, Gemini handles most well
    except Exception as e:
        print(f"yt-dlp error: {e}")
        return None, None

def get_youtube_transcript(url: str) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    """
    Fetch transcript or audio for a YouTube video.
    Returns: (text, audio_bytes, mime_type)
    """
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError("Invalid YouTube URL")

    # 1. Try Transcript
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        formatter = TextFormatter()
        text = formatter.format_transcript(transcript_list)
        return text, None, None
    except Exception:
        # 2. Fallback to Audio Download
        print("Transcript failed, falling back to audio download...")
        audio_data, mime = download_audio(url)
        if audio_data:
            return None, audio_data, mime
        
        raise RuntimeError("Could not retrieve transcript or download audio (possibly too large or restricted).")
