# YouTube Transcription & ffmpeg

## Overview
The bot uses YouTube transcripts when available. If transcripts are unavailable, it falls back to downloading audio using `yt-dlp`.

## ffmpeg (Optional but Recommended)

### Why ffmpeg?
- **Improves audio quality**: Converts audio to optimal format
- **Reduces file size**: Better compression
- **Eliminates warnings**: Fixes timestamp issues

### Installation

#### Windows
```powershell
# Using Chocolatey
choco install ffmpeg

# Using winget (Windows 11)
winget install ffmpeg

# Manual
# Download from https://ffmpeg.org/download.html
# Extract and add to PATH
```

#### Linux
```bash
sudo apt install ffmpeg  # Ubuntu/Debian
sudo yum install ffmpeg  # CentOS/RHEL
```

#### macOS
```bash
brew install ffmpeg
```

### Verification
```bash
ffmpeg -version
```

## Current Behavior

### Without ffmpeg:
- ✅ Audio download works
- ⚠️ Shows warnings about timestamps
- ⚠️ Larger file sizes

### With ffmpeg:
- ✅ Audio download works
- ✅ No warnings
- ✅ Optimized audio format
- ✅ Smaller file sizes

## Note
The bot works perfectly fine **without** ffmpeg. The warnings are cosmetic and don't affect functionality. Installing ffmpeg is optional but recommended for production use.
