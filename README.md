# StemSplitAudio

A web-based application to download audio from various sources and optionally split them into separate stems (vocals, drums, bass, etc.) using Demucs.

## Features

- **Audio Download**: Download audio from YouTube and other platforms using `yt-dlp`.
- **Stem Splitting**: Use Meta's `demucs` to separate audio into 2, 4, or 6 stems.
- **Web Interface**: Easy-to-use browser interface for managing downloads and playing back results.
- **Stem Player**: Integrated player for listening to individual stems.

## Prerequisites

- Python 3.8 or higher
- FFmpeg (required for `yt-dlp` and `demucs`)

## Installation

1. **Clone the repository**:

   ```bash
   git clone https://github.com/aviatorcards/StemSplitAudio
   cd StemSplitAudio
   ```

2. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

3. **Install FFmpeg** (if not already installed):
   - **Ubuntu/Debian**: `sudo apt install ffmpeg`
   - **macOS**: `brew install ffmpeg`
   - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html)

## Running the Application

You can start the application using the provided script:

```bash
./start.sh
```

Or run it directly with Python:

```bash
python3 app.py
```

The application will be available at `http://localhost:5050`.

## Usage

1. Enter a URL (e.g., a YouTube link) in the input field.
2. Select the desired audio format (MP3, WAV, FLAC).
3. (Optional) Check "Split stems" and choose a mode:
   - **Two stems**: Vocals and Instrumental.
   - **Four stems** (Default): Vocals, Drums, Bass, Other.
   - **Six stems**: Vocals, Drums, Bass, Other, Guitar, Piano.
4. Click **Download**.
5. Once complete, you can listen to the audio or its stems in the browser or download them to your machine.

## Project Structure

- `app.py`: Main Flask application.
- `templates/`: HTML templates for the web interface.
- `static/`: Static assets (JavaScript, CSS).
- `downloads/`: Directory where downloaded and processed files are stored.
- `start.sh`: Convenience script to run the application.
