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
  CUSTOM_STT_PROGRESS_URL=https://manager.knpu.re.kr/progress   # (일괄 방식) pid 등록/진행 메시지
  CUSTOM_STT_STREAM_URL=            # 비우면 CUSTOM_STT_URL + "/stream". NDJSON 실시간 전사 (있으면 우선 사용, 404면 일괄 방식 폴백)
  CUSTOM_STT_MODEL=2                # 1=small(빠름), 2=medium(권장), 3=large(정확)
  (LecAI 이름도 인식: AUDIO_LLM_TOKEN / CUSTOM_TOKEN, AUDIO_LLM_URL, AUDIO_PROGRESS_URL)
"""

import json
import os
import subprocess
import tempfile
import threading
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
DEFAULT_PROGRESS_URL = "https://manager.knpu.re.kr/progress"
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MAX_BYTES = 24 * 1024 * 1024  # 공식 한도 25MB, 여유를 둠
OPENAI_CHUNK_SECONDS = 600  # 한도 초과 시 10분 단위로 분할
DEFAULT_OPENAI_MODEL = "whisper-1"
AUTO = "auto"
STREAM_LOG_STEP = 5  # 실시간 전사 시 로그에 진행률을 남기는 간격(%) — 전사 문장은 로그에 찍지 않음

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, str], None]  # (percent 0~100, message)
_NO_LOG: LogFn = lambda s: None
_NO_PROGRESS: ProgressFn = lambda p, m: None

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
    custom_progress_url: str = DEFAULT_PROGRESS_URL
    custom_stream_url: str = ""  # 비우면 custom_url + "/stream"
    custom_model: int = 2

    @property
    def stream_url(self) -> str:
        return self.custom_stream_url or (self.custom_url.rstrip("/") + "/stream")

    @classmethod
    def from_env(cls, provider: str = PROVIDER_CUSTOM) -> "SttConfig":
        """키/토큰/모델은 .env 에서, provider 는 인자로."""
        # 앱 실행 중 .env 를 고쳐도 다시 읽히도록 매번 로드
        load_dotenv(ENV_PATH, override=True)

        def env(*names: str, default: str = "") -> str:
            for n in names:  # 앞에 있는 이름 우선, LecAI .env 이름도 허용
                v = (os.getenv(n) or "").strip()
                if v:
                    return v
            return default

        try:
            custom_model = int(env("CUSTOM_STT_MODEL", default="2"))
        except ValueError:
            custom_model = 2
        return cls(
            provider=provider,
            language=env("STT_LANGUAGE", default=AUTO).lower(),
            openai_api_key=env("OPENAI_API_KEY"),
            openai_model=env("OPENAI_STT_MODEL", default=DEFAULT_OPENAI_MODEL),
            custom_token=env("CUSTOM_STT_TOKEN", "AUDIO_LLM_TOKEN", "CUSTOM_TOKEN"),
            custom_url=env("CUSTOM_STT_URL", "AUDIO_LLM_URL", default=DEFAULT_CUSTOM_URL),
            custom_progress_url=env("CUSTOM_STT_PROGRESS_URL", "AUDIO_PROGRESS_URL", default=DEFAULT_PROGRESS_URL),
            custom_stream_url=env("CUSTOM_STT_STREAM_URL"),
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


def transcribe_openai(cfg: SttConfig, audio_path: str, log: LogFn, progress: ProgressFn = _NO_PROGRESS) -> str:
    size = os.path.getsize(audio_path)
    if size <= OPENAI_MAX_BYTES:
        progress(10, "OpenAI 전송 중")
        text, _ = _transcribe_openai_file(cfg, audio_path, 0.0)
        progress(100, "완료")
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
            progress(int(100 * (i - 1) / len(chunks)), f"조각 {i}/{len(chunks)} 전사 중")
            text, dur = _transcribe_openai_file(cfg, chunk, offset)
            if text:
                parts.append(text)
            offset += _probe_duration(chunk) or dur or OPENAI_CHUNK_SECONDS
        progress(100, "완료")
        return "\n".join(parts)


# ------------------------------------------------------------------ 커스텀(매니저앱)
class _ProgressSubscriber:
    """
    매니저 진행상황 서버에 pid 를 등록하고, 웹소켓(/ws/{pid})으로 진행 메시지를 받아 log 로 넘긴다.
    (LecAI audio_processor._ProgressSubscriber 와 동일한 방식)

    ※ pid 등록은 필수: GPU 서버가 전사 중 /notify/{pid} 로 진행 메시지를 보내는데,
       미등록 pid 는 404 를 받고 raise_for_status() 로 전사 전체가 실패한다.
    """

    # 일괄 방식은 세그먼트 진행률이 없어 단계 메시지로 대략적인 퍼센트만 잡는다 (LecAI CUSTOM_PHASES)
    PHASES = (("모델 로드", 20), ("변환 중", 45), ("완료", 90))

    def __init__(self, base_url: str, pid: str, title: str, log: LogFn, progress: ProgressFn = _NO_PROGRESS):
        self.base_url = base_url.rstrip("/")
        self.pid = pid
        self.title = title
        self.log = log
        self.progress = progress
        self.percent = 10
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    def start(self) -> bool:
        """pid 등록. 성공(200) 또는 이미 존재(400)면 True."""
        try:
            resp = requests.post(
                f"{self.base_url}/process",
                json={"title": self.title, "process_id": self.pid},
                timeout=10,
            )
            if resp.status_code not in (200, 400):
                self.log(f"[STT][WARN] 진행상황 서버 등록 실패: {resp.status_code} {resp.text[:100]}\n")
                return False
        except Exception as e:
            self.log(f"[STT][WARN] 진행상황 서버 연결 실패: {e}\n")
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        try:
            from websockets.sync.client import connect
        except Exception as e:
            self.log(f"[STT][WARN] websockets 모듈 없음 (진행 메시지 생략): {e}\n")
            return
        ws_url = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        try:
            with connect(f"{ws_url}/ws/{self.pid}", open_timeout=10) as ws:
                self._ws = ws
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=1)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = {"type": "message", "text": str(raw)}
                    self._on_event(payload)
        except Exception as e:
            if not self._stop.is_set():
                self.log(f"[STT][WARN] 진행 웹소켓 오류: {e}\n")

    def _on_event(self, payload: dict):
        kind = payload.get("type")
        if kind == "message":
            text = str(payload.get("text", "")).strip()
            for key, pct in self.PHASES:
                if key in text:
                    self.percent = max(self.percent, pct)
            self.log(f"[STT] {text}\n")
            self.progress(self.percent, text)
        elif kind == "progress":
            cur, total = payload.get("current", 0), payload.get("total", 0) or 0
            if total:
                self.percent = max(self.percent, 10 + int(80 * cur / total))
            msg = payload.get("message") or f"진행 {cur}/{total}"
            self.log(f"[STT] {msg}\n")
            self.progress(self.percent, msg)
        elif kind == "status":
            self.log(f"[STT] 상태: {payload.get('phase', '')}\n")
            self.progress(self.percent, f"상태: {payload.get('phase', '')}")

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3)


def _custom_language(cfg: SttConfig):
    # 매니저 GPU 서버는 language 를 faster-whisper 에 그대로 넘기므로 None 이면 자동 감지
    return None if cfg.language in ("", AUTO, None) else cfg.language


def _custom_ts(t: float) -> str:
    """GPU 서버 text_with_time 과 같은 형식: HH:MM:SS,mmm"""
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def transcribe_custom_batch(cfg: SttConfig, audio_path: str, log: LogFn, progress: ProgressFn = _NO_PROGRESS) -> str:
    """기존 /whisper 라우트 (일괄 응답). 진행 메시지는 진행상황 서버 웹소켓으로 받는다."""
    fname = Path(audio_path).name
    pid = str(uuid.uuid4())
    option = {"pid": pid, "language": _custom_language(cfg), "model": cfg.custom_model}

    subscriber = None
    if cfg.custom_progress_url:
        subscriber = _ProgressSubscriber(cfg.custom_progress_url, pid, f"LMS Downloader STT: {fname}", log, progress)
        subscriber.start()

    log(f"[STT] 커스텀 서버로 전송 중... ({fname})\n")
    progress(5, f"커스텀 서버로 전송 중 ({fname})")
    try:
        with open(audio_path, "rb") as f:
            res = requests.post(
                cfg.custom_url,
                headers={"Authorization": f"Bearer {cfg.custom_token}"},
                files={"file": (fname, f, "audio/mpeg")},
                data={"option": json.dumps(option)},
                timeout=3600,  # 긴 음성은 전사에 몇 분씩 걸림
            )
    finally:
        if subscriber:
            subscriber.stop()

    if res.status_code == 401:
        raise RuntimeError(
            "커스텀 STT 서버 인증 실패(401): CUSTOM_STT_TOKEN 이 만료되었을 수 있습니다. "
            "매니저 앱에서 다시 로그인 후 /token 값을 .env 에 갱신하세요."
        )
    if res.status_code != 200:
        raise RuntimeError(f"커스텀 API 오류 {res.status_code}: {res.text[:300]}")

    result = res.json()
    text = ((result.get("text_with_time") or result.get("text")) or "").strip()
    if not text:
        raise RuntimeError("STT 결과가 비어 있습니다.")
    return text


class StreamUnavailable(Exception):
    """스트리밍 라우트가 아직 배포되지 않음(404/405) → 일괄 방식으로 폴백."""


def transcribe_custom_stream(cfg: SttConfig, audio_path: str, log: LogFn, progress: ProgressFn = _NO_PROGRESS) -> str:
    """
    /whisper/stream 라우트 (NDJSON 실시간). GPU 서버가 세그먼트를 디코딩하는 즉시 흘려보내므로
    seg.end / info.duration 으로 정확한 진행률을 낸다. pid 등록/웹소켓이 필요 없다.
      {"type":"status","message":..} → {"type":"info","duration":..,"language":..}
      → {"type":"segment","start":..,"end":..,"text":..}* → {"type":"done"} | {"type":"error","message":..}
    """
    fname = Path(audio_path).name
    option = {"language": _custom_language(cfg), "model": cfg.custom_model}

    log(f"[STT] 커스텀 서버로 전송 중 (실시간)... ({fname})\n")
    progress(2, f"커스텀 서버로 전송 중 ({fname})")
    with open(audio_path, "rb") as f:
        res = requests.post(
            cfg.stream_url,
            headers={"Authorization": f"Bearer {cfg.custom_token}"},
            files={"file": (fname, f, "audio/mpeg")},
            data={"option": json.dumps(option)},
            stream=True,
            timeout=(30, 3600),
        )
    if res.status_code in (404, 405):
        res.close()
        raise StreamUnavailable(f"HTTP {res.status_code}")
    if res.status_code == 401:
        res.close()
        raise RuntimeError(
            "커스텀 STT 서버 인증 실패(401): CUSTOM_STT_TOKEN 이 만료되었을 수 있습니다. "
            "매니저 앱에서 다시 로그인 후 /token 값을 .env 에 갱신하세요."
        )
    if res.status_code != 200:
        body = res.text[:300]
        res.close()
        raise RuntimeError(f"커스텀 API 오류 {res.status_code}: {body}")

    duration = 0.0
    lines: list[str] = []
    done = False
    last_pct = -1
    last_logged_pct = -1  # 로그에는 전사 문장 대신 진행률만 STREAM_LOG_STEP % 단위로 남긴다
    try:
        for raw in res.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                log(f"[STT] {raw.strip()}\n")
                continue
            kind = ev.get("type")
            if kind == "status":
                msg = str(ev.get("message") or ev.get("stage") or "")
                log(f"[STT] {msg}\n")
                progress(max(last_pct, 5), msg)
            elif kind == "info":
                duration = float(ev.get("duration") or 0)
                lang = ev.get("language") or "?"
                log(f"[STT] 언어={lang}, 길이={_fmt_ts(duration)} → 전사 시작\n")
                progress(max(last_pct, 8), f"언어={lang}, 길이={_fmt_ts(duration)}")
            elif kind == "segment":
                start, end = float(ev.get("start", 0)), float(ev.get("end", 0))
                text = str(ev.get("text") or "").strip()
                if not text:
                    continue
                lines.append(f"[{_custom_ts(start)} - {_custom_ts(end)}] {text}")
                pct = min(99, int(100 * end / duration)) if duration > 0 else max(last_pct, 8)
                last_pct = max(last_pct, pct)
                pos = f"{_fmt_ts(end)} / {_fmt_ts(duration)}"
                if last_pct - last_logged_pct >= STREAM_LOG_STEP:
                    log(f"[STT] 진행 {last_pct}% ({pos})\n")
                    last_logged_pct = last_pct
                progress(last_pct, pos)
            elif kind == "error":
                raise RuntimeError(f"커스텀 STT 오류: {ev.get('message')}")
            elif kind == "done":
                done = True
                break
    finally:
        res.close()

    if not done:
        raise RuntimeError("스트림이 완료(done) 없이 끊겼습니다. 네트워크 또는 서버 오류.")
    text = "\n".join(lines).strip()
    if not text:
        raise RuntimeError("STT 결과가 비어 있습니다.")
    progress(100, "완료")
    return text


def transcribe_custom(cfg: SttConfig, audio_path: str, log: LogFn, progress: ProgressFn = _NO_PROGRESS) -> str:
    """스트리밍 라우트 우선, 아직 배포 전(404/405)이면 일괄 방식으로 폴백."""
    try:
        return transcribe_custom_stream(cfg, audio_path, log, progress)
    except StreamUnavailable as e:
        log(f"[STT] 실시간 라우트 없음({e}) → 일괄 방식으로 전환\n")
        return transcribe_custom_batch(cfg, audio_path, log, progress)


# ------------------------------------------------------------------ 진입점
def transcribe(cfg: SttConfig, audio_path: str, log: LogFn = _NO_LOG, progress: ProgressFn = _NO_PROGRESS) -> str:
    err = cfg.validate()
    if err:
        raise RuntimeError(err)
    if cfg.provider == PROVIDER_OPENAI:
        return transcribe_openai(cfg, audio_path, log, progress)
    return transcribe_custom(cfg, audio_path, log, progress)


def transcribe_to_txt(
    cfg: SttConfig, audio_path: str, txt_path: str, log: LogFn = _NO_LOG, progress: ProgressFn = _NO_PROGRESS
) -> str:
    """오디오를 전사해 txt_path 에 저장하고 텍스트를 반환."""
    text = transcribe(cfg, audio_path, log, progress)
    Path(txt_path).write_text(text + ("\n" if text and not text.endswith("\n") else ""), encoding="utf-8")
    return text
