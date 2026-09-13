# LMS Downloader

한 번 로그인으로 세션을 유지한 채, 여러 개의 **LMS 강의 페이지 URL**(예: `ys.learnus.org`, `plms.postech.ac.kr`)을 입력하면 **HLS(.m3u8) 영상을 ffmpeg로 순차 다운로드**하는 GUI 툴입니다.

![App Screen](./imgs/app.png)

- 로그인: Selenium이 크롬 창을 띄우면 사용자가 직접 로그인
- 추출: `<video><source>` 또는 HTML에서 `.m3u8` URL 탐지
- 다운로드: ffmpeg 사용
- STT(선택): MP3 저장 시 OpenAI 공식 API 또는 커스텀 Whisper API로 전사해 같은 이름의 `.txt`도 저장

---

## 요구 사항

- **Python** 3.9 이상
- **Google Chrome**
- **ffmpeg**
- 파이썬 패키지: `PyQt5`, `selenium`, `requests`, `python-dotenv`

---

## 설치 & 실행

### 1) macOS

```bash
brew install ffmpeg python uv

터미널 종료 후 재실행 (환경변수 설정 반영)

cd <프로젝트-폴더>
uv sync
source .venv/bin/activate
python3 main.py
```

### 2) Windows

```powershell
winget install Gyan.FFmpeg astral-sh.uv

터미널 종료 후 재실행 (환경변수 설정 반영)

cd <프로젝트-폴더>
uv sync
.\.venv\Scripts\Activate.ps1
python main.py
```

---

## STT (음성 → 텍스트) 설정

1. `.env.example` 을 `.env` 로 복사하고 값을 채웁니다.
2. `STT_PROVIDER` 로 사용할 엔진을 고릅니다.
   - `openai`: `OPENAI_API_KEY` 필요. 모델은 `OPENAI_STT_MODEL` (기본 `whisper-1`, 타임스탬프 포함). 25MB 초과 파일은 자동으로 10분 단위 분할 전사.
   - `custom`: 매니저앱 Whisper API. `CUSTOM_STT_TOKEN` (매니저앱 `/token` 값) 필요, `CUSTOM_STT_MODEL` 1=small / 2=medium / 3=large.
3. 앱에서 **MP3로 변환 저장** → **STT 텍스트(.txt) 함께 저장** 을 체크하고 다운로드하면 `제목.mp3` 옆에 `제목.txt` 가 생성됩니다.

전사는 다운로드와 병렬로 백그라운드에서 진행되며, 모든 다운로드·전사가 끝나면 저장 폴더가 열립니다.
