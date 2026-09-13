# stt.py
"""
STT(음성 → 텍스트) 모듈.

엔진(provider)은 앱 옵션에서 고르고, 키/토큰은 .env 에 둔다.

.env 설정:
  # 공통
  STT_LANGUAGE=auto                 # 앱 언어 드롭다운 기본값. auto=자동 감지, 또는 ko/en/ja/...

  # OpenAI 공식 API (provider=openai)
  OPENAI_API_KEY=sk-...
  OPENAI_STT_MODEL=whisper-1        # whisper-1(타임스탬프 포함) | gpt-4o-transcribe | gpt-4o-mini-transcribe

  # 커스텀 Whisper API (provider=custom, 매니저앱)
  CUSTOM_STT_TOKEN=...              # 매니저앱 /token 값
  CUSTOM_STT_URL=https://manager.knpu.re.kr/api/analysis/whisper
  CUSTOM_STT_MODEL=2                # 1=small(빠름), 2=medium(권장), 3=large(정확)
"""

import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

# .env는 프로젝트 루트(이 파일 옆) 기준으로 읽는다.
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

PROVIDER_CUSTOM = "custom"
PROVIDER_OPENAI = "openai"
PROVIDERS = {  # 표시명 → 코드 (앱 드롭다운 순서)
    "커스텀 Whisper (매니저앱)": PROVIDER_CUSTOM,
    "OpenAI 공식 API": PROVIDER_OPENAI,
}

DEFAULT_CUSTOM_URL = "https://manager.knpu.re.kr/api/analysis/whisper"
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MAX_BYTES = 24 * 1024 * 1024  # 공식 한도 25MB, 여유를 둠
OPENAI_CHUNK_SECONDS = 600  # 한도 초과 시 10분 단위로 분할
DEFAULT_OPENAI_MODEL = "whisper-1"
AUTO = "auto"

LogFn = Callable[[str], None]

# 지원 언어 (매니저앱 WhisperOptionDialog 와 동일 + 자동 감지). 표시명 → 코드
LANGUAGES = {
    "자동 감지": AUTO,
    "한국어": "ko",
    "영어": "en",
    "일본어": "ja",
    "중국어": "zh",
    "프랑스어": "fr",
    "독일어": "de",
    "스페인어": "es",
    "이탈리아어": "it",
    "포르투갈어": "pt",
    "러시아어": "ru",
    "아랍어": "ar",
    "힌디어": "hi",
    "태국어": "th",
    "베트남어": "vi",
    "인도네시아어": "id",
}
LANGUAGE_NAMES = {code: name for name, code in LANGUAGES.items()}


@dataclass
class SttConfig:
    provider: str = PROVIDER_CUSTOM  # 앱 옵션에서 선택 (.env 아님)
    language: str = AUTO             # 앱 옵션에서 선택, AUTO 면 자동 감지
    openai_api_key: str = ""
    openai_model: str = DEFAULT_OPENAI_MODEL
    custom_token: str = ""
    custom_url: str = DEFAULT_CUSTOM_URL
    custom_model: int = 2

    @classmethod
    def from_env(cls, provider: str = PROVIDER_CUSTOM) -> "SttConfig":
        """키/토큰/모델은 .env 에서, provider 는 인자로."""
        # 앱 실행 중 .env 를 고쳐도 다시 읽히도록 매번 로드
        load_dotenv(ENV_PATH, override=True)
        try:
            custom_model = int(os.getenv("CUSTOM_STT_MODEL", "2"))
        except ValueError:
            custom_model = 2
        return cls(
            provider=provider,
            language=(os.getenv("STT_LANGUAGE") or AUTO).strip().lower(),
            openai_api_key=(os.getenv("OPENAI_API_KEY") or "").strip(),
            openai_model=(os.getenv("OPENAI_STT_MODEL") or DEFAULT_OPENAI_MODEL).strip(),
            custom_token=(os.getenv("CUSTOM_STT_TOKEN") or "").strip(),
            custom_url=(os.getenv("CUSTOM_STT_URL") or DEFAULT_CUSTOM_URL).strip(),
            custom_model=custom_model,
        )

    def describe(self) -> str:
        """UI에 표시할 짧은 설명."""
        if self.provider == PROVIDER_OPENAI:
            return f"OpenAI ({self.openai_model})"
        host = self.custom_url.split("//", 1)[-1].split("/", 1)[0]
        return f"커스텀 ({host}, model={self.custom_model})"

    def language_label(self) -> str:
        name = LANGUAGE_NAMES.get(self.language, self.language)
        return f"{name}({self.language})"

    def validate(self) -> Optional[str]:
        """설정이 불완전하면 이유를 문자열로 반환, 정상이면 None."""
        if self.provider == PROVIDER_OPENAI:
            if not self.openai_api_key:
                return f"OPENAI_API_KEY 가 .env 에 없습니다. (.env 위치: {ENV_PATH})"
        elif self.provider == PROVIDER_CUSTOM:
            if not self.custom_token:
                return f"CUSTOM_STT_TOKEN 이 .env 에 없습니다. (.env 위치: {ENV_PATH})"
        else:
            return f"알 수 없는 STT 엔진: {self.provider}"
        return None


