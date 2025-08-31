# LMS Downloader

한 번 로그인으로 세션을 유지한 채, 여러 개의 **LMS 강의 페이지 URL**(예: `ys.learnus.org`, `plms.postech.ac.kr`)을 입력하면 각 페이지의 `<h1 class="vod-title">` 제목을 파일명으로 사용하여 **HLS(.m3u8) 영상을 ffmpeg로 순차 다운로드**하는 GUI 툴입니다.

- 로그인: Selenium이 크롬 창을 띄우면 사용자가 직접 로그인
- 추출: `<video><source>` 또는 HTML에서 `.m3u8` URL 탐지
- 다운로드: ffmpeg 사용

---

## 요구 사항

- **Python** 3.9 이상
- **Google Chrome**
- **ffmpeg**
- 파이썬 패키지: `PyQt5`, `selenium`

---

## 설치 & 실행

### 1) macOS

```bash
brew install ffmpeg
brew install python

cd <프로젝트-폴더>
python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install PyQt5 selenium

python main.py
```

### 2) Windows

```powershell
winget install Gyan.FFmpeg
# 또는 choco install ffmpeg

cd <프로젝트-폴더>
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install PyQt5 selenium

python main.py
```