# ------------------------------------------------------------------ 유틸
def _fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _probe_duration(path: str) -> Optional[float]:
    if which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def _split_audio(path: str, tmp_dir: str, seconds: int) -> list[str]:
    """ffmpeg로 오디오를 seconds 단위 조각으로 분할(재인코딩 없음)."""
    pattern = str(Path(tmp_dir) / "chunk_%03d.mp3")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", path, "-f", "segment", "-segment_time", str(seconds),
         "-c", "copy", pattern],
        check=True, timeout=600,
    )
    return sorted(str(p) for p in Path(tmp_dir).glob("chunk_*.mp3"))


# ------------------------------------------------------------------ OpenAI
def _transcribe_openai_file(cfg: SttConfig, path: str, offset: float) -> tuple[str, Optional[float]]:
    """단일 파일 전사. (텍스트, 파일 길이[초]) 반환."""
    use_verbose = cfg.openai_model.startswith("whisper")
    data = {"model": cfg.openai_model}
    if cfg.language and cfg.language != AUTO:
        data["language"] = cfg.language  # 생략하면 OpenAI가 자동 감지
    if use_verbose:
        data["response_format"] = "verbose_json"
        data["timestamp_granularities[]"] = "segment"
    else:
        data["response_format"] = "json"

    with open(path, "rb") as f:
        res = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
            files={"file": (Path(path).name, f, "audio/mpeg")},
            data=data,
            timeout=3600,
        )
    if res.status_code >= 400:
        raise RuntimeError(f"OpenAI API 오류 {res.status_code}: {res.text[:500]}")
    body = res.json()

    duration = body.get("duration") if isinstance(body, dict) else None
    segments = body.get("segments") if isinstance(body, dict) else None
    if segments:
        lines = []
        for seg in segments:
            text = (seg.get("text") or "").strip()
            if text:
                lines.append(f"[{_fmt_ts(offset + float(seg.get('start', 0)))}] {text}")
        return "\n".join(lines), duration
    return (body.get("text") or "").strip(), duration


def transcribe_openai(cfg: SttConfig, audio_path: str, log: LogFn) -> str:
    size = os.path.getsize(audio_path)
    if size <= OPENAI_MAX_BYTES:
        text, _ = _transcribe_openai_file(cfg, audio_path, 0.0)
        return text

    log(f"[STT] 파일 크기 {size/1024/1024:.1f}MB > {OPENAI_MAX_BYTES/1024/1024:.0f}MB 한도 → {OPENAI_CHUNK_SECONDS}초 단위로 분할 전사\n")
    if which("ffmpeg") is None:
        raise RuntimeError("파일이 25MB를 초과하지만 분할에 필요한 ffmpeg 가 없습니다.")

    with tempfile.TemporaryDirectory(prefix="lms_stt_") as tmp:
        chunks = _split_audio(audio_path, tmp, OPENAI_CHUNK_SECONDS)
        parts = []
        offset = 0.0
        for i, chunk in enumerate(chunks, 1):
            log(f"[STT] 조각 {i}/{len(chunks)} 전사 중...\n")
            text, dur = _transcribe_openai_file(cfg, chunk, offset)
            if text:
                parts.append(text)
            offset += _probe_duration(chunk) or dur or OPENAI_CHUNK_SECONDS
        return "\n".join(parts)


# ------------------------------------------------------------------ 커스텀(매니저앱)
def transcribe_custom(cfg: SttConfig, audio_path: str, log: LogFn) -> str:
    option = {
        "pid": str(uuid.uuid4()),
        # 매니저 GPU 서버는 language 를 faster-whisper 에 그대로 넘기므로 null 이면 자동 감지
        "language": None if cfg.language == AUTO else (cfg.language or "ko"),
        "model": cfg.custom_model,
    }
    with open(audio_path, "rb") as f:
        res = requests.post(
            cfg.custom_url,
            headers={"Authorization": f"Bearer {cfg.custom_token}"},
            files={"file": (Path(audio_path).name, f, "audio/mpeg")},
            data={"option": json.dumps(option)},
            timeout=3600,  # 긴 음성은 전사에 몇 분씩 걸림
        )
    if res.status_code >= 400:
        raise RuntimeError(f"커스텀 API 오류 {res.status_code}: {res.text[:500]}")
    result = res.json()
    text = result.get("text_with_time") or result.get("text") or ""
    return text.strip()


# ------------------------------------------------------------------ 진입점
def transcribe(cfg: SttConfig, audio_path: str, log: LogFn = lambda s: None) -> str:
    err = cfg.validate()
    if err:
        raise RuntimeError(err)
    if cfg.provider == PROVIDER_OPENAI:
        return transcribe_openai(cfg, audio_path, log)
    return transcribe_custom(cfg, audio_path, log)


def transcribe_to_txt(cfg: SttConfig, audio_path: str, txt_path: str, log: LogFn = lambda s: None) -> str:
    """오디오를 전사해 txt_path 에 저장하고 텍스트를 반환."""
    text = transcribe(cfg, audio_path, log)
    Path(txt_path).write_text(text + ("\n" if text and not text.endswith("\n") else ""), encoding="utf-8")
    return text
